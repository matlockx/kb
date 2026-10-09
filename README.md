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
| `domain.yaml` | vocabulary: name, instructions, doc types, publishers, tags, modalities, topics |
| `sources.yaml` | the documents to ingest |
| `AGENTS.md` | instructions for an agent that fills in the files (written by `setup`) |
| `scopes.yaml` | optional: the scopes sources belong to, such as jurisdictions (see below) |
| `prompts/extract.md` | what the model extracts from each section |
| `eval/golden.yaml` | test questions with their expected sections |
| `data/kb.db`, `raw/` | built by the pipeline; keep them out of version control |

Every `kb` command reads and writes relative to the current directory, or to
the directory given with `-C DIR`. Directories never share state, so several
knowledge bases can be built and served at the same time; only the embedding
model cache (`~/.cache/huggingface`) is shared, read-only.

Run the engine from any directory without installing it (fish):

```sh
alias setup ~/github/kb/setup && funcsave setup
alias kb 'uv run --project ~/github/kb --quiet kb' && funcsave kb
```

## Interactive menu

`kb` (or `setup`) without arguments, on a terminal, opens a small menu in the
style of the [bubbletea](https://github.com/charmbracelet/bubbletea) examples:
arrow keys or `j`/`k` move, enter chooses, `esc` or `q` goes back.

- The first screen lists every knowledge base in `~/kbs` and every other one
  registered in `~/.omp/agent/mcp.json` as `kb -C DIR serve`, each with its
  source and statement counts and whether its MCP server is registered.
- A knowledge base opens a menu of its commands: check, build (the `setup
  NAME` run below), fetch, parse, extract, index, eval, quality, review (vet or
  dispute a source), register, and publish
  once a catalog is connected. `kb -C DIR` without a command opens this menu
  for the knowledge base in `DIR` directly.
- `+ new` asks for a name and creates the directory as `setup NAME` does;
  `age key` prints your public key, creating it on first use; `name` sets the
  name shown beside your key and on reviews.
- `⇅ catalog` manages the shared catalog (see "Sharing knowledge bases between
  devices"). Without one it offers to connect: clone an existing GitHub
  catalog, create a new private GitHub repository with `gh` and clone it, or
  use a local or synced folder; the choice is saved in `~/.config/kb/catalog`,
  and `KB_CATALOG` still takes precedence. With one it lists every published
  knowledge base with its version, size, the copy `~/kbs` holds and its title.
  An entry installs, updates or reinstalls a pulled copy, shows the details of
  `kb catalog NAME` (`info`) and, for a knowledge base on this device,
  publishes a new version, shares it with another age key and the name of its
  owner, revokes a key (listed by name), or turns it private or public; the
  menu title says whether the local copy has unpublished changes. `+ publish`
  shares a local knowledge base not yet
  in the catalog. The menu never offers to pull over a knowledge base built on
  this device, and creates your age key before the first publish.

Every entry runs the same code as the matching command and prints its output
above the menu. Editing files and the git history stay with their own tools;
GitHub is reached through `gh`.

## Starting a new knowledge base

```sh
setup running
```

The first run asks what the knowledge base is about and whether to make it a
git repository, then creates `~/kbs/running` (`--dir DIR` for another place)
with template files and a `.gitignore` for `data/` and `raw/`.

Fill in the four files; an agent can draft them from your sources, following
the `AGENTS.md` that `setup` writes next to them:

- `domain.yaml`:
  - `name` and `instructions`: what the knowledge base holds; shown to the
    agent as the MCP server instructions. Pre-filled from your answer.
  - `publishers`: who publishes the sources and the domains their URLs may be
    on, see "Source trust, reviews and quality".
  - `doc_types`: the kinds of document you ingest, most authoritative first
    (for running plans, for example `[position_stand, review, study, book,
    coaching_guide]`). An entry may be a mapping `{id, binding, note}`:
    `binding: false` marks a type whose sections are not binding text (case
    law, say), and `note` is shown with every section of the type.
  - `tags`: one facet to filter on (for example `[beginner, intermediate,
    advanced, 5k, 10k, half_marathon, marathon, ultra]`). Each source lists the
    tags it covers; each statement the subset it applies to.
  - `modalities`: what kind of statement the model records, strongest first,
    each with the description the model sees.
  - `topics`: a fixed taxonomy; each statement gets one to three ids.
  - `scopes` and `availability`: optional, see below.
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

## Scopes, availability and translations

A second facet that every document has exactly one value of, such as the
jurisdiction of a law, is a scope. `domain.yaml` turns it on with
`scopes: {label: jurisdiction}`; `scopes.yaml` lists the values:

```yaml
- id: GB
  name: Great Britain
  aliases: [uk, united kingdom, british]   # further names a question may use
  languages: [en]                          # a source in another language must be a translation
  details: {regulator: Gambling Commission, eu: false}   # shown verbatim by kb_sources
  availability:                            # only with availability in domain.yaml
    casino: {status: licensed, source: gb-gambling-act-2005, note: "s. 65(2)(a)"}
    poker: {status: unknown}
```

Every source then names its `scope`. Search filters by scope; without one, a
scope whose name or alias the question uses ("in Portugal") becomes the filter
and is reported as `scopes_inferred`.

`availability: {tags: [...], values: [...], unknown: unknown}` in
`domain.yaml` makes every scope state, for each of those tags, one of the
values with the source that backs it, or the unknown value without one; for
example whether a product can be licensed in a jurisdiction. An
`effective_from` date (`YYYY`, `YYYY-MM` or `YYYY-MM-DD`) still ahead is shown
with `in_force: false`.

A source with `translation_of: <id>` is an unofficial translation of another
source in the same scope and another language. Its sections say `binding:
false`, and each of its statements names the original's section with the same
numbers as `original_section`. `kb_topic` lists the statements of a translated
original through its translation. `extract_note` adds one line to every
extraction message of a source, for example to tell the model how that
document words its rules.

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
      "args": ["run", "--project", "/Users/me/github/kb", "--quiet", "kb", "-C", "/Users/me/kbs/running", "serve"],
      "timeout": 120000
    },
    "web": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "/Users/me/github/kb", "--quiet", "kb", "-C", "/Users/me/github/kb/examples/web-principles", "serve"],
      "timeout": 120000
    }
  }
}
```

`/mcp` in an omp session lists the servers and their state.

**Claude Code:**

```sh
claude mcp add running -s user -- uv run --project ~/github/kb --quiet kb -C ~/kbs/running serve
```

omp also imports Claude Code's user-level servers, so register each server in
one place only: the same name in both makes one shadow the other.

## Source trust, reviews and quality

A knowledge base is only as good as its sources, so every source gets a trust
level with the reason for it, and a report shows how much of what was ingested
can be relied on.

**Publishers.** `domain.yaml` lists who publishes the sources and the only
domains their URLs may be on:

```yaml
publishers:
  - name: W3C Technical Architecture Group   # spelled as in sources.yaml
    domains: [w3.org]                          # w3.org and its subdomains
    official: true                             # issues the texts itself
