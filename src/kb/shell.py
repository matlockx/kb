"""`kb` without a command: a small interactive shell to create, build, register, publish and pull knowledge bases.

Menus render inline in the style of the charmbracelet/bubbletea examples: a title badge, a cursor list and a help
line. Every action calls the same function as the matching `kb` command; git, gh and editors stay outside.
"""

import json
import os
import shutil
import sqlite3
import sys
import termios
import tty
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TextIO

from kb import catalog, setup
from kb import keys as age
from kb import review as reviews
from kb.db import DEFAULT_PATH as DB
from kb.domain import ConfigError

# 256-colour palette of the bubbletea list example (lipgloss colours 62, 212, 241)
TITLE = "\x1b[1;38;5;230;48;5;62m"
ACCENT = "\x1b[38;5;212m"
DIM = "\x1b[38;5;241m"
GREEN = "\x1b[38;5;42m"
RED = "\x1b[38;5;203m"
RESET = "\x1b[0m"
HIDE_CURSOR, SHOW_CURSOR = "\x1b[?25l", "\x1b[?25h"

KEYS = {
    b"\x1b[A": "up",
    b"\x1bOA": "up",
    b"k": "up",
    b"\x1b[B": "down",
    b"\x1bOB": "down",
    b"j": "down",
    b"\r": "enter",
    b"\n": "enter",
    b"\x1b": "back",
    b"q": "back",
    b"\x04": "back",  # Ctrl-D
    b"": "back",  # closed input
}

Keys = Callable[[], str]  # -> 'up', 'down', 'enter', 'back' or '' for any other key


@dataclass
class Item:
    label: str
    detail: str
    action: Callable[[], object]


def read_key(fd: int | None = None) -> str:
    """The next key press on the terminal at fd (default stdin), named as in KEYS; '' for any other key."""
    fd = sys.stdin.fileno() if fd is None else fd
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        data = os.read(fd, 8)  # an arrow key arrives as one 3-byte read
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return KEYS.get(data, "")


def choose(title: str, items: list[Item], keys: Keys, out: TextIO, back: str = "back", start: int = 0) -> int | None:
    """Draw the menu, redraw it in place on every key, and erase it; the chosen index, None on back."""
    cursor, drawn = min(start, len(items) - 1), 0
    out.write(HIDE_CURSOR)
    try:
        while True:
            frame = render(title, items, cursor, back)
            out.write((f"\x1b[{drawn}F" if drawn else "") + "\x1b[J" + "\n".join(frame) + "\n")
            out.flush()
            drawn = len(frame)
            key = keys()
            if key == "enter":
                return cursor
            if key == "back":
                return None
            cursor = (cursor + {"up": -1, "down": 1}.get(key, 0)) % len(items)
    finally:
        out.write((f"\x1b[{drawn}F\x1b[J" if drawn else "") + SHOW_CURSOR)
        out.flush()


def render(title: str, items: list[Item], cursor: int, back: str) -> list[str]:
    """The menu lines, each cut to the terminal width so that none wraps and the redraw stays in place."""
    width = shutil.get_terminal_size().columns - 5  # indent and cursor
    pad = max(len(item.label) for item in items) + 3
    lines = ["", f"  {TITLE} {title[: width - 2]} {RESET}", ""]
    for n, item in enumerate(items):
        text = f"{item.label:<{pad}}{item.detail}"[:width]
        label, detail = text[:pad], text[pad:]
        mark, colour = ("> ", ACCENT) if n == cursor else ("  ", "")
        lines.append(f"  {colour}{mark}{label}{RESET}{DIM}{detail}{RESET}")
    keys = f"↑/k up • ↓/j down • enter choose • esc/q {back}"
    lines += ["", f"  {DIM}{keys[:width]}{RESET}"]
    return lines


