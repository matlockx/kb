-- Knowledge base source of truth. Portable by rule so the file reads in DuckDB and
-- copies to Postgres unchanged: plain tables only, TEXT ids, ISO-8601 UTC TEXT
-- timestamps, JSON arrays as TEXT, vectors as little-endian float32 BLOB.
-- Search indexes (FTS5 tables, vectors) are derived, rebuilt by `kb index`,
-- and never the only copy of anything.

-- One row per entry in sources.yaml.
CREATE TABLE IF NOT EXISTS documents (
  id        TEXT PRIMARY KEY,
  publisher TEXT NOT NULL,
  title     TEXT NOT NULL,
  url       TEXT NOT NULL,
  language  TEXT NOT NULL,                           -- ISO 639-1
  doc_type  TEXT NOT NULL,                           -- one of domain.yaml doc_types
  tags      TEXT NOT NULL                            -- JSON array of domain.yaml tags
);

-- One row per distinct downloaded content; a changed hash is a new version.
-- The current version of a document is the one with the latest last_checked_at,
-- so content that reverts to an earlier version makes that version current again.
CREATE TABLE IF NOT EXISTS document_versions (
  id              TEXT PRIMARY KEY,
  document_id     TEXT NOT NULL REFERENCES documents (id),
  sha256          TEXT NOT NULL,
  raw_path        TEXT NOT NULL,                     -- relative to the raw directory: <document>/<sha256>.<ext>
  content_type    TEXT,
  etag            TEXT,
  last_modified   TEXT,
  fetched_at      TEXT NOT NULL,
  last_checked_at TEXT NOT NULL,
  UNIQUE (document_id, sha256)
);

-- A section of a version, split on the document's own numbering.
CREATE TABLE IF NOT EXISTS chunks (
  id           TEXT PRIMARY KEY,
  version_id   TEXT NOT NULL REFERENCES document_versions (id),
  ord          INTEGER NOT NULL,                     -- position within the version
  section_ref  TEXT NOT NULL,                        -- e.g. "3.4.3", "Chapter 2 § 5", "p. 12"
  heading_path TEXT NOT NULL,                        -- JSON array of the headings above the section
  text         TEXT NOT NULL,
  sha256       TEXT NOT NULL,                        -- of text; keys the extraction cache
  UNIQUE (version_id, ord)
);

-- Statements extracted from a chunk by the model; each quote is an exact substring of the chunk text.
CREATE TABLE IF NOT EXISTS statements (
  id             TEXT PRIMARY KEY,
  chunk_id       TEXT NOT NULL REFERENCES chunks (id),
  verbatim_quote TEXT NOT NULL,                      -- exact substring of the chunk text
  summary        TEXT NOT NULL,                      -- one self-contained sentence, in the language the prompt asks for
  modality       TEXT NOT NULL,                      -- one of domain.yaml modalities
  applies_to     TEXT NOT NULL,                      -- JSON array, a subset of the document's tags
  model          TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS statements_chunk ON statements (chunk_id);

-- Raw model output per exact prompt, so re-runs and re-parses only pay for new or changed sections.
-- statements are rebuilt from it; deleting this table only costs model calls.
CREATE TABLE IF NOT EXISTS extraction_cache (
  message_sha256 TEXT NOT NULL,                      -- of the user message: document context plus section text
  model          TEXT NOT NULL,
  prompt_version TEXT NOT NULL,                      -- sha256 prefix of the system prompt, modalities and topics included
  output         TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  PRIMARY KEY (message_sha256, model, prompt_version)
);

CREATE TABLE IF NOT EXISTS topics (
  id          TEXT PRIMARY KEY,
  label       TEXT NOT NULL,
  description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS statement_topics (
  statement_id TEXT NOT NULL REFERENCES statements (id),
  topic_id     TEXT NOT NULL REFERENCES topics (id),
  PRIMARY KEY (statement_id, topic_id)
);
CREATE INDEX IF NOT EXISTS statement_topics_topic ON statement_topics (topic_id);

-- Embedding vectors for semantic search, rebuilt incrementally by `kb index`. Derived: text_sha256 marks
-- which text a vector was made from, so changed chunks and statements are embedded again.
CREATE TABLE IF NOT EXISTS vectors (
  kind        TEXT NOT NULL CHECK (kind IN ('chunk', 'statement')),
  item_id     TEXT NOT NULL,                         -- chunks.id or statements.id
  model       TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  vector      BLOB NOT NULL,                         -- little-endian float32, L2-normalised
  PRIMARY KEY (kind, item_id, model)
);

-- Filled only in a published snapshot (`kb publish`), so the file alone is a whole knowledge base: the
-- configuration files it was built from, verbatim, and facts about the snapshot. Where the files exist on disk
-- they win; `kb unpack` writes these copies out.
CREATE TABLE IF NOT EXISTS kb_files (
  path    TEXT PRIMARY KEY,                          -- relative: domain.yaml, sources.yaml, prompts/..., eval/...
  content TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_meta (
  key   TEXT PRIMARY KEY,                            -- schema, name, version, published_at
  value TEXT NOT NULL
);