```

`kb sources --check` (and every command that reads `sources.yaml`) fails for a
source whose `publisher` is not listed or whose URL is on another domain, so a
look-alike or typo-squatted site is stopped before anything is fetched. Use the
narrowest domain that belongs to the publisher. A domain written before this
field existed still loads and serves, but its registry fails the check until
its publishers are declared; the message names each offending source.

**Trust levels**, from `kb review` and the `trust` and `trust_reason` fields of
`kb_sources`:

| Level | When |
|---|---|
| `official` | the publisher is declared `official: true`, the URL is on its domains, the doc type is binding and the source is no translation |
| `secondary` | the URL is on a declared publisher's domains, but the publisher is not official, the doc type is not binding, or the source is a translation |
| `unverified` | the publisher is not declared, or the URL is on none of its domains (only in a database whose registry was not checked) |
| `disputed` | a person disputed the current version |

**Reviews.** A person records a verdict on the current version of a source with
`kb review ID --vet` (optionally `--note`) or `kb review ID --dispute --note
WHY`; the menu of a knowledge base has the same under `review`, and `kb review
ID` shows the source with its reviews. The reviewer is your name from `kb name`.
A vetted verdict raises the level by one (`unverified` to `secondary`,
`secondary` to `official`), a disputed one overrides every vetted one, and the
latest verdict of each reviewer counts. A review belongs to one version of the
source: when the download changes, the new version starts without verdicts and
the old ones stay as history. Reviews live in the database (`reviews`) and
travel in a published snapshot. A pulled copy is replaced by the published
database, so reviews recorded only on the pulled copy are lost: review on the
device that publishes. Reviewing is optional; without reviews the level comes
from the publisher and the doc type alone.

**Quality.** `kb quality` prints, per source, its trust level and the counts
for its current version:

- `sections` and `covered`: the sections and how many have a statement;
- `statements` and `verified`: the statements and how many still have their
  quote in their section, the machine check that `kb eval` also runs (quotes
  rejected at extraction are not stored, so they are not counted);
- `evidence`: the verified statements of a source whose trust is `official`
  and whose doc type is binding, the ones you can cite as the text itself.

Trust says where a text comes from and what people made of it; it does not
prove that a page at an official URL is genuine, nor that its content is
correct. A check against the publisher's own site, by a person, is what
`--vet` records. `kb fetch` also fails a source whose download ends, after
redirects, on a host outside its publisher's domains (the message names the
host); add the host to the publisher's `domains` when it is the publisher's
own, for example a CDN. A file saved by hand is not checked.

## Sharing knowledge bases between devices

`kb publish` turns a built knowledge base into one encrypted file, a bundle;
`kb pull` installs it on another device. The bundle is a snapshot of
`data/kb.db` holding the source-of-truth tables, the extraction cache (so the
copy can take new sources without paying again for the sections already
extracted) and the configuration files (`domain.yaml`, `sources.yaml`,
`scopes.yaml`, `prompts/`, `eval/`, verbatim in `kb_files`). It leaves out what `kb index`
rebuilds, the full-text tables and the vectors, and the raw downloads. For the
running-coach knowledge base that is 10 MB instead of 470 MB; a first pull
rebuilds the index in about 8 minutes on an M-series Mac.

Bundles are compressed with zstd and encrypted with [age](https://age-encryption.org)
to a list of public keys, so a catalog can live in a public place and still be
readable only by the people you list. Every device and person has its own key:

```sh
kb keygen     # writes ~/.config/kb/identity.txt (or $KB_AGE_IDENTITY) and prints the public key
```

Back the identity file up: without it nothing published for it can be opened.

A catalog is a directory with one folder per knowledge base: `manifest.json`
(version, checksums, where the bundle is), `recipients.txt` (the public keys
that can open it) and, for a public knowledge base, a readable copy of its
configuration, so the git history shows which sources were added.

- A folder catalog (a cloud drive, a network share) keeps the bundle beside
  the manifest and only the newest one.
- A git catalog (a clone with a GitHub remote) uploads each bundle as an asset
  of a GitHub release named `NAME-vN`, through the `gh` command line (install
  it and run `gh auth login` once). Git holds only the small text files, so the
  repository stays small however often you publish; old releases stay until you
  delete them. Every command pulls the clone first, and `publish` commits and
  pushes.

```sh
set -Ux KB_CATALOG ~/kb-catalog                 # or connect one in the menu, or --catalog DIR on each command
kb -C ~/kbs/running publish                     # publish as "running" (--name to rename)
kb -C ~/kbs/running publish --recipient age1... # also encrypt to a colleague's key
kb -C ~/kbs/running publish --revoke age1...    # stop encrypting later versions to a key
kb -C ~/kbs/acme-contracts publish --private    # configuration only inside the bundle
kb catalog                                      # name, version, size, the version ~/kbs holds, title
kb catalog running                              # details: published, counts, bundle, storage, keys
kb pull running                                 # install into ~/kbs/running and offer to register
kb pull running --force                         # update an installed copy
```

`publish` always adds your own public key to `recipients.txt`; add other
people's with `--recipient AGE1...=NAME` (the name is optional) and remove
them with `--revoke`, or edit the file (one `age1...` key per line, a name
after a `#`) and publish again. The names are how you tell the keys apart
later: your own key is written as `NAME (HOST)`, with the name from `kb name`
(asked once, saved in `~/.config/kb/name`) and the device's host name, so
every device of yours has its own line. An own key that already has a name
keeps it. `kb catalog NAME` lists each key with its name. A publish whose
content, recipients and privacy match the last version prints `unchanged` and
uploads nothing; when only names differ, it updates `recipients.txt` of that
version and commits it without making a new version, because the bundle is
encrypted to the same keys. The names are plain text in the catalog, visible to
everyone who can read it, so with real names in a git catalog that is personal
data. `--private` stays in force for later publishes until `--no-private`.

