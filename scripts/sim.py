"""Deterministic simulation testing core.

A whole distributed system, its network, its disks, and its clock, collapsed into
one single-threaded loop driven by one integer seed. Same seed, same bytes, every
time, on every machine.

Three rules make this work, and breaking any one of them silently destroys the
value of the whole approach:

1. ONE source of randomness. Everything nondeterministic reads from `Sim.rng` or
   `Faults.rng`. No `time.now()`, no module-level `random` calls, no thread
   scheduling, no iteration over unordered containers.
2. Randomness is drawn UNCONDITIONALLY, then used or discarded. A branch that
   skips a draw shifts the entire downstream stream and destroys comparability
   between configurations. This applies to BOTH streams: see `Sim.send`, which
   draws all three latency values and all three fault decisions before acting
   on any of them.
3. Faults are addressed by SEMANTIC key, never by wall-clock time or draw index.
   `("crash", node_id, messages_processed)` survives shrinking; `("crash", t=4711us)`
   does not.

stdlib only. No dependencies, ever.
"""

from __future__ import annotations

import heapq
import random
import re
from typing import Any, Callable, Iterable, Iterator, Optional

__all__ = [
    "Sim",
    "Faults",
    "Disk",
    "Node",
    "InvariantViolation",
    "DEFAULT_PROBS",
    "HARSH_PROBS",
    "search",
    "shrink",
    "signature",
]

US = 1  # simulator time unit: one microsecond
MS = 1000 * US

# The one canonical starting point. Referenced by the docs and the demo so there
# is a single number to tune rather than three that disagree.
DEFAULT_PROBS = {"drop": 0.10, "dup": 0.05, "slow": 0.10, "crash": 0.02}

# Diagnostic only. If an adversary this aggressive cannot violate an invariant,
# the invariant is too weak; fix that before tuning probabilities.
HARSH_PROBS = {"drop": 0.50, "dup": 0.20, "slow": 0.30, "crash": 0.10}


class InvariantViolation(AssertionError):
    """Raised the instant a system invariant stops holding."""


class Faults:
    """Decides whether a given fault fires, and remembers that it did.

    Two modes:

    * generate - decisions come from `rng` against `probs`; every fault that
      fires is recorded in `journal`.
    * replay - `journal` is authoritative and `rng` is never consulted, so a
      journal can be edited (shrunk) and re-run.

    Only POSITIVE decisions are journalled. That makes the journal a set of
    "things that went wrong", which is exactly the object you want to minimise.
    """

    def __init__(
        self,
        rng: random.Random | None = None,
        journal: Iterable[tuple] | None = None,
        probs: dict[str, float] | None = None,
    ) -> None:
        if rng is None and journal is None:
            raise ValueError("Faults needs an rng (generate) or a journal (replay)")
        self.rng = rng
        self.replay = journal is not None
        self.journal: set[tuple] = set(journal or ())
        self.probs = dict(probs or {})
        self.draws = 0

    def hit(self, kind: str, *key: Any) -> bool:
        """Would fault `kind` fire for this semantic key?"""
        k = (kind, *key)
        if self.replay:
            return k in self.journal
        p = self.probs.get(kind, 0.0)
        # Draw unconditionally so the stream does not depend on `p`.
        self.draws += 1
        roll = self.rng.random()
        if p > 0.0 and roll < p:
            self.journal.add(k)
            return True
        return False

    def sorted_journal(self) -> list[tuple]:
        return sorted(self.journal, key=repr)


