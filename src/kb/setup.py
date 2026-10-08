"""`kb setup NAME`: create a knowledge base directory, or build and register an existing one."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

from kb import evaluate
from kb.domain import ConfigError
from kb.sources import ID_RE

ENGINE = Path(__file__).resolve().parents[2]  # the checkout whose environment serves every knowledge base
KB_ROOT = Path.home() / "kbs"
OMP_MCP = Path.home() / ".omp" / "agent" / "mcp.json"
TEMPLATE_FILES = ("domain.yaml", "sources.yaml", "prompts/extract.md", "eval/golden.yaml", "AGENTS.md")
MCP_TIMEOUT_MS = 120_000  # the first search loads the embedding model, which outlasts omp's 30 s default

Ask = Callable[[str], str]  # prompt -> answer, e.g. input
Run = Callable[[list[str]], int]  # kb CLI argv -> exit code, e.g. kb.cli.main


def setup(name: str, home: Path | None, ask: Ask, run: Run, omp_config: Path = OMP_MCP) -> int:
    """Create the knowledge base directory when it has no domain.yaml; otherwise build and register it."""
    if not ID_RE.fullmatch(name):
        print(f"name {name!r} must be lowercase letters, digits and single hyphens, e.g. running", file=sys.stderr)
        return 1
    home = (home or KB_ROOT / name).expanduser().resolve()
    if (home / "domain.yaml").exists():
        return build(name, home, ask, run, omp_config)
    create(name, home, ask)
    return 0


def create(name: str, home: Path, ask: Ask) -> None:
    """Write the template files that are missing, a .gitignore, and optionally a git repository."""
    about = _answer(ask, f"What is the {name} knowledge base about? It completes 'a knowledge base on ...' [{name}]: ")
    about = about or name  # empty or closed input
    values = {
        "<<NAME>>": json.dumps(f"{name} knowledge base"),  # a JSON string is a valid YAML scalar
        "<<INSTRUCTIONS>>": json.dumps(
            f"Holds documents on {about}: the source texts split into sections, and the statements extracted "
            "from them with verbatim quotes."
        ),
        "<<ABOUT>>": about,
    }
    template = files("kb").joinpath("template")
    for rel in TEMPLATE_FILES:
        target = home / rel
        if target.exists():
            continue
        text = template.joinpath(rel).read_text(encoding="utf-8")
        for token, value in values.items():
            text = text.replace(token, value)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    gitignore = home / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("data/\nraw/\ndownloads/\n", encoding="utf-8")
    if shutil.which("git") and not (home / ".git").exists() and _confirm(ask, f"Make {home} a git repository?"):
        subprocess.run(["git", "init", "-q", str(home)], check=False)  # noqa: S603, S607
    print(f"Created {home}.")
    print("Next: fill in domain.yaml, sources.yaml, prompts/extract.md and eval/golden.yaml there (an agent can")
    print(f"draft them from your sources), then run `setup {name}` again to build and register it.")


def build(name: str, home: Path, ask: Ask, run: Run, omp_config: Path) -> int:
    """Validate, fetch, parse, extract and index; register the MCP server; run the golden set when it has questions.

    A failing source does not stop the other steps; the exit code is 1 when any step failed.
    """
    base = ["-C", str(home)]
    if run([*base, "sources", "--check"]) != 0:
        print(f"Fix the problems above in {home}, then run `setup {name}` again.", file=sys.stderr)
        return 1
    failed = [step for step in ("fetch", "parse", "extract") if run([*base, step]) != 0]
    if run([*base, "index"]) != 0:
        print("index failed; the knowledge base is not registered", file=sys.stderr)
        return 1
    register_omp(name, home, ask, omp_config)
    try:
        evaluate.load(home / "eval" / "golden.yaml")
    except ConfigError:
        print("eval skipped: eval/golden.yaml has no valid questions yet")
    else:
        if run([*base, "eval"]) != 0:
            failed.append("eval")
    if failed:
        print(f"{name}: {', '.join(failed)} reported failures; see the output above", file=sys.stderr)
        return 1
    print(f"{name} is built. Run `setup {name}` again after editing its files; unchanged sections cost nothing.")
    return 0


def mcp_entry(home: Path) -> dict[str, object]:
    return {
        "type": "stdio",
        "command": "uv",
        "args": ["run", "--project", str(ENGINE), "--quiet", "kb", "-C", str(home), "serve"],
        "timeout": MCP_TIMEOUT_MS,
    }


def register_omp(name: str, home: Path, ask: Ask, config: Path) -> None:
    """Add or update the server `name` in an omp mcp.json after confirmation; other content is kept as it is."""
    try:
        data = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"not registered: cannot read {config}: {exc}", file=sys.stderr)
        return
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        print(f"not registered: {config} has no mcpServers mapping", file=sys.stderr)
        return
    entry = mcp_entry(home)
    if servers.get(name) == entry:
        print(f"{name} is registered in {config}")
        return
    question = (
        f"Replace the existing omp MCP server {name!r} in {config}?"
        if name in servers
        else f"Register {name!r} as an MCP server for all omp sessions ({config})?"
    )
    if not _confirm(ask, question):
        print(f"not registered; the entry for {config} would be:\n{json.dumps({name: entry}, indent=2)}")
        return
    data["mcpServers"] = {**servers, name: entry}
    _write_atomic(config, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"Registered {name} in {config}; new omp sessions get its kb_* tools.")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".part-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _answer(ask: Ask, prompt: str) -> str | None:
    """The trimmed answer; None when input is closed (not a terminal, Ctrl-D)."""
    try:
        return ask(prompt).strip()
    except EOFError:
        return None


def _confirm(ask: Ask, question: str) -> bool:
    """An empty answer accepts; closed input declines, so an unattended run never changes outside files."""
    answer = _answer(ask, f"{question} [Y/n] ")
    return answer is not None and answer.lower() in {"", "y", "yes"}
