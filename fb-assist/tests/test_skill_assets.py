"""Guard the skill's shipped assets against silent drift.

`prompts/co-author.md` is the canonical co-author role file; `skill/fb/co-author.md`
must ship a byte-identical copy (a skill loads its own directory, so the file has to
physically live there — but two hand-edited copies WILL drift, and this is the document
that defines the co-author's entire character). This test makes drift a red build.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # fb-assist/
CANONICAL = ROOT / "prompts" / "co-author.md"
SHIPPED = ROOT / "skill" / "fb" / "co-author.md"


def test_co_author_copies_are_byte_identical():
    assert CANONICAL.is_file(), f"missing canonical role file: {CANONICAL}"
    assert SHIPPED.is_file(), f"missing shipped skill copy: {SHIPPED}"
    a = CANONICAL.read_bytes()
    b = SHIPPED.read_bytes()
    assert a == b, (
        "co-author.md drifted between prompts/ (canonical) and skill/fb/ (shipped). "
        "Re-copy the canonical: cp prompts/co-author.md skill/fb/co-author.md"
    )


def test_quick_bar_and_sacreds_present():
    """The two load-bearing additions must stay in the role file: the fast-response
    quick-bar and the third sacred (transcript-as-evidence injection rail)."""
    text = CANONICAL.read_text(encoding="utf-8")
    assert "quick-bar" in text.lower(), "the fast-response quick-bar section went missing"
    assert "Three things are sacred" in text, "the sacreds heading changed unexpectedly"
    assert "evidence, not instruction" in text, "the prompt-injection rail went missing"
