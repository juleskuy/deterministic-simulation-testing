# Shrinking: from a failing seed to a minimal replay

A failing seed proves a bug exists. A minimal replay explains it. The gap between
those two is usually the difference between a bug that gets fixed this week and one
that sits in the backlog for a year labelled "flaky".

## What is being minimised

Not the seed. Seeds are opaque: seed 71 and seed 72 have no relationship, so there is
no gradient to descend and nothing to bisect.

What gets minimised is the **fault journal**: the set of faults that actually fired.
`Faults` records only positive decisions, which makes the journal a set of things
that went wrong, and a set is the right object for delta debugging.

From `demo_bug.py`, actual output:

```
seed 71: invariant violated at t=11265us: acked k0=v0 by n0, n0 is down, no durable copy anywhere
6 faults fired

shrunk 6 faults -> 3
  ('crash', 'n0', 2)
  ('drop', 2)
  ('drop', 3)
```

Three faults, and now the bug explains itself: both replication messages for `k0` were
dropped, the leader acknowledged anyway, then the leader crashed. The acknowledged
write existed only in a volatile page cache. Reading those three lines is faster than
reading the code.

## ddmin

`shrink()` implements delta debugging, from Zeller and Hildebrandt's "Simplifying and
Isolating Failure-Inducing Input".

1. Split the journal into `n` chunks, starting at `n=2`.
2. For each chunk, try the journal WITHOUT it. If the failure persists, keep the
   reduced journal and reset `n=2`.
3. If no chunk can be removed, double `n` for finer granularity.
4. Stop when `n` exceeds the journal length.

Complexity is O(k log n) test runs for a journal of n faults with k that are
load-bearing. In practice it converges in well under a second, because each run is a
simulation rather than a real deployment. This is a second-order benefit of DST that
is easy to overlook: minimisation is only affordable because reproduction is
instant.

## Re-verification is mandatory, and it must hold the signature

The critical detail, and the thing that separates a shrinker you can trust from one
that reports fiction:

**Every candidate journal is RE-RUN and RE-VERIFIED. Never assume a smaller journal
still fails.**

This is not defensive programming. Removing a fault changes which messages exist,
which changes timestamps, which changes the entire downstream execution. A smaller
journal is a different universe, not a subset of the same one. It may fail for a
different reason, or not fail at all.

"Or it may fail for a different reason" is the trap, and re-verification alone does
not close it. A shrinker that accepts ANY failure will happily descend into a
different, easier-to-trigger bug and then report that small journal as the minimum
for the bug you were investigating. Everything looks right: the journal reproduces
a failure, the failure is real, the journal is small. It is still a false report,
and the fix you derive from it will not touch the original bug.

So `shrink()` computes a **signature** for the original error - the message with
numbers erased, since timestamps, ids, and counts vary between runs of the same bug
- and accepts a candidate only if its signature matches. `signature()` is exported
so you can check it yourself:

```python
signature("invariant violated at t=11265us: acked k0=v0 by n0, no durable copy")
# 'invariant violated at t=Nus: acked kN=vN by nN, no durable copy'
```

With both properties in place the claim is precise: **the reported minimal journal
reproduces the SAME failure**, because it was observed doing so. `test_sim.py`
builds a world with two distinct bugs and asserts the shrinker does not slide
between them.

### The empty journal must be tested explicitly

ddmin splits a journal into chunks and removes one chunk at a time, so it can never
propose the empty set. If the failure needs no faults at all - a plain happy-path
bug that a fault-laden seed happened to surface - ddmin bottoms out at one
arbitrary fault and reports it as load-bearing. The reader concludes the bug
requires a dropped message. It does not.

`shrink()` therefore tests `[]` before entering the ddmin loop. This is the same
class of dishonesty as a non-reproducing minimum, in the opposite direction, and it
is easy to ship without noticing.

## Honest non-reproduction

`shrink()` first checks that the journal alone reproduces the failure. If it does not,
it returns the original journal and the message `journal did not reproduce; report
the seed instead`.

