# kb

A cited, versioned knowledge base engine that coding agents can search, quote
and cite instead of relying on their training data. The engine is
domain-neutral: one knowledge base is a directory of four files you edit, and
one checkout of this repository serves any number of them side by side.

Pipeline: hand-curated sources, versioned downloads, sections split on the
document's own numbering, model-extracted statements that must quote the
source verbatim, hybrid search, an MCP server and a golden-set evaluation.

`examples/web-principles/` is a small worked knowledge base (the W3C Ethical
Web Principles) that runs the whole pipeline out of the box.

## Knowledge base directories

A knowledge base is a directory holding:

| Path | Content |
|---|---|
| `domain.yaml` | vocabulary: name, instructions, doc types, tags, modalities, topics |
| `sources.yaml` | the documents to ingest |
| `prompts/extract.md` | what the model extracts from each section |
| `eval/golden.yaml` | test questions with their expected sections |
| `data/kb.db`, `raw/` | built by the pipeline; keep them out of version control |

Every `kb` command reads and writes relative to the current directory, or to
the directory given with `-C DIR`. Directories never share state, so several
knowledge bases can be built and served at the same time; only the embedding
model cache (`~/.cache/huggingface`) is shared, read-only.

Run the engine from any directory without installing it (fish):

```sh
alias setup ~/github/reg/setup && funcsave setup
alias kb 'uv run --project ~/github/reg --quiet kb' && funcsave kb
```

## Starting a new knowledge base

```sh
setup running
```

The first run asks what the knowledge base is about and whether to make it a
git repository, then creates `~/kbs/running` (`--dir DIR` for another place)
with template files and a `.gitignore` for `data/` and `raw/`.

Fill in the four files; an agent can draft them from your sources:

- `domain.yaml`:
  - `name` and `instructions`: what the knowledge base holds; shown to the
    agent as the MCP server instructions. Pre-filled from your answer.
  - `doc_types`: the kinds of document you ingest, most authoritative first
    (for running plans, for example `[position_stand, review, study, book,
    coaching_guide]`).
  - `tags`: one facet to filter on (for example `[beginner, intermediate,
    advanced, 5k, 10k, half_marathon, marathon, ultra]`). Each source lists the
    tags it covers; each statement the subset it applies to.
  - `modalities`: what kind of statement the model records, strongest first,
    each with the description the model sees.
  - `topics`: a fixed taxonomy; each statement gets one to three ids.
- `prompts/extract.md`: who the statements are for and what counts as one.
  Keep the JSON shape and the verbatim-quote rules; the modality and topic
  lists are appended automatically.
- `sources.yaml`: your documents (https URLs to HTML or PDF).
- `eval/golden.yaml`: questions whose expected sections you chose by reading
  the sources.

Then run `setup running` again. With a `domain.yaml` present it validates the
files (and stops with the problems if any), runs fetch, parse, extract and
index, asks once whether to register `running` as an MCP server in
`~/.omp/agent/mcp.json`, and runs the golden set once it has questions. Run it
again whenever you change the files: unchanged downloads, sections and model
outputs are reused, so only new or edited sources cost time and model calls.
Without a terminal (closed input) it never registers or creates a repository.

`kb -C ~/kbs/running parse --sample 3` shows whether a source splits well; add
`section_pattern`, `body_start` and the other parse settings until every
section has a citable ref.

## Using it from omp, Claude Code and other MCP clients

`kb serve` is a stdio MCP server; register one server per knowledge base,
each under its own name. Clients put the server name into the tool names
(`mcp__running_kb_search` in omp, `mcp__running__kb_search` in Claude Code),
so the tools of several knowledge bases never collide, and the server sends
the `name` and `instructions` from `domain.yaml` as its instructions.

Give each server an absolute `-C` path: the client's working directory is the
agent session's, not the knowledge base's. The first search loads the
embedding model, which can take longer than omp's 30-second default request
timeout, hence `timeout`.

