"""Download sources and record each distinct content as a document version."""

import hashlib
import http.client
import os
import sqlite3
import ssl
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import truststore

from kb.extract import HTML_TYPES
from kb.parse import sections_of
from kb.sources import Source

# Some bot filters (cdc.gov, mayoclinic.org, mdpi.com) answer 403 to a request without Accept-Language,
# whatever its User-Agent; "*" leaves the server's language choice as it was without the header.
HEADERS = {"User-Agent": "kb/0.1", "Accept-Language": "*"}
# Verify against the OS trust store, as curl and browsers do; the OpenSSL bundle
# Python ships with can lack roots that some publishers use (e.g. HARICA, Sectigo R46).
TLS = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
TIMEOUT_S = 60
MAX_BYTES = 50 * 1024 * 1024
EXTENSIONS = {"application/pdf": "pdf", "text/html": "html", "application/xhtml+xml": "html"}

Status = Literal["new", "changed", "unchanged", "failed"]


class FetchError(Exception):
    pass


@dataclass(frozen=True)
class Download:
    body: bytes
    content_type: str | None
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class Result:
    source_id: str
    status: Status
    detail: str


Opener = Callable[..., Any]  # urllib.request.urlopen or a test double


def download(url: str, opener: Opener = urllib.request.urlopen, max_bytes: int = MAX_BYTES) -> Download:
    """GET url; raise FetchError on HTTP or network failure, a non-https redirect, a bot challenge, or an empty,
    truncated or oversized body."""
    request = urllib.request.Request(url, headers=HEADERS)  # noqa: S310 - registry enforces https
    try:
        with opener(request, timeout=TIMEOUT_S, context=TLS) as response:
            final = response.geturl()
            if not final.startswith("https://"):
                raise FetchError(f"redirected to non-https URL {final}")
            body = response.read(max_bytes + 1)
            headers = response.headers
    except urllib.error.HTTPError as exc:
        challenge = " (Cloudflare challenge)" if exc.headers.get("cf-mitigated") == "challenge" else ""
        raise FetchError(f"HTTP {exc.code}{challenge}") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise FetchError(str(getattr(exc, "reason", exc))) from exc
    if len(body) > max_bytes:
        raise FetchError(f"body exceeds {max_bytes} bytes")
    if not body:
        raise FetchError("empty body")
    # read(amt) returns a short body without raising when the connection drops.
    length = headers.get("Content-Length")
    if length and length.isdigit() and int(length) != len(body):
        raise FetchError(f"truncated: got {len(body)} of {length} bytes")
    content_type = headers.get("Content-Type")
    if headers.get("cf-mitigated") == "challenge" or b"_cf_chl_opt" in body:
        raise FetchError("Cloudflare challenge page")
    return Download(
        body=body,
        content_type=content_type.split(";")[0].strip().lower() if content_type else None,
        etag=headers.get("ETag"),
        last_modified=headers.get("Last-Modified"),
    )


def store(conn: sqlite3.Connection, source: Source, got: Download, raw_dir: Path, now: str) -> Result:
    """Write the body under raw_dir and record it; the current version is the one checked most recently.

    A download whose body sections (preamble excluded) match the current version's is a markup-only change:
    the current version is marked checked and the download is discarded. A download that looks like a block
    or challenge page next to the current version (HTML where the current version is a document, or no body
    sections where the current version has some) is refused: it fails and is discarded.
    """
    sha = hashlib.sha256(got.body).hexdigest()
    rel = Path(source.id) / f"{sha}.{EXTENSIONS.get(got.content_type or '', 'bin')}"
    path = raw_dir / rel
    wrote = not path.exists()
    if wrote:
        _write_atomic(path, got.body)

    current = conn.execute(
        "SELECT sha256, raw_path, content_type FROM document_versions WHERE document_id = ? "
        "ORDER BY last_checked_at DESC, fetched_at DESC LIMIT 1",
        (source.id,),
    ).fetchone()
    if current is not None and current[0] != sha:
        before = _body(source, raw_dir / current[1], current[2])
        after = _body(source, path, got.content_type)
        if before and before == after:
            with conn:
                conn.execute(
                    "UPDATE document_versions SET last_checked_at = ? WHERE document_id = ? AND sha256 = ?",
                    (now, source.id, current[0]),
                )
            if wrote:
                path.unlink()
            return Result(source.id, "unchanged", f"{current[0][:12]} (markup-only change {sha[:12]} ignored)")
        problem = None
        if got.content_type in HTML_TYPES and current[2] is not None and current[2] not in HTML_TYPES:
            problem = f"got {got.content_type} where {current[0][:12]} is {current[2]}"
        elif before and after == []:
            problem = f"{sha[:12]} has no body sections where {current[0][:12]} has"
        if problem:
            if wrote:
                path.unlink()
            return Result(source.id, "failed", f"{problem}; a block page? current version kept")
    with conn:
        known = conn.execute(
            "UPDATE document_versions SET last_checked_at = ? WHERE document_id = ? AND sha256 = ?",
            (now, source.id, sha),
        ).rowcount
        if not known:
            conn.execute(
                """INSERT INTO document_versions
                     (id, document_id, sha256, raw_path, content_type, etag, last_modified, fetched_at, last_checked_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"{source.id}@{sha[:12]}",
                    source.id,
                    sha,
                    rel.as_posix(),
                    got.content_type,
                    got.etag,
                    got.last_modified,
                    now,
                    now,
                ),
            )
    if current is None:
        return Result(source.id, "new", sha[:12])
    if current[0] == sha:
        return Result(source.id, "unchanged", sha[:12])
    return Result(source.id, "changed", f"{current[0][:12]} -> {sha[:12]}")


def _body(source: Source, path: Path, content_type: str | None) -> list[tuple[str, str]] | None:
    """(ref, text) of the file's body sections, the preamble (navigation, print dates) left out; None when it
    does not parse (parse reports the error)."""
    try:
        return [(s.ref, s.text) for s in sections_of(path, content_type, source) if not s.ref.startswith("(preamble)")]
    except Exception:
        return None


def fetch_all(
    conn: sqlite3.Connection,
    sources: Iterable[Source],
    raw_dir: Path,
    get: Callable[[str], Download] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[Result]:
    """Fetch every source; a failure is reported and leaves that source's versions untouched."""
    get = get or download  # resolved per call, so a patched module-level download is honoured
    results = []
    for source in sources:
        try:
            got = get(source.url)
        except FetchError as exc:
            results.append(Result(source.id, "failed", str(exc)))
            continue
        now = clock().isoformat(timespec="seconds").replace("+00:00", "Z")
        results.append(store(conn, source, got, raw_dir, now))
    return results


def _write_atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".part-")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(body)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
