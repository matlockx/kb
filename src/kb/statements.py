"""Extract structured statements from chunks with Claude (through `pi -p`), keeping only verbatim quotes."""

import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from kb.domain import ConfigError, Domain, Topic, is_partial_date
from kb.parse import current_version
from kb.sources import Source

DEFAULT_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_PROMPT = Path("prompts/extract.md")
MAX_QUOTE_CHARS = 1_000  # the prompt asks for at most 600; long sentences may run over
CALL_TIMEOUT_S = 300
RETRY_DELAY_S = 10.0
PI_FLAGS = (
    "-p", "--no-session", "--no-tools", "--no-extensions", "--no-skills", "--no-prompt-templates",
    "--no-context-files", "--no-themes", "--no-approve", "--thinking", "off",
)  # fmt: skip

# Extensions loaded despite --no-extensions. pi-anthropic-auth shapes requests so an Anthropic subscription
# (OAuth) login accepts them; without it they are rejected as a third-party app ("draw from extra usage").
ANTHROPIC_AUTH = Path.home() / ".pi/agent/npm/node_modules/@gotgenes/pi-anthropic-auth"

Call = Callable[[str, str, str], str]  # (system prompt, message, model) -> raw model output


class ExtractionError(Exception):
    pass


@dataclass(frozen=True)
class Statement:
    verbatim_quote: str
    summary: str
    modality: str
    topics: tuple[str, ...]
    applies_to: tuple[str, ...]
    effective_from: str | None = None  # partial date the section states for the rule, when the prompt asks for it


@dataclass
class Report:
    source_id: str
    sections: int = 0
    cached: int = 0
    called: int = 0
    statements: int = 0
    rejected: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: int = 0  # sections excluded by the source's skip_sections


def sync_topics(conn: sqlite3.Connection, topics: Iterable[Topic]) -> None:
    with conn:
        conn.executemany(
            "INSERT INTO topics (id, label, description) VALUES (?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
            "label = excluded.label, description = excluded.description",
            [(t.id, t.label, t.description) for t in topics],
        )


def system_prompt(path: Path, domain: Domain) -> str:
    """The extraction prompt file followed by the domain's modalities and topics; raise ConfigError if unreadable."""
    try:
        base = path.read_text(encoding="utf-8").rstrip()
    except OSError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return (
        base
        + "\n\nModalities:\n"
        + "".join(f"- {m.id}: {m.description}\n" for m in domain.modalities)
        + "\nTopics:\n"
        + "".join(f"- {t.id}: {t.description}\n" for t in domain.topics)
    )


def prompt_version(system: str) -> str:
    return hashlib.sha256(system.encode()).hexdigest()[:12]


def message(source: Source, section_ref: str, heading_path: list[str], text: str) -> str:
    publisher = f"{source.publisher}, {source.scope}" if source.scope else source.publisher
    return (
        f"Document: {source.title} ({publisher})\n"
        f"Type: {source.doc_type}\n"
        f"Language: {source.language}\n"
        f"Tags: {', '.join(source.tags)}\n"
        + (f"{source.extract_note}\n" if source.extract_note else "")
        + f"Section: {section_ref}\n"
        f"Headings: {' > '.join(heading_path) or '-'}\n\n"
        f"<section>\n{text}\n</section>"
    )


def call_pi(system: str, text: str, model: str) -> str:
    """One stateless model call through the local pi login; no tools, extensions or context files."""
    prefix = shlex.split(os.environ.get("KB_PI_PREFIX", ""))
    extensions = [arg for path in pi_extensions() for arg in ("-e", path)]  # pi rejects "-e=path"
    command = [*prefix, "pi", *PI_FLAGS, *extensions, "--model", model, "--system-prompt", system, text]
    # DEV-NOTE: pi -p reads piped stdin as extra prompt text; without DEVNULL it waits forever under a non-TTY
    # parent (agents, cron, CI) until CALL_TIMEOUT_S.
    result = subprocess.run(  # noqa: S603
        command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=CALL_TIMEOUT_S, check=False
    )
    if result.returncode != 0:
        raise ExtractionError(f"pi exited {result.returncode}: {result.stderr.strip()[-300:]}")
    return result.stdout


def pi_extensions() -> list[str]:
    """KB_PI_EXTENSIONS (paths joined with os.pathsep; empty for none), else pi-anthropic-auth if installed."""
    configured = os.environ.get("KB_PI_EXTENSIONS")
    if configured is not None:
        return [p for p in configured.split(os.pathsep) if p]
    return [str(ANTHROPIC_AUTH)] if ANTHROPIC_AUTH.is_dir() else []