**omp:** `setup NAME` writes the entry to `~/.omp/agent/mcp.json`
(`--omp-config` for a profile or a project's `.omp/mcp.json`). By hand it
looks like this:

```json
{
  "mcpServers": {
    "running": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "/Users/me/github/reg", "--quiet", "kb", "-C", "/Users/me/kbs/running", "serve"],
      "timeout": 120000
    },
    "web": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "/Users/me/github/reg", "--quiet", "kb", "-C", "/Users/me/github/reg/examples/web-principles", "serve"],
      "timeout": 120000
    }
  }
}
```

`/mcp` in an omp session lists the servers and their state.

**Claude Code:**

```sh
claude mcp add running -s user -- uv run --project ~/github/reg --quiet kb -C ~/kbs/running serve
```

omp also imports Claude Code's user-level servers, so register each server in
one place only: the same name in both makes one shadow the other.

## How it works

1. `sources.yaml` lists every document by hand: publisher, title, URL,
   language, `doc_type` and tags. `domain.yaml` defines the allowed values.
2. `kb fetch` downloads each source and stores each distinct content as a new
   version; `kb parse` splits the current version into sections on the
   document's own numbering; `kb extract` sends each section to Claude (through
   the local `pi` login) and keeps a statement only if its quote is an exact
   substring of the section.
3. Everything lands in one SQLite file, `data/kb.db`. Plain tables are the
   source of truth (`src/kb/schema.sql`); the full-text and vector indexes are
   derived and rebuildable, so the file also reads in DuckDB
   (`ATTACH 'data/kb.db' (TYPE sqlite)`) and copies to Postgres.
4. An MCP server exposes read-only tools (`kb_search`, `kb_get`, `kb_topic`,
   `kb_sources`).
5. A golden set of questions (`eval/golden.yaml`) measures whether search finds
   the right section.

## Usage

Inside a knowledge base directory, with the `kb` alias above:

```sh
kb sources --check   # validate domain.yaml and sources.yaml
kb sources           # sync both into data/kb.db
kb fetch             # download every source into raw/ (files in downloads/ first)
kb fetch --source ID # one source (repeatable)
kb parse             # split current versions into section chunks
kb parse --source ID --sample 3   # spot-check three random chunks
kb links ID          # links in a source's current version, to pick new sources
kb extract --source ID            # statements via Claude (pi -p)
kb extract --matching '(?i)taper' # only sections whose text matches
kb index             # full-text index and embeddings (downloads bge-m3 once)
kb eval              # golden-set hit rate
kb serve             # MCP server on stdio
```

`-C DIR` runs any command against another knowledge base directory; `--db` and
`--domain` override single files. Engine tests run from the repository:
`uv run pytest`.

## Fetching and versions

`kb fetch` stores each download as `raw/<source>/<sha256>.<ext>` and records it
in `document_versions`. New content becomes a new version; old versions and
their files are never overwritten. Identical content only updates
`last_checked_at`, and the version checked most recently is the current one. A
download whose body sections (preamble excluded) parse identically to the
current version's is a markup-only change (a print date, a breadcrumb): it is
reported as unchanged and discarded. A failed download leaves the source's
versions untouched and makes the command exit 1. TLS is verified against the
operating system's trust store. Only `https` URLs are accepted. Requests send
`Accept-Language: *`, without which several bot filters answer 403.

A block or challenge page must not hide a good copy, so these fail too and the
download is discarded: a Cloudflare challenge (the `cf-mitigated` header or its
challenge script), an AWS WAF challenge (the `x-amzn-waf-action` header on an
empty 202, as EUR-Lex answers), HTML where the current version is a PDF or
another non-HTML document, and a download with no body sections where the
current version has some. A challenge needs a browser that runs its script; no
HTTP library gets past it. If the page really changed that way, delete the old
versions (and the statements citing them, as for re-chunking below) to accept
it.

