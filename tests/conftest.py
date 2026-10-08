from pathlib import Path

import pytest

from kb import keys


@pytest.fixture(autouse=True)
def owner_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The saved name of this device's owner, outside the real home, so no test prompts for or writes it there."""
    path = tmp_path / "home" / "name"
    path.parent.mkdir()
    path.write_text("Tester\n", encoding="utf-8")
    monkeypatch.setattr(keys, "NAME_FILE", path)
    return path
