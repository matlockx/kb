from pathlib import Path

import pytest

from kb import keys

KEY = "age1vd7flhrjpm5g34gwajfeen208f7fdp05ryv45tzlcwmegx329dtqy2770w"
OTHER = "age172al35nhtau5h5v25encg0266x2cz4w0jgkvfx8saesdj8qg3s5qcq3xfw"


def test_labels_follow_the_hash_and_the_first_one_wins() -> None:
    text = f"# header\n{KEY}  # Martin   (macbook)\n{OTHER}\n{KEY} # ignored duplicate\n\n"
    assert keys.parse_labelled(text, "r") == {KEY: "Martin (macbook)", OTHER: ""}
    assert keys.parse_recipients(text, "r") == [KEY, OTHER]


def test_a_label_does_not_hide_a_bad_key() -> None:
    with pytest.raises(keys.KeysError, match=r"r:2: 'age1nope' is not an age public key"):
        keys.parse_labelled(f"{KEY} # fine\nage1nope # Nobody\n", "r")


def test_a_recipient_is_a_key_with_an_optional_name() -> None:
    assert keys.split_recipient(f"{KEY}=Ana  Smith") == (KEY, "Ana Smith")
    assert keys.split_recipient(f" {KEY} ") == (KEY, "")


def test_person_prefers_the_saved_name_then_asks_once_then_falls_back_to_the_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "name"
    monkeypatch.setattr(keys.getpass, "getuser", lambda: "login")
    assert keys.person(None, path) == "login"
    assert not path.exists()  # not asking never saves

    def closed(_: str) -> str:
        raise EOFError

    assert keys.person(closed, path) == "login"
    assert keys.person(lambda _: "  ", path) == "login"
    assert not path.exists()  # neither an empty nor a closed answer is saved

    assert keys.person(lambda _: " Martin\tJ ", path) == "Martin J"
    assert path.read_text(encoding="utf-8") == "Martin J\n"
    assert keys.person(lambda _: pytest.fail("asked twice"), path) == "Martin J"


def test_own_label_names_the_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keys.socket, "gethostname", lambda: "macbook")
    path = tmp_path / "name"
    keys.save_name("Martin", path)
    assert keys.own_label(None, path) == "Martin (macbook)"


def test_cli_name_shows_and_saves_the_owner_name(owner_name: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from kb.cli import main

    assert main(["name"]) == 0
    assert capsys.readouterr().out == "Tester\n"
    assert main(["name", "Martin   J"]) == 0
    assert owner_name.read_text(encoding="utf-8") == "Martin J\n"
    owner_name.unlink()
    assert main(["name"]) == 0
    assert "no name saved; set one with `kb name NAME`" in capsys.readouterr().out