def perform(label: str, action: Callable[[], object], out: TextIO) -> None:
    """Run a leaf action below a heading and report its outcome; failures return to the menu."""
    out.write(f"\n{ACCENT}> {label}{RESET}\n")
    out.flush()
    try:
        code = action()
    except KeyboardInterrupt:
        code = "interrupted"
    except SystemExit as exc:  # argparse rejecting a kb command line
        code = exc.code
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        code = "error"
    ok = code in {0, None}
    out.write(f"{GREEN}✓ {label}{RESET}\n" if ok else f"{RED}✗ {label} failed ({code}){RESET}\n")
    out.flush()


def servers(config: Path) -> dict:
    """The mcpServers mapping of an omp mcp.json; empty when it is missing or unreadable."""
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    found = data.get("mcpServers") if isinstance(data, dict) else None
    return found if isinstance(found, dict) else {}


def discover(root: Path, registered: dict) -> list[tuple[str, Path]]:
    """(name, directory) of every knowledge base under root, then of every other one registered as `kb -C DIR serve`."""
    found = {}
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if (path / "domain.yaml").exists() or (path / DB).exists():
                found[path.resolve()] = path.name
    for name, entry in sorted(registered.items()):
        args = entry.get("args") if isinstance(entry, dict) else None
        if isinstance(args, list) and "kb" in args and "-C" in args[:-1] and args[-1] == "serve":
            found.setdefault(Path(str(args[args.index("-C") + 1])).resolve(), name)
    return [(name, home) for home, name in found.items()]


def describe(name: str, home: Path, registered: dict) -> str:
    """One line on whether the knowledge base is built and registered in omp under name."""
    if not (home / DB).exists():
        built = "not built"
    else:
        try:
            rows = catalog.counts(home / DB)
            built = f"sources: {rows['documents']} · statements: {rows['statements']}"
        except sqlite3.Error:
            built = "unreadable database"
    entry = registered.get(name)
    if entry is None:
        return f"{built} · not registered"
    return f"{built} · " + ("registered" if entry == setup.mcp_entry(home) else "registered with another command")


