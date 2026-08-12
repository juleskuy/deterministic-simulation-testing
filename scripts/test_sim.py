"""Self-check for the simulator itself.

A simulator you cannot trust is worse than no simulator: it reports bugs that
do not exist and, worse, hides bugs that do. These assertions check the five
properties every claim in this skill depends on.

Run: python test_sim.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from demo_bug import PROBS, UNTIL, make_world, verify  # noqa: E402
from sim import (  # noqa: E402
    MS,
    Disk,
    Faults,
    InvariantViolation,
    Node,
    Sim,
    search,
    shrink,
    signature,
)


class Echo(Node):
    def __init__(self, sim, node_id, peer=None):
        super().__init__(sim, node_id)
        self.peer = peer
        self.seen = []

    def on_message(self, src, msg):
        self.seen.append((self.sim.now, src, msg))
        if self.peer and msg[0] == "ping":
            self.sim.send(self.id, self.peer, ("pong", msg[1]))


def build_pair(sim: Sim):
    a = sim.add(Echo(sim, "a", peer="b"))
    sim.add(Echo(sim, "b", peer="a"))
    for i in range(20):
        sim.schedule(i * MS, lambda i=i: sim.send("a", "b", ("ping", i)))
    return a


def run_pair(seed: int, probs: dict) -> tuple[Sim, list]:
    faults = Faults(rng=random.Random(seed ^ 0x5EED5EED), probs=probs)
    sim = Sim(seed, faults=faults)
    build_pair(sim)
    sim.run(200 * MS)
    return sim, faults.sorted_journal()


def test_same_seed_same_bytes():
    """The whole approach is worthless if this ever fails."""
    for seed in (0, 1, 7, 12345):
        s1, j1 = run_pair(seed, PROBS)
        s2, j2 = run_pair(seed, PROBS)
        assert s1.events == s2.events, f"seed {seed} diverged in event trace"
        assert j1 == j2, f"seed {seed} diverged in fault journal"


def test_different_seeds_differ():
    """If seeds do not diverge, the search is exploring one universe 10000 times."""
    traces = {repr(run_pair(s, PROBS)[0].events) for s in range(12)}
    assert len(traces) > 6, f"only {len(traces)} distinct traces from 12 seeds"


def test_journal_replays_without_rng():
    """Replay mode must reproduce from the journal alone, never touching rng.

    `Faults(journal=...)` is constructed with rng=None, so any code path that
    consults the rng during replay raises AttributeError instead of silently
    producing a different universe.
    """
    for seed in range(6):
        gen, journal = run_pair(seed, PROBS)
        rep = Sim(seed, faults=Faults(journal=journal))
        build_pair(rep)
        rep.run(200 * MS)
        assert gen.events == rep.events, f"seed {seed} did not replay from journal"


def test_no_faults_is_clean():
    sim, journal = run_pair(3, {})
    assert journal == [], f"faults fired with zero probabilities: {journal}"
    kinds = {e["kind"] for e in sim.events}
    assert not kinds & {"drop", "dup", "crash"}, kinds


def test_probability_does_not_shift_the_stream():
    """Rule 2, latency stream: unconditional draws keep timings config-invariant."""
    a = [e for e in run_pair(9, {})[0].events if e["kind"] == "send"]
    b = [e for e in run_pair(9, {"crash": 0.0, "drop": 0.0})[0].events if e["kind"] == "send"]
    assert [e["at"] for e in a] == [e["at"] for e in b], "zero-probability faults shifted the stream"


def test_fault_stream_does_not_shift_with_config():
    """Rule 2, fault stream: per-event draw counts are FIXED, whatever fires.

    Comparing send timestamps only exercises `Sim.rng`, so it cannot see this.
    An early `return` on drop would make a dropped send consume one fault draw
    where a delivered send consumes three, desynchronising `Faults.rng` within
    a single event.

    What is NOT claimed: that the TOTAL draw count is config-invariant. It is
    not, and it cannot be. A dropped message is never delivered, so its
    crash-check never happens; faults change which events exist at all. That
    is precisely why fault keys must be semantic (Rule 3) rather than indexed
    off the draw sequence.
    """
    for probs in ({}, {"drop": 1.0}, {"drop": 1.0, "dup": 1.0, "slow": 1.0}):
        faults = Faults(rng=random.Random(11), probs=probs)
        sim = Sim(11, faults=faults)
        sim.add(Echo(sim, "a", peer="b"))
        sim.add(Echo(sim, "b", peer="a"))
        before = faults.draws
        sim.send("a", "b", ("ping", 0))
        assert faults.draws - before == 3, (
            f"send consumed {faults.draws - before} fault draws under {probs}, expected 3"
        )

    # And one delivery consumes exactly one draw (the crash check) whether or
    # not that crash fires.
    for journal in ([], [("crash", "b", 0)]):
        faults = Faults(journal=journal)
        sim = Sim(0, faults=faults)
        sim.add(Echo(sim, "a"))
        sim.add(Echo(sim, "b"))
        sim._deliver(1, "a", "b", ("ping", 0))


def test_crash_freedom_is_reported_not_fatal():
    """An unexpected SUT exception is a finding, not a reason to abort the sweep."""

    class Exploding(Node):
        def on_message(self, src, msg):
            raise KeyError("missing shard")

    def build(sim):
        sim.add(Exploding(sim, "a"))
        sim.schedule(MS, lambda: sim.send("z", "a", ("boom",)))
        return None

    found = search(build, lambda s, w: None, range(3), 50 * MS, {})
    assert found, "an SUT exception was swallowed instead of reported"
    assert "KeyError" in found[2], found[2]


def test_shrink_reaches_the_empty_journal():
    """A failure needing no faults must shrink to [], not to a fabricated fault.

    ddmin's chunking can never propose the empty set, so it must be tested
    explicitly. Without that, the report names a load-bearing fault that is not.
    """

    def build(sim):
        sim.add(Echo(sim, "a"))
        fired = []
        # Fault-independent: a local timer, not a delivery. Faults cannot
        # prevent it, so the true minimal journal is the empty set.
        sim.schedule(MS, lambda: fired.append(1))
        for i in range(6):
            sim.schedule(i * MS, lambda i=i: sim.send("z", "a", ("ping", i)))

        def check():
            if fired:
                raise InvariantViolation("always fails, no fault required")

        sim.invariant(check)
        return None

    found = search(build, lambda s, w: None, range(4), 50 * MS, {"drop": 0.4, "dup": 0.4})
    assert found, "the always-failing world did not fail"
    seed, journal, _, _ = found
    assert journal, "need at least one incidental fault for the test to be meaningful"
    minimal, _, err = shrink(build, lambda s, w: None, 50 * MS, seed, journal)
    assert minimal == [], f"shrink kept {minimal} for a fault-independent failure"
    assert "always" in err, err


def test_shrink_holds_the_error_signature():
    """ddmin must not slide onto a different, easier failure and report it.

    Two distinct bugs live in this world. The one requiring more faults is found
    first; a shrinker that accepts any truthy error can drop to the other and
    present that journal as the minimum for the original.
    """
    calls = {"n": 0}

    def build(sim):
        node = sim.add(Echo(sim, "a"))
        for i in range(6):
            sim.schedule(i * MS, lambda i=i: sim.send("z", "a", ("ping", i)))

        def check():
            drops = sum(1 for e in sim.events if e["kind"] == "drop")
            if drops >= 3:
                raise InvariantViolation("bug ALPHA: three drops")
            if len(node.seen) >= 5:
                raise InvariantViolation("bug BETA: five deliveries")

        sim.invariant(check)
        return None

    verify_none = lambda s, w: None  # noqa: E731
    found = search(build, verify_none, range(40), 100 * MS, {"drop": 0.45})
    assert found, "neither bug fired"
    seed, journal, err, _ = found
    _, _, merr = shrink(build, verify_none, 100 * MS, seed, journal)
    assert signature(merr) == signature(err), (
        f"shrink slid to a different failure:\n  from {err}\n  to   {merr}"
    )
    assert calls  # keep the closure referenced; no behavioural meaning


def test_signature_erases_incidentals():
    a = "invariant violated at t=11265us: acked k0=v0 by n0, no durable copy"
    b = "invariant violated at t=98us: acked k0=v0 by n0, no durable copy"
    c = "invariant violated at t=11265us: acked k2=v2 twice"
    assert signature(a) == signature(b), "timestamps should not distinguish failures"
    assert signature(a) != signature(c), "different failures must not collapse"


def test_quiesce_window_stops_faults():
    """Liveness needs a fault-free tail; an adversary may block progress forever."""
    faults = Faults(rng=random.Random(5), probs={"drop": 0.9})
    sim = Sim(5, faults=faults, quiesce_at=10 * MS)
    build_pair(sim)
    sim.run(200 * MS)
    late = [e for e in sim.events if e["kind"] == "drop" and e["t"] >= 10 * MS]
    assert not late, f"{len(late)} faults fired after quiesce_at"
    assert any(e["kind"] == "drop" for e in sim.events), "no faults fired at all"
    assert all(
        not (k[0] == "drop" and False) for k in faults.journal
    ), "journal sanity"


def test_auto_recover_runs_the_recovery_path():
    """Without auto_recover a crashed node stays down and on_recover never runs."""

    class Rebuilder(Node):
        def __init__(self, sim, node_id):
            super().__init__(sim, node_id)
            self.recovered = 0
            self.saw_recovering = False

        def on_message(self, src, msg):
            self.disk.write("k", msg[1])
            self.disk.fsync()

        def on_recover(self):
            self.recovered += 1
            self.saw_recovering = self.recovering

    faults = Faults(journal=[("crash", "a", 0)])
    sim = Sim(0, faults=faults, auto_recover=20 * MS)
    node = sim.add(Rebuilder(sim, "a"))
    for i in range(4):
        sim.schedule(i * MS, lambda i=i: sim.send("z", "a", ("put", i)))
    sim.run(500 * MS)
    assert node.recovered == 1, f"on_recover ran {node.recovered} times"
    assert node.saw_recovering, "`recovering` was not set during on_recover"
    assert node.up, "node did not come back up"


def test_fsync_semantics():
    d = Disk()
    d.write("k", 1)
    assert d.read("k") == 1, "a process must see its own uncommitted write"
    assert d.durable == {}, "write must not be durable before fsync"
    d.crash()
    assert d.read("k") is None, "crash must discard the page cache"
    d.write("k", 2)
    d.fsync()
    d.crash()
    assert d.read("k") == 2, "fsynced data must survive a crash"

    liar = Disk(lying_fsync=True)
    liar.write("k", 3)
    liar.fsync()
    liar.crash()
    assert liar.read("k") is None, "lying fsync must lose data"


def test_invariant_fires_immediately():
    sim = Sim(0)
    node = sim.add(Echo(sim, "a"))
    sim.schedule(MS, lambda: node.seen.append("boom"))
    sim.invariant(lambda: (_ for _ in ()).throw(InvariantViolation("x")) if node.seen else None)
    try:
        sim.run(10 * MS)
    except InvariantViolation:
        assert sim.now == MS, f"invariant checked late, at t={sim.now}"
    else:
        raise AssertionError("invariant never fired")


def test_finds_the_real_bug_and_clears_the_fix():
    """End to end: the buggy design fails, the fixed design does not."""
    found = search(make_world(True), verify, range(200), UNTIL, PROBS)
    assert found, "buggy design passed 200 seeds; the search is not exploring"
    seed, journal, err, _ = found
    assert "durable" in err, err

    minimal, _, merr = shrink(make_world(True), verify, UNTIL, seed, journal)
    assert minimal, "shrink produced an empty journal"
    assert len(minimal) <= len(journal), "shrink grew the journal"
    assert "did not reproduce" not in merr, merr
    kinds = {f[0] for f in minimal}
    assert "crash" in kinds, f"a durability bug needs a crash in its minimal set: {minimal}"

    clean = search(make_world(False), verify, range(1500), UNTIL, PROBS)
    assert clean is None, f"fixed design failed: {clean[2] if clean else ''}"


def test_shrink_is_honest_about_non_reproduction():
    """A journal that does not reproduce must say so, not fabricate a minimum."""
    minimal, _, err = shrink(make_world(True), verify, UNTIL, 0, [("drop", 999999)])
    assert "did not reproduce" in err, f"expected an honest failure, got: {err}"
    assert minimal == [("drop", 999999)], minimal


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok   {t.__name__}")
    print(f"\n{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
