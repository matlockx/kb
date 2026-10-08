import argparse
import dataclasses
import email.message
import io
import re
import shlex
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from kb import db, fetch, sources
from kb.cli import ingest_script, main
from kb.domain import Domain, Publisher
from kb.sources import Source

SOURCE = Source(
    id="gb-act",
    publisher="Parliament",
    title="Act",
    url="https://example.org/act",
    language="en",
    doc_type="act",
    tags=("food",),
)


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, url: str = SOURCE.url, **headers: str) -> None:
        super().__init__(body)
        self.url = url
        self.headers = email.message.Message()
        for name, value in headers.items():
            self.headers[name.replace("_", "-")] = value

    def geturl(self) -> str:
        return self.url


def opener_returning(response: FakeResponse) -> fetch.Opener:
    def opener(request: object, timeout: float, context: object) -> FakeResponse:
        assert timeout == fetch.TIMEOUT_S
        assert context is fetch.TLS
        assert request.get_header("Accept-language") == "*"  # type: ignore[attr-defined]
        return response

    return opener


def opener_raising(exc: BaseException) -> fetch.Opener:
    def opener(request: object, timeout: float, context: object) -> FakeResponse:  # noqa: ARG001
        raise exc

    return opener


def test_download_reads_body_and_headers() -> None:
    response = FakeResponse(
        b"%PDF", Content_Type="application/PDF; charset=binary", ETag='"v1"', Last_Modified="x", Content_Length="4"
    )
    got = fetch.download(SOURCE.url, opener=opener_returning(response))
    assert got == fetch.Download(b"%PDF", "application/pdf", '"v1"', "x", SOURCE.url)


CHALLENGED = FakeResponse(b"", cf_mitigated="challenge").headers


@pytest.mark.parametrize(
    ("opener", "message"),
    [
        (opener_returning(FakeResponse(b"x", url="http://example.org/act")), "non-https"),
        (opener_returning(FakeResponse(b"")), "empty body"),
        (opener_returning(FakeResponse(b"12345")), "exceeds 4 bytes"),
        (opener_returning(FakeResponse(b"123", Content_Length="4")), "truncated: got 3 of 4 bytes"),
        (opener_returning(FakeResponse(b"x", cf_mitigated="challenge")), "Cloudflare challenge page"),
        (opener_returning(FakeResponse(b"", x_amzn_waf_action="challenge")), r"AWS WAF challenge \(needs a browser\)"),
        (opener_raising(urllib.error.HTTPError(SOURCE.url, 503, "down", email.message.Message(), None)), "HTTP 503$"),
        (
            opener_raising(urllib.error.HTTPError(SOURCE.url, 403, "no", CHALLENGED, None)),
            r"HTTP 403 \(Cloudflare challenge\)",
        ),
        (opener_raising(urllib.error.URLError("no route")), "no route"),
        (opener_raising(TimeoutError("timed out")), "timed out"),
    ],
)
def test_download_failures(opener: fetch.Opener, message: str) -> None:
    with pytest.raises(fetch.FetchError, match=message):
        fetch.download(SOURCE.url, opener=opener, max_bytes=4)


@pytest.mark.parametrize(
    ("page", "message"),
    [
        (b"<html><script>window._cf_chl_opt={cType: 'managed'}</script></html>", "Cloudflare challenge page"),
        (b'<script id="anubis_challenge" type="application/json">{}</script>', "Anubis challenge page"),
    ],
)
def test_download_rejects_a_challenge_page_served_with_200(page: bytes, message: str) -> None:
    with pytest.raises(fetch.FetchError, match=message):
        fetch.download(SOURCE.url, opener=opener_returning(FakeResponse(page, Content_Type="text/html")))


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = db.connect(tmp_path / "kb.db")
    sources.sync(conn, [SOURCE])
    yield conn
    conn.close()


def clock() -> Iterator[datetime]:
    start = datetime(2026, 9, 24, tzinfo=UTC)
    for minute in range(100):
        yield start + timedelta(minutes=minute)


