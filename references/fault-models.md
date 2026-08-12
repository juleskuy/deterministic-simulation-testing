# Fault models: what to inject, and how to key it

A simulator needs faults to exercise recovery, retry, and ordering paths that a
normal test run may never reach.

## Semantic keys

Rule 3 is what makes shrinking possible.

A fault journal is a set of decisions. Shrinking removes some and re-runs. Removing
a fault changes which messages exist and shifts every timestamp downstream. So the
key identifying a fault must survive that.

```python
faults.hit("crash", node_id, node.processed)  # good
faults.hit("drop", message_id)                # good
faults.hit("partition", frozenset({a, b}), epoch)  # good
faults.hit("crash", sim.now)                  # useless after one removal
faults.hit("crash", draw_index)               # worse: shifts on every edit
```

Good keys are things the system itself would recognise: node identity, message id,
sequence number, request id, count of operations performed, term or epoch number.

A subtle corollary: message ids must be assigned from a counter that advances
identically regardless of which faults fire. In `sim.py`, `_msg_id` increments at
the top of `send` before any fault check, so a dropped message still consumes an
id. If dropping a message skipped the increment, every later id would shift and
the journal would be meaningless.

## Network faults

**Drop.** Real networks drop packets, and TCP hides most drops until it
cannot. A dropped message is not a delayed message: it never arrives, and the
sender may or may not learn this.

**Duplicate.** TCP prevents duplicates within one connection. It does not prevent
your retry logic from delivering a request twice, and any at-least-once queue
guarantees duplicates. This exposes missing idempotency handling.

**Reorder.** Two messages between the same pair arriving out of order. TCP prevents
this within one connection; nothing prevents it across connections, across
reconnects, or through a proxy or load balancer. In `sim.py` this emerges naturally,
since independent latency draws mean send order does not determine arrival order.

**Delay and slow.** A message that arrives after the sender gave up. This is the
fault that finds the zombie-response bug: a reply to a request that was already
retried and answered. Realistic latency is long-tailed, so a multiplier of 10x to
100x on the base latency is closer to production than a uniform draw.

**Asymmetric partition.** A can reach B, but B cannot reach A. Heartbeats may
succeed in one direction, so each side has a different view of liveness. Consensus
implementations that assume symmetric reachability can fail here.

**Partial partition.** Three nodes, where A-B works, B-C works, A-C does not. This
breaks the common assumption that reachability is transitive, and it produces
split-brain in systems that would survive a clean two-way split.

**Flaky link.** Alternating up and down at a period near your timeout value. Finds
retry storms and leader-election churn that a clean partition does not.

To add asymmetric partitions to `sim.py`, key on the ordered pair and check in
`send`:

```python
if self.faults.hit("partition", src, dst, epoch):
    self.trace("partitioned", src=src, dst=dst)
    return
```

Keying on an `epoch` counter rather than time keeps it shrinkable. Increment the
epoch on a topology change you control.

## Disk and durability faults

`Disk` in `sim.py` implements the honest model: `write` lands in a volatile page
cache, only `fsync` makes bytes durable, `crash` discards the cache, and `read`
sees the cache first. That last detail is why lost-write bugs pass unit tests: a
process cannot detect its own missing durability by reading back what it wrote.

Faults worth adding, in rough order of how often they find real bugs:

**Lost writes on crash.** Already modelled. Any unfsynced write disappears.

**Torn writes.** A crash mid-write leaves a partially updated block: first half new,
second half old. Real on any device where your record spans sectors. Systems that
assume a write is atomic fail here, which is why real databases use checksums per
page and why a "just overwrite the header" design is unsafe.

**fsync failure.** `fsync` can return `EIO`. On some Linux versions the dirty page
was already discarded, so retrying `fsync` returns success while the data is gone
forever. This wrecked several real databases. Model it as: `fsync` raises, and the
cache is cleared without becoming durable.

**Lying fsync.** `Disk(lying_fsync=True)` in `sim.py`. Some consumer SSDs and
virtualised disks acknowledge `fsync` while data is still volatile. Keep this in a
separate test tier: with it enabled, no single-disk design can be correct, so it
tells you about your replication strategy rather than your code.

**Bit rot and misdirected writes.** A block is silently corrupted, or written to
the wrong offset. Finds missing checksums. Cheap to model: mutate a durable value
at a keyed point.

