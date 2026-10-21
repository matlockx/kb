# Installing a knowledge base someone shared with you

This guide is for a person who has been offered a knowledge base (for example
`running-coach`) by its owner, and for the coding agent that person asks to set
it up. It needs no programming knowledge: the agent runs the commands, the
person does the three steps marked **Person**.

The result: your AI assistant (Claude Code, Claude Desktop or omp) gets four
tools (`kb_search`, `kb_get`, `kb_topic`, `kb_sources`) that answer from the
shared documents and cite the exact section each answer comes from.

## Instructions for the agent

Work through the steps in order and stop at every step marked **Person**: tell
the person exactly what to do, wait until they confirm, then continue. Run
every command in a POSIX shell (zsh or bash). Do not run `kb setup`, `kb
extract`, `kb fetch` or `kb publish`: a pulled knowledge base is complete, and
those commands either cost model usage or write to the owner's catalog.

The owner sends three values; ask the person for any that are missing:

| Value | Example |
|---|---|
| `NAME`, the knowledge base | `running-coach` |
| `CATALOG`, the GitHub repository holding the catalog (`owner/name`) | `OWNER/CATALOG-REPO`, as sent by the owner |
| the owner's name, for the messages to the person | `Martin` |

Paths used below (keep them unless the person asks otherwise):

| Path | Holds |
|---|---|
| `~/github/kb` | the kb engine (public: <https://github.com/matlockx/kb>) |
| `~/kb-catalog` | a clone of the catalog |
| `~/kbs/NAME` | the installed knowledge base |
| `~/.config/kb/identity.txt` | the person's private decryption key |

Requirements: macOS (Linux works the same with its package manager; Windows is
not covered), about 4 GB of free disk space and a network connection. The
first install downloads a 2 GB search model and builds the index, which takes
about 8 minutes on an Apple-silicon Mac and longer on older machines.

### 1. Install the tools

Check what is already there:

```sh
command -v brew git gh uv
```

- No `brew`: **Person** installs Homebrew from <https://brew.sh> (it asks for
  the Mac password, which only the person can type), then opens a new terminal
  window.
- Then install whatever is missing:

```sh
brew install git gh uv
```

`uv` installs the right Python by itself on first use; no separate Python is
needed.

### 2. Sign in to GitHub

The catalog lives in a private GitHub repository, so the person needs a GitHub
account that the owner has invited.

**Person**:

1. Create a free account at <https://github.com/signup> if you have none, and
   send the user name to the owner.