This happens legitimately. Some failures depend on the random stream itself, not only
on the faults: a specific latency ordering with no fault involved at all. Journal
replay reproduces fault decisions, not the base random draws that were consumed by
`rng` inside the generating run.

The honest response is to say so. `test_shrink_is_honest_about_non_reproduction`
asserts this behavior, because the tempting alternative, returning a plausible-looking
minimum, produces a report nobody can act on.

If you need journal replay to cover base draws as well, record the full draw sequence
alongside the journal and replay from that. It is a larger artifact and it does not
shrink as cleanly, which is why the fault journal is the default.

## After shrinking: the permanent regression test

The minimal journal plus seed is a few hundred bytes and reproduces in milliseconds,
on any machine, forever. Commit it.

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "scripts"))

from demo_bug import UNTIL, make_world, verify
from sim import Faults, InvariantViolation, Sim


def _run(buggy: bool, seed: int, journal: list[tuple]):
    sim = Sim(seed, faults=Faults(journal=journal))
    try:
        client = make_world(buggy)(sim)
        sim.run(UNTIL)
    except InvariantViolation as exc:
        return f"{exc}"
    return verify(sim, client)


def test_lost_write_under_batch_commit_regression():
    """Was: acked on batch entry, before fsync. Found by DST, seed 71.

    Minimal: both replication messages for k0 dropped, then leader crashed
    with the batch still in the page cache.

    BOTH sides are asserted. A regression test that only checks the fixed
    design keeps passing if the bug is reintroduced anywhere the fix does not
    cover, which is the failure mode regression tests exist to prevent.
    """
    seed = 71
    journal = [("crash", "n0", 2), ("drop", 2), ("drop", 3)]

    assert _run(buggy=False, seed=seed, journal=journal) is None, "the fix regressed"

    still_broken = _run(buggy=True, seed=seed, journal=journal)
    assert still_broken and "durable" in still_broken, (
        "the replay no longer reproduces the original bug, so it guards nothing"
    )
```

Four properties that make this better than the average regression test:

- **It is fast.** No containers, no network, no sleeps. Milliseconds.
- **It cannot become flaky.** It is deterministic by construction.
- **It documents the bug.** The journal names the exact conditions in three lines.
- **It cannot silently stop guarding.** The second assertion fails if the replay
  drifts to a state where the buggy design no longer reproduces, which is how
  one-sided regression tests quietly become no-ops.

Note the seed and journal in that snippet are illustrative. Use the ones your own
run reports, since they depend on the fault stream, and a transcribed journal from
someone else's run will not reproduce.

Keep the seed sweep in CI as well, but as a separate, longer job. The regression test
guards the specific bug; the sweep hunts for the next one. Growing the seed range over
time, or seeding from the commit hash, means CI explores new universes continuously
rather than re-running the same 200 forever.

## Making failures more shrinkable

Design choices that pay off later:

**Semantic fault keys.** Covered in `fault-models.md`. Time-based keys do not survive
a single removal, so shrinking degrades to noise.

**Small workloads.** Six writes in `demo_bug.py`, not six hundred. Fewer operations
means fewer faults means a shorter journal. Scale up only when small workloads stop
finding new bugs.

**Unique values per operation.** `k0=v0`, `k1=v1`. If every write stored the same
value, a lost write could hide behind a legal one and the invariant would miss it.

**One invariant per promise.** A composite invariant that checks five things reports
"something is wrong". Five separate invariants report which promise was broken, and
that narrows the fix immediately.

**Deterministic ordering in your own data structures.** If the system under test
iterates a set, the shrunk journal will not reproduce reliably and you will blame the
shrinker.

## Shrinking the workload too

The journal is the first thing to minimise because it is cheap. Two more axes are
worth trying when the journal alone is still hard to read:

**Operation count.** Bisect the number of client operations. A bug reproducible with
two writes instead of six is dramatically easier to reason about.

**Simulated time.** Reduce `until` to just past the violation timestamp. Everything
after the violation is noise in the trace.

Both need the same re-verification discipline as the journal. Shrink one axis at a
time, and re-run ddmin on the journal after reducing another axis, since a shorter
workload often makes more faults removable.