def parse_output(raw: str) -> list[object]:
    text = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"output is not JSON: {text[:120]!r}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("statements"), list):
        raise ExtractionError("output is not an object with a statements list")
    return data["statements"]


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def comparable(text: str) -> str:
    """Text without whitespace and without hyphens between letters, for the verbatim check.

    PDF extraction breaks words at line ends ("virtu-\nelle", soft hyphens) and, with some kerning, inside words
    ("me r"); the model copies either form or the whole word. Only hyphens between letters go: "2-3" must not
    match "23".
    """
    text = normalise(text.replace("\xad", ""))  # soft hyphens are invisible line-break hints
    joined = re.sub(r"(?<=[^\W\d_]) ?- ?(?=[^\W\d_])", "", text)
    return re.sub(r"\s+", "", joined)


def validate(
    record: object, chunk_text: str, source: Source, topic_ids: frozenset[str], modalities: frozenset[str]
) -> Statement:
    """Check one model record; raise ExtractionError naming the first problem."""
    if not isinstance(record, dict):
        raise ExtractionError("record is not an object")
    quote = record.get("verbatim_quote")
    if not isinstance(quote, str) or not quote.strip():
        raise ExtractionError("verbatim_quote missing")
    quote = normalise(quote)
    if len(quote) > MAX_QUOTE_CHARS:
        raise ExtractionError(f"verbatim_quote longer than {MAX_QUOTE_CHARS} characters")
    if comparable(quote) not in comparable(chunk_text):
        raise ExtractionError(f"verbatim_quote not found in the section: {quote[:80]!r}")
    summary = record.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ExtractionError("summary missing")
    modality = record.get("modality")
    if modality not in modalities:
        raise ExtractionError(f"modality {modality!r} not in {sorted(modalities)}")
    topics = record.get("topics")
    if not isinstance(topics, list) or not 1 <= len(topics) <= 3 or not set(topics) <= topic_ids:
        raise ExtractionError(f"topics {topics!r} must be one to three known topic ids")
    applies = record.get("applies_to")
    if not isinstance(applies, list) or not applies or not set(applies) <= set(source.tags):
        raise ExtractionError(f"applies_to {applies!r} must be a non-empty subset of {list(source.tags)}")
    effective = record.get("effective_from")
    if effective is not None and not is_partial_date(effective):
        raise ExtractionError(f"effective_from {effective!r} must be YYYY, YYYY-MM, YYYY-MM-DD or null")
    return Statement(
        quote,
        normalise(summary),
        str(modality),
        tuple(dict.fromkeys(topics)),
        tuple(dict.fromkeys(applies)),
        effective if isinstance(effective, str) else None,
    )


def ref_key(ref: str) -> tuple[str, ...]:
    """Numbers and single letters of a section ref, so "Chapter 14 § 6 a" matches "14 kap. 6 a §"."""
    return tuple(re.findall(r"\d+|\b[a-z]\b", ref))


def original_chunks(conn: sqlite3.Connection, source: Source) -> dict[tuple[str, ...], str]:
    """For a translation: section ref key -> chunk id in the original's current version, where the key is unique."""
    if source.translation_of is None or (version := current_version(conn, source.translation_of)) is None:
        return {}
    by_key: dict[tuple[str, ...], list[str]] = {}
    for chunk_id, ref in conn.execute("SELECT id, section_ref FROM chunks WHERE version_id = ?", (version[0],)):
        if key := ref_key(ref):
            by_key.setdefault(key, []).append(chunk_id)
    return {key: ids[0] for key, ids in by_key.items() if len(ids) == 1}


def extract_all(
    conn: sqlite3.Connection,
    sources: Iterable[Source],
    domain: Domain,
    system: str,
    model: str = DEFAULT_MODEL,
    workers: int = 4,
    call: Call | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    progress: Callable[[str], None] = lambda _: None,
    matching: str | None = None,
) -> list[Report]:
    """Extract statements for the current version of each source with the given system prompt; cached outputs
    are reused.

    matching, a regex, limits extraction to sections whose text it matches (e.g. to tag a new topic).
    """
    job = _Job(
        system=system,
        prompt_version=prompt_version(system),
        topic_ids=frozenset(t.id for t in domain.topics),
        modalities=frozenset(m.id for m in domain.modalities),
        model=model,
        call=call or call_pi,
        clock=clock,
        progress=progress,
        text_re=re.compile(matching) if matching else None,
    )
    reports: list[Report] = []
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        for source in sources:
            reports.append(_extract_source(conn, source, job, pool))
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)  # Ctrl-C must not wait for every queued model call
        raise
    pool.shutdown()
    return reports


@dataclass(frozen=True)
class _Job:
    system: str
    prompt_version: str
    topic_ids: frozenset[str]
    modalities: frozenset[str]
    model: str
    call: Call
    clock: Callable[[], datetime]
    progress: Callable[[str], None]
    text_re: re.Pattern[str] | None


