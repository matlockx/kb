"""Turn a raw download into text blocks: headings, paragraphs, page-numbered PDF lines and structural section starts."""

import codecs
import json
import re
import xml.etree.ElementTree as ET  # expat >= 2.4.1 caps entity expansion; external entities are never fetched
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
XML_TYPES = frozenset({"application/xml", "text/xml"})


@dataclass(frozen=True)
class Block:
    text: str
    level: int | None = None  # heading level (1 = top) or None for body text
    ref: str | None = None  # set when the format itself marks a section start, e.g. "§ 6" or "s. 65"
    page: int | None = None  # 1-based, PDFs only
    quoted: bool = False  # inside an HTML <blockquote>, e.g. a judgment quoting another court's paragraphs


def extract(path: Path, content_type: str | None, skip_classes: Collection[str] = ()) -> list[Block]:
    """Blocks of one stored download; raise ValueError for a format with no reader.

    PDF by type or magic; XML by type, or by an XML declaration on a body not typed as HTML, read by its root
    element; application/json as a GOV.UK Content API item. HTML elements carrying any of skip_classes as a class
    are left out with their content; other formats ignore it.
    """
    data = path.read_bytes()
    if content_type == "application/pdf" or data.startswith(b"%PDF"):
        return from_pdf(data)
    if content_type in XML_TYPES or (content_type not in HTML_TYPES and data.lstrip().startswith(b"<?xml")):
        return from_xml(data)
    if content_type == "application/json":
        return from_govuk_json(data, skip_classes)
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


def from_govuk_json(data: bytes, skip_classes: Collection[str] = ()) -> list[Block]:
    """A GOV.UK Content API item: its title and the HTML of details.body (the HTML page carries a per-request
    CSRF token, so it hashes anew on every fetch)."""
    doc = json.loads(data)  # a JSONDecodeError is a ValueError, which parse reports per source
    details = doc.get("details") if isinstance(doc, dict) else None
    body = details.get("body") if isinstance(details, dict) else None
    if not isinstance(body, str):
        raise ValueError("JSON is not a GOV.UK content item with details.body")
    return from_html(f"<h1>{doc.get('title', '')}</h1>{body}", skip_classes)


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
        self.quote_depth = 0

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
            if tag == "blockquote":
                self.quote_depth += 1
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
                if open_tag == "blockquote":
                    self.quote_depth -= 1
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.buffer.append(data)

    def flush(self) -> None:
        text = clean("".join(self.buffer).strip(" |"))
        if text:
            self.blocks.append(Block(text, self.level, quoted=self.quote_depth > 0))
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


# --- XML --------------------------------------------------------------------


def from_xml(data: bytes) -> list[Block]:
    """Blocks of a LexDania, CLML, BWB, Akoma Ntoso or BOE document, chosen by its root element."""
    try:
        root = ET.fromstring(data)  # noqa: S314 - see import note
    except ET.ParseError as exc:  # a SyntaxError; parse reports ValueErrors per source
        raise ValueError(f"malformed XML: {exc}") from exc
    name = _local(root.tag)
    if name == "Dokument":
        return _lexdania(root)
    if name == "Legislation":
        return _clml(root)
    if name == "toestand":
        return _bwb(root)
    if name == "akomaNtoso":
        return _akoma_ntoso(root)
    if name == "response" and root.find("data/texto") is not None:
        return _boe(root)
    raise ValueError(f"unsupported XML document <{name}>")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element: ET.Element, skip: frozenset[str] = frozenset()) -> str:
    parts: list[str] = []

    def walk(node: ET.Element) -> None:
        if _local(node.tag) in skip:
            if node.tail:
                parts.append(node.tail)
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
        if node.tail:
            parts.append(node.tail)

    if element.text:
        parts.append(element.text)
    for child in element:
        walk(child)
    return clean(" ".join(parts))