class Disk:
    """A disk that tells the truth about `fsync`.

    `write` lands in the OS page cache. It is NOT durable. A crash discards it.
    `fsync` is the only operation that makes bytes survive a crash.
    `read` sees the page cache first, so a process cannot detect its own
    missing durability by reading back what it just wrote. This is precisely
    why lost-write bugs survive unit tests.
    """

    def __init__(self, lying_fsync: bool = False) -> None:
        self.durable: dict[Any, Any] = {}
        self.cache: dict[Any, Any] = {}
        self.lying_fsync = lying_fsync
        self.fsyncs = 0

    def write(self, key: Any, value: Any) -> None:
        self.cache[key] = value

    def fsync(self) -> None:
        self.fsyncs += 1
        if self.lying_fsync:
            # Some consumer SSDs and virtualised disks acknowledge fsync while
            # the data is still volatile. Off by default: with this enabled NO
            # single-disk design can be correct, so it belongs in a separate
            # test tier, not in your default suite.
            return
        self.durable.update(self.cache)
        self.cache.clear()

    def read(self, key: Any, default: Any = None) -> Any:
        if key in self.cache:
            return self.cache[key]
        return self.durable.get(key, default)

    def crash(self) -> None:
        self.cache.clear()

    def durable_items(self) -> list[tuple]:
        return sorted(self.durable.items(), key=repr)


class Node:
    """Base class for a simulated process. Override the hooks you need."""

    def __init__(self, sim: "Sim", node_id: Any) -> None:
        self.sim = sim
        self.id = node_id
        self.disk = Disk()
        self.up = True
        self.processed = 0
        self.recovering = False

    # -- hooks ---------------------------------------------------------------
    def on_message(self, src: Any, msg: Any) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def on_timer(self, name: str) -> None:
        pass

    def on_crash(self) -> None:
        """Drop volatile state here. Called after the disk cache is discarded."""

    def on_recover(self) -> None:
        """Rebuild volatile state from `self.disk.durable`."""

    # -- lifecycle -----------------------------------------------------------
    def crash(self) -> None:
        if not self.up:
            return
        self.up = False
        self.recovering = False
        self.disk.crash()
        self.on_crash()
        self.sim.trace("crash", node=self.id, after_messages=self.processed)
        if self.sim.auto_recover is not None:
            self.sim.schedule(self.sim.auto_recover, self.recover)

    def recover(self) -> None:
        """Come back up and rebuild from durable state.

        `recovering` is True for the duration of `on_recover`, so an invariant
        or a fault can target the recovery path specifically. Recovery is the
        least-tested code in most systems and the highest-value crash point.
        """
        if self.up:
            return
        self.up = True
        self.recovering = True
        try:
            self.on_recover()
        finally:
            self.recovering = False
        self.sim.trace("recover", node=self.id)


