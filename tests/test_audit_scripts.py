import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "audit_structure", Path(__file__).parent.parent / "scripts" / "audit_structure.py"
)
assert SPEC and SPEC.loader
audit_structure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_structure)


def test_number_gaps_counts_missing_plain_numbers_and_ignores_letters_and_big_jumps() -> None:
    refs = ["(preamble)", "s. 1", "s. 2A", "Article 5", "§ 6", "Schedule 1", "40"]
    # 3 and 4 missing between 2 and 5; the jump from 6 to 40 is over 20 and not a gap.
    assert audit_structure.number_gaps(refs) == 2
    assert audit_structure.number_gaps(["1", "1A", "2"]) == 0


def test_toc_like_flags_table_of_contents_but_not_body_text() -> None:
    toc = "\n".join(f"{n} Chapter title {n} ........ {n * 3}" for n in range(1, 7))
    assert audit_structure.toc_like(toc)
    assert not audit_structure.toc_like("1 Short\n2 Lines\n3 only\n4 four")  # fewer than five lines
    body = "\n".join("The operator shall keep records of every transaction." for _ in range(6))
    assert not audit_structure.toc_like(body)