class Shell:
    def __init__(
        self,
        cli: setup.Run,
        keys: Keys = read_key,
        ask: setup.Ask = input,
        out: TextIO = sys.stdout,
        root: Path = setup.KB_ROOT,
        config: Path = setup.OMP_MCP,
        shelf: Path | None = None,
        saved: Path = catalog.SAVED,
    ) -> None:
        self.cli, self.keys, self.ask, self.out = cli, keys, ask, out
        self.root, self.config, self.shelf, self.saved = root, config, shelf, saved

    def run(self, home: Path | None = None) -> int:
        """The first screen, or only the menu of the knowledge base in home when one is given."""
        cursor: int | None = 0
        try:
            if home is not None:
                home = home.expanduser().resolve()
                found = {path: name for name, path in discover(self.root, servers(self.config))}
                self.manage(found.get(home, home.name), home)
                return 0
            while True:
                items = self.home_items()
                cursor = choose("kb", items, self.keys, self.out, "quit", cursor)
                if cursor is None:
                    break
                items[cursor].action()
        except KeyboardInterrupt:
            pass
        return 0

    def leaf(self, label: str, detail: str, action: Callable[[], object], heading: str = "") -> Item:
        return Item(label, detail, lambda: perform(heading or label, action, self.out))

    def home_items(self) -> list[Item]:
        registered = servers(self.config)
        items = [
            Item(name, describe(name, home, registered), lambda name=name, home=home: self.manage(name, home))
            for name, home in discover(self.root, registered)
        ]
        items.append(self.leaf("+ new", f"create a knowledge base in {self.root}", self.create))
        items.append(
            Item("⇅ catalog", str(self.shelf) if self.shelf else "connect a GitHub catalog to share", self.browse)
        )
        items.append(self.leaf("age key", "show your public key, created on first use", lambda: self.cli(["keygen"])))
        items.append(self.leaf("name", "set the name shown beside your key and on reviews", self.rename))
        return items

    def manage(self, name: str, home: Path) -> None:
        cursor: int | None = 0
        while True:
            title = f"{name} · {describe(name, home, servers(self.config))}"
            items = self.base_items(name, home)
            cursor = choose(title, items, self.keys, self.out, start=cursor)
            if cursor is None:
                return
            items[cursor].action()

    def base_items(self, name: str, home: Path) -> list[Item]:
        def step(*argv: str) -> Callable[[], int]:
            return lambda: self.cli(["-C", str(home), *argv])

        items = [
            Item("check", "validate domain.yaml and sources.yaml", step("sources", "--check")),
            Item("build", "check, fetch, parse, extract, index, register, eval", lambda: self.build(name, home)),
            Item("fetch", "download new and changed sources", step("fetch")),
            Item("parse", "split the current versions into sections", step("parse")),
            Item("extract", "extract statements with Claude", step("extract")),
            Item("index", "rebuild the full-text index and embeddings", step("index")),
            Item("eval", "golden-set hit rate", step("eval")),
            Item("quality", "ingested, verified and evidence counts, trust per source", step("quality")),
            Item("register", f"add the MCP server to {self.config}", lambda: self.register(name, home)),
        ]
        if self.shelf is not None:
            shelf = self.shelf
            items.append(Item("publish", f"to {shelf}", self.publisher(shelf, name, home)))
        leaves = [self.leaf(item.label, item.detail, item.action, f"{name} > {item.label}") for item in items]
        at = [item.label for item in items].index("quality") + 1
        leaves.insert(
            at, Item("review", "vet or dispute a source, raising or lowering its trust", lambda: self.review(home))
        )
        return leaves

    def rename(self) -> int:
        answer = self.prompt("your name or acronym", age.person())
        return self.cli(["name", answer]) if answer else 0

    def review(self, home: Path) -> None:
        """The sources of a knowledge base with their trust; choosing one opens its vet, dispute and history menu."""
        cursor: int | None = 0
        while True:
            try:
                found = reviews.overview(home)
            except (ConfigError, sqlite3.Error) as exc:
                self.out.write(f"{RED}{exc}{RESET}\n")
                return
            if not found:
                self.out.write(f"{DIM}no sources in the database yet; run fetch first{RESET}\n")
                return
            items = [Item(sid, f"{t.level} · {t.reason}", partial(self.review_source, home, sid)) for sid, t in found]
            cursor = choose(f"review sources of {home.name}", items, self.keys, self.out, start=cursor)
            if cursor is None:
                return
            items[cursor].action()

    def review_source(self, home: Path, source_id: str) -> None:
        def verdict(flag: str) -> int:
            note = self.prompt("why" + (" (required)" if flag == "--dispute" else " (optional)"))
            if flag == "--dispute" and not note:
                return 0
            return self.cli(["-C", str(home), "review", source_id, flag, *(["--note", note] if note else [])])

        items = [
            self.leaf("vet", "vouch for this version: raises its trust", lambda: verdict("--vet"), f"vet {source_id}"),
            self.leaf("dispute", "mark it untrustworthy", lambda: verdict("--dispute"), f"dispute {source_id}"),
            self.leaf("history", "its trust and reviews", lambda: self.cli(["-C", str(home), "review", source_id])),
        ]
        picked = choose(f"review {source_id}", items, self.keys, self.out)
        if picked is not None:
            items[picked].action()

    def build(self, name: str, home: Path) -> int:
        return setup.setup(name, home, self.ask, self.cli, self.config)

    def register(self, name: str, home: Path) -> None:
        setup.register_omp(name, home, self.ask, self.config)

    def create(self) -> int:
        try:
            name = self.ask(f"{ACCENT}?{RESET} name {DIM}(lowercase letters, digits, hyphens; empty cancels){RESET} ")
        except EOFError:
            return 0
        name = name.strip()
        return setup.setup(name, self.root / name, self.ask, self.cli, self.config) if name else 0

    def prompt(self, question: str, default: str = "") -> str:
        """One line of input; the default for an empty answer, '' when cancelled."""
        hint = f" {DIM}({default}; empty for it){RESET}" if default else f" {DIM}(empty cancels){RESET}"
        try:
            answer = self.ask(f"{ACCENT}?{RESET} {question}{hint} ").strip()
        except EOFError:
            return ""
        return answer or default

    def browse(self) -> None:
        """The catalog menu: its knowledge bases, publishing a local one, and connecting another catalog."""
        cursor: int | None = 0
        while True:
            if self.shelf is None or not self.shelf.is_dir():
                if self.shelf is not None:
                    self.out.write(f"{RED}catalog {self.shelf} is not a directory{RESET}\n")
                self.connect()
                if self.shelf is None or not self.shelf.is_dir():
                    return
            shelf = self.shelf
            local = dict(discover(self.root, servers(self.config)))
            found = catalog.entries(shelf, self.root)
            items = [self.entry_item(shelf, entry, local.get(entry.name)) for entry in found]
            unpublished = {name: home for name, home in local.items() if name not in {e.name for e in found}}
            if unpublished:
                publish = partial(self.publish_new, shelf, unpublished)
                items.append(Item("+ publish", "share a local knowledge base", publish))
            items.append(Item("connect", "use another catalog, or forget this one", self.connect))
            cursor = choose(f"catalog {shelf}", items, self.keys, self.out, start=cursor)
            if cursor is None:
                return
            items[cursor].action()

    def entry_item(self, shelf: Path, entry: catalog.Entry, home: Path | None) -> Item:
        if entry.manifest is None:
            return Item(entry.name, str(entry.problem), lambda: None)
        manifest = entry.manifest
        if catalog.outdated(entry):
            state = f"update {entry.local} → v{manifest['version']}"
        elif entry.local.startswith("v"):
            state = f"installed {entry.local}"
        else:
            state = entry.local or "not installed"
        detail = f"v{manifest['version']} · {manifest['bundle']['size'] / 1e6:.1f} MB · {state} · "
        return Item(entry.name, detail + (manifest.get("title") or "(private)"), lambda: self.entry(shelf, entry, home))

    def entry(self, shelf: Path, entry: catalog.Entry, home: Path | None) -> None:
        """The menu of one catalog entry: install or update it, show its details and, for a local knowledge base,
        publish and share."""
        cursor: int | None = 0
        while entry.manifest is not None:
            items = self.entry_items(shelf, entry, home)
            title = f"{entry.name} v{entry.manifest['version']} · {len(catalog.recipients(shelf, entry.name))} keys"
            title += " · private" if entry.manifest.get("private") else " · public"
            if home is not None:  # DEV-NOTE: one database copy per redraw of this menu, see catalog.sync_state
                local = catalog.local_version(entry.name, home / DB)
                state = catalog.sync_state(home, entry.manifest, local)
                title += f" · {state}" if state else ""
            cursor = choose(title, items, self.keys, self.out, start=cursor)
            if cursor is None:
                return
            items[cursor].action()
            entry = next((e for e in catalog.entries(shelf, self.root) if e.name == entry.name), entry)

    def entry_items(self, shelf: Path, entry: catalog.Entry, home: Path | None) -> list[Item]:
        name, version = entry.name, (entry.manifest or {}).get("version")
        target = home or self.root / name
        local = catalog.local_version(name, target / DB)

        def install() -> int:
            index = lambda path: self.cli(["--db", str(path), "index"])  # noqa: E731
            return catalog.pull(name, shelf, target, bool(local), self.ask, index, self.config)

        items = []
        own = local == "built here" or (not local and (target / "domain.yaml").exists())
        if not own:  # pulling would replace the configuration and database this device builds and publishes
            verb = "install" if not local else ("reinstall" if local == f"v{version}" else "update")
            items.append(self.leaf(verb, f"v{version} into {target}", install, f"{verb} {name}"))
        about = "manifest details and keys"
        details = self.leaf("info", about, lambda: catalog.info(shelf, name, self.root), f"info {name}")
        if home is None:
            return [*items, details]
        private = bool((entry.manifest or {}).get("private"))
        return [
            *items,
            self.leaf("publish", f"a new version from {home}", self.publisher(shelf, name, home), f"publish {name}"),
            self.leaf("share", "add someone's age key and publish", lambda: self.share(shelf, name, home)),
            Item("revoke", "remove someone's key and publish", lambda: self.revoke(shelf, name, home)),
            self.leaf(
                "make public" if private else "make private",
                "configuration readable in the catalog" if private else "configuration only inside the bundle",
                self.publisher(shelf, name, home, private=not private),
            ),
            details,
        ]

    def publisher(
        self, shelf: Path, name: str, home: Path, add: str = "", remove: str = "", private: bool | None = None
    ) -> Callable[[], int]:
        def publish() -> int:
            if not age.identity_path().exists():  # every bundle is encrypted to its publisher, so make the key first
                self.cli(["keygen"])
            return catalog.publish(
                home, shelf, name, [add] if add else [], private, (remove,) if remove else (), age.own_label(self.ask)
            )

        return publish

    def share(self, shelf: Path, name: str, home: Path) -> int:
        key = self.prompt("age public key to add (age1...)")
        if not key:
            return 0
        owner = self.prompt("name of its owner, shown beside the key")
        return self.publisher(shelf, name, home, add=f"{key}={owner}" if owner else key)()

    def revoke(self, shelf: Path, name: str, home: Path) -> None:
        try:
            own = age.public_keys(age.load(age.identity_path()))
        except age.KeysError:
            own = []
        labels = catalog.labelled(shelf, name)
        others = [k for k in labels if k not in own]
        if not others:
            self.out.write(f"{DIM}{name} is encrypted to your own keys only{RESET}\n")
            return
        items = [
            self.leaf(
                labels[k] or f"{k[:16]}…",
                f"{k[:16]}… remove and publish" if labels[k] else "remove and publish",
                self.publisher(shelf, name, home, remove=k),
                f"revoke {labels[k] or k[:16] + '…'}",
            )
            for k in others
        ]
        picked = choose(f"revoke a key of {name}", items, self.keys, self.out)
        if picked is not None:
            items[picked].action()

    def publish_new(self, shelf: Path, unpublished: dict[str, Path]) -> None:
        items = [
            self.leaf(name, str(home), self.publisher(shelf, name, home), f"publish {name}")
            for name, home in sorted(unpublished.items())
        ]
        picked = choose(f"publish to {shelf}", items, self.keys, self.out)
        if picked is not None:
            items[picked].action()

    def connect(self) -> None:
        """Connect a catalog: clone or create a GitHub repository, or use a folder; or forget the current one."""
        items = [
            self.leaf("clone", "an existing GitHub catalog (owner/name)", lambda: self.clone(create=False)),
            self.leaf("create", "a new private GitHub repository as the catalog", lambda: self.clone(create=True)),
            self.leaf("folder", "a local or synced folder", self.use_folder),
        ]
        if self.shelf is not None:
            items.append(self.leaf("forget", f"stop using {self.shelf}", self.forget))
        picked = choose("connect a catalog", items, self.keys, self.out)
        if picked is not None:
            items[picked].action()

    def clone(self, create: bool) -> int:
        repo = self.prompt("GitHub repository (owner/name)")
        if not repo:
            return 0
        target = Path(self.prompt("clone into", str(self.root.parent / "kb-catalog"))).expanduser().resolve()
        done = catalog.connect(repo, target, create, self.saved)
        if done == 0:
            self.shelf = target
        return done

    def use_folder(self) -> int:
        answer = self.prompt("catalog folder")
        if not answer:
            return 0
        folder = Path(answer).expanduser().resolve()
        if not folder.is_dir():
            self.out.write(f"{RED}{folder} is not a directory{RESET}\n")
            return 1
        catalog.remember(folder, self.saved)
        self.shelf = folder
        return 0

    def forget(self) -> None:
        catalog.remember(None, self.saved)
        self.shelf = catalog.location(self.saved)  # KB_CATALOG still applies


def run(cli: setup.Run, home: Path | None = None) -> int:
    """The shell on the terminal with the catalog of catalog.location(); home opens one knowledge base."""
    return Shell(cli, shelf=catalog.location()).run(home)