class Sim:
    """Single-threaded event loop standing in for a whole machine room."""

    def __init__(
        self,
        seed: int = 0,
        faults: Faults | None = None,
        latency: tuple[int, int] = (200 * US, 5 * MS),
        quiesce_at: int | None = None,
        auto_recover: int | None = None,
    ) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self.faults = faults or Faults(rng=random.Random(seed ^ 0x5EED5EED))
        self.latency = latency
        # After `quiesce_at`, faults stop firing. Required for any LIVENESS
        # invariant: an adversary may legitimately prevent progress forever, so
        # "eventually" is only meaningful once the adversary stops.
        self.quiesce_at = quiesce_at
        # If set, a crashed node reboots after this delay. Without it a crashed
        # node stays down for the rest of the run and recovery code never runs.
        self.auto_recover = auto_recover
        self.now = 0
        self._queue: list[tuple[int, int, Callable[[], None]]] = []
        self._tiebreak = 0
        self._msg_id = 0
        self.nodes: dict[Any, Node] = {}
        self.events: list[dict] = []
        self.invariants: list[Callable[[], None]] = []

    # -- wiring --------------------------------------------------------------
    def add(self, node: Node) -> Node:
        self.nodes[node.id] = node
        return node

    def invariant(self, fn: Callable[[], None]) -> None:
        """Register a predicate re-checked after EVERY event.

        Checking continuously rather than at the end is what turns a vague
        "the final state looks wrong" into "it broke at t=8412us, here."
        """
        self.invariants.append(fn)

    def trace(self, kind: str, **fields: Any) -> None:
        self.events.append({"t": self.now, "kind": kind, **fields})

    # -- scheduling ----------------------------------------------------------
    def schedule(self, delay: int, fn: Callable[[], None]) -> None:
        self._tiebreak += 1
        heapq.heappush(self._queue, (self.now + max(0, delay), self._tiebreak, fn))

    def set_timer(self, node_id: Any, delay: int, name: str) -> None:
        def fire() -> None:
            node = self.nodes[node_id]
            if node.up:
                node.on_timer(name)

        self.schedule(delay, fire)

    # -- faults --------------------------------------------------------------
    def _fault(self, kind: str, *key: Any) -> bool:
        """Consult the fault model, honouring the quiesce window.

        The draw happens either way (Rule 2). Only the DECISION is suppressed
        after `quiesce_at`, and the suppressed entry is removed from the journal
        so a shrunk journal never contains a fault that cannot fire.
        """
        hit = self.faults.hit(kind, *key)
        if hit and self.quiesce_at is not None and self.now >= self.quiesce_at:
            if not self.faults.replay:
                self.faults.journal.discard((kind, *key))
            return False
        return hit

    # -- network -------------------------------------------------------------
    def send(self, src: Any, dst: Any, msg: Any) -> None:
        """Deliver `msg`, subject to the fault model.

        Rule 2 in force: THREE latency values and THREE fault decisions are
        drawn before any of them is acted on. An early `return` on drop would
        make a dropped send consume one fault draw where a delivered send
        consumes three, so seed N with drops enabled would explore a different
        universe than seed N without, and nothing would be comparable.
        """
        self._msg_id += 1
        mid = self._msg_id
        lo, hi = self.latency
        base = self.rng.randint(lo, hi)
        dup_gap = self.rng.randint(lo, hi)
        slow_mult = self.rng.randint(10, 100)

        dropped = self._fault("drop", mid)
        slowed = self._fault("slow", mid)
        duplicated = self._fault("dup", mid)

        if dropped:
            self.trace("drop", mid=mid, src=src, dst=dst, msg=_brief(msg))
            return

        delay = base * slow_mult if slowed else base
        self.trace("send", mid=mid, src=src, dst=dst, at=self.now + delay, msg=_brief(msg))
        self.schedule(delay, lambda: self._deliver(mid, src, dst, msg))

        if duplicated:
            self.trace("dup", mid=mid, src=src, dst=dst)
            self.schedule(delay + dup_gap, lambda: self._deliver(mid, src, dst, msg))

    def _deliver(self, mid: int, src: Any, dst: Any, msg: Any) -> None:
        node = self.nodes[dst]
        if not node.up:
            self.trace("lost_to_down_node", mid=mid, dst=dst)
            return
        # Crash keyed on how much work the node has done, not on the clock, so
        # the key stays meaningful when earlier faults are shrunk away.
        if self._fault("crash", dst, node.processed):
            node.crash()
            return
        node.processed += 1
        node.on_message(src, msg)

    # -- running -------------------------------------------------------------
    def run(self, until: int) -> None:
        while self._queue and self._queue[0][0] <= until:
            t, _, fn = heapq.heappop(self._queue)
            self.now = t
            fn()
            for check in self.invariants:
                check()
        self.now = until

    def recover_all(self) -> None:
        for nid in sorted(self.nodes, key=repr):
            self.nodes[nid].recover()

    def replay_text(self, limit: int = 60) -> str:
        out = []
        for e in self.events[:limit]:
            fields = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("t", "kind"))
            out.append(f"  {e['t']:>9}us  {e['kind']:<18} {fields}")
        if len(self.events) > limit:
            out.append(f"  ... {len(self.events) - limit} more events")
        return "\n".join(out)


def _brief(msg: Any) -> str:
    s = repr(msg)
    return s if len(s) <= 60 else s[:57] + "..."


# ---------------------------------------------------------------------------
# Search and shrink
# ---------------------------------------------------------------------------

Build = Callable[[Sim], Any]
# PEP 604 unions are supported in annotations under ``from __future__ import
# annotations``, but this alias is EXECUTED at import time. ``str | None``
# therefore crashes Python 3.9 even though every other annotation parses.
# `Optional[str]` keeps the declared 3.9 CI floor honest.
Verify = Callable[[Sim, Any], Optional[str]]

_NUM = re.compile(r"\d+")


def signature(err: str | None) -> str:
    """Collapse an error message to a comparable shape.

    Timestamps, ids, and counts differ between runs of the same bug, so they
    are erased. What remains identifies WHICH failure occurred, which is what
    shrinking must hold constant.
    """
    return _NUM.sub("N", err or "")