Whether this device holds changes that were not published is shown in the
menu of a catalog entry (`matches v3` or `unpublished changes`) and as `local
state` in `kb catalog NAME`. It snapshots the database exactly as `publish`
does and compares the checksum with the manifest, so it costs one copy of the
database per check, and it also turns on after a `kb fetch` that found nothing
new, because the fetch time is part of the snapshot.

`pull` checks the bundle against the checksum in the manifest, decrypts it,
writes the database and the configuration files into `~/kbs/NAME` (`--dir`
for another place), runs `kb index` (downloading the embedding model when it is
not cached), and offers to register the MCP server as `setup` does. An
installed knowledge base is replaced only with `--force`; its vectors are
reused for every unchanged section and statement, so an update costs seconds,
and `raw/` and other local files are kept.

A pulled copy is a full knowledge base: add sources to `sources.yaml` and run
`setup NAME` (or fetch, parse, extract and index) as usual, then publish it.
`kb parse` keeps the stored sections of a source whose download is not on the
device and reports it as `kept`; `kb fetch` downloads it again. Build and
publish each knowledge base from one device at a time: the version number is
the catalog's, so two devices publishing the same name replace each other's
work.

The database alone is a knowledge base too: `kb serve` falls back to the
`domain.yaml` stored in it, and `kb unpack` (`--force` to overwrite files that
differ) writes the stored configuration files into the directory.

