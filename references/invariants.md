# Invariants: catching bugs instead of restating the implementation

The fault model decides which universes you visit. The invariants decide whether you
notice anything wrong when you get there. Weak invariants are the most common reason
a DST effort runs 100,000 seeds and reports nothing.

## The test that a good invariant passes

**An invariant must be derived from what the system PROMISES, not from what the code
DOES.**

```python
# Worthless. Restates the implementation, so it holds by construction and can
# never fail. This is what an LLM writes when asked to add assertions.
assert leader.batch == leader.batch

# Worthless. Asserts a mechanism, not a promise. Passes while data is lost.
assert leader.disk.fsyncs > 0

# Real. Derived from the promise made to the client, and it can fail.
for key, value, by in client.acked:
    if not sim.nodes[by].up:
        assert any(n.disk.durable.get(key) == value for n in sim.nodes.values())
```

Practical way to find real invariants: read the system's documentation, its API
contract, or its marketing page, and write down every sentence containing "always",
"never", "guaranteed", "at least once", "exactly once", "durable", "consistent", or
"atomic". Each one is an invariant, and each one is a claim someone will rely on.

Then ask, for each candidate: **can I write a code change that breaks this and still
passes the assertion?** If yes, the assertion is too weak. If no code change could
ever break it, it holds by construction and is worthless.

## Continuous, not final

`sim.invariant(fn)` re-checks after every event. This matters more than it sounds.

A final-state check reports "the end state is wrong" and leaves you diagnosing from
wreckage, possibly thousands of events after the cause. A continuous check reports
"it broke at t=11265us, on this event, with this state" and the preceding twenty
trace lines contain the entire cause. That is the difference between a five-minute
diagnosis and a five-hour one.

Cost is real: an O(n) invariant checked after every one of n events is O(n squared).
Three mitigations, in order of preference:

1. Make the invariant incremental. Check only the entity the current event touched.
2. Check cheap invariants continuously and expensive ones every k events, with the
   expensive ones also run at the end.
3. Keep the expensive full-history checks in `verify()`, which runs once.

Keep a continuous check for properties that need event-level failure timing. Use
periodic or final checks only for properties whose cost requires that trade-off.

## Taxonomy of invariants that find real bugs

**Safety: nothing bad ever happens.** These are the ones worth writing first.

- An acknowledged write is recoverable. (`demo_bug.py` uses exactly this.)
- Two nodes never both believe they are leader in the same term.
- A committed value never changes.
- The sum of all balances is constant across any transfer.
- A monotonic counter never decreases, in any observer's view.
- A lock is held by at most one holder at a time.
- No request is processed twice with different effects.

**Liveness: something good eventually happens.** Harder, and only checkable at the
end of a run, because "eventually" has no instant.

- After faults stop and the system is given time, every submitted write is either
  visible or explicitly failed. Nothing is stuck forever.
- A leader is elected within N election timeouts of the last partition healing.
- Every queue drains.

The correct shape is: run with faults until time T, stop injecting faults, run to
2T, then assert progress. Asserting liveness while faults are still active is
wrong; an adversary can legitimately prevent progress forever.

`sim.py` supports this directly with `Sim(quiesce_at=T)`: after `T` the draws still
happen (Rule 2) but no fault DECISION is honoured, and a suppressed entry is removed
from the journal so a shrunk journal never contains a fault that cannot fire.

```python
sim = Sim(seed, faults=faults, quiesce_at=500 * MS)
sim.run(1000 * MS)          # faults for the first half, quiet for the second
assert not client.pending   # now "eventually" means something
```

Crash recovery interacts with this. By default a crashed node stays down for the
rest of the run, so no liveness property involving that node can hold and the
recovery path never executes. Pass `Sim(auto_recover=D)` to reboot a crashed node
after `D`, and note that `Node.recovering` is True for the duration of
`on_recover`, so an invariant can target the recovery path specifically. Recovery
is the least-tested code in most systems.

**Conservation.** Any quantity that should be neither created nor destroyed. Money is
the obvious one. Also: reference counts, allocated resources, in-flight request
counts, replica counts. Conservation invariants are cheap, incremental, and catch a
surprising range of bugs.

**Monotonicity.** Sequence numbers, terms, epochs, log indices, version vectors, and
timestamps in a single node's view. Most consensus bugs show up as a monotonicity
violation before they show up as anything else, which makes these excellent early
warning.

**Referential integrity.** Every reference resolves. No entry points at a deleted
parent. No index entry without a corresponding row. Finds partial-failure bugs where
a multi-step update was interrupted.

