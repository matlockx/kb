# Agent Instructions

Read `README.md` first: it describes the pipeline, the files a domain owns and
every command.

## Layout

- Generic engine: `src/kb/`. Keep domain vocabulary out of it; a new value
  set belongs in a knowledge base's `domain.yaml`, not in a Python constant.
- A knowledge base is a directory with `domain.yaml`, `sources.yaml`,
  `prompts/extract.md` and `eval/golden.yaml`; `kb -C DIR` targets one.
  `examples/web-principles/` is the worked example the tests validate.
  `./setup NAME` (`kb setup`, `src/kb/setup.py`) creates one from
  `src/kb/template/`, or builds and registers an existing one.
- Derived data, never committed: `data/` (SQLite database), `raw/` (downloads).

## Rules

- A statement is stored only when its quote is an exact substring of its
  section (`statements.validate`). Never relax that check to keep more records.
- Plain tables in `src/kb/schema.sql` are the source of truth; FTS5 tables and
  `vectors` are derived and rebuilt by `kb index`. Keep the schema portable
  (TEXT ids, ISO-8601 TEXT timestamps, JSON arrays as TEXT).
- Downloads are versioned by content hash and never overwritten.
- Changing `prompts/extract.md`, modalities or topics re-extracts every
  section on the next `kb extract`; say so before doing it on a large corpus.

## Checks

```sh
uv run ruff check . && uv run ruff format --check .
uv run pytest
uv run kb -C examples/web-principles sources --check
uv run kb -C examples/web-principles eval   # after index; exits 1 below a 90% hit rate
```
