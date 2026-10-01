from pathlib import Path

import pytest
import yaml

from kb import domain, sources
from kb.domain import ConfigError, Modality, Topic

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "web-principles"

VALID = {
    "name": "Running",
    "instructions": "Answer questions\n  about running.",
    "doc_types": ["rule", "guide"],
    "tags": ["marathon", "trail-run"],
    "modalities": [{"id": "must", "description": "Required."}, {"id": "may", "description": "Allowed."}],
    "topics": [
        {"id": "pacing", "label": "Pacing", "description": "Speed."},
        {"id": "kit", "label": "Kit", "description": "Gear."},
    ],
}


def write(tmp_path: Path, raw: object) -> Path:
    path = tmp_path / "domain.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def errors_for(tmp_path: Path, raw: object) -> str:
    with pytest.raises(ConfigError) as exc:
        domain.load(write(tmp_path, raw))
    return str(exc.value)


def test_valid_domain_loads_in_order(tmp_path: Path) -> None:
    loaded = domain.load(write(tmp_path, VALID))
    assert loaded.name == "Running"
    assert loaded.instructions == "Answer questions about running."
    assert loaded.doc_types == ("rule", "guide")
    assert loaded.tags == ("marathon", "trail-run")
    assert loaded.modalities == (Modality("must", "Required."), Modality("may", "Allowed."))
    assert loaded.topics == (Topic("pacing", "Pacing", "Speed."), Topic("kit", "Kit", "Gear."))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"extra": 1}, "unknown field 'extra'"),
        ({"name": None}, "name must be a non-empty string"),
        ({"name": "  "}, "name must be a non-empty string"),
        ({"instructions": ""}, "instructions must be a non-empty string"),
        ({"doc_types": []}, "doc_types must be a non-empty list of strings"),
        ({"tags": []}, "tags must be a non-empty list of strings"),
        ({"doc_types": ["rule", "rule"]}, "doc_types contains duplicates"),
        ({"tags": ["a", "a"]}, "tags contains duplicates"),
        ({"tags": ["Trail Run"]}, "tags: 'Trail Run' must be lowercase"),
        ({"modalities": [{"id": "must"}]}, "modalities entry 1: needs exactly id, description"),
        ({"modalities": [{"id": "must", "description": "x", "x": 1}]}, "modalities entry 1: needs exactly"),
        ({"topics": [{"id": "kit", "label": "Kit"}]}, "topics entry 1: needs exactly id, label, description"),
        ({"topics": [{"id": "Kit", "label": "Kit", "description": "x"}]}, "topics entry 1: id must be lowercase"),
        ({"topics": [*VALID["topics"], VALID["topics"][1]]}, "topics: duplicate id 'kit'"),
        ({"modalities": []}, "modalities must be a non-empty list"),
    ],
)
def test_domain_rejected(tmp_path: Path, change: dict[str, object], message: str) -> None:
    assert message in errors_for(tmp_path, {**VALID, **change})


def test_missing_name_rejected(tmp_path: Path) -> None:
    raw = {k: v for k, v in VALID.items() if k != "name"}
    assert "name must be a non-empty string" in errors_for(tmp_path, raw)


def test_all_problems_reported(tmp_path: Path) -> None:
    message = errors_for(tmp_path, {**VALID, "extra": 1, "name": "", "tags": ["a", "a"], "topics": []})
    for expected in ("unknown field 'extra'", "name must be", "tags contains duplicates", "topics must be"):
        assert expected in message


@pytest.mark.parametrize("content", ["", "- a list", "[unclosed"])
def test_malformed_file_rejected(tmp_path: Path, content: str) -> None:
    path = tmp_path / "domain.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        domain.load(path)


def test_repository_example_is_valid() -> None:
    assert sources.load(EXAMPLE / "sources.yaml", domain.load(EXAMPLE / "domain.yaml"))
