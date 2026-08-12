## Why

<!-- Problem, constraint, or invariant this changes. Link the issue: Fixes #123. -->

## What changed

<!-- Smallest behavior change. Name every changed surface. -->

## Determinism impact

<!-- Required for sim.py, demo_bug.py, or scanner changes. Write N/A only for docs-only changes. -->

- [ ] Rule 1: no new unseeded nondeterminism
- [ ] Rule 2: fixed draw count within each event
- [ ] Rule 3: fault keys are semantic and shrink-stable
- [ ] N/A — no simulation or scanner behavior changed

## Verification

<!-- Paste actual commands and outcomes. Do not write “tests pass” without evidence. -->

```text
python scripts/test_sim.py
python scripts/test_scan.py
python scripts/test_docs.py
python scripts/scan_nondeterminism.py scripts/sim.py scripts/demo_bug.py
python scripts/demo_bug.py
```

## Checklist

- [ ] No new dependency.
- [ ] Every changed simulator behavior has a matching assertion.
- [ ] Every changed scanner rule has positive and negative fixtures.
- [ ] Reference snippets still execute (`test_docs.py`).
- [ ] No stale simulator output was pasted into SKILL.md or a docstring.
- [ ] Changelog updated when this changes user-visible behavior.
- [ ] Contributor terms in CODE_OF_CONDUCT.md and CONTRIBUTING.md were followed.