def run(conn: sqlite3.Connection, raw: Path, ticks: Iterator[datetime], *bodies: bytes | Exception) -> list[str]:
    statuses = []
    for body in bodies:

        def get(url: str, body: bytes | Exception = body) -> fetch.Download:
            assert url == SOURCE.url
            if isinstance(body, Exception):
                raise body
            return fetch.Download(body, "application/pdf", None, None)

        [result] = fetch.fetch_all(conn, [SOURCE], raw, get=get, clock=lambda: next(ticks))
        statuses.append(result.status)
    return statuses


def versions(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    return conn.execute(
        "SELECT raw_path, fetched_at, last_checked_at, id FROM document_versions ORDER BY fetched_at"
    ).fetchall()


def test_versions_are_added_never_overwritten(conn: sqlite3.Connection, tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    ticks = clock()
    assert run(conn, raw, ticks, b"v1", b"v1", b"v2") == ["new", "unchanged", "changed"]

    (v1_path, v1_fetched, v1_checked, v1_id), (v2_path, v2_fetched, _, _) = versions(conn)
    assert (v1_fetched, v1_checked, v2_fetched) == (
        "2026-09-24T00:00:00Z",
        "2026-09-24T00:01:00Z",
        "2026-09-24T00:02:00Z",
    )
    assert v1_path.startswith("gb-act/") and v1_path.endswith(".pdf")
    assert v1_id == f"gb-act@{v1_path.split('/')[-1][:12]}"
    assert (raw / v1_path).read_bytes() == b"v1"
    assert (raw / v2_path).read_bytes() == b"v2"
    assert not list(raw.rglob(".part-*"))


def test_revert_makes_the_earlier_version_current(conn: sqlite3.Connection, tmp_path: Path) -> None:
    ticks = clock()
    assert run(conn, tmp_path / "raw", ticks, b"v1", b"v2", b"v1", b"v1") == ["new", "changed", "changed", "unchanged"]
    assert len(versions(conn)) == 2


def test_failure_leaves_versions_untouched(conn: sqlite3.Connection, tmp_path: Path) -> None:
    ticks = clock()
    run(conn, tmp_path / "raw", ticks, b"v1")
    before = versions(conn)
    assert run(conn, tmp_path / "raw", ticks, fetch.FetchError("HTTP 503")) == ["failed"]
    assert versions(conn) == before


def test_cli_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    entry = {k: v for k, v in SOURCE.__dict__.items() if v not in (None, "", ())} | {"tags": ["food"]}
    other = {**entry, "id": "gb-rts", "url": "https://example.org/rts"}
    registry = tmp_path / "sources.yaml"
    registry.write_text(yaml.safe_dump([entry, other]), encoding="utf-8")
    domain = tmp_path / "domain.yaml"
    domain.write_text(
        yaml.safe_dump(
            {
                "name": "Test",
                "instructions": "Test domain.",
                "doc_types": ["act"],
                "tags": ["food"],
                "modalities": [{"id": "must", "description": "required"}],
                "topics": [{"id": "registration", "label": "Registration", "description": "registrations"}],
                "publishers": [{"name": "Parliament", "domains": ["example.org"], "official": True}],
            }
        ),
        encoding="utf-8",
    )

    def fake_download(url: str) -> fetch.Download:
        if url.endswith("rts"):
            raise fetch.FetchError("HTTP 404")
        return fetch.Download(b"<html>", "text/html", None, None)

    monkeypatch.setattr(fetch, "download", fake_download)
    base = ["--db", str(tmp_path / "kb.db"), "--domain", str(domain), "fetch", "--file", str(registry)]
    inbox = tmp_path / "downloads"
    base += ["--raw", str(tmp_path / "raw"), "--downloads", str(inbox)]

    assert main(base) == 1
    out = capsys.readouterr().out
    assert "new       gb-act" in out
    assert "failed    gb-rts  HTTP 404" in out
    assert "1 new, 0 changed, 0 unchanged, 1 failed\n" in out
    assert f"  1  {inbox.resolve()}/gb-rts/  https://example.org/rts\n" in out  # the act downloaded: not listed
    script = inbox.resolve() / "ingest.sh"
    assert out.rstrip().endswith("The script removes itself once every step succeeded.")
    assert f"\n  {script}\n" in out
    assert script.stat().st_mode & 0o111  # executable
    lines = script.read_text().splitlines()
    assert lines[0] == "#!/bin/sh"
    assert f"#   1  {inbox.resolve()}/gb-rts/  https://example.org/rts" in lines  # the table, for later reference
    kb = f'--quiet kb -C {Path.cwd()} --db {(tmp_path / "kb.db").resolve()} --domain {domain.resolve()} "$@"; }}'
    assert lines[lines.index("status=0") - 1].endswith(kb)
    file, raw = f"--file {registry.resolve()}", f"--raw {(tmp_path / 'raw').resolve()}"
    assert lines[lines.index("status=0") + 1 :] == [  # every option this run changed is carried over
        f"kb fetch {file} {raw} --downloads {inbox.resolve()} --source gb-rts || status=1",
        f"kb parse {file} {raw} --source gb-rts || status=1",
        f"kb extract {file} --source gb-rts || status=1",
        "kb index || status=1",
        'if [ "$status" -eq 0 ]; then rm -f -- "$0"; fi',
        'exit "$status"',
    ]
    assert sorted(p.name for p in inbox.iterdir()) == ["gb-rts", "ingest.sh"]  # the folder to save into exists

    (inbox / "gb-rts" / "rts (1).pdf").write_bytes(b"%PDF-1.4 rts")
    assert main([*base, "--source", "gb-rts"]) == 0
    out = capsys.readouterr().out
    assert "new       gb-rts" in out
    assert "(saved by hand: rts (1).pdf)" in out
    assert not list((inbox / "gb-rts").iterdir())  # stored under raw/, so removed from the inbox
    assert [p.suffix for p in (tmp_path / "raw" / "gb-rts").iterdir()] == [".pdf"]

    assert main([*base, "--source", "gb-act"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("0 new, 0 changed, 1 unchanged, 0 failed")

    assert main([*base, "--source", "nope"]) == 1
    assert "unknown source ids: nope" in capsys.readouterr().err


def test_ingest_script_opens_links_in_batches(tmp_path: Path) -> None:
    failed = [dataclasses.replace(SOURCE, id=f"gb-{n}", url=f"https://example.org/{n}?a=1&b=2") for n in range(7)]
    inbox = tmp_path / "down loads"
    args = argparse.Namespace(
        db=db.DEFAULT_PATH, domain=Path("domain.yaml"), file=Path("sources.yaml"), raw=Path("raw"), downloads=inbox
    )
    lines = ingest_script(failed, args, []).splitlines()
    calls = [line.strip() for line in lines if line.startswith("  browse ")]
    assert [len(shlex.split(call)) - 1 for call in calls] == [10, 4]  # folder and link pairs: 5, then 2
    assert lines.index("if [ -t 0 ]; then") < lines.index("status=0")  # only on a terminal; before the ingest steps

    for source in failed:
        (inbox / source.id).mkdir(parents=True)
    (inbox / "gb-0" / "a.pdf").write_bytes(b"%PDF")  # saved already: not opened again
    (inbox / "gb-1" / ".DS_Store").write_bytes(b"x")  # dotfiles do not count, as in fetch.saved_by_hand
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "opened"
    (bin_dir / "xdg-open").write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> {shlex.quote(str(log))}\n')
    (bin_dir / "xdg-open").chmod(0o755)
    start = next(n for n, line in enumerate(lines) if line.startswith("open_link()"))
    functions = "\n".join(lines[start : lines.index("}") + 1])

    def browse(call: str, answer: str, path: str = f"{bin_dir}:/usr/bin:/bin") -> str:
        sh = ["/bin/sh", "-c", f"{functions}\n{call}"]
        run = subprocess.run(sh, input=answer, capture_output=True, text=True, env={"PATH": path}, check=True)  # noqa: S603
        return run.stdout

    out = browse(calls[0], "\n")
    assert log.read_text().splitlines() == [s.url for s in failed[1:5]]
    assert f"save into {inbox / 'gb-1'}/" in out
    assert out.endswith("then press Enter. ")

    for source in failed[5:]:
        (inbox / source.id / "a.html").write_text("<html>")
    assert browse(calls[1], "") == ""  # every folder holds a file: nothing opened, no wait

    (bin_dir / "xdg-open").rename(bin_dir / "open")  # macOS: no xdg-open, so open is used
    (bin_dir / "ls").symlink_to(str(shutil.which("ls")))  # PATH holds nothing else: no system xdg-open
    (inbox / "gb-5" / "a.html").unlink()
    log.unlink()
    assert browse(calls[1], "\n", str(bin_dir)).endswith("then press Enter. ")
    assert log.read_text().splitlines() == [failed[5].url]


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"a.pdf": b"%PDF", "b.pdf": b"%PDF"}, "2 files in .*; keep one of a.pdf, b.pdf"),
        ({"a.docx": b"PK"}, r"a\.docx is not a \.pdf, \.html, \.htm, \.xhtml file"),
        ({"a.pdf": b""}, "a.pdf is empty"),
        ({"a.html": b"<script>window._cf_chl_opt={}</script>"}, r"a.html is a challenge page \(Cloudflare\)"),
        ({"a.html": b'<script id="anubis_challenge">{}</script>'}, r"a.html is a challenge page \(Anubis\)"),
    ],
)
def test_unusable_file_saved_by_hand_fails_and_stays(
    conn: sqlite3.Connection, tmp_path: Path, files: dict[str, bytes], message: str
) -> None:
    folder = tmp_path / "downloads" / SOURCE.id
    folder.mkdir(parents=True)
    for name, body in files.items():
        (folder / name).write_bytes(body)

    def offline(url: str) -> fetch.Download:
        raise AssertionError(f"{url} must not be downloaded while a file is saved by hand")

    [result] = fetch.fetch_all(conn, [SOURCE], tmp_path / "raw", get=offline, inbox=tmp_path / "downloads")
    assert result.status == "failed"
    assert re.search(message, result.detail)
    assert sorted(p.name for p in folder.iterdir()) == sorted(files)
    assert versions(conn) == []