What the encryption does and does not do:

- Only the listed keys can read a bundle. Removing a key from
  `recipients.txt` protects later versions only; whoever held it keeps what
  they could already decrypt.
- age does not prove who encrypted a bundle: anyone with the public keys can
  make one. What binds a bundle to its knowledge base is the checksum in the
  manifest, so write access to the catalog is what has to be guarded. `pull`
  writes only the configuration paths a bundle names, nowhere else.
- A pulled database is plain on disk, like one you built; FileVault protects
  it there.
- Turning a published knowledge base `--private` keeps its configuration out of
  later versions, not out of the git history.
- The bundle holds the full text of every source. Encrypting it does not make
  sharing it with others lawful; check the sources' licences before you add a
  recipient.

## How it works

1. `sources.yaml` lists every document by hand: publisher, title, URL,
   language, `doc_type` and tags. `domain.yaml` defines the allowed values and
   the publishers whose domains a source's URL must be on.
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

[`docs/architecture.md`](docs/architecture.md) follows the whole flow step by
step, with diagrams of every command, the data model and the sharing protocol.

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
kb quality           # per source: trust, sections, statements, verified, evidence
kb review            # trust level of every source, and why
kb review ID --vet   # record your verdict on a source (--dispute --note WHY to downvote)
kb name Martin       # your name, shown beside your key and on reviews
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

A few hosts need more than a GET. `www.boe.es` is asked for
`Accept: application/xml` (its open-data API answers 400 without it). A
Normattiva Akoma Ntoso export URL (`/do/atto/caricaAKN?dataGU=…&codiceRedaz=…`)
opens the act's detail page first in the same cookie session and asks for the
text in force today (`dataVigenza`); an HTML answer fails. A BWB manifest URL
(`repository.officiele-overheidspublicaties.nl/bwb/BWBR…/manifest.xml`) is
followed to the consolidation its `_latestItem` names. XML is stored as
`.xml`, JSON as `.json`.