def _lexdania(root: ET.Element) -> list[Block]:
    """Danish retsinformation.dk XML: Kapitel and ParagrafGruppe headings, one section per Paragraf.

    In an amending act (lov or bekendtgørelse om ændring) each top-level paragraph is one section, ref "§ 4", holding
    the name of the law it amends and its numbered amendments with their new text; the commencement paragraph
    is a section too. Sections quoted inside an amendment stay in that amendment's text.
    """
    blocks: list[Block] = []

    def walk(node: ET.Element) -> None:
        name = _local(node.tag)
        if name == "Kapitel":
            heading = " ".join(_text(c) for c in node if _local(c.tag) in {"Explicatus", "Rubrica"})
            if heading:
                blocks.append(Block(heading, level=1))
        elif name == "ParagrafGruppe":
            rubrica = next((c for c in node if _local(c.tag) == "Rubrica"), None)
            if rubrica is not None:
                blocks.append(Block(_text(rubrica), level=2))
        elif name in {"Paragraf", "AendringCentreretParagraf", "IkraftCentreretParagraf"}:
            label = next((c for c in node if _local(c.tag) == "Explicatus"), None)
            ref = clean(_text(label).rstrip(".")) if label is not None else None
            for index, stk in enumerate(c for c in node if _local(c.tag) in {"Stk", "Exitus", "AendringsNummer"}):
                text = _text(stk)
                if index == 0 and label is not None:
                    text = f"{_text(label)} {text}"
                blocks.append(Block(text, ref=ref if index == 0 else None))
            return
        elif name in {"Rubrica", "Explicatus", "Ikraft", "Nota"}:  # Ikraft: commencement notes of amending acts
            return
        for child in node:
            walk(child)

    for child in root:
        if _local(child.tag) != "Meta":
            walk(child)
    return blocks


BOE_HEADINGS = {"anexo": 1, "titulo": 1, "capitulo": 2, "seccion": 3, "subseccion": 4}


def _boe(root: ET.Element) -> list[Block]:
    """Spanish BOE consolidated text (datosabiertos API): one section per precepto, read from its latest version.

    Encabezado blocks give the Anexo, Título and Capítulo headings; blockquote amendment notes are skipped.
    Preceptos after an ANEXO heading get refs "Anexo <n>", because annexes restart their numbering, and an
    annex point "3." splits further at its sub-points "3.1", "3.2", ...
    """
    blocks: list[Block] = []
    in_annex = False
    for bloque in root.iterfind("data/texto/bloque"):
        versions = bloque.findall("version")
        if not versions or bloque.get("tipo") == "firma":
            continue
        paragraphs = versions[-1].findall("p")  # direct children only: notes sit inside blockquote
        if bloque.get("tipo") == "encabezado":
            parts: dict[int, list[str]] = {}
            for p in paragraphs:
                kind = (p.get("class") or "").removesuffix("_num").removesuffix("_tit")
                if kind in BOE_HEADINGS:
                    parts.setdefault(BOE_HEADINGS[kind], []).append(_text(p))
                    in_annex = in_annex or kind == "anexo"
            blocks += [Block(clean(" ".join(texts)), level=level) for level, texts in parts.items()]
            if parts:
                continue
        ref = None
        if bloque.get("tipo") == "precepto":
            ref = clean(bloque.get("titulo") or "") or None  # some titles use NBSP
        sub = None
        if ref and in_annex:
            number = re.match(r"(\d+)\.? ", ref)
            ref = f"Anexo {number.group(1) if number else ref}"
            sub = re.compile(rf"({number.group(1)}(?:\.\d+)+)\.? ") if number else None
        for p in paragraphs:
            if not (text := clean(_text(p))):
                continue
            point = sub.match(text) if sub else None
            blocks.append(Block(text, ref=f"Anexo {point.group(1)}" if point else ref))
            ref = None
    return blocks


def _clml(root: ET.Element) -> list[Block]:
    """legislation.gov.uk CLML: Part, Chapter and cross-heading headings, one section per P1."""
    blocks: list[Block] = []
    skip = frozenset({"Commentaries", "CommentaryRef", "Metadata", "Contents"})
    for parent in root.iter():  # print subsection numbers as "(1)" and "(a)", as the published Act does
        if _local(parent.tag) in {"P2", "P3", "P4", "P5", "P6"}:
            for number in (c for c in parent if _local(c.tag) == "Pnumber"):
                if len(number):  # the digits follow the CommentaryRef children, in the last tail
                    number[-1].tail = f"({(number[-1].tail or '').strip()})"
                else:
                    number.text = f"({(number.text or '').strip()})"

    def heading(node: ET.Element) -> str:
        return " ".join(_text(c, skip) for c in node if _local(c.tag) in {"Number", "Title"})

    def walk(node: ET.Element, title: str | None, prefix: str) -> None:
        name = _local(node.tag)
        if name in skip:
            return
        if name in {"Part", "Chapter", "Schedule"}:
            text = heading(node)
            if text:
                blocks.append(Block(text, level={"Part": 1, "Schedule": 1, "Chapter": 2}[name]))
            if name == "Schedule":
                number = next((c for c in node if _local(c.tag) == "Number"), None)
                digits = re.findall(r"\d+[A-Z]*", _text(number, skip)) if number is not None else []
                prefix = f"Sch. {digits[0]} para." if digits else "Sch. para."
        elif name == "Pblock":
            text = heading(node)
            if text:
                blocks.append(Block(text, level=3))
        elif name == "P1group":
            title = _text(next((c for c in node if _local(c.tag) == "Title"), node), skip)
            if not any(_local(c.tag) == "P1" for c in node.iter()):
                # DEV-NOTE: a group of P2s with no P1, such as the Notes closing each VATA 1994 Sch. 9 Group, holds
                # definitions; walking into it would drop the text, as only P1 emits a block.
                base = "" if prefix == "s." else prefix.removesuffix(" para.")
                blocks.append(Block(_text(node, skip), ref=f"{base} {title.rstrip(':')}".strip() or None))
                return
        elif name == "P1":
            number = next((c for c in node if _local(c.tag) == "Pnumber"), None)
            ref = f"{prefix} {_text(number, skip)}" if number is not None else None
            text = _text(node, skip)
            blocks.append(Block(f"{title} {text}" if title else text, ref=ref))
            return
        elif name in {"Number", "Title"}:
            return
        for child in node:
            walk(child, title, prefix)

    body = next((c for c in root.iter() if _local(c.tag) in {"Body", "Primary"}), root)
    walk(body, None, "s.")
    return blocks