**Read-your-writes.** After a client observes a write, the same client never sees an
older value. This is the invariant that finds cache and replica-routing bugs, and
it needs per-client state, so it is worth building the bookkeeping for.

## Linearizability

The strongest useful consistency property: every operation appears to take effect
instantaneously at some point between its invocation and its response, and that
sequential order is consistent with real time.

Checking it is the standard approach that Jepsen uses, and it is worth the effort for
anything claiming strong consistency.

1. Record a history: `(client, operation, invoke_time, response_time, result)`.
2. Define a sequential model: the same data structure, single-threaded, obviously
   correct. For a register: `put` sets, `get` reads. Twenty lines.
3. Search for an ordering of the concurrent operations that the model accepts, and
   that respects real-time ordering of non-overlapping operations.

The search is NP-hard in general, and the practical algorithm is P-compositionality
plus the Wing and Gong linearizability check with memoisation, which is what Knossos
and Elle implement. For a first pass, three cheaper checks catch most violations:

- **Non-overlapping operations must be ordered.** If `put(x, 1)` fully completes
  before `get(x)` begins, `get` must not return an older value. No search needed,
  and this alone finds most stale-read bugs.
- **A value read must have been written.** No fabricated values.
- **Once a value is read by anyone, no earlier value is ever read again.** Catches
  most replica-rollback bugs, and is O(n) with a high-water mark per key.

Practical note: record the invoke and response times of every operation from the
start, even if you do not check linearizability yet. Retrofitting the history
recording later means rerunning everything, and the history is cheap to collect.

## Ghost state

Invariants often need information the production system does not track: what the
client believes, what was acknowledged, what the true value should be. Keeping that
bookkeeping in the simulator, never in the system under test, is standard practice
and is called ghost state in the verification literature.

`demo_bug.py` uses `client.acked` for exactly this. The system does not need it; the
invariant cannot exist without it. Keep ghost state strictly outside the system so
it can never influence behavior. If the system reads its own ghost state, the
simulation is testing something other than production.

## When invariants find nothing

Diagnose in this order:

1. **Can you break it deliberately?** Introduce a bug you know is real, and confirm
   the invariants catch it. `demo_bug.py` demonstrates the pattern: the same code
   with one flag flipped fails, and the fixed version passes 10,000 seeds. If a
   deliberately broken version passes, the invariants are the problem, not the
   fault model, and no amount of extra seeds will help.
2. **Are faults reaching the interesting code?** Trace whether the recovery path
   ever executes. Add a counter, assert it is non-zero across the seed sweep.
   Untriggered code is untested code regardless of seed count.
3. **Is the window long enough?** See `fault-models.md`. A retry that never fires
   is a code path that never runs.
4. **Are the invariants only about the happy path?** The interesting invariants
   constrain behavior DURING failure, not after successful completion.

## When an invariant fails with zero faults

This is the most common early confusion, and the workflow's step 4 depends on
getting it right. Write the strongest honest invariant and it will often fail
before you inject a single fault.

That is not a DST finding. It is an ordinary happy-path bug, and DST is a wildly
expensive way to have found it. Triage:

| Result | What it is | What to do |
|---|---|---|
| Fails at zero faults | Ordinary bug | Fix it; keep a plain unit test. No simulation needed. |
| Passes at zero faults, fails under faults | DST target | Shrink it. |
| Passes under `HARSH_PROBS` | Probably a weak invariant | Work the list above. |

**Do not weaken the invariant to manufacture a DST target.** That is the failure
mode this section exists to prevent: you delete the promise that was catching a
real bug in order to make the sweep interesting. Split instead. Keep the strong
version as a happy-path unit test, and derive a second, weaker invariant that
holds at zero faults, for the sweep to attack.

Concretely, in `demo_bug.py`: "an acked write has two durable copies" is the
strong promise and it fails immediately on the buggy design, at zero faults.
"an acked write is recoverable from somewhere when its acking node is down" holds
at zero faults and needs a specific fault combination to break. The first belongs
in a unit test; the second is the DST target.

## Coverage as a first-class metric

Seeds run is not a coverage measure. Track what the simulation actually reached:

- Which fault kinds fired at least once across the sweep.
- Whether recovery, leader election, and log truncation each executed.
- Distribution of how many faults fired per run, since a sweep where most runs
  have zero faults is mostly testing the happy path.
- Line or branch coverage of the system under test, collected across the whole sweep.

Report these alongside seed counts. "10,000 seeds, of which 8,400 injected at least
one fault, exercising recovery 3,100 times" is a claim with content. "10,000 seeds
passed" could mean the fault injector was misconfigured and never fired.
