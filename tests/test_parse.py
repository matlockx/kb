import dataclasses
import io
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from pypdf import PdfWriter
from pypdf.annotations import Link
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from kb import chunk, db, extract, parse, sources
from kb.chunk import Section
from kb.extract import Block
from kb.sources import Source

# --- extract ------------------------------------------------------------------


def test_html_keeps_main_text_and_heading_levels() -> None:
    html = """<html><body><nav>Menu</nav><div class="cookie-banner">We use cookies</div>
    <main><h2>RTS 12 - Financial limits</h2><p>Operators <b>must</b>
    offer limits.</p><script>x()</script><ul><li>daily</li><li>weekly</li></ul>
    <table><tr><td>a</td><td>b</td></tr></table><p hidden>secret</p></main>
    <footer>Footer</footer></body></html>"""
    assert extract.from_html(html) == [
        Block("RTS 12 - Financial limits", level=2),
        Block("Operators must offer limits."),
        Block("daily"),
        Block("weekly"),
        Block("a | b"),
    ]


def test_html_reads_inside_the_aspnet_page_form_whatever_its_id() -> None:
    html = (
        '<body><form id="newsletter"><p>Subscribe</p></form><form method="post" id="form">'
        '<div class="aspNetHidden"><input type="hidden" name="__VIEWSTATE" value="x"></div>'
        "<h1>Rates</h1><form><p>Search</p></form><p>2%</p></form></body>"
    )
    assert extract.from_html(html) == [Block("Rates", level=1), Block("2%")]


def test_html_unclosed_tags_do_not_leak_skipping() -> None:
    assert extract.from_html("<body><p>one<nav>skip</p><p>two</p></body>") == [Block("one"), Block("two")]


def test_html_skip_classes_drop_elements_and_their_links(tmp_path: Path) -> None:
    html = (
        '<body><h4><span class="LegP1No"><a class="LegCommentaryLink" href="#c1">X1</a>180</span> Rights</h4>'
        '<p class="Note other">dropped <a href="https://example.org/n">n</a></p><p class="Notes">kept</p></body>'
    )
    assert extract.from_html(html, {"LegCommentaryLink", "Note"}) == [Block("180 Rights", level=4), Block("kept")]
    assert extract.from_html(html)[0] == Block("X1180 Rights", level=4)
    path = tmp_path / "doc.html"
    path.write_text(html, encoding="utf-8")
    assert extract.links(path, "text/html", "https://example.org/act") == ["https://example.org/n"]
    assert extract.links(path, "text/html", "https://example.org/act", {"Note"}) == []


def test_html_cookie_state_class_on_body_does_not_skip_the_page() -> None:
    html = '<body class="cookies-agreement-present"><div class="cookie-bar">Accept</div><h2>Methods</h2><p>x</p></body>'
    assert extract.from_html(html) == [Block("Methods", level=2), Block("x")]


def test_html_decodes_the_declared_charset(tmp_path: Path) -> None:
    path = tmp_path / "page.html"
    iso = '<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1" />'
    for meta in (iso, "<meta charset=bogus>", ""):  # "": no label at all
        body = "<p>„Gemüse“ \u2013 x</p>".encode("cp1252")
        path.write_bytes(f"<html><head>{meta}</head><body>".encode() + body + b"</body></html>")
        expected = "„Gemüse“ \u2013 x"  # an unknown label falls back to sniffing: not UTF-8, so windows-1252
        assert extract.extract(path, "text/html") == [Block(expected)]
    path.write_text("<body><p>Gemüse</p></body>", encoding="utf-8")
    assert extract.extract(path, "text/html") == [Block("Gemüse")]


