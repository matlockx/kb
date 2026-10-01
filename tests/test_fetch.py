import email.message
import io
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
    tags=("casino",),
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
        assert request.get_header("User-agent") == fetch.USER_AGENT  # type: ignore[attr-defined]
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


@pytest.mark.parametrize(
    ("opener", "message"),
    [
        (opener_returning(FakeResponse(b"x", url="http://example.org/act")), "non-https"),
        (opener_returning(FakeResponse(b"")), "empty body"),
        (opener_returning(FakeResponse(b"12345")), "exceeds 4 bytes"),
        (opener_returning(FakeResponse(b"123", Content_Length="4")), "truncated: got 3 of 4 bytes"),
        (opener_raising(urllib.error.HTTPError(SOURCE.url, 503, "down", email.message.Message(), None)), "HTTP 503"),
        (opener_raising(urllib.error.URLError("no route")), "no route"),
        (opener_raising(TimeoutError("timed out")), "timed out"),
    ],
)
def test_download_failures(opener: fetch.Opener, message: str) -> None:
    with pytest.raises(fetch.FetchError, match=message):
        fetch.download(SOURCE.url, opener=opener, max_bytes=4)


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
    entry = {k: v for k, v in SOURCE.__dict__.items() if v not in (None, "")} | {"tags": ["casino"]}
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
                "tags": ["casino"],
                "modalities": [{"id": "must", "description": "required"}],
                "topics": [{"id": "licensing", "label": "Licensing", "description": "licences"}],
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
    base += ["--raw", str(tmp_path / "raw")]

    assert main(base) == 1
    out = capsys.readouterr().out
    assert "new       gb-act" in out
    assert "failed    gb-rts  HTTP 404" in out
    assert out.rstrip().endswith("1 new, 0 changed, 0 unchanged, 1 failed")

    assert main([*base, "--source", "gb-act"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("0 new, 0 changed, 1 unchanged, 0 failed")

    assert main([*base, "--source", "nope"]) == 1
    assert "unknown source ids: nope" in capsys.readouterr().err


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
