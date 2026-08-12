---
name: deterministic-simulation-testing
description: "Use when a system has concurrency, I/O, networking, retries, or durability claims and normal tests keep passing while production keeps breaking. Builds a seeded simulator that collapses the whole system into one deterministic loop, injects network and disk and crash faults, searches thousands of universes, and shrinks any failure to a minimal byte-identical replay. Triggers on: deterministic simulation, DST, flaky test, heisenbug, race condition, distributed bug, lost write, fsync, durability, crash consistency, retry storm, cannot reproduce, works on my machine, fault injection, chaos testing, TigerBeetle, FoundationDB, Antithesis, madsim, turmoil, deterministic scheduler."
license: MIT
metadata:
  author: juleskuy
  version: "1.0.0"
  category: software-development
---

# Deterministic Simulation Testing

The most valuable testing technique in existence, and almost nobody uses it, because
the first attempt always fails for the same three reasons. This skill exists to get
those three right the first time.

FoundationDB reached a decade of production with zero data-corruption bugs on this
technique. TigerBeetle designed its entire architecture around it. Antithesis built a
company on it. All of them do the same thing: **stop testing the real system, and start
testing a deterministic model of it, thousands of times, with the adversary in control.**

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

DST finds bugs in your logic under adversarial scheduling. It does not find bugs in
the layers you replaced with a model. Say this out loud when reporting results, because
the difference is exactly where overconfidence comes from.

## The three rules

Everything else in this skill is detail. These three are load-bearing, and violating
any one of them produces a simulator that appears to work and silently proves nothing.

### Rule 1: exactly one source of nondeterminism

Every nondeterministic decision in the entire system reads from one seeded RNG.
No exceptions, and the exceptions are never where you expect. Run
`scripts/scan_nondeterminism.py` on the code under test before writing a single
line of simulator; it finds the ones people forget.

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

Verification, not hope: run the same seed twice, assert the event traces are
byte-identical. `scripts/test_sim.py::test_same_seed_same_bytes` is that assertion.
Add it to CI on day one. The day it fails is the day every other DST result becomes
meaningless, and you want to learn that from CI rather than from a bug you cannot trust.

### Rule 2: draw randomness unconditionally, then decide

This is the rule nobody warns you about, and it destroys more DST attempts than
anything else. Stated precisely, because the loose version is false and a
reviewer will catch you: **within a single event, the number of draws consumed
must not depend on which faults fire.**

```python
# WRONG. When p == 0 the draw never happens, so every downstream random value
# shifts, and seed 42 with faults enabled is a different universe from seed 42
# with faults disabled. Now you cannot compare runs, and shrinking is fiction.
if p > 0 and rng.random() < p:
    inject_fault()

# RIGHT. The stream advances identically no matter what the configuration says.
roll = rng.random()
if p > 0 and roll < p:
    inject_fault()
```

Same failure mode, subtler form: drawing a different NUMBER of values on different
branches. In `sim.py::Sim.send`, three values are drawn on every single send -
base latency, duplicate gap, slowdown multiplier - even though most sends use only
the first, and all three fault decisions (`drop`, `slow`, `dup`) are taken BEFORE
the early `return` on drop. That waste is deliberate and load-bearing.

**What this rule does NOT claim:** that the TOTAL number of draws across a run is
config-invariant. It is not, and it cannot be. A dropped message is never
delivered, so its crash-check never happens; faults change which events exist at
all. This is exactly why fault keys must be semantic (Rule 3) rather than indexed
off the draw sequence. Anyone who tells you the whole stream is config-invariant
has not counted the draws.

Two guards, and you need both. `test_probability_does_not_shift_the_stream`
compares send timestamps, which only exercises `Sim.rng`; it passes even when the
fault stream is broken. `test_fault_stream_does_not_shift_with_config` counts
`Faults.draws` per event directly. A rule whose own guard cannot see the
violation is not guarded.

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
   never a composite: `demo_bug.py` ships a durability fix that passes 10,000
   seeds and still acknowledges the same write twice, because `verify` only ever
   promised durability. Invariant design, including the trap of writing invariants
   that merely restate the implementation, is in `references/invariants.md`.
