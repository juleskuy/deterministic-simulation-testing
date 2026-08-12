"""Verify the illustrative snippets in references/*.md actually run.

A skill that ships code in prose ships two artifacts: the working scripts and the
snippets people copy. The snippets rot silently, because nothing executes them.
This file executes them.

Run: python test_docs.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

REFS = Path(__file__).parent.parent / "references"
SKILL = Path(__file__).parent.parent / "SKILL.md"


def python_blocks(md: Path) -> list[str]:
    return re.findall(r"```python\n(.*?)```", md.read_text(encoding="utf-8"), re.S)


def test_shrinking_regression_snippet_runs():
    """The regression-test recipe must be runnable, not just plausible.

    It is the one snippet readers copy verbatim into their own repo, so an
    import it forgot or a name it never defined costs every reader the same
    ten minutes.
    """
    blocks = python_blocks(REFS / "shrinking.md")
    target = [b for b in blocks if "def test_lost_write" in b]
    assert target, "the regression snippet disappeared from shrinking.md"
    code = target[0]

    # The snippet resolves `scripts/` relative to its own __file__, which in the
    # doc means the repo root. Emulate that.
    ns = {"__file__": str(Path(__file__).parent.parent / "placeholder.py")}
    exec(compile(code, "shrinking.md:snippet", "exec"), ns)

    fn = ns["test_lost_write_under_batch_commit_regression"]
    fn()  # raises if either side of the assertion fails


def test_signature_snippet_matches_reality():
    """The documented `signature()` output must be what the code produces."""
    from sim import signature

    blocks = python_blocks(REFS / "shrinking.md")
    target = [b for b in blocks if "signature(" in b and "#" in b]
    assert target, "the signature example disappeared"
    call = re.search(r'signature\((".*?")\)', target[0])
    shown = re.search(r"#\s*'(.*)'", target[0])
    assert call and shown, f"could not parse the example: {target[0]!r}"
    actual = signature(eval(call.group(1)))  # noqa: S307 - a literal from our own docs
    assert actual == shown.group(1), f"docs say {shown.group(1)!r}, code gives {actual!r}"


def test_documented_probs_match_the_code():
    """DEFAULT_PROBS/HARSH_PROBS appear in prose; prose must not drift."""
    from sim import DEFAULT_PROBS, HARSH_PROBS

    text = (REFS / "fault-models.md").read_text(encoding="utf-8") + SKILL.read_text(encoding="utf-8")
    for name, probs in (("DEFAULT_PROBS", DEFAULT_PROBS), ("HARSH_PROBS", HARSH_PROBS)):
        for kind, p in probs.items():
            pair = f'"{kind}": {p}'
            assert pair in text, f"{name} value {pair} is not documented anywhere"


def test_referenced_files_exist():
    """Every references/ path named in SKILL.md must resolve."""
    text = SKILL.read_text(encoding="utf-8")
    for rel in sorted(set(re.findall(r"`(references/[\w./-]+|scripts/[\w./-]+)`", text))):
        assert (SKILL.parent / rel).exists(), f"SKILL.md points at missing {rel}"


def test_skill_frontmatter_is_valid():
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n"), "frontmatter must start at byte 0"
    end = text.index("\n---\n", 3)
    fm = text[4:end]
    assert re.search(r"^name: [a-z0-9-]+$", fm, re.M), "name missing or not slug-cased"
    desc = re.search(r"^description: (.+)$", fm, re.M)
    assert desc, "description missing"
    assert len(desc.group(1)) <= 1024, f"description is {len(desc.group(1))} chars, spec ceiling is 1024"
    assert text[end + 5 :].strip(), "body is empty"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok   {t.__name__}")
    print(f"\n{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
