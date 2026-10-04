"""age keys: the identity that opens published knowledge bases on this device, and the recipients of a bundle.

The identity file has the format of age-keygen: comment lines and AGE-SECRET-KEY-1... lines. Recipients are
age public keys (age1...), one per line, # comments allowed.
"""

import os
from datetime import UTC, datetime
from pathlib import Path

import pyrage

IDENTITY_ENV = "KB_AGE_IDENTITY"
DEFAULT_IDENTITY = Path.home() / ".config" / "kb" / "identity.txt"


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


def parse_recipients(text: str, origin: str) -> list[str]:
    """The public keys in a recipients text, in order and each once; raise KeysError naming every bad line."""
    keys, problems = [], []
    for number, line in enumerate(text.splitlines(), start=1):
        key = line.split("#", 1)[0].strip()
        if not key:
            continue
        try:
            pyrage.x25519.Recipient.from_str(key)
        except pyrage.RecipientError:
            problems.append(f"{origin}:{number}: {key!r} is not an age public key (age1...)")
            continue
        if key not in keys:
            keys.append(key)
    if problems:
        raise KeysError("\n".join(problems))
    return keys


def recipients(keys: list[str]) -> list[pyrage.x25519.Recipient]:
    return [pyrage.x25519.Recipient.from_str(key) for key in keys]
