import email.message
import io
import re
import sqlite3
import urllib.error
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from kb import db, fetch, sources
from kb.cli import main
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
    assert got == fetch.Download(b"%PDF", "application/pdf", '"v1"', "x")


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


def test_download_rejects_a_challenge_page_served_with_200() -> None:
    page = b"<html><script>window._cf_chl_opt={cType: 'managed'}</script></html>"
    with pytest.raises(fetch.FetchError, match="Cloudflare challenge page"):
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


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"a.pdf": b"%PDF", "b.pdf": b"%PDF"}, "2 files in .*; keep one of a.pdf, b.pdf"),
        ({"a.docx": b"PK"}, r"a\.docx is not a \.pdf, \.html, \.htm, \.xhtml file"),
        ({"a.pdf": b""}, "a.pdf is empty"),
        ({"a.html": b"<script>window._cf_chl_opt={}</script>"}, "a.html is a Cloudflare challenge page"),
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


@pytest.mark.parametrize(
    "good",
    [fetch.Download(b"%PDF-1.4", "application/pdf", None, None), html_download("Menu", "Labels must list allergens.")],
    ids=["html-for-a-pdf", "no-body-sections"],
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