def pdf_with_pages(*pages: str) -> bytes:
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=600, height=800)
        lines = "".join(
            f"BT /F1 10 Tf 40 {760 - 14 * i} Td ({line}) Tj ET\n" for i, line in enumerate(text.splitlines())
        )
        stream = DecodedStreamObject()
        stream.set_data(lines.encode("latin-1"))
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_pdf_drops_running_headers_and_page_numbers() -> None:
    words = ["Formaal", "Konto", "Betaling", "Rapportering"]
    pages = [
        f"Tekniske krav 2025\n{n}.1 {words[n - 1]}\nThe {words[n - 1]} rule applies.\nSee {words[n - 1].lower()}.\n"
        f"Page {n} of 4\n{n}"
        for n in range(1, 5)
    ]
    blocks = extract.from_pdf(pdf_with_pages(*pages))
    assert [b.text for b in blocks[:3]] == ["1.1 Formaal", "The Formaal rule applies.", "See formaal."]
    assert {b.page for b in blocks} == {1, 2, 3, 4}
    assert not any("Tekniske krav" in b.text or b.text.startswith("Page") or b.text.isdigit() for b in blocks)


def test_extract_dispatches_on_content_type_and_magic(tmp_path: Path) -> None:
    path = tmp_path / "doc.bin"
    path.write_bytes(pdf_with_pages("Section 1"))
    assert extract.extract(path, None) == [Block("Section 1", page=1)]
    path.write_bytes(b"<html><body><p>x</p></body></html>")
    assert extract.extract(path, "text/html") == [Block("x")]
    assert extract.extract(path, None) == [Block("x")]


def test_xhtml_is_read_as_html(tmp_path: Path) -> None:
    path = tmp_path / "doc.html"
    path.write_text('<?xml version="1.0"?><html><body><p>Article 135</p></body></html>', encoding="utf-8")
    assert extract.extract(path, "application/xhtml+xml") == [Block("Article 135")]


def test_html_links_resolve_dedupe_and_skip_navigation_and_self(tmp_path: Path) -> None:
    path = tmp_path / "doc.html"
    base = "https://example.org/act/index.html"
    path.write_text(
        '<body><nav><a href="/home">Home</a></nav><p>See <a href="annex.pdf#p2">Annex</a>, '
        '<a href="#art1">Article 1</a>, <a href="https://other.org/x">x</a>, <a href=" annex.pdf ">again</a>, '
        '<a href="mailto:a@b.org">mail</a> and <a href="index.html?print=1">print</a>.</p></body>',
        encoding="utf-8",
    )
    assert extract.links(path, "text/html", base) == [
        "https://example.org/act/annex.pdf",
        "https://other.org/x",
        "https://example.org/act/index.html?print=1",
    ]


def test_pdf_links_come_from_uri_annotations(tmp_path: Path) -> None:
    reader_input = io.BytesIO(pdf_with_pages("Section 1", "Section 2"))
    writer = PdfWriter(clone_from=reader_input)
    writer.add_annotation(1, Link(rect=(10, 10, 50, 50), url="https://example.org/annex"))
    writer.add_annotation(0, Link(rect=(10, 10, 50, 50), target_page_index=1))  # internal jump, no URI
    path = tmp_path / "doc.pdf"
    with path.open("wb") as out:
        writer.write(out)
    assert extract.links(path, "application/pdf", "https://example.org/act.pdf") == ["https://example.org/annex"]


def test_links_of_an_unreadable_pdf_raise_value_error(tmp_path: Path) -> None:
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.7 truncated")
    with pytest.raises(ValueError, match="unreadable PDF"):
        extract.links(path, "application/pdf", "https://example.org/act.pdf")


def test_unsupported_content_type_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "doc.txt"
    path.write_text("Code", encoding="utf-8")
    with pytest.raises(ValueError, match="no reader for content type 'text/plain'"):
        extract.extract(path, "text/plain")


# --- chunk --------------------------------------------------------------------


def refs(sections: list[Section]) -> list[str]:
    return [s.ref for s in sections]


def test_marked_blocks_win_over_patterns() -> None:
    blocks = [Block("§ 1. A.", ref="§ 1"), Block("Stk. 2. B."), Block("§ 2. C.", ref="§ 2")]
    sections = chunk.split(blocks, section_pattern=r"^(?P<ref>Stk\. \d+)")
    assert sections == [Section("§ 1", (), "§ 1. A.\nStk. 2. B."), Section("§ 2", (), "§ 2. C.")]


