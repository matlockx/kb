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
from pathlib import Path
from typing import TextIO

from kb import catalog, setup
from kb.db import DEFAULT_PATH as DB

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
    ) -> None:
        self.cli, self.keys, self.ask, self.out = cli, keys, ask, out
        self.root, self.config, self.shelf = root, config, shelf

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
        if self.shelf is not None:
            shelf = self.shelf
            items.append(Item("↓ pull", f"install or update one from {shelf}", lambda: self.pull(shelf)))
        items.append(self.leaf("age key", "show your public key, created on first use", lambda: self.cli(["keygen"])))
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
            Item("register", f"add the MCP server to {self.config}", lambda: self.register(name, home)),
        ]
        if self.shelf is not None:
            shelf = self.shelf
            items.append(Item("publish", f"to {shelf}", lambda: catalog.publish(home, shelf, name, [], None)))
        return [self.leaf(item.label, item.detail, item.action, f"{name} > {item.label}") for item in items]

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

    def pull(self, shelf: Path) -> None:
        items = []
        for name in catalog.names(shelf):
            home = self.root / name
            installed = (home / "domain.yaml").exists()

            def install(name: str = name, home: Path = home, installed: bool = installed) -> int:
                index = lambda path: self.cli(["--db", str(path), "index"])  # noqa: E731
                return catalog.pull(name, shelf, home, installed, self.ask, index, self.config)

            items.append(self.leaf(name, "installed; updates it" if installed else "", install, f"pull {name}"))
        if not items:
            self.out.write(f"{DIM}no knowledge bases in {shelf}{RESET}\n")
            return
        picked = choose(f"catalog {shelf}", items, self.keys, self.out)
        if picked is not None:
            items[picked].action()


def run(cli: setup.Run, home: Path | None = None) -> int:
    """The shell on the terminal, with KB_CATALOG as the catalog when it is set; home opens one knowledge base."""
    shelf = os.environ.get("KB_CATALOG")
    return Shell(cli, shelf=Path(shelf).expanduser().resolve() if shelf else None).run(home)
