"""Split extracted blocks into sections on the document's own numbering."""

import re
from dataclasses import dataclass, field

from kb.extract import Block

MAX_CHARS = 12_000  # longer sections are split on block boundaries into "(part n)" chunks
PREAMBLE = "(preamble)"
TITLE_CHARS = 150


@dataclass(frozen=True)
class Section:
    ref: str
    heading_path: tuple[str, ...]
    text: str


@dataclass
class _Open:
    ref: str
    path: tuple[str, ...]
    texts: list[str] = field(default_factory=list)


def split(
    blocks: list[Block],
    section_pattern: str | None = None,
    chapter_pattern: str | None = None,
    body_start: str | None = None,
    chapter_label: str | None = None,
    first_chapter: str = "",
    body_end: str | None = None,
    section_label: str | None = None,
) -> list[Section]:
    """Group blocks into sections.

    Sections start at blocks the format marks (Block.ref, set by a structured-format reader); otherwise at
    blocks matching section_pattern, whose named group "ref" is the section reference; else at every heading
    (HTML) or every page (PDF). Patterns see HTML headings in Markdown form ("## 1 Introduction"), so they can
    require a heading level.

    chapter_pattern (group "ref") matches chapter headings and prefixes the refs that follow, for texts
    that restart numbering per chapter ("3 kap. 6 §"). chapter_label, a re.Match.expand template such as
    "Part \\g<n>", replaces that prefix; first_chapter is the prefix before any chapter matches. body_start
    matches the first line of the body: everything before it, such as a table of contents, stays in the
    preamble. body_end matches the first line after the body, such as an appendix or the next article in a
    collected volume; it and everything after it are dropped. section_label, a re.Match.expand template,
    rewrites the ref of each section_pattern match, e.g. "\\g<num> §" for a text that prints both "1§" and
    "10 §". Headings, and in pattern mode a title line just before a section number, travel with the section
    they introduce. A ref that repeats gets "(2)", "(3)".
    """
    section_re = re.compile(section_pattern) if section_pattern else None
    chapter_re = re.compile(chapter_pattern) if chapter_pattern else None
    if body_start is not None:
        body_re = re.compile(body_start)
        first = next((i for i, b in enumerate(blocks) if body_re.match(_marked(b))), None)
        if first is None:
            raise ValueError(f"body_start {body_start!r} matches no line; the document layout has changed")
    else:
        first = 0
    if body_end is not None:
        end_re = re.compile(body_end)
        end = next((i for i, b in enumerate(blocks) if i > first and end_re.match(_marked(b))), None)
        if end is None:
            raise ValueError(f"body_end {body_end!r} matches no line after the body start; the layout has changed")
        blocks = blocks[:end]
    has_headings = any(b.level for b in blocks)
    if any(b.ref for b in blocks):
        mode = "marked"
    elif section_re is not None:
        mode = "pattern"
    else:
        mode = "heading" if has_headings else "page"

    refs = [None] * first + _starts(blocks[first:], mode, section_re, section_label)

    sections: list[_Open] = []
    headings: list[tuple[int, str]] = []
    chapter = first_chapter
    pending: list[str] = []  # headings waiting for the section they introduce
    for index, (block, ref) in enumerate(zip(blocks, refs, strict=True)):
        chapter_match = chapter_re.match(_marked(block)) if chapter_re and index >= first else None
        if chapter_match:
            chapter = chapter_match.expand(chapter_label) if chapter_label else chapter_match.group("ref")
        if block.level:
            headings = [h for h in headings if h[0] < block.level]
        elif chapter_match:
            headings = []

        if ref is not None:
            carried, pending = pending, []
            previous = sections[-1] if sections else None
            if (
                mode == "pattern"
                and not carried
                and previous
                and len(previous.texts) > 1
                and _is_title(previous.texts[-1])
            ):
                carried = [previous.texts.pop()]
            full_ref = f"{chapter} {ref}" if chapter and not ref.startswith(chapter) else ref
            sections.append(_Open(full_ref, tuple(h[1] for h in headings), [*carried, block.text]))
        elif block.level or chapter_match:
            pending.append(block.text)
        else:
            if not sections:
                sections.append(_Open(PREAMBLE, ()))
            sections[-1].texts.extend([*pending, block.text])
            pending = []

        if block.level:
            headings.append((block.level, block.text))
        elif chapter_match:
            headings = [(1, block.text)]

    if pending:
        if not sections:
            sections.append(_Open(PREAMBLE, ()))
        sections[-1].texts.extend(pending)
    _disambiguate(sections)
    return [piece for section in sections for piece in _cap(section)]


def _disambiguate(sections: list[_Open]) -> None:
    """Make refs unique: a ref seen again (quoted amending text, a schedule part restarting at 1) gets "(2)"."""
    # ponytail: numbering is positional, so an inserted duplicate shifts later suffixes. Refine with a
    # chapter_pattern for the source when a duplicate is a citation target.
    seen: dict[str, int] = {}
    for section in sections:
        count = seen.get(section.ref, 0) + 1
        seen[section.ref] = count
        if count > 1:
            section.ref = f"{section.ref} ({count})"


def _starts(
    blocks: list[Block], mode: str, section_re: re.Pattern[str] | None, label: str | None = None
) -> list[str | None]:
    refs: list[str | None] = []
    last_page = None
    for block in blocks:
        ref = None
        if mode == "marked":
            ref = block.ref
        elif mode == "pattern" and section_re is not None:
            match = section_re.match(_marked(block))
            ref = (match.expand(label) if label else match.group("ref")) if match else None
        elif mode == "heading" and block.level:
            ref = block.text[:120]
        elif mode == "page" and block.page is not None and block.page != last_page:
            ref = f"p. {block.page}"
        last_page = block.page
        refs.append(ref)
    return refs


def _marked(block: Block) -> str:
    return f"{'#' * block.level} {block.text}" if block.level else block.text


def _is_title(text: str) -> bool:
    """A short line with no closing punctuation, e.g. the title printed above "169. (1) ..."."""
    return len(text) <= TITLE_CHARS and not re.search(r"[.;:,!?)\]»”\"]$", text) and not text[:1].islower()


def _cap(section: _Open) -> list[Section]:
    parts: list[list[str]] = [[]]
    size = 0
    for text in section.texts:
        if parts[-1] and size + len(text) > MAX_CHARS:
            parts.append([])
            size = 0
        parts[-1].append(text)
        size += len(text) + 1
    if len(parts) == 1:
        return [Section(section.ref, section.path, "\n".join(section.texts))]
    return [Section(f"{section.ref} (part {n})", section.path, "\n".join(p)) for n, p in enumerate(parts, start=1)]