2. Accept the invitation to `CATALOG` that the owner sends (GitHub e-mails it;
   it is also listed at <https://github.com/notifications>).
3. In a terminal, run `gh auth login`, choose `GitHub.com`, `HTTPS`, answer
   yes to authenticating Git, and log in with the browser.

Check:

```sh
gh auth status && gh repo view CATALOG --json name
```

A `Could not resolve to a Repository` error means the invitation is not
accepted yet.

### 3. Install the engine

```sh
mkdir -p ~/github
gh repo clone matlockx/kb ~/github/kb
uv run --project ~/github/kb --quiet kb --help
```

The last command prepares the engine's environment (a minute on the first run)
and prints the list of commands. Every `kb` command below is written out in
full as `uv run --project ~/github/kb --quiet kb ...`, so no alias or shell
configuration is needed.

### 4. Create the decryption key and send the public half

```sh
uv run --project ~/github/kb --quiet kb keygen
```

It writes `~/.config/kb/identity.txt` and prints a line starting with `age1`.
That line is the public key: safe to send by any channel. The file is the
private key: never send it anywhere.

**Person**:

1. Send the `age1...` line to the owner and ask them to publish the knowledge
   base for this key.
2. Back up `~/.config/kb/identity.txt` (a password manager or an encrypted
   backup). Without it, nothing published for this key can be opened; a new
   key means asking the owner to publish again.
3. Wait for the owner's message that it is published.

### 5. Download and install the knowledge base

```sh
gh repo clone CATALOG ~/kb-catalog
uv run --project ~/github/kb --quiet kb catalog --catalog ~/kb-catalog
uv run --project ~/github/kb --quiet kb pull NAME --catalog ~/kb-catalog
```

`kb catalog` lists what the catalog holds; `NAME` must be in it. `kb pull`
downloads the encrypted bundle, decrypts it with the key from step 4, writes
the knowledge base to `~/kbs/NAME` and builds the search index (the long part).
At the end it may ask whether to register an omp MCP server; answer `y` only if
the person uses omp, otherwise the next step registers it.

Errors and what they mean:

| Message contains | Cause | Fix |
|---|---|---|
| `no age identity at` | step 4 was skipped or the key file moved | step 4 |
| `cannot decrypt` | the bundle was not published for this key | the owner has to publish with this exact `age1...` key |
| `gh ... failed` | no access to the catalog | step 2 |
| `needs a newer engine` | the engine is older than the bundle | `git -C ~/github/kb pull`, then pull again |
| `the search index is not built` | the model download broke off | `uv run --project ~/github/kb --quiet kb -C ~/kbs/NAME index` |

Check:

```sh
uv run --project ~/github/kb --quiet kb catalog NAME --catalog ~/kb-catalog
```

The `local copy` line must name the published version, without `(older)` and
not `not installed`.

### 6. Connect it to the assistant

Use the section for the assistant the person uses. The server name (`NAME`)
becomes part of the tool names, for example `mcp__running-coach__kb_search` in
Claude Code.

**Claude Code** (one command, for all projects):

```sh
claude mcp add NAME -s user -- uv run --project ~/github/kb --quiet kb -C ~/kbs/NAME serve
claude mcp list
```

**Claude Desktop**: the app does not see the terminal's search path, so every
path must be absolute. Find them first:

```sh
command -v uv
echo ~
```

Then add an entry to `~/Library/Application Support/Claude/claude_desktop_config.json`
(create the file if it does not exist; keep any servers already in it), with
`/opt/homebrew/bin/uv` and `/Users/PERSON` replaced by the two printed values:

```json
{
  "mcpServers": {
    "NAME": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "--project", "/Users/PERSON/github/kb", "--quiet", "kb", "-C", "/Users/PERSON/kbs/NAME", "serve"]
    }
  }
}
```

**Person** quits Claude Desktop completely (Cmd-Q) and opens it again.

**omp**: answering `y` in step 5 registered it. Otherwise **Person** runs, in
a terminal, `uv run --project ~/github/kb --quiet kb pull NAME --force --catalog ~/kb-catalog`
and answers `y` at the end; the pull takes seconds, since the index is kept.

### 7. Try it

**Person** opens a new conversation and asks something the knowledge base
covers, for example for `running-coach`:

> What does the running-coach knowledge base say about tapering before a
> marathon? Cite the sources.

The answer should name sources with a `source_id` and `section_ref`. The first
question in each session loads the search model and can take up to a minute;
if it times out, ask again.

## Updates

The owner says when a new version is published. Then:

```sh
git -C ~/github/kb pull
uv run --project ~/github/kb --quiet kb pull NAME --force --catalog ~/kb-catalog
```

Unchanged sections keep their index, so an update takes seconds to minutes.
Restart the assistant afterwards.

## Removing it

```sh
claude mcp remove NAME -s user        # Claude Code; for Claude Desktop delete the entry from its config file
rm -rf ~/kbs/NAME ~/kb-catalog
```

`~/github/kb` and the search model in `~/.cache/huggingface` can go too when
no other knowledge base uses them. Keep `~/.config/kb/identity.txt` if the
owner may share something again.

## What the person should know

- Everything runs on this computer. Questions to the knowledge base do not
  leave it; the assistant itself still sends the conversation, including the
  quoted sections, to its model provider as usual.
- The installed copy is a plain file on disk, protected by the disk
  encryption of the Mac (FileVault).
- The documents in it are shared for personal use. Do not pass the files on.