A block or challenge page must not hide a good copy, so these fail too and the
download is discarded: a Cloudflare challenge (the `cf-mitigated` header or its
challenge script), an AWS WAF challenge (the `x-amzn-waf-action` header on an
empty 202, as EUR-Lex answers), an Anubis proof-of-work page (as BAILII
serves), HTML where the current version is a PDF or another non-HTML document,
and a download with no body sections where the
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

Run from a terminal, the script first opens the links in the default browser
(`xdg-open`, else `open`), five at a time, prints the folder for each and
waits for Enter before the next five; a link whose folder already holds a file
(dotfiles ignored) is not opened again. It then runs `fetch`, `parse` and
`extract` for those sources, then `index`, with the options of the run that
wrote it; every step runs even when an earlier one failed. Without a terminal
on standard input it skips the browser and only ingests. A source still
without a file is tried online again; if that fails, it is listed again and the
script is rewritten for the sources still missing. Once every step succeeds the
script deletes itself.

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
`p. 12`. HTML (and XHTML), PDF, XML legislation and GOV.UK Content API JSON
are read; any other content type fails with a message naming the reader to add
in `src/kb/extract.py`. XML (typed `application/xml` or `text/xml`, or an XML
declaration on a body not typed as HTML) is read by its root element: LexDania
(`Dokument`, retsinformation.dk), CLML (`Legislation`, legislation.gov.uk,
`s. 65`, `Sch. 18 para. 9`), BWB (`toestand`, wetten.overheid.nl, repealed
articles dropped), Akoma Ntoso (`akomaNtoso`, Finlex and Normattiva) and BOE
(`response`, the latest version of each block, annex points as `Anexo 3.1`).
These readers mark each section start themselves, so they need no
`section_pattern`. `application/json` is read as a GOV.UK Content API item: its
title and the HTML of `details.body`. Sources use the
`section_pattern`, `chapter_pattern` and `body_start` regexes in
`sources.yaml`, plus `body_end` (drop an appendix or the next article in a
volume) and `section_label` / `chapter_label` (normalise refs). Patterns see
HTML headings in Markdown form (`## 1. Introduction`), and lines inside a
`<blockquote>` prefixed with `> `, so `^\d+\.$` skips paragraph numbers a
judgment quotes from another judgment. `skip_sections`, a
regex matched at the start of a section ref, keeps those sections searchable
but out of extraction. `drop_preamble: true` (with `body_start`) stores no
preamble at all, for a source that is one window of a document another source
also covers, so the text outside the window is not indexed twice.
`skip_classes`, a list of HTML class names, drops every
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
from `domain.yaml`; each message names the document, its publisher and scope,
type, language, tags and `extract_note`. Each statement carries a verbatim
quote, a one-sentence summary, a modality, one to three topics, the tags it
applies to and, when the prompt asks for it and the section states it, an
`effective_from` date. A record
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
| `kb_search(query, scopes?, tags?, topics?, limit?)` | ranked sections with excerpt, citation fields and their statements |
| `kb_get(source_id, section_ref)` | one section's full text and statements; similar refs when not found |
| `kb_topic(topic, scopes?, tags?, limit?)` | every statement on one topic, strongest modality and doc type first, deduplicated; with scopes, per scope with its availability |
| `kb_sources(scope?)` | documents with version, fetch date, tags and counts, the topic ids and each scope's details and availability |

Every result carries `source_id`, `section_ref`, `url`, `version` and
`fetched_at`; with non-binding doc types or translations, sections also carry
`binding` and a `note`. `kb serve` adds the columns of a newer schema to an
older database once at start, then opens it read-only.

## Evaluation and audits

`kb eval` runs the golden questions and reports whether an expected section is
in the top five, and re-checks that every stored quote still appears in its
section; it exits 1 below 90%. A question may carry `tags` and `scopes` to
filter its search; without `scopes`, a scope the question names applies.

`scripts/` holds three quality checks. Run them from a knowledge base
directory with the engine's environment, for example
`uv run --project ~/github/kb python ~/github/kb/scripts/audit_structure.py`:

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
