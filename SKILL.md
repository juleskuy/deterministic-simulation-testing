---
name: deterministic-simulation-testing
description: "Use when concurrent or fault-prone systems have flaky, unreproducible failures or need crash, retry, durability, or ordering tests."
license: MIT
metadata:
  author: juleskuy
  version: "1.0.0"
  category: software-development
---

# Deterministic Simulation Testing

Deterministic simulation testing replaces selected external dependencies with models
you control. A seeded run can then be repeated, inspected, and reduced to a small
failure case.

This approach appears in projects such as FoundationDB and TigerBeetle. It is useful
when correctness depends on the order of messages, timers, crashes, or durable writes.
It does not replace integration testing against the real components.

## When to use this

Reach for DST when any of these is true:

- A test is flaky and the response has been `@retry` or `sleep(100)`.
- A bug report says "cannot reproduce" or "happens maybe once a week in prod."
- The system claims durability, exactly-once, idempotency, or consistency.
- Correctness depends on the ORDER of concurrent events, not just their values.
- There is a retry, a timeout, a lock, a queue, a cache, a replica, or an `fsync`.

Do NOT reach for DST when:

- The logic is pure and single-threaded. Property-based testing is cheaper and
  strictly better. Use `hypothesis`, `proptest`, `fast-check`.
- You need to validate real driver, kernel, or hardware behavior. A simulator
  tests YOUR logic against YOUR model of the world; it cannot find a bug in the
  Postgres wire protocol. Keep a thin integration tier for that.
- The bug is already deterministically reproducible. Just fix it.

DST tests the logic represented by the model under adversarial scheduling. It cannot
test a driver, kernel, or service that the model replaces. Report that boundary with
the result.

## The three rules

These three rules determine whether replay and shrinking are reliable. If one is
broken, a simulation result may not reproduce.

### Rule 1: exactly one source of nondeterminism

Every nondeterministic decision in the model reads from a seeded RNG. Run
`scripts/scan_nondeterminism.py` on the code under test before writing a single
line of simulator; use its findings as a review list, not as proof.

The complete taxonomy, per language, with the fix for each, is in
`references/nondeterminism.md`. The ones that bite hardest:

- **Time.** Wall clock, monotonic clock, timeouts, TTLs, `Date.now()`, `Instant::now()`.
  Every one becomes `sim.now`.
- **Iteration order.** Go map ranging is deliberately randomised. Python `set`
  ordering varies with `PYTHONHASHSEED` and with insertion history. Rust `HashMap`
  is randomly seeded per process. Sort before you iterate, or use an ordered container.
- **Address-dependent behavior.** Default `hash()` on objects, `id()`, pointer
  formatting, default `__repr__`. These change per run under ASLR.
- **Thread and task scheduling.** The single biggest one. You do not control the OS
  scheduler, so the simulator must own concurrency: one thread, one event queue.
- **Environment.** Locale, timezone, CPU count, `os.environ`, filesystem readdir order,
  available memory, GC timing, `float` formatting across platforms.
- **Concurrent hashing of untrusted input.** Any structure whose behavior depends on
  a per-process random seed.

Run the same seed twice and compare the event traces byte for byte.
`scripts/test_sim.py::test_same_seed_same_bytes` demonstrates the check. Put an
equivalent test in CI before relying on any seed sweep.

### Rule 2: draw randomness unconditionally, then decide

Within a single event, the number of random draws must not depend on which faults
fire. Otherwise an early branch changes every later decision in that run.

```python
# Wrong: when p == 0 the draw never happens. Later random decisions shift.
if p > 0 and rng.random() < p:
    inject_fault()

# Better: draw first, then decide whether to inject the fault.
roll = rng.random()
if p > 0 and roll < p:
    inject_fault()
```

The same problem appears when different branches draw different numbers of values.
In `sim.py::Sim.send`, three values are drawn on every send:
base latency, duplicate gap, slowdown multiplier - even though most sends use only
the first, and all three fault decisions (`drop`, `slow`, `dup`) are taken BEFORE
the early `return` on drop.

This does not make the total draw count across a whole run configuration-invariant.
A dropped message is not delivered, so its crash check does not happen. Faults change
which events exist. That is why Rule 3 uses semantic keys instead of draw indexes.