def test_pattern_with_chapters_headings_and_titles() -> None:
    blocks = [
        Block("Livsmedelslag", level=1),
        Block("Innehåll: 1 kap. Tillämpning"),
        Block("1 kap. Tillämpning", level=3),
        Block("1 § Lagen gäller livsmedel."),
        Block("Omsorgsplikt"),
        Block("2 § Företagare ska skydda konsumenter."),
        Block("2 kap. Uttryck", level=3),
        Block("1 § I lagen avses."),
    ]
    sections = chunk.split(blocks, r"^(?P<ref>\d+ §)", r"^### (?P<ref>\d+ kap\.)", body_start=r"^### 1 kap\.")
    assert sections == [
        Section("(preamble)", (), "Livsmedelslag\nInnehåll: 1 kap. Tillämpning"),
        Section(
            "1 kap. 1 §", ("Livsmedelslag", "1 kap. Tillämpning"), "1 kap. Tillämpning\n1 § Lagen gäller livsmedel."
        ),
        Section(
            "1 kap. 2 §",
            ("Livsmedelslag", "1 kap. Tillämpning"),
            "Omsorgsplikt\n2 § Företagare ska skydda konsumenter.",
        ),
        Section("2 kap. 1 §", ("Livsmedelslag", "2 kap. Uttryck"), "2 kap. Uttryck\n1 § I lagen avses."),
    ]


def test_body_end_drops_the_next_act() -> None:
    blocks = [Block("front"), Block("ANEXO"), Block("1 — Rule."), Block("Regulamento n.º 903-B/2015"), Block("1 — X.")]
    sections = chunk.split(blocks, r"^(?P<ref>\d+) — ", body_start="^ANEXO$", body_end=r"^Regulamento n\.º 903-B")
    assert [(s.ref, s.text) for s in sections] == [("(preamble)", "front"), ("1", "ANEXO\n1 — Rule.")]
    with pytest.raises(ValueError, match="body_end"):
        chunk.split(blocks, r"^(?P<ref>\d+) — ", body_end="^nothing$")


def test_body_start_that_never_matches_fails_loudly() -> None:
    with pytest.raises(ValueError, match="layout has changed"):
        chunk.split([Block("x")], r"^(?P<ref>\d+)", body_start=r"^Anexo I$")


def test_section_label_normalises_refs() -> None:
    blocks = [Block("1§ Dessa föreskrifter gäller."), Block("10 § En konsument får bara ha ett konto.")]
    sections = chunk.split(blocks, r"^(?P<ref>(?P<num>\d+) ?§)", section_label=r"\g<num> §")
    assert refs(sections) == ["1 §", "10 §"]


def test_chapter_label_and_first_chapter() -> None:
    blocks = [Block("1.1.1 - Qualified persons"), Block("Ordinary code"), Block("1.1.1 - Cooperation")]
    sections = chunk.split(blocks, r"^(?P<ref>\d+\.\d+\.\d+) - ", r"^(?P<ref>Ordinary code)$", None, "code", "LC")
    assert refs(sections) == ["LC 1.1.1", "code 1.1.1"]
    assert sections[1].text == "Ordinary code\n1.1.1 - Cooperation"


def test_repeated_refs_are_numbered() -> None:
    blocks = [Block("17. Membership."), Block("268. Amends:"), Block("17. Quoted section."), Block("17. Again.")]
    assert refs(chunk.split(blocks, r"^(?P<ref>\d+)\. ")) == ["17", "268", "17 (2)", "17 (3)"]


def test_paragraph_numbers_quoted_from_another_judgment_do_not_start_sections() -> None:
    html = (
        "<body><p>1.</p><p>The appeal concerns Navitaire.</p><blockquote><p>126.</p><p>Business logic is not"
        " protected.</p></blockquote><p>As quoted.</p><p>2.</p><p>Dismissed.</p></body>"
    )
    blocks = extract.from_html(html)
    assert [b.quoted for b in blocks] == [False, False, True, True, False, False, False]
    sections = chunk.split(blocks, r"^(?P<ref>\d+)\.$")
    assert [(s.ref, s.text) for s in sections] == [
        ("1", "1.\nThe appeal concerns Navitaire.\n126.\nBusiness logic is not protected.\nAs quoted."),
        ("2", "2.\nDismissed."),
    ]


