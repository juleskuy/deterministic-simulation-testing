# Contributing to deterministic-simulation-testing

Thanks for contributing. This repository includes runnable code and documentation
that describes its behavior. Changes should include evidence for the behavior they
introduce or alter.

## Before you write anything

1. **Open an issue first** for anything beyond a typo or a docs fix. DST has subtle correctness properties (Rules 1–3, shrink honesty, determinism) and a change that looks right can silently break one. An issue lets us agree on the approach before you spend time.
2. **Read `SKILL.md`** — specifically the three rules and the workflow. If your change touches the simulator, it must not violate any of them.

## What contributions are wanted

- **New fault types** for `sim.py` (asymmetric partition, torn write, fsync-EIO, clock skew). Each must carry a test in `test_sim.py` proving it fires and that journal replay reproduces it.
- **New scanner rules** for `scan_nondeterminism.py`. Each must carry a positive fixture (it fires) and a negative fixture (the safe pattern stays quiet) in `test_scan.py`.
- **New language support** for the scanner. Cover every HIGH rule with at least one fixture, following the existing `POSITIVE` / `NEGATIVE` pattern in `test_scan.py`.
- **Bug reports** with a minimal reproducer (seed + journal). The best bug report is a failing test.
- **Documentation corrections** with evidence. If a claim in a reference file is wrong, cite the code line that contradicts it.

## The three gates every PR must pass

CI runs these, but run them locally first:

```bash
python scripts/test_sim.py            # 16 assertions: determinism, shrink honesty, crash-freedom
python scripts/test_scan.py           # 12 assertions: scanner coverage, false-positive suppression
python scripts/test_docs.py           # 5 assertions: reference snippets run, constants match code
python scripts/scan_nondeterminism.py scripts/sim.py scripts/demo_bug.py   # 0 HIGH
python scripts/demo_bug.py            # finds both bugs, clears the fix over 10,000 seeds
```

Additionally:

- The suite must pass under `PYTHONHASHSEED=0` and `PYTHONHASHSEED=12345` in **separate processes**. Hash-seed hazards hide inside one process.
- `scripts/sim.py` and `scripts/demo_bug.py` must have **zero HIGH findings** from the scanner. `test_scan.py` and `test_sim.py` are excluded: their fixtures ARE hazard strings.

## Rules for changes to `sim.py`

`sim.py` is load-bearing. Every property it guarantees has an assertion guarding it. If you change behavior:

1. **State which rule or property your change touches.** "This adds asymmetric partition support" is fine. "This cleans up the send function" is not — `send` has three draws taken before an early return for a reason, and that reason is Rule 2.
2. **Add or update the assertion** that guards the property you changed. A behavior change without a test change is a review block.
3. **Do not add dependencies.** The project uses the Python standard library only.

## Rules for changes to the scanner

The scanner is deliberately noisy in the honest direction: false negatives (missed hazards) are worse than false positives (a human spends a minute). If you add a rule:

- Give it a severity (`high` / `med` / `low`) and a one-line fix.
- Add a positive fixture to `POSITIVE` in `test_scan.py` so it is exercised.
- Check it does not false-positive on the `NEGATIVE` fixtures.
- If the rule has a known blind spot (like bare `for x in items:`), say so in the output, not just the docs.

## Rules for changes to docs

`references/*.md` contains runnable code snippets. `test_docs.py` executes them. If you change a snippet:

- It must still run. `test_docs.py` will fail the build if it does not.
- Documented constants (like `DEFAULT_PROBS`) are diffed against the code. Update both or neither.
- Do not paste simulator output into `SKILL.md` or docstrings. It becomes stale
  when the fault model changes. The README has one captured run; CI runs the demo.

## Commit style

- Conventional subject line: `fix:`, `feat:`, `docs:`, `test:`, `refactor:`.
- Body explains WHY, not what. The diff shows what.
- If you found a defect by running the skill against itself (the TDD approach in `writing-skills`), say so in the body and link the issue.

## Changes outside scope

- A new dependency. Stdlib only.
- A behavior change to `sim.py` without a corresponding test.
- A scanner rule that false-positives on the documented safe patterns.
- A documentation claim that `test_docs.py` cannot verify.
- Pasted simulator output in docs (it will rot and cannot be CI-checked).