def _run_once(
    build: Build,
    verify: Verify,
    until: int,
    seed: int,
    faults: Faults,
) -> tuple[Sim, str | None]:
    sim = Sim(seed, faults=faults)
    try:
        world = build(sim)
        sim.run(until)
    except InvariantViolation as exc:
        return sim, f"invariant violated at t={sim.now}us: {exc}"
    except Exception as exc:
        # Crash-freedom is the cheapest invariant there is. An unexpected
        # exception from the system under test is a FINDING, not a reason to
        # abandon the sweep.
        return sim, f"{type(exc).__name__} at t={sim.now}us: {exc}"
    err = verify(sim, world)
    return sim, err


def search(
    build: Build,
    verify: Verify,
    seeds: Iterable[int],
    until: int,
    probs: dict[str, float] | None = None,
    quiesce_at: int | None = None,
) -> tuple[int, list[tuple], str, Sim] | None:
    """Run `build`/`verify` across seeds until something breaks.

    Returns `(seed, journal, error, sim)` for the first failure, else None.
    One seed is one universe. Thousands of cheap universes beat one careful
    hand-written test, because you are sampling the interleaving space instead
    of guessing at it.
    """
    probs = DEFAULT_PROBS if probs is None else probs
    for seed in seeds:
        faults = Faults(rng=random.Random(seed ^ 0x5EED5EED), probs=probs)
        sim = Sim(seed, faults=faults, quiesce_at=quiesce_at)
        try:
            world = build(sim)
            sim.run(until)
            err = verify(sim, world)
        except InvariantViolation as exc:
            err = f"invariant violated at t={sim.now}us: {exc}"
        except Exception as exc:
            err = f"{type(exc).__name__} at t={sim.now}us: {exc}"
        if err:
            return seed, faults.sorted_journal(), err, sim
    return None


def shrink(
    build: Build,
    verify: Verify,
    until: int,
    seed: int,
    journal: list[tuple],
) -> tuple[list[tuple], Sim, str]:
    """Delta-debug the fault journal down to a minimal still-failing set.

    ddmin over the set of faults, with two properties that a naive
    implementation gets wrong:

    * Every candidate is RE-RUN. Removing a fault changes which messages exist,
      so a smaller journal is a different universe, not a subset of this one.
    * The candidate must fail with the SAME error signature. Accepting any
      failure lets ddmin slide onto a different, easier-to-trigger bug and
      report that journal as the minimum for the original, which is a false
      report that looks authoritative.

    The empty journal is tested explicitly: some failures need no faults at all,
    and ddmin's chunking can never propose the empty set on its own. Without
    that check the result claims a fault is load-bearing when it is not.
    """
    original = list(journal)
    sim, err = _run_once(build, verify, until, seed, Faults(journal=original))
    if not err:
        # The journal alone does not reproduce it, so there is nothing to
        # minimise honestly. Hand back the original and say so.
        return original, sim, "journal did not reproduce; report the seed instead"
    sig = signature(err)

    def fails_same(candidate: list[tuple]):
        cs, ce = _run_once(build, verify, until, seed, Faults(journal=candidate))
        if ce and signature(ce) == sig:
            return cs, ce
        return None, None

    zero_sim, zero_err = fails_same([])
    if zero_err:
        return [], zero_sim, zero_err

    current = original
    n = 2
    while len(current) >= 2:
        chunk = max(1, len(current) // n)
        reduced = None
        for i in range(0, len(current), chunk):
            candidate = current[:i] + current[i + chunk :]
            if not candidate:
                continue  # the empty set was already tested above
            cs, ce = fails_same(candidate)
            if ce:
                reduced, sim, err = candidate, cs, ce
                break
        if reduced is None:
            if n >= len(current):
                break  # chunk reached 1: every single removal was tried
            n = min(len(current), n * 2)
        else:
            current = reduced
            n = 2
    return current, sim, err


def seeds(count: int, start: int = 0) -> Iterator[int]:
    return iter(range(start, start + count))