def html_download(preamble: str, body: str) -> fetch.Download:
    page = f"<html><body><p>{preamble}</p><h2>1 Scope</h2><p>{body}</p></body></html>"
    return fetch.Download(page.encode(), "text/html", None, None)


def test_markup_only_change_keeps_the_current_version(conn: sqlite3.Connection, tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    ticks = iter(clock())
    now = lambda: next(ticks).isoformat(timespec="seconds").replace("+00:00", "Z")  # noqa: E731
    first = fetch.store(
        conn, SOURCE, html_download("Printed on 24 September", "Operators must verify age."), raw, now()
    )
    assert first.status == "new"
    again = fetch.store(
        conn, SOURCE, html_download("Printed on 29 September", "Operators must verify age."), raw, now()
    )
    assert again.status == "unchanged"
    assert "markup-only change" in again.detail
    [(path, fetched, checked, _)] = versions(conn)
    assert (fetched, checked) == ("2026-09-24T00:00:00Z", "2026-09-24T00:01:00Z")
    assert [p.name for p in raw.rglob("*.html")] == [Path(path).name]  # the discarded download is not kept

    changed = fetch.store(
        conn, SOURCE, html_download("Printed on 30 September", "Operators must verify ID."), raw, now()
    )
    assert changed.status == "changed"
    assert len(versions(conn)) == 2


CHALLENGE = fetch.Download(b"<html><body><p>Verifying you are human</p></body></html>", "text/html", None, None)


BWB_ACT = (
    '<?xml version="1.0"?><toestand><wetgeving><meta-data>{meta}</meta-data><wettekst>'
    "<artikel><kop><label>Artikel</label><nr>1</nr></kop><al>{text}</al></artikel></wettekst></wetgeving></toestand>"
)


def xml_download(text: str, meta: str = "2026-09-24") -> fetch.Download:
    return fetch.Download(BWB_ACT.format(meta=meta, text=text).encode(), "application/xml", None, None)


@pytest.mark.parametrize(
    "good",
    [
        fetch.Download(b"%PDF-1.4", "application/pdf", None, None),
        html_download("Menu", "Labels must list allergens."),
        xml_download("Labels must list allergens."),
    ],
    ids=["html-for-a-pdf", "no-body-sections", "html-for-xml"],
)
def test_block_page_does_not_replace_the_current_version(
    conn: sqlite3.Connection, tmp_path: Path, good: fetch.Download
) -> None:
    raw = tmp_path / "raw"
    ticks = iter(clock())
    now = lambda: next(ticks).isoformat(timespec="seconds").replace("+00:00", "Z")  # noqa: E731
    assert fetch.store(conn, SOURCE, good, raw, now()).status == "new"
    before = versions(conn)
    refused = fetch.store(conn, SOURCE, CHALLENGE, raw, now())
    assert refused.status == "failed"
    assert "current version kept" in refused.detail
    assert versions(conn) == before
    assert len(list(raw.rglob("*.*"))) == 1  # the refused download is discarded


def test_html_may_replace_an_untyped_current_version(conn: sqlite3.Connection, tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    ticks = iter(clock())
    now = lambda: next(ticks).isoformat(timespec="seconds").replace("+00:00", "Z")  # noqa: E731
    untyped = fetch.Download(b"<body><p>Menu</p><h2>1 Scope</h2><p>Labels list allergens.</p></body>", None, None, None)
    assert fetch.store(conn, SOURCE, untyped, raw, now()).status == "new"
    assert fetch.store(conn, SOURCE, html_download("Menu", "Labels list nuts."), raw, now()).status == "changed"


def test_xml_versions_compare_on_their_sections(conn: sqlite3.Connection, tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    ticks = iter(clock())
    now = lambda: next(ticks).isoformat(timespec="seconds").replace("+00:00", "Z")  # noqa: E731
    assert fetch.store(conn, SOURCE, xml_download("Labels list nuts."), raw, now()).status == "new"
    again = fetch.store(conn, SOURCE, xml_download("Labels list nuts.", meta="2026-09-25"), raw, now())
    assert again.status == "unchanged"
    assert "markup-only change" in again.detail
    assert fetch.store(conn, SOURCE, xml_download("Labels list eggs."), raw, now()).status == "changed"
    assert sorted(Path(path).suffix for path, *_ in versions(conn)) == [".xml", ".xml"]


def test_download_sends_accept_only_to_hosts_that_need_it() -> None:
    seen: list[str | None] = []

    def opener(request: urllib.request.Request, timeout: float, context: object) -> FakeResponse:  # noqa: ARG001
        seen.append(request.get_header("Accept"))
        return FakeResponse(b"<x/>", url=request.full_url)

    fetch.download("https://www.boe.es/datosabiertos/api/legislacion-consolidada/id/X/texto", opener=opener)
    fetch.download(SOURCE.url, opener=opener)
    assert seen == ["application/xml", None]


NORMATTIVA = "https://www.normattiva.it/do/atto/caricaAKN?dataGU=20240403&codiceRedaz=24G00060"


def test_normattiva_primes_the_session_and_asks_for_today() -> None:
    seen: list[str] = []

    def opener(request: urllib.request.Request, timeout: float, context: object) -> FakeResponse:  # noqa: ARG001
        seen.append(request.full_url)
        kind = "text/xml" if "caricaAKN" in request.full_url else "text/html"
        return FakeResponse(b"<akomaNtoso/>", url=request.full_url, Content_Type=kind)

    got = fetch.normattiva(NORMATTIVA, opener, today=datetime(2026, 9, 25, tzinfo=UTC))
    assert got.body == b"<akomaNtoso/>"
    assert seen == [
        "https://www.normattiva.it/atto/caricaDettaglioAtto?atto.dataPubblicazioneGazzetta=2024-04-03"
        "&atto.codiceRedazionale=24G00060",
        f"{NORMATTIVA}&dataVigenza=20260925",
    ]


def test_normattiva_rejects_the_error_page_and_other_urls() -> None:
    def opener(request: urllib.request.Request, timeout: float, context: object) -> FakeResponse:  # noqa: ARG001
        return FakeResponse(b"<html>Errore</html>", url=request.full_url, Content_Type="text/html")

    with pytest.raises(fetch.FetchError, match="instead of Akoma Ntoso"):
        fetch.normattiva(NORMATTIVA, opener)
    with pytest.raises(fetch.FetchError, match="not a Normattiva AKN export URL"):
        fetch.normattiva(SOURCE.url, opener)


def test_bwb_manifest_resolves_to_latest_item() -> None:
    manifest = "https://repository.officiele-overheidspublicaties.nl/bwb/BWBR0044773/manifest.xml"
    latest = "2022-07-15_0/xml/BWBR0044773_2022-07-15_0.xml"
    bodies = {manifest: f'<work label="BWBR0044773" _latestItem="{latest}">'.encode()}

    def get(url: str) -> fetch.Download:
        return fetch.Download(bodies[url], "application/xml", None, None)

    assert (
        fetch.resolve(manifest, get) == f"https://repository.officiele-overheidspublicaties.nl/bwb/BWBR0044773/{latest}"
    )
    assert fetch.resolve("https://example.org/x", get) == "https://example.org/x"
    for bad in (b'<work _latestItem="../../evil">', b"<work/>"):
        bodies[manifest] = bad
        with pytest.raises(fetch.FetchError, match="_latestItem"):
            fetch.resolve(manifest, get)


def test_fetch_all_follows_pointers_and_sessions_by_default(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = "https://repository.officiele-overheidspublicaties.nl/bwb/BWBR0000001/manifest.xml"
    item = "https://repository.officiele-overheidspublicaties.nl/bwb/BWBR0000001/2026-01-01_0/xml/act.xml"
    seen: list[str] = []

    def download(url: str, opener: object = None) -> fetch.Download:  # noqa: ARG001
        seen.append(url)
        if url == manifest:
            return fetch.Download(b'<work _latestItem="2026-01-01_0/xml/act.xml">', "application/xml", None, None)
        return xml_download("Labels list nuts.")

    monkeypatch.setattr(fetch, "download", download)
    ticks = clock()
    sources_ = [dataclasses.replace(SOURCE, url=manifest), dataclasses.replace(SOURCE, url=NORMATTIVA)]
    results = fetch.fetch_all(conn, sources_, tmp_path / "raw", clock=lambda: next(ticks))
    assert [r.status for r in results] == ["new", "unchanged"]
    assert seen[:2] == [manifest, item]
    assert seen[3].endswith("&dataVigenza=20260924")  # the clock's date; seen[2] opens the session


def test_download_reports_the_url_it_ended_on() -> None:
    moved = FakeResponse(b"x", url="https://cdn.example.net/a", Content_Type="application/pdf")
    assert fetch.download(SOURCE.url, opener=opener_returning(moved)).final_url == "https://cdn.example.net/a"


REGISTERED = Domain(
    name="T",
    instructions="i",
    doc_types=("act",),
    tags=("food",),
    modalities=(),
    topics=(),
    publishers=(Publisher("Parliament", ("example.org",), True),),
)


@pytest.mark.parametrize(
    ("final", "domain", "status"),
    [
        ("https://example.org/act", REGISTERED, "new"),
        ("https://www.example.org/moved", REGISTERED, "new"),  # a subdomain of the publisher
        ("", REGISTERED, "new"),  # a download that reports no final URL has nothing to check
        ("https://evil.example.net/act", None, "new"),  # without a domain nothing is checked
        ("https://evil.example.net/act", REGISTERED, "failed"),
        ("https://example.org.evil.net/act", REGISTERED, "failed"),  # a look-alike host
    ],
)
def test_a_redirect_off_the_publishers_domains_fails_that_source(
    conn: sqlite3.Connection, tmp_path: Path, final: str, domain: Domain | None, status: str
) -> None:
    def get(_url: str) -> fetch.Download:
        return fetch.Download(b"v1", "application/pdf", None, None, final)

    [result] = fetch.fetch_all(conn, [SOURCE], tmp_path / "raw", get=get, domain=domain)
    assert result.status == status
    if status == "failed":
        assert result.detail.startswith("redirected off the publisher's domains: host '")
        assert versions(conn) == []  # nothing stored for it
