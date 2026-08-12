# Changelog

## 1.0.0 — 2026-08-12

First release.

Nine defects found by testing the skill the way it asks you to test systems: an
agent was given a durability-bug task with the skill and without it, and its
critique drove the fixes. Every fix carries an assertion, so the same claim
cannot rot again silently.

### Fixed — prose that asserted what the code did not do

- **Rule 2 was false as written.** It claimed the whole random stream is
  config-invariant. `Sim.send` returned early on drop, so a dropped send consumed
  one fault draw where a delivered send consumed three (measured: 90 draws at
  `probs={}` vs 74 at `drop=0.3`). All three fault decisions are now taken before
  the early return, and the rule is restated per-event. The run total is NOT
  config-invariant and the docs now say so, because that is why Rule 3 exists.
- **Rule 2's guard could not see the violation.** `test_probability_does_not_shift_the_stream`
  compared send timestamps, which only exercises `Sim.rng`. Added
  `test_fault_stream_does_not_shift_with_config`, which counts `Faults.draws`
  per event.
- **`shrink()` could report a different bug's journal as the minimum.** It
  accepted any truthy error, so ddmin could slide onto an easier failure. Added
  `signature()` (numbers erased) and candidates must match the original.
- **`shrink()` could not return the empty journal.** `if not candidate: continue`
  made it unreachable, so a fault-independent failure was reported as requiring a
  fault. `[]` is now tested explicitly before the ddmin loop.

### Fixed — workflow

- **Steps 3 and 4 contradicted each other.** "Write invariants first" plus "stop
  if zero faults fail" means the strongest honest invariant ends the workflow at
  step 4. Step 4 is now a triage table, and weakening an invariant to manufacture
  a DST target is called out as the failure mode it is.
- **Three disagreeing starting probabilities** across SKILL.md, `fault-models.md`,
  and `demo_bug.py`. Replaced by `DEFAULT_PROBS` and `HARSH_PROBS` in `sim.py`,
  imported everywhere. `search()` defaults to `DEFAULT_PROBS`.
- **Stale transcripts.** A docstring claimed `seed 3, shrunk 9 -> 3` while the code
  produced seed 71. Transcripts are gone from SKILL.md and the docstring;
  `test_docs.py` now executes the reference snippets and diffs documented
  constants against the code.

### Added — capability the references described but the core lacked

- `Sim(quiesce_at=T)` — faults stop firing after `T`, so liveness assertions are
  meaningful. Draws still happen (Rule 2); only the decision is suppressed, and the
  suppressed entry is removed from the journal.
- `Sim(auto_recover=D)` — a crashed node reboots, so `on_recover` actually runs.
  Previously a crashed node stayed down for the rest of the run and every recovery
  bug was invisible. `Node.recovering` is exposed for recovery-only invariants.
- **Crash-freedom.** An unexpected exception from the system under test is reported
  as a failing seed instead of killing the sweep.
- **A second bug in the fixed design.** `demo_bug.py` now shows the same write
  acknowledged twice after the durability fix clears 10,000 seeds, proving its own
  limit rather than claiming correctness.
- **Seven more set-iteration patterns** in `scan_nondeterminism.py`
  (`list(set(...))`, `next(iter(...))`, comprehensions, set-operation results,
  suspicious container names), plus a printed blind spot: `for x in items:` cannot
  be judged lexically.
- `test_docs.py` — executes reference snippets, validates frontmatter, checks that
  every path SKILL.md names resolves.
- CI runs the suite under `PYTHONHASHSEED=0` and `12345` in separate processes,
  because hash-seed hazards cannot be caught inside one process.

### Counts

16 simulator assertions, 12 scanner assertions, 5 documentation assertions.