EUR-Lex documents are also served, without the challenge, by the Publications
Office's Cellar repository. Ask it for the CELEX number with
`Accept: application/xhtml+xml` and `Accept-Language: eng`; it redirects to a
`http://` Cellar URL, which works over `https://` too:

```sh
curl -sI -H 'Accept: application/xhtml+xml' -H 'Accept-Language: eng' \
  https://publications.europa.eu/resource/celex/32009L0024 | grep -i location
```

Put that URL, with `https://`, in `sources.yaml`. It names one manifestation,
so a later amendment is not picked up as `changed`; look the CELEX number up
again to move on.

### Downloading by hand

When sources fail, `kb fetch` ends with a numbered table: one line per failed
source with its link and the folder to save the file into,
`downloads/<source>/` (created for you; `--downloads DIR` moves it). Open each
link in a browser, save the PDF, or the page as HTML, into the folder on its
line; the file name does not matter, one file per folder. Then run the script
the table points to, `downloads/ingest.sh`:

```sh
~/kbs/NAME/downloads/ingest.sh
```

It runs `fetch`, `parse` and `extract` for those sources, then `index`, with
the options of the run that wrote it; every step runs even when an earlier one
failed. A source still without a file is tried online again; if that fails, it
is listed again and the script is rewritten for the sources still missing. Once
every step succeeds the script deletes itself.

A plain `kb fetch` (or `./setup NAME`) picks the files up too. A file saved by
hand is stored instead of downloading that source's URL, with the same checks
as a download, and is removed from its folder once stored; a file that fails
(several files, a type other than `.pdf`, `.html`, `.htm` or `.xhtml`, an empty
file, a challenge page, or a refused block page) stays where it is. Add
`downloads/` to the `.gitignore` of a knowledge base created before it existed.

### Links

`kb links ID` prints the absolute `http(s)` URLs the current version of a
source links to, in document order and each once: `<a href>` in the HTML text
the parser keeps (navigation and footers left out) and URI link annotations in
a PDF. Fragments are dropped and links into the source itself left out. Add the
ones worth keeping to `sources.yaml` by hand; `kb fetch` follows no links.

## Parsing

`kb parse` extracts text from the current version of each source and splits
it into chunks with a citable `section_ref` such as `2.4`, `Chapter 3 § 5` or
`p. 12`. HTML (and XHTML) and PDF are read; any other content type fails with a
message naming the reader to add in `src/kb/extract.py`. Sources use the
`section_pattern`, `chapter_pattern` and `body_start` regexes in
`sources.yaml`, plus `body_end` (drop an appendix or the next article in a
volume) and `section_label` / `chapter_label` (normalise refs). Patterns see
HTML headings in Markdown form (`## 1. Introduction`). `skip_sections`, a
regex matched at the start of a section ref, keeps those sections searchable
but out of extraction. `skip_classes`, a list of HTML class names, drops every
element carrying one of them, content included, before patterns run; on
legislation.gov.uk, `[LegCommentaryLink]` removes the amendment markers glued
to section numbers (`X1180` becomes `180`). Sources without a pattern fall back
to headings, or to pages for PDFs. Forms are skipped, except the ASP.NET page
form (the one holding `__VIEWSTATE`), which wraps the whole page. HTML is
decoded with the charset its `<meta>` tag declares, else as UTF-8 or, when the
bytes are not valid UTF-8, windows-1252. Sections longer than 12 000 characters
are split
into `(part n)` chunks.

Re-parsing replaces a version's chunks, and refuses to once statements cite
them. To re-chunk such a source after changing its patterns, back up the
database, delete that version's statements and their topic links, then run
`parse`, `extract` and `index` for the source:

```sh
V=<source-id>@<version>   # current version id
sqlite3 data/kb.db "DELETE FROM statement_topics WHERE statement_id IN
  (SELECT s.id FROM statements s JOIN chunks c ON c.id = s.chunk_id WHERE c.version_id = '$V');
  DELETE FROM statements WHERE chunk_id IN (SELECT id FROM chunks WHERE version_id = '$V');"
```