Use two checks. `test_probability_does_not_shift_the_stream` compares send
timestamps and exercises `Sim.rng`. `test_fault_stream_does_not_shift_with_config`
counts `Faults.draws` per event. Each covers a different stream.

### Rule 3: address faults by semantic key, never by time or draw index

A fault journal is only useful if it survives editing. Shrinking removes faults,
which changes which messages exist, which changes every timestamp downstream.

```python
faults.hit("crash", node_id, node.processed)   # survives shrinking
faults.hit("crash", t=4711)                    # meaningless after one removal
faults.hit("crash", draw_number=317)           # worse: shifts on every edit
```

Key on things that mean something to the system: node identity, message id,
count of messages processed, sequence number, request id. See
`references/fault-models.md` for the full key design discussion.

## Workflow

1. **Scan for nondeterminism.** `python scripts/scan_nondeterminism.py <path>`.
   Fix or inject every hit before writing simulator code. Doing this later means
   rewriting the system under test twice.
2. **Extract the logic from the I/O.** DST is only possible if business logic can
   run without touching a real socket, disk, or clock. This is usually 80% of the
   work and it improves the codebase whether or not you finish the simulator.
   Patterns per language in `references/nondeterminism.md`.
3. **Write invariants first, before any fault injection.** An invariant is a
   predicate that must hold at EVERY instant, not at the end. `sim.invariant(fn)`
   re-checks after every event, so a violation is reported at the microsecond it
   occurs rather than diagnosed from wreckage. Write **one invariant per promise**,
   rather than a composite: `demo_bug.py` ships a durability fix that passes 10,000
   seeds and still acknowledges the same write twice, because `verify` only ever
   promised durability. Invariant design, including the trap of writing invariants
   that merely restate the implementation, is in `references/invariants.md`.
4. **Run with zero faults and classify failures.** Strong invariants may fail here:
   - **Fails at zero faults**: an ordinary bug on the happy path. Fix it, and
     keep it as a plain unit test. It did not need DST and never will.
   - **Passes at zero faults, fails under faults**: a DST target.
   - **Passes under `HARSH_PROBS` too**: inspect the invariant before changing the
     system.
     See `references/invariants.md`, "When invariants find nothing".

   Do not weaken an invariant only to reach the fault sweep. Split it: keep the strong
   version as a unit test on the happy path, and derive the fault-only version
   for the sweep.
5. **Turn on faults, sweep seeds.** Start with `DEFAULT_PROBS` from `sim.py`
   (`{"drop": 0.10, "dup": 0.05, "slow": 0.10, "crash": 0.02}`) - one number to
   tune, imported rather than retyped. Sweep enough seeds to exercise the fault
   paths you care about. If nothing fails, use `HARSH_PROBS` to check that the
   invariant can fail at all.
6. **Shrink.** `shrink()` runs ddmin over the fault journal and RE-VERIFIES every
   candidate against the original failure's signature, so it cannot slide onto a
   different, easier bug and report that journal as the minimum. It tests the
   empty journal explicitly, because ddmin's chunking can never propose it and a
   fault-independent failure would otherwise be reported as needing a fault.
   When the journal alone does not reproduce, it says so instead of inventing a
   minimum.
7. **Check liveness in a quiesced tail.** Pass `quiesce_at` to `Sim`: faults stop
   firing after that instant, so "eventually" becomes meaningful. Asserting
   progress while the adversary is still active is wrong, because an adversary may
   legitimately block progress forever.
8. **Commit the minimal replay as a permanent regression test.** Seed plus journal is
   a few hundred bytes and reproduces in milliseconds, forever, on every machine.
   Assert BOTH sides: the fixed design passes it, and the buggy design still fails
   it. A one-sided regression test keeps passing after the bug is reintroduced
   anywhere the fix does not cover.
9. **Report honestly.** State seeds run, fault probabilities, simulated time covered,
   and what the model replaced. "10,000 seeds, no violation" is a real claim.
   "It is correct" is not.

## Working code

`scripts/sim.py` is a small stdlib-only simulator core. It provides a seeded RNG,
virtual clock, priority-queue executor, network faults (drop, duplicate, delay, and
slow), a `Disk` model with `fsync`, node crash and recovery, continuous invariants,
seed search, and signature-preserving ddmin shrinking. `auto_recover` restarts a
crashed node; `quiesce_at` creates a fault-free period for liveness checks.