def test_heading_and_page_fallbacks() -> None:
    by_heading = chunk.split([Block("intro"), Block("A", level=2), Block("a1"), Block("B", level=2)])
    assert refs(by_heading) == ["(preamble)", "A", "B"]
    by_page = chunk.split([Block("x", page=1), Block("y", page=1), Block("z", page=3)])
    assert [(s.ref, s.text) for s in by_page] == [("p. 1", "x\ny"), ("p. 3", "z")]


def test_long_sections_are_split_on_block_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chunk, "MAX_CHARS", 10)
    sections = chunk.split([Block("§ 1 aaaa", ref="§ 1"), Block("bbbbbb"), Block("cc")])
    assert [(s.ref, s.text) for s in sections] == [("§ 1 (part 1)", "§ 1 aaaa"), ("§ 1 (part 2)", "bbbbbb\ncc")]


# --- parse (end to end through the database) ---------------------------------

SOURCE = Source(
    id="dk-act",
    publisher="x",
    title="Fødevareloven",
    url="https://example.org/dk",
    language="da",
    doc_type="act",
    tags=("food",),
    section_pattern=r"^(?P<ref>§ \d+)\.",
)

PAGE = """<html><body><h1>Kapitel 1 Formål</h1><h2>Tilsyn</h2>
<p>§ 6. Tilladelse kan gives til Dansk Mejeri A/S.</p><p>Stk. 2. Tilladelsen kan overdrages.</p></body></html>"""


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = db.connect(tmp_path / "kb.db")
    sources.sync(conn, [SOURCE])
    yield conn
    conn.close()


def add_version(
    conn: sqlite3.Connection, raw: Path, name: str, body: bytes, checked: str, content_type: str = "text/html"
) -> str:
    (raw / name).parent.mkdir(parents=True, exist_ok=True)
    (raw / name).write_bytes(body)
    version = f"dk-act@{name}"
    with conn:
        conn.execute(
            "INSERT INTO document_versions (id, document_id, sha256, raw_path, content_type, fetched_at, "
            "last_checked_at) VALUES (?, 'dk-act', ?, ?, ?, ?, ?)",
            (version, name, name, content_type, checked, checked),
        )
    return version


