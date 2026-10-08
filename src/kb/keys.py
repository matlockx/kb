"""age keys: the identity that opens published knowledge bases on this device, and the recipients of a bundle.

The identity file has the format of age-keygen: comment lines and AGE-SECRET-KEY-1... lines. Recipients are
age public keys (age1...), one per line, with an optional label after a #.
"""

import getpass
import os
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pyrage

IDENTITY_ENV = "KB_AGE_IDENTITY"
DEFAULT_IDENTITY = Path.home() / ".config" / "kb" / "identity.txt"
NAME_FILE = Path.home() / ".config" / "kb" / "name"  # the owner's name, shown beside the key and on reviews


class KeysError(Exception):
    pass


def identity_path() -> Path:
    return Path(os.environ.get(IDENTITY_ENV) or DEFAULT_IDENTITY).expanduser()


def load(path: Path) -> list[pyrage.x25519.Identity]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise KeysError(f"no age identity at {path}; create one with `kb keygen` (or set ${IDENTITY_ENV})") from exc
    except OSError as exc:
        raise KeysError(f"{path}: {exc.strerror}") from exc
    identities = []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("AGE-SECRET-KEY-"):
            try:
                identities.append(pyrage.x25519.Identity.from_str(line.strip()))
            except pyrage.IdentityError as exc:
                raise KeysError(f"{path}:{number}: not a valid age identity") from exc  # never echo the secret
    if not identities:
        raise KeysError(f"{path} holds no AGE-SECRET-KEY line")
    return identities


def public_keys(identities: list[pyrage.x25519.Identity]) -> list[str]:
    return [str(identity.to_public()) for identity in identities]


def generate(path: Path) -> str:
    """Create a new identity file readable only by its owner; returns its public key. Never overwrites."""
    identity = pyrage.x25519.Identity.generate()
    public = str(identity.to_public())
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    created = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(f"# created: {created}\n# public key: {public}\n{identity}\n")
    return public


def clean(label: str) -> str:
    """label on one line with single spaces, so it fits behind a # in recipients.txt."""
    return " ".join(label.split())


def parse_labelled(text: str, origin: str) -> dict[str, str]:
    """The public keys in a recipients text, in order and each once, with the label behind their # ('' without one);
    raise KeysError naming every bad line."""
    found: dict[str, str] = {}
    problems = []
    for number, line in enumerate(text.splitlines(), start=1):
        key, _, comment = line.partition("#")
        key = key.strip()
        if not key:
            continue
        try:
            pyrage.x25519.Recipient.from_str(key)
        except pyrage.RecipientError:
            problems.append(f"{origin}:{number}: {key!r} is not an age public key (age1...)")
            continue
        found[key] = found.get(key) or clean(comment)
    if problems:
        raise KeysError("\n".join(problems))
    return found


def parse_recipients(text: str, origin: str) -> list[str]:
    """The public keys in a recipients text, in order and each once; raise KeysError naming every bad line."""
    return list(parse_labelled(text, origin))


def split_recipient(value: str) -> tuple[str, str]:
    """(key, label) of a --recipient value, written KEY or KEY=LABEL."""
    key, _, label = value.partition("=")
    return key.strip(), clean(label)


def person(ask: Callable[[str], str] | None = None, path: Path | None = None) -> str:
    """The name of this device's owner: the saved one; else the answer to ask, which is then saved; else the
    operating system user, which is not saved. ask None never prompts."""
    path = path or NAME_FILE
    try:
        saved = clean(path.read_text(encoding="utf-8"))
    except OSError:
        saved = ""
    if saved or ask is None:
        return saved or getpass.getuser()
    try:
        answer = clean(ask("your name, shown beside your key and on reviews (empty uses your login) "))
    except EOFError:
        answer = ""
    if not answer:
        return getpass.getuser()
    save_name(answer, path)
    return answer


def save_name(name: str, path: Path | None = None) -> None:
    path = path or NAME_FILE
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(clean(name) + "\n", encoding="utf-8")


def own_label(ask: Callable[[str], str] | None = None, path: Path | None = None) -> str:
    """'NAME (HOST)': the owner's name and this device, as the label of this device's key in recipients.txt."""
    return f"{person(ask, path)} ({socket.gethostname()})"


def recipients(keys: list[str]) -> list[pyrage.x25519.Recipient]:
    return [pyrage.x25519.Recipient.from_str(key) for key in keys]