## Statement extraction

`kb extract` sends each section (preamble chunks excluded) to Claude through
the local `pi` login: `pi -p` with tools, extensions, skills and context files
off and stdin closed, so nothing but the prompt and the section reaches the
model. The one exception is `@gotgenes/pi-anthropic-auth`, loaded with `-e`
when installed: an Anthropic subscription (OAuth) login rejects requests
without it as a third-party app. `KB_PI_EXTENSIONS` (paths joined with `:`,
empty for none) replaces that list. The default model is
`anthropic/claude-sonnet-5` (`--model` to change); `KB_PI_PREFIX` wraps the
command, for example in a sandbox.

The system prompt is `prompts/extract.md` followed by the modalities and topics
from `domain.yaml`. Each statement carries a verbatim quote, a one-sentence
summary, a modality, one to three topics and the tags it applies to. A record
is kept only if its quote appears in the section word for word (ignoring
whitespace, and hyphens between letters, because PDF extraction breaks words at
line ends) and every field validates; rejects are reported. Raw model output
is cached by prompt, model and section in `extraction_cache`, so a re-run only
calls the model for new or changed sections, and changing the prompt or the
domain's modalities or topics makes every cached output stale.

## Search and the MCP server

`kb index` rebuilds two FTS5 tables (sections, and statement summaries with
their quotes) from the current versions, and embeds every section and statement
whose text is new or changed with the local multilingual `BAAI/bge-m3` model
(about 2 GB, downloaded on the first run; no API key, and queries never leave
the machine). Vectors live in the plain `vectors` table; search compares a
query with all of them in memory, which is fast up to tens of thousands of
vectors.

`kb_search` fuses four rankings by reciprocal rank (k = 10): keywords on
section text and on statement summaries (half weight), meaning on section text
(triple weight) and on statement summaries. The weights were tuned on a
regulatory golden set; re-check them with `kb eval` on yours. A section that
appears verbatim in several documents is shown once with `also_in`.

| Tool | Returns |
|---|---|
| `kb_search(query, tags?, topics?, limit?)` | ranked sections with excerpt, citation fields and their statements |
| `kb_get(source_id, section_ref)` | one section's full text and statements; similar refs when not found |
| `kb_topic(topic, tags?, limit?)` | every statement on one topic, strongest modality and doc type first, deduplicated |
| `kb_sources()` | documents with version, fetch date, tags and counts, plus the topic ids |

Every result carries `source_id`, `section_ref`, `url`, `version` and
`fetched_at`. `kb serve` opens the database read-only.

## Evaluation and audits

`kb eval` runs the golden questions and reports whether an expected section is
in the top five, and re-checks that every stored quote still appears in its
section; it exits 1 below 90%.

`scripts/` holds three quality checks. Run them from a knowledge base
directory with the engine's environment, for example
`uv run --project ~/github/reg python ~/github/reg/scripts/audit_structure.py`:

- `audit_structure.py [data/kb.db]`: one TSV row per source with chunk counts,
  preamble size, oversized and tiny chunks, duplicate refs, numbering gaps,
  table-of-contents leakage, sections without statements and statements from an
  outdated prompt.
- `judge_statements.py`: a second-opinion model review of a sample of stored
  statements against their section text.
- `compare_kb.py base.db rerun.db`: agreement between two builds from the same
  sources, since model extraction is not deterministic.

## DuckDB, Parquet and Postgres

The database file is the export: copy it to share it. DuckDB reads it
directly; skip the `*_fts*` tables, which are SQLite-only indexes.

```sql
ATTACH 'data/kb.db' AS k (TYPE sqlite, READ_ONLY);
COPY (SELECT * FROM k.statements) TO 'statements.parquet' (FORMAT parquet);
```

For Postgres, copy the plain tables through DuckDB's postgres extension and
rebuild the indexes there with `tsvector` and pgvector.

## Licence

MIT; see [`LICENSE`](LICENSE).