def _extract_source(conn: sqlite3.Connection, source: Source, job: _Job, pool: ThreadPoolExecutor) -> Report:
    report = Report(source.id)
    version = current_version(conn, source.id)
    if version is None:
        report.failed.append("not fetched yet")
        return report
    chunks = conn.execute(
        "SELECT id, section_ref, heading_path, text FROM chunks WHERE version_id = ? "
        "AND section_ref NOT LIKE '(preamble)%' ORDER BY ord",
        (version[0],),
    ).fetchall()
    if not chunks:
        report.failed.append("not parsed yet")
        return report
    if source.skip_sections:
        skip_re = re.compile(source.skip_sections)
        skipped = {c[0] for c in chunks if skip_re.match(c[1])}
        _drop(conn, sorted(skipped))  # earlier runs may have extracted them
        chunks = [c for c in chunks if c[0] not in skipped]
        report.skipped = len(skipped)
    if job.text_re is not None:
        chunks = [c for c in chunks if job.text_re.search(c[3])]
    originals = original_chunks(conn, source)
    pending: list[tuple[str, str, str, str, Future[str] | str]] = []
    for chunk_id, ref, heading_path, text in chunks:
        prompt = message(source, ref, json.loads(heading_path), text)
        key = hashlib.sha256(prompt.encode()).hexdigest()
        cached = conn.execute(
            "SELECT output FROM extraction_cache WHERE message_sha256 = ? AND model = ? AND prompt_version = ?",
            (key, job.model, job.prompt_version),
        ).fetchone()
        if cached:
            report.cached += 1
            pending.append((chunk_id, ref, text, key, cached[0]))
        else:
            future = pool.submit(_call_with_retry, job.call, job.system, prompt, job.model)
            pending.append((chunk_id, ref, text, key, future))
    report.sections = len(pending)

    for done, (chunk_id, ref, text, key, outcome) in enumerate(pending, start=1):
        job.progress(f"{source.id} {done}/{len(pending)} {ref}")
        try:
            if isinstance(outcome, Future):
                raw = outcome.result()
                records = parse_output(raw)
                report.called += 1
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO extraction_cache "
                        "(message_sha256, model, prompt_version, output, created_at) VALUES (?, ?, ?, ?, ?)",
                        (key, job.model, job.prompt_version, raw, _stamp(job.clock)),
                    )
            else:
                records = parse_output(outcome)
        except (ExtractionError, subprocess.TimeoutExpired, OSError) as exc:
            report.failed.append(f"{ref}: {exc}")
            continue
        kept = []
        for record in records:
            try:
                kept.append(validate(record, text, source, job.topic_ids, job.modalities))
            except ExtractionError as exc:
                report.rejected.append(f"{ref}: {exc}")
        _store(conn, chunk_id, originals.get(ref_key(ref)), kept, job.model, job.prompt_version, _stamp(job.clock))
        report.statements += len(kept)
    return report


def _call_with_retry(call: Call, system: str, prompt: str, model: str) -> str:
    """Call the model; one retry after RETRY_DELAY_S on a failed call or unparsable output."""
    for attempt in (1, 2):
        try:
            raw = call(system, prompt, model)
            parse_output(raw)
        except (ExtractionError, subprocess.TimeoutExpired):
            if attempt == 2:
                raise
            time.sleep(RETRY_DELAY_S)
        else:
            return raw
    raise AssertionError("unreachable")  # pragma: no cover


def _drop(conn: sqlite3.Connection, chunk_ids: list[str]) -> None:
    """Delete the statements of chunks that are no longer extracted."""
    with conn:
        for chunk_id in chunk_ids:
            conn.execute(
                "DELETE FROM statement_topics WHERE statement_id IN (SELECT id FROM statements WHERE chunk_id = ?)",
                (chunk_id,),
            )
            conn.execute("DELETE FROM statements WHERE chunk_id = ?", (chunk_id,))


def _store(
    conn: sqlite3.Connection,
    chunk_id: str,
    original: str | None,
    kept: list[Statement],
    model: str,
    prompt_version: str,
    now: str,
) -> None:
    with conn:
        conn.execute(
            "DELETE FROM statement_topics WHERE statement_id IN (SELECT id FROM statements WHERE chunk_id = ?)",
            (chunk_id,),
        )
        conn.execute("DELETE FROM statements WHERE chunk_id = ?", (chunk_id,))
        for number, s in enumerate(kept, start=1):
            statement_id = f"{chunk_id}/{number}"
            conn.execute(
                "INSERT INTO statements (id, chunk_id, verbatim_quote, summary, modality, applies_to, model, "
                "prompt_version, created_at, effective_from, original_chunk_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (statement_id, chunk_id, s.verbatim_quote, s.summary, s.modality, json.dumps(list(s.applies_to)),
                 model, prompt_version, now, s.effective_from, original),
            )  # fmt: skip
            conn.executemany(
                "INSERT INTO statement_topics (statement_id, topic_id) VALUES (?, ?)",
                [(statement_id, topic) for topic in s.topics],
            )


def _stamp(clock: Callable[[], datetime]) -> str:
    return clock().isoformat(timespec="seconds").replace("+00:00", "Z")