4. **Run with zero faults, and TRIAGE what fails.** This step is not "proceed only
   if clean". Expect strong invariants to fail here, and sort them:
   - **Fails at zero faults** -> an ordinary bug on the happy path. Fix it, and
     keep it as a plain unit test. It did not need DST and never will.
   - **Passes at zero faults, fails under faults** -> a DST target. This is the
     whole point.
   - **Passes under `HARSH_PROBS` too** -> suspect the invariant, not the system.
     See `references/invariants.md`, "When invariants find nothing".

   Do not weaken an invariant to get past this step. Split it: keep the strong
   version as a unit test on the happy path, and derive the fault-only version
   for the sweep.
5. **Turn on faults, sweep seeds.** Start with `DEFAULT_PROBS` from `sim.py`
   (`{"drop": 0.10, "dup": 0.05, "slow": 0.10, "crash": 0.02}`) - one number to
   tune, imported rather than retyped. Sweep thousands. One seed is one universe;
   the value is in the volume. Found nothing? Confirm you CAN break it with
   `HARSH_PROBS` before touching anything else.
6. **Shrink.** `shrink()` runs ddmin over the fault journal and RE-VERIFIES every
   candidate against the ORIGINAL failure's signature, so it cannot slide onto a
   different, easier bug and report that journal as the minimum. It tests the
   empty journal explicitly, because ddmin's chunking can never propose it and a
   fault-independent failure would otherwise be reported as needing a fault.
   When the journal alone does not reproduce, it says so instead of inventing a
   minimum, because a shrinker that reports fiction is worse than no shrinker.
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

`scripts/sim.py` is a complete simulator core, stdlib only, roughly 300 lines:
seeded RNG, virtual clock, priority-queue executor, fault-injecting network
(drop, duplicate, delay, slow), a `Disk` with honest `fsync` semantics, node
crash and recovery (`auto_recover` reboots a crashed node so the recovery path
actually runs), a `quiesce_at` window for liveness, continuous invariant checking,
seed search, and signature-preserving ddmin shrinking.

`scripts/demo_bug.py` is the proof. It plants a real bug - a group-commit
replicated log that acknowledges the client on batch entry instead of after
durability, the same class of bug that has shipped in real databases - finds it,
shrinks it, verifies the fix over 10,000 seeds, and then finds a SECOND bug in the
fixed design: the same write acknowledged twice, because `verify` only ever
promised durability and nothing promised idempotency.

Run it rather than reading a transcript here. Output is not reproduced in this file
on purpose: a pasted transcript rots the moment the fault stream changes, and CI
cannot check prose. The README carries one captured run, and CI fails if the script
stops finding either bug.

What to read in its output: the shrunk journal names the exact conditions in a few
lines. Under zero faults the buggy code passes everything, because a process reads
its own page cache and cannot detect its own missing durability. No hand-written
test suite finds that by inspection, and the sweep finds it in under a second.

`scripts/test_sim.py` verifies the simulator itself: determinism, seed divergence,
journal replay without touching the RNG, zero-fault cleanliness, stream stability
across configurations (both streams, separately), `fsync` semantics including the
lying-fsync case, immediate invariant firing, crash-freedom reporting, the quiesce
window, the recovery path, end-to-end bug detection, and three separate shrink
honesty properties. 16 assertions, all passing. A simulator nobody checked is a
random number generator with good PR.

`scripts/scan_nondeterminism.py` scans nine languages for Rule 1 violations. Its
own limits are printed on every run: `for x in items:` cannot be judged lexically
because the type of `items` is unknown, and set iteration is the highest-frequency
source in real code. Audit bare loops by hand.

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
