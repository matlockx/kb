"""Turn a raw download into text blocks: headings, paragraphs and page-numbered PDF lines."""

import codecs
import re
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from pypdf import PageObject, PdfReader
from pypdf.errors import PyPdfError

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})


@dataclass(frozen=True)
class Block:
    text: str
    level: int | None = None  # heading level (1 = top) or None for body text
    ref: str | None = None  # set when the format itself marks a section start; no built-in reader sets it yet
    page: int | None = None  # 1-based, PDFs only


def extract(path: Path, content_type: str | None, skip_classes: Collection[str] = ()) -> list[Block]:
    """Blocks of one stored download; raise ValueError for a format with no reader.

    HTML elements carrying any of skip_classes as a class are left out with their content; PDFs ignore it.
    """
    data = path.read_bytes()
    if content_type == "application/pdf" or data.startswith(b"%PDF"):
        return from_pdf(data)
    if content_type is None or content_type in HTML_TYPES:
        return from_html(data.decode(_html_charset(data), errors="replace"), skip_classes)
    raise ValueError(f"no reader for content type {content_type!r}; add one to kb/extract.py")


def links(path: Path, content_type: str | None, base: str, skip_classes: Collection[str] = ()) -> list[str]:
    """Absolute http(s) URLs a stored download links to, in document order, each once and without its #fragment.

    Relative links resolve against base; links into base itself are left out. HTML links come from the text the
    HTML reader keeps (no navigation or footers, no element carrying one of skip_classes), PDF links from the pages'
    URI link annotations. Raise ValueError for a format with no reader or a PDF that does not parse.
    """
    data = path.read_bytes()
    if content_type == "application/pdf" or data.startswith(b"%PDF"):
        try:
            found = [str(uri) for page in PdfReader(BytesIO(data)).pages for uri in _pdf_uris(page)]
        except PyPdfError as exc:
            raise ValueError(f"unreadable PDF: {exc}") from exc
    elif content_type is None or content_type in HTML_TYPES:
        found = _read_html(data.decode(_html_charset(data), errors="replace"), skip_classes).hrefs
    else:
        raise ValueError(f"no reader for content type {content_type!r}; add one to kb/extract.py")
    own = urldefrag(base).url
    urls: dict[str, None] = {}
    for href in found:
        url = urldefrag(urljoin(base, href.strip())).url
        if urlparse(url).scheme in {"http", "https"} and url != own:
            urls.setdefault(url)
    return list(urls)


def _html_charset(data: bytes) -> str:
    """The charset a <meta> tag declares, else UTF-8.

    Latin-1 labels decode as windows-1252, as browsers do (WHATWG Encoding), so typographic quotes and dashes survive.
    Without a usable label, bytes that are not valid UTF-8 are read as windows-1252: some servers send their charset
    only in the HTTP header, which fetch does not keep.
    """
    match = re.search(rb"<meta[^>]*charset=[\"']?([\w-]+)", data[:2048], re.I)
    try:
        name = codecs.lookup(match.group(1).decode()).name if match else None
    except LookupError:
        name = None
    if name is None:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return "cp1252"
        return "utf-8"
    return "cp1252" if name in {"iso8859-1", "ascii"} else name


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# --- HTML -------------------------------------------------------------------

BLOCK_TAGS = {
    "p", "li", "dt", "dd", "tr", "div", "section", "article", "blockquote", "pre", "table", "ul", "ol", "br",
    "h1", "h2", "h3", "h4", "h5", "h6",
}  # fmt: skip
SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "button", "svg", "template"}
VOID_TAGS = {"br", "img", "hr", "meta", "link", "input", "source", "wbr"}