BWB_HEADINGS = {"boek": 1, "deel": 1, "hoofdstuk": 1, "titeldeel": 1, "afdeling": 2, "paragraaf": 3, "sub-paragraaf": 4}


def _bwb(root: ET.Element) -> list[Block]:
    """Dutch wetten.overheid.nl BWB XML: hoofdstuk/afdeling/paragraaf headings, one section per artikel."""
    blocks: list[Block] = []
    skip = frozenset({"meta-data", "redactie", "aanhef", "wetsluiting", "noot", "jcis", "brondata"})

    def kop(node: ET.Element) -> str:
        head = next((c for c in node if c.tag == "kop"), None)
        return _text(head, skip) if head is not None else ""

    def walk(node: ET.Element) -> None:
        if node.tag in skip:
            return
        if node.tag in BWB_HEADINGS:
            if text := kop(node):
                blocks.append(Block(text, level=BWB_HEADINGS[node.tag]))
        elif node.tag == "artikel":
            head = next((c for c in node if c.tag == "kop"), None)
            number = head.find("nr") if head is not None else None
            ref = f"Artikel {_text(number, skip)}" if number is not None else None
            body = [c for c in node if c.tag not in {"kop", *skip}]
            text = " ".join(t for t in (kop(node), *(_text(c, skip) for c in body)) if t)
            if text and node.get("status") != "vervallen":
                blocks.append(Block(text, ref=ref))
            return
        elif node.tag == "kop":
            return
        for child in node:
            walk(child)

    walk(root)
    return blocks


def _akoma_ntoso(root: ET.Element) -> list[Block]:
    """Akoma Ntoso: part/chapter headings, one section per unit.

    Finlex numbers <section>s ("6 §"); Normattiva numbers <article>s ("Art. 1.") and uses <section> for a Capo
    between the chapter (Titolo) and its articles, so a section that contains articles is a heading.
    """
    blocks: list[Block] = []
    skip = frozenset({"meta", "authorialNote", "noteRef"})
    levels = {"part": 1, "chapter": 2, "section": 3}

    def child(node: ET.Element, name: str) -> ET.Element | None:
        return next((c for c in node if _local(c.tag) == name), None)

    def head(node: ET.Element) -> str:
        parts = (_text(c, skip) for c in (child(node, "num"), child(node, "heading")) if c is not None)
        return " ".join(t for t in parts if t)  # Normattiva leaves <heading/> empty

    def walk(node: ET.Element) -> None:
        name = _local(node.tag)
        if name in skip:
            return
        unit = name == "article" or (name == "section" and not any(_local(d.tag) == "article" for d in node.iter()))
        if name in levels and not unit:
            if text := head(node):
                blocks.append(Block(text, level=levels[name]))
        elif unit:
            num = child(node, "num")
            number = _text(num, skip).rstrip(".") if num is not None else ""
            ref = number or None  # both number their units through the whole act
            body = [c for c in node if _local(c.tag) not in {"num", "heading", *skip}]
            text = " ".join(t for t in (head(node), *(_text(c, skip) for c in body)) if t)
            if text:
                blocks.append(Block(text, ref=ref))
            return
        for c in node:
            walk(c)

    body = next((c for c in root.iter() if _local(c.tag) == "body"), root)
    walk(body)
    return blocks