**Directory fsync.** `fsync` on a file does not guarantee its directory entry is
durable. After `create` + `write` + `fsync(file)` + crash, the file can be absent.
`rename` is atomic with respect to content but the rename itself needs a directory
`fsync` to be durable. Almost every "atomic file write" helper in every language
gets this wrong.

**Disk full and quota.** `ENOSPC` mid-write, including during recovery.

## Process faults

**Crash.** Instant termination, volatile state lost. Key it on work performed, as
`sim.py` does with `node.processed`, so the interesting crash points are exactly
the boundaries between operations.

**Crash during recovery.** Recovery code needs its own crash points. Model this by
allowing a crash inside `on_recover`.

A crashed node in `sim.py` stays down for the rest of the run by default, so
`on_recover` never executes and every recovery bug is invisible. Pass
`Sim(auto_recover=D)` to reboot it after `D`, and key a crash on the recovery
state to target that path:

```python
def on_recover(self):
    if self.sim.faults.hit("crash_in_recovery", self.id, self.disk.fsyncs):
        self.crash()
        return
    self.rebuild_from(self.disk.durable)
```

`Node.recovering` is True for the duration of `on_recover`, so an invariant can
also assert things that must hold only during recovery.

**Pause and resume.** A process is frozen longer than its lease or heartbeat timeout,
then resumes while still believing it holds the lease. GC pauses, VM live migration,
and CPU throttling can produce this. The resumed process may act on stale authority.

**Clock skew and jump.** Two nodes disagree about the time; NTP steps a clock
backwards. Any logic comparing timestamps across nodes breaks. Model as a per-node
offset applied when the node reads the clock. A jump backwards is particularly nasty
for anything using timestamps as identifiers or for ordering.

**Slow node.** A node runs, but much more slowly. This can expose timeout settings
and a leader that is slow enough to block progress without triggering failure
detection.

**Byzantine-lite.** Not full Byzantine fault tolerance, just a node that sends a
stale-but-valid message: an old term, an outdated view, a replayed request. Cheap
to model and finds real bugs, because "stale but well-formed" is what actually
happens after a partition heals.

## Probabilities

Start here and tune upward until you find bugs, then downward for the regression
suite:

```python
from sim import DEFAULT_PROBS, HARSH_PROBS

DEFAULT_PROBS   # {"drop": 0.10, "dup": 0.05, "slow": 0.10, "crash": 0.02}
HARSH_PROBS     # {"drop": 0.50, "dup": 0.20, "slow": 0.30, "crash": 0.10}
```

Import them rather than retyping them. `search()` uses `DEFAULT_PROBS` when `probs`
is omitted, so there is one number to tune and no chance of the docs, the demo, and
your own driver disagreeing about where to start.

Three practical rules:

**`HARSH_PROBS` is a diagnostic, not a test.** Its only job is to confirm you CAN
break the system. If an adversary dropping half the network cannot violate any
invariant, your invariants are too weak. Fix the invariants before tuning
probabilities. Never report a `HARSH_PROBS` sweep as evidence of correctness: it
explores a world so hostile that no useful system makes progress in it.

**Probabilities that are too high find nothing useful.** A system permanently
partitioned never reaches interesting states; it just sits there failing to make
progress. The interesting bugs live at the boundary where the system almost works.

**Vary probabilities across seeds.** A fixed fault rate explores one slice of the
space. Deriving the rate from the seed (low-fault runs plus occasional storms)
covers far more, and mirrors production, where most days are quiet.

## The simulation window

Faults only matter if the system gets to react to them. A retry that fires at 30
seconds never fires inside a 5-second window, so the retry path is untested no
matter how many seeds you run.

Check explicitly: is `until` longer than your longest timeout multiplied by the
number of retries? If not, either shorten the timeouts under test or lengthen the
window, and say which you did when reporting results. Shortening timeouts is
usually right, since it tests the same logic faster, but it must be a parameter of
the system rather than a hardcoded constant.

## What this model does not cover

Be explicit about the boundary when reporting results. A simulator tests your logic
against your model of the world. It cannot find:

- Bugs in the real driver, kernel, filesystem, or hardware.
- Protocol-level mismatches with a real peer implementation.
- Resource exhaustion the model does not represent: file descriptors, memory
  fragmentation, connection pool limits.
- Anything in code paths that the simulation replaces with a stub.

Keep a thin integration tier that tests those assumptions directly, and treat the
list of model assumptions as a living document. Every production incident that the
simulator "should have caught" is a missing fault or a wrong assumption; add it and
re-run the whole seed corpus.