class _PageForm(HTMLParser):
    """Finds the ASP.NET page form: the outermost form holding the __VIEWSTATE field, numbered by start tag."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms = 0
        self.open: list[int] = []
        self.found: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "form":
            self.forms += 1
            self.open.append(self.forms)
        elif tag == "input" and self.open and self.found is None and ("name", "__VIEWSTATE") in attrs:
            self.found = self.open[0]

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.open:
            self.open.pop()


class _HTMLBlocks(HTMLParser):
    def __init__(self, page_form: int | None = None, skip_classes: Collection[str] = ()) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.hrefs: list[str] = []  # href of every <a> in the text read, skipped regions left out
        self.buffer: list[str] = []
        self.level: int | None = None
        self.skip_depth = 0
        self.stack: list[str] = []
        self.page_form = page_form  # ASP.NET wraps the whole page in this form; other forms are skipped
        self.forms = 0
        self.skip_classes = frozenset(skip_classes)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in VOID_TAGS:
            if tag == "br":
                self.buffer.append(" ")
            return
        classes = " ".join(v or "" for k, v in attrs if k in {"class", "id"}).lower()
        hidden = any(k == "hidden" or (k == "aria-hidden" and v == "true") for k, v in attrs)
        if tag == "form":
            self.forms += 1
        page_form = tag == "form" and self.forms == self.page_form
        # A cookie banner is skipped by its class; <html> and <body> carry state classes such as
        # "cookies-agreement-present" (Human Kinetics) and wrap the whole page.
        banner = "cookie" in classes and tag not in {"html", "body"}
        skipped_class = bool(self.skip_classes) and any(
            k == "class" and not self.skip_classes.isdisjoint((v or "").split()) for k, v in attrs
        )
        skipping = self.skip_depth or (tag in SKIP_TAGS and not page_form) or hidden or banner or skipped_class
        self.stack.append(tag)
        if skipping:
            self.skip_depth += 1
            return
        if tag == "a":
            self.hrefs.extend(v for k, v in attrs if k == "href" and v)
        if tag in BLOCK_TAGS:
            self.flush()
            if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
                self.level = int(tag[1])
        elif tag in {"td", "th"}:
            self.buffer.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_TAGS or tag not in self.stack:
            return
        while self.stack:
            open_tag = self.stack.pop()
            if self.skip_depth:
                self.skip_depth -= 1
            elif open_tag in BLOCK_TAGS:
                self.flush()
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.buffer.append(data)

    def flush(self) -> None:
        text = clean("".join(self.buffer).strip(" |"))
        if text:
            self.blocks.append(Block(text, self.level))
        self.buffer = []
        self.level = None


def from_html(html: str, skip_classes: Collection[str] = ()) -> list[Block]:
    """Block text from the page's <main> (or <body>), without navigation, footers, cookie banners and elements
    carrying one of skip_classes."""
    return _read_html(html, skip_classes).blocks


def _read_html(html: str, skip_classes: Collection[str] = ()) -> _HTMLBlocks:
    for tag in ("main", "body"):
        match = re.search(rf"<{tag}[\s>].*?</{tag}>", html, re.S | re.I)
        if match:
            html = match.group(0)
            break
    page_form = None
    if "__VIEWSTATE" in html:
        finder = _PageForm()
        finder.feed(html)
        finder.close()
        page_form = finder.found
    parser = _HTMLBlocks(page_form, skip_classes)
    parser.feed(html)
    parser.close()
    parser.flush()
    return parser


# --- PDF --------------------------------------------------------------------


def from_pdf(data: bytes) -> list[Block]:
    """One block per text line, without the running headers and footers repeated across pages."""
    pages = [(page.extract_text() or "").splitlines() for page in PdfReader(BytesIO(data)).pages]
    repeated = _running_lines(pages)
    blocks = []
    for number, lines in enumerate(pages, start=1):
        for line in lines:
            text = clean(line)
            if text and _shape(text) not in repeated:
                blocks.append(Block(text, page=number))
    return blocks


def _pdf_uris(page: PageObject) -> list[object]:
    """URIs of the page's link annotations (/A /URI); other annotations and actions are left out."""
    uris = []
    annots = page.get("/Annots")
    for ref in annots.get_object() if annots is not None else []:
        action = ref.get_object().get("/A")
        uri = action.get_object().get("/URI") if action is not None else None
        if uri is not None:
            uris.append(uri)
    return uris


def _shape(line: str) -> str:
    return re.sub(r"\d+", "#", clean(line))


def _running_lines(pages: list[list[str]]) -> set[str]:
    """Line shapes (digits masked) found in the first or last three lines of at least half the pages.

    Catches running titles, "Page 3 of 59" and bare page numbers ("#").
    """
    if len(pages) < 4:
        return set()
    counts: Counter[str] = Counter()
    for lines in pages:
        edges = [line for line in lines if line.strip()]
        counts.update({_shape(line) for line in edges[:3] + edges[-3:]})
    return {shape for shape, n in counts.items() if n >= len(pages) / 2}