`scripts/demo_bug.py` models a group-commit replicated log that acknowledges a write
when it enters a batch rather than after durability. It finds and shrinks that loss,
then runs the corrected version over 10,000 seeds. It also checks a separate
idempotency property and demonstrates a duplicate acknowledgement.

Run the demo for current output. The README includes one captured result, but the
seed and journal can change when the fault model changes.

`scripts/test_sim.py` covers determinism, journal replay, `fsync`, invariant timing,
crash reporting, recovery, liveness quiescence, and shrink behavior. The test suite
currently contains 16 assertions.

`scripts/scan_nondeterminism.py` scans nine languages for common Rule 1 violations.
It cannot infer the type of an arbitrary loop variable such as `items`, so bare loops
still need review when their source may be unordered.

## Failure modes to expect

**"It found nothing after 10,000 seeds."** Almost always the fault model, not the
system, or the invariants. In order: sweep again with `HARSH_PROBS` and confirm you
CAN break it at all; if an adversary dropping half the network cannot violate
anything, the invariants are too weak (see `references/invariants.md`). Then check
the window: `until` must exceed your longest timeout times the retry count, or the
retry path never executes. Tune `until` first and the `Sim(latency=...)` tuple only
if your timeouts are calibrated against real network latencies.

**"My strongest invariant fails with zero faults."** Expected, and it is not a DST
finding. Workflow step 4 tells you to triage: fix it as an ordinary happy-path bug
with a plain unit test. Do NOT weaken the invariant to manufacture a DST target;
split it instead.

**"The bug disappeared on rerun."** Rule 1 is violated somewhere. Run
`test_same_seed_same_bytes`. Common culprits: a `dict`/`set` iteration, a real
clock read in logging, a hash-ordered comparison, or a library that spawns a thread.

**"Shrinking made it pass."** Correct and expected: some faults are load-bearing.
`shrink()` re-runs every candidate for exactly this reason.

**"Simulated time is huge but nothing happens."** Timeouts are longer than the run
window. A retry that fires at 30s never fires inside a 5s simulation. Either
shorten the timeouts under test or lengthen the window, and be explicit about which.

**"It passes in the simulator and fails in production."** The model diverged from
reality. Write down what the model assumes, and add a thin real-integration tier that
tests those assumptions specifically. This is a real limitation, not a bug to hide.

**"The sweep is green, so the system is correct."** No. It means every promise you
encoded holds under the faults you modelled. `demo_bug.py` demonstrates the gap
directly: a durability fix clears 10,000 seeds while the same design acknowledges
the same write twice, because nothing promised idempotency. A green sweep bounds
what you checked, not what is true.

## Language notes

Python's GIL does not give you determinism; thread scheduling is still the OS's
decision. Use the single-threaded executor here, not threads.

Rust: `madsim` and `turmoil` provide production-grade deterministic runtimes with
`tokio` API compatibility. Prefer them over rolling your own; the patterns in this
skill map onto both.

Go: map iteration is randomised on purpose, `select` is randomised on purpose, and
the scheduler is not controllable. Determinism requires funnelling concurrency into
one goroutine plus a channel-driven event queue, and `GOMAXPROCS=1` alone is not enough.

JVM: `Instant`, `ThreadLocalRandom`, `HashMap` ordering, and `System.identityHashCode`
are the four to hunt first.

JS/TS: `Date.now`, `Math.random`, `setTimeout`, microtask interleaving, and
`Object.keys` on integer-like keys. Fake timers get you part of the way; a real
deterministic executor gets you the rest.

## References

- `references/nondeterminism.md` - complete per-language taxonomy of nondeterminism
  sources and how to eliminate each one
- `references/fault-models.md` - what to inject, realistic distributions, semantic
  key design, and the faults people forget (clock skew, partial writes, byzantine
  duplicates, asymmetric partitions)
- `references/invariants.md` - writing invariants that catch bugs instead of
  restating the implementation, plus linearizability checking
- `references/shrinking.md` - ddmin, why re-verification is mandatory, and turning
  a minimal replay into a permanent regression test
