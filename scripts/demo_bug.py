"""A replicated-log durability example.

The leader batches writes, replicates them to two followers, and flushes the batch
with `fsync`. The buggy version acknowledges a write before it is durable. The demo
searches for a failing seed, shrinks the associated journal, then checks the corrected
version. It also demonstrates a separate idempotency failure.

Run this file for current output. The README contains one example run.
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
                # Bug: the value is still in the local page cache and in
                # replication messages that may not arrive. It is not durable.
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
        """Acknowledge after local durability and one remote durable copy.

        Local `fsync` handles a process crash, but replication provides a second
        durable copy if the leader's disk or node is unavailable.
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
    """Issues writes and records acknowledgements for the test oracle."""

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
            """Check recovery of acknowledgements after each event.

            If the acknowledging node is down, another durable copy must exist.
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
    """Add a separate invariant: each write is acknowledged at most once.

    `verify` checks durability only, so this stays separate from that check.
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

    # Summarize the journal produced by this run.
    kinds = ", ".join(sorted({f[0] for f in minimal}))
    print(f"\nThe fix is one condition. The minimal trigger is {len(minimal)} fault(s)")
    print(f"({kinds}) at one specific point in one specific interleaving, and the")
    print("buggy code passes with zero faults because a process reads its own")
    print("page cache.")

    # Check an independent property against the same corrected design.
    print("\nIdempotency check: one put => at most one ack ...")
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
        print("  `verify` checks durability only; this invariant checks idempotency.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