def test_parse_replaces_chunks_of_the_current_version(conn: sqlite3.Connection, tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    old = add_version(conn, raw, "old.html", PAGE.encode(), "2026-01-01T00:00:00Z")
    [result] = parse.parse_all(conn, [SOURCE], raw)
    assert (result.version_id, result.error, refs(result.sections)) == (old, None, ["§ 6"])

    newer = PAGE.replace("Dansk Mejeri A/S.", "Dansk Mejeri A/S og andre.")
    new = add_version(conn, raw, "new.html", newer.encode(), "2026-02-01T00:00:00Z")
    parse.parse_all(conn, [SOURCE], raw)
    parse.parse_all(conn, [SOURCE], raw)  # idempotent
    rows = conn.execute(
        "SELECT id, version_id, section_ref, heading_path, text, sha256 FROM chunks ORDER BY id"
    ).fetchall()
    assert [r[:3] for r in rows] == [(f"{new}#0000", new, "§ 6"), (f"{old}#0000", old, "§ 6")]
    assert json.loads(rows[0][3]) == ["Kapitel 1 Formål", "Tilsyn"]
    assert rows[0][4] == (
        "Kapitel 1 Formål\nTilsyn\n§ 6. Tilladelse kan gives til Dansk Mejeri A/S og andre.\n"
        "Stk. 2. Tilladelsen kan overdrages."
    )
    assert rows[0][5] != rows[1][5]


def test_drop_preamble_stores_only_the_body_window(tmp_path: Path) -> None:
    path = tmp_path / "doc.html"
    path.write_text("<body><p>Act text</p><p>FIFTH SCHEDULE</p><p>§ 9. Lotteries.</p></body>", encoding="utf-8")
    window = dataclasses.replace(SOURCE, body_start="^FIFTH SCHEDULE$")
    assert refs(parse.sections_of(path, "text/html", window)) == ["(preamble)", "§ 9"]
    assert refs(parse.sections_of(path, "text/html", dataclasses.replace(window, drop_preamble=True))) == ["§ 9"]


def test_parse_failures_leave_chunks_untouched(conn: sqlite3.Connection, tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    assert parse.parse_all(conn, [SOURCE], raw)[0].error == "not fetched yet"

    add_version(conn, raw, "good.html", PAGE.encode(), "2026-01-01T00:00:00Z")
    parse.parse_all(conn, [SOURCE], raw)
    before = conn.execute("SELECT * FROM chunks").fetchall()

    add_version(conn, raw, "bad.txt", b"{}", "2026-02-01T00:00:00Z", "text/plain")
    [result] = parse.parse_all(conn, [SOURCE], raw)
    assert result.error == "no reader for content type 'text/plain'; add one to kb/extract.py"
    assert conn.execute("SELECT * FROM chunks").fetchall() == before


def test_parse_keeps_stored_sections_when_the_download_is_not_on_this_device(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    raw = tmp_path / "raw"
    add_version(conn, raw, "v.html", PAGE.encode(), "2026-01-01T00:00:00Z")
    parse.parse_all(conn, [SOURCE], raw)
    before = conn.execute("SELECT * FROM chunks").fetchall()
    (raw / "v.html").unlink()  # a pulled copy: the catalog carries no raw downloads
    [result] = parse.parse_all(conn, [SOURCE], raw)
    assert (result.kept, result.error) == (True, None)
    assert conn.execute("SELECT * FROM chunks").fetchall() == before


def test_parse_refuses_to_rechunk_a_version_with_statements(conn: sqlite3.Connection, tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    version = add_version(conn, raw, "v.html", PAGE.encode(), "2026-01-01T00:00:00Z")
    parse.parse_all(conn, [SOURCE], raw)
    with conn:
        conn.execute(
            "INSERT INTO statements (id, chunk_id, verbatim_quote, summary, modality, applies_to, model, "
            "prompt_version, created_at) VALUES ('r1', ?, 'q', 's', 'must', '[]', 'm', 'p1', 'now')",
            (f"{version}#0000",),
        )
    [result] = parse.parse_all(conn, [SOURCE], raw)
    assert result.error is None  # identical sections: nothing to re-chunk
    rechunked = dataclasses.replace(SOURCE, section_pattern=None)  # splits at headings instead
    [result] = parse.parse_all(conn, [rechunked], raw)
    assert result.error == "1 statements already cite this version's chunks; not re-chunking"


def write_domain(tmp_path: Path) -> Path:
    path = tmp_path / "domain.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "Test",
                "instructions": "Test domain.",
                "doc_types": ["act"],
                "tags": ["food"],
                "modalities": [{"id": "must", "description": "required"}],
                "topics": [{"id": "registration", "label": "Registration", "description": "registrations"}],
                "publishers": [{"name": "x", "domains": ["example.org"], "official": True}],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_cli_parse(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from kb.cli import main

    registry = tmp_path / "sources.yaml"
    entry = {k: v for k, v in SOURCE.__dict__.items() if v not in (None, "", ())} | {"tags": ["food"]}
    registry.write_text(yaml.safe_dump([entry], allow_unicode=True), encoding="utf-8")
    database = tmp_path / "kb.db"
    args = ["--db", str(database), "--domain", str(write_domain(tmp_path)), "parse", "--file", str(registry)]
    args += ["--raw", str(tmp_path / "raw")]

    assert main(args) == 1
    assert "dk-act                             failed: not fetched yet" in capsys.readouterr().out

    conn = db.connect(database)
    add_version(conn, tmp_path / "raw", "v.html", PAGE.encode(), "2026-01-01T00:00:00Z")
    conn.close()
    assert main([*args, "--sample", "1"]) == 0
    out = capsys.readouterr().out
    assert "dk-act                                  1" in out
    assert "  --- § 6  [Kapitel 1 Formål > Tilsyn]" in out
    assert out.rstrip().endswith("1 parsed, 0 failed")
