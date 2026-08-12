"""A real durability bug, found by simulation, shrunk to a two-fault replay.

The system under test is a three-node replicated log with GROUP COMMIT: writes
accumulate in a batch and one `fsync` covers the whole batch. This is not a
strawman. MySQL's binlog, PostgreSQL's `commit_delay`, Kafka, and most
write-ahead logs all batch fsyncs, because fsync is the single most expensive
operation in the storage stack.

The bug is one line: the leader acknowledges the client as soon as the write
enters the batch, instead of after the batch is durable. Under no faults this
is invisible - every read succeeds, because a process reads its own page cache.
It needs a crash inside the batch window AND the replication messages to be
lost, at the same time, to lose an acknowledged write.

Run it. The output is produced, not described, so it is not transcribed here:
a docstring cannot be kept honest by discipline alone. The README shows a
captured run and CI fails if this script stops finding the bug.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sim import (  # noqa: E402
    DEFAULT_PROBS,
    MS,
    US,
    InvariantViolation,
    Node,
    Sim,
    search,
    shrink,
)

BATCH = 4  # writes per fsync
FSYNC_DELAY = 30 * MS  # flush a partial batch after this long
FOLLOWERS = ("n1", "n2")


class Leader(Node):
    """Accepts writes, batches them, replicates them, acknowledges the client.

    `ack_before_durable=True` is the bug. Everything else is the same code.
    """

    def __init__(self, sim: Sim, node_id: str, client: "Client", ack_before_durable: bool):
        super().__init__(sim, node_id)
        self.client = client
        self.ack_before_durable = ack_before_durable
        self.batch: list[tuple] = []
        self.pending: dict[tuple, set[str]] = {}
        self.flush_armed = False

    def on_message(self, src, msg):
        tag = msg[0]
        if tag == "put":
            _, key, value = msg
            self.batch.append((key, value))
            self.disk.write(key, value)  # page cache only, NOT durable
            for f in FOLLOWERS:
                self.sim.send(self.id, f, ("repl", key, value))

            if self.ack_before_durable:
                # BUG. The value exists only in this node's volatile page cache
                # and in messages that may never arrive. Nothing is durable
                # anywhere, yet the client is told the write is safe.
                self.client.on_ack(key, value, self.id)
            else:
                self.pending[(key, value)] = set()

            if len(self.batch) >= BATCH:
                self._flush()
            elif not self.flush_armed:
                self.flush_armed = True
                self.sim.set_timer(self.id, FSYNC_DELAY, "flush")

        elif tag == "repl_ok":
            _, key, value = msg
            waiters = self.pending.get((key, value))
            if waiters is None:
                return
            waiters.add(src)
            self._maybe_ack(key, value)

    def on_timer(self, name):
        if name == "flush":
            self.flush_armed = False
            self._flush()

    def _flush(self):
        if not self.batch:
            return
        self.disk.fsync()
        flushed, self.batch = self.batch, []
        for key, value in flushed:
            self._maybe_ack(key, value)

    def _maybe_ack(self, key, value):
        """Correct rule: durable locally AND durable on at least one follower.

        Local fsync alone is not enough. A single-node fsync survives a process
        crash but not a disk loss, and it does not survive the node never coming
        back. One remote durable copy is the minimum honest bar for calling a
        write acknowledged in a replicated system.
        """
        if self.ack_before_durable:
            return
        if (key, value) not in self.pending:
            return
        local = self.disk.durable.get(key) == value
        remote = len(self.pending[(key, value)]) >= 1
        if local and remote:
            del self.pending[(key, value)]
            self.client.on_ack(key, value, self.id)

    def on_crash(self):
        self.batch.clear()
        self.pending.clear()
        self.flush_armed = False


class Follower(Node):
    def on_message(self, src, msg):
        if msg[0] == "repl":
            _, key, value = msg
            self.disk.write(key, value)
            self.disk.fsync()  # followers commit eagerly
            self.sim.send(self.id, src, ("repl_ok", key, value))


class Client(Node):
    """Issues writes and remembers exactly what the system promised."""

    def __init__(self, sim: Sim, node_id: str, count: int):
        super().__init__(sim, node_id)
        self.count = count
        self.acked: list[tuple] = []

    def start(self):
        for i in range(self.count):
            self.sim.schedule(i * 5 * MS, lambda i=i: self._put(i))

    def _put(self, i):
        # A unique key per write. Overwrite semantics are a separate concern;
        # mixing them in here would let a lost write hide behind a legal update.
        self.sim.send(self.id, "n0", ("put", f"k{i}", f"v{i}"))

    def on_ack(self, key, value, by):
        self.acked.append((key, value, by))
        self.sim.trace("ack", key=key, value=value, by=by)

    def on_message(self, src, msg):
        pass


def make_world(ack_before_durable: bool):
    def build(sim: Sim):
        client = sim.add(Client(sim, "client", count=6))
        leader = sim.add(Leader(sim, "n0", client, ack_before_durable))
        for f in FOLLOWERS:
            sim.add(Follower(sim, f))
        client.start()

        def durability_holds():
            """Re-checked after every single event in the simulation.

            An acknowledged write whose acknowledging node is down must be
            recoverable from durable storage somewhere, or the acknowledgement
            was a lie. Checking continuously, rather than at the end, is what
            turns "the final state is wrong" into "it broke at t=41120us".
            """
            for key, value, by in client.acked:
                if sim.nodes[by].up:
                    continue
                if any(n.disk.durable.get(key) == value for n in sim.nodes.values()):
                    continue
                raise InvariantViolation(
                    f"acked {key}={value} by {by}, {by} is down, no durable copy anywhere"
                )

        sim.invariant(durability_holds)
        return client

    return build


def verify(sim: Sim, client: Client):
    """Final check, after every node has been given the chance to recover."""
    sim.recover_all()
    for key, value, by in client.acked:
        if not any(n.disk.durable.get(key) == value for n in sim.nodes.values()):
            return f"acked {key}={value} survived nowhere after recovery"
    return None


def add_idempotency_invariant(build):
    """Wrap a world with a SECOND, independent promise: one put, one ack.

    Kept separate on purpose. `verify` above only checks durability, so it
    passes on a design that acknowledges the same write twice. This is the
    trap `references/invariants.md` warns about: a suite that checks one
    promise reports "fixed" while another promise is still broken.
    """

    def wrapped(sim: Sim):
        client = build(sim)

        def acked_at_most_once():
            seen = set()
            for key, value, _by in client.acked:
                if (key, value) in seen:
                    raise InvariantViolation(f"acked {key}={value} twice")
                seen.add((key, value))

        sim.invariant(acked_at_most_once)
        return client

    return wrapped


PROBS = dict(DEFAULT_PROBS, drop=0.30)  # a bit harsher on drops than the default
UNTIL = 5000 * MS


def main() -> int:
    print("BUGGY  ack-on-batch-entry, searching 200 seeds ...")
    found = search(make_world(True), verify, range(200), UNTIL, PROBS)
    if not found:
        print("  no violation found; widen PROBS or raise the seed count")
        return 1

    seed, journal, err, sim = found
    print(f"  seed {seed}: {err}")
    print(f"  {len(journal)} faults fired")

    minimal, msim, merr = shrink(make_world(True), verify, UNTIL, seed, journal)
    print(f"\n  shrunk {len(journal)} faults -> {len(minimal)}")
    for f in minimal:
        print(f"    {f}")
    print(f"  still fails: {merr}")
    print("\n  minimal replay:")
    print(msim.replay_text(limit=24))

    n = 10000
    print(f"\nFIXED  ack-after-durable, searching {n} seeds ...")
    still = search(make_world(False), verify, range(n), UNTIL, PROBS)
    if still:
        s, j, e, _ = still
        print(f"  STILL BROKEN on seed {s}: {e}  ({len(j)} faults)")
        return 1
    print(f"  {n} seeds, no violation")

    # Describe what was actually found, rather than asserting a story that a
    # later change to the fault stream would quietly falsify.
    kinds = ", ".join(sorted({f[0] for f in minimal}))
    print(f"\nThe fix is one condition. The minimal trigger is {len(minimal)} fault(s)")
    print(f"({kinds}) at one specific point in one specific interleaving, and the")
    print("buggy code passes with zero faults because a process reads its own")
    print("page cache. No hand-written test suite finds that by inspection.")

    # The durability fix is real, and the system is still not correct. A second
    # promise, never checked by `verify`, is broken in the SAME fixed design.
    print("\nSECOND PROMISE  'one put => at most one ack', same FIXED design ...")
    dup = search(add_idempotency_invariant(make_world(False)), verify,
                 range(3000), UNTIL, PROBS)
    if not dup:
        print("  3000 seeds, no violation")
    else:
        s, j, e, _ = dup
        m, _, _ = shrink(add_idempotency_invariant(make_world(False)), verify, UNTIL, s, j)
        print(f"  seed {s}: {e}")
        print(f"  shrunk {len(j)} faults -> {len(m)}: {m}")
        print("  A duplicated request is acknowledged twice: no request-id dedup.")
        print("  Durability was fixed; idempotency was never promised in `verify`.")
        print("  One invariant per promise, or a passing suite means nothing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
