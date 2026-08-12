# deterministic-simulation-testing

A `SKILL.md` for testing concurrent or fault-prone systems with a deterministic
simulator. It includes a small Python implementation, a worked failure, and tests
for the claims made in the documentation.

The approach is familiar from systems such as FoundationDB and TigerBeetle. Replace
the real clock, network, and disk with controlled models, then run many seeded
executions. A failed run leaves behind a replayable fault journal.

## Worked example

`scripts/demo_bug.py` plants a real bug in a three-node replicated log with group
commit: it acknowledges the client when a write enters the fsync batch, instead of
after the batch is durable. The same bug class has shipped in real databases. Under
zero faults it is invisible, because a process reads its own page cache and cannot
detect its own missing durability.

```
$ python scripts/demo_bug.py
BUGGY  ack-on-batch-entry, searching 200 seeds ...
  seed 48: invariant violated at t=7474us: acked k0=v0 by n0, n0 is down, no durable copy anywhere
  4 faults fired

  shrunk 4 faults -> 1
    ('crash', 'n0', 1)

  minimal replay:
          0us  send    mid=1 src=client dst=n0 at=4690 msg=('put', 'k0', 'v0')
       4690us  send    mid=2 src=n0 dst=n1 at=9452 msg=('repl', 'k0', 'v0')
       4690us  send    mid=3 src=n0 dst=n2 at=9029 msg=('repl', 'k0', 'v0')
       4690us  ack     key=k0 value=v0 by=n0
       6556us  crash   node=n0 after_messages=1

FIXED  ack-after-durable, searching 10000 seeds ...
  10000 seeds, no violation

Idempotency check: one put => at most one ack ...
  seed 227: invariant violated at t=37345us: acked k4=v4 twice
  shrunk 10 faults -> 2: [('drop', 7), ('dup', 10)]
```

In the replay, `k0` is acknowledged at 4690us while both replication messages are
still in flight and no copy is durable. The leader crashes at 6556us, so the client
has an acknowledgement for a write that cannot be recovered. The smallest replay
contains one crash fault. It ran in under a second on this repository's test setup.

The second result is deliberate. The durability fix clears 10,000 seeds, yet the
same design can still acknowledge one write twice because `verify` checks durability
only. A passing sweep describes the properties that were checked, not every property
the system might need.

## What is here

```
SKILL.md                        the skill: when to use DST, the three rules, workflow
references/nondeterminism.md    per-language taxonomy of nondeterminism + fixes
references/fault-models.md      what to inject, key design, realistic distributions
references/invariants.md        invariants that catch bugs vs restate the code
references/shrinking.md         ddmin, why re-verification is mandatory
scripts/sim.py                  simulator core: clock, network, disk, crash, shrink
scripts/demo_bug.py             the planted bug, found and shrunk
scripts/scan_nondeterminism.py  9-language scanner for Rule 1 violations
scripts/test_sim.py             16 assertions on the simulator itself
scripts/test_scan.py            12 assertions on the scanner
scripts/test_docs.py            executes the snippets in references/*.md
```

Everything is stdlib Python. No dependencies, no install, no network.

## The three rules

Most first attempts at DST fail for the same three reasons, and each failure produces
a simulator that looks like it works while proving nothing.

**1. Exactly one source of nondeterminism.** Every nondeterministic decision reads
from one seeded RNG. The ones people forget: `set` iteration order, Go map ranging,
Rust's randomly seeded `HashMap`, default object hashes under ASLR, a log line that
stamps the wall clock. `scripts/scan_nondeterminism.py` finds these across Python,
Rust, Go, JS/TS, JVM, C/C++, .NET, Ruby, and Elixir.

**2. Draw randomness unconditionally, then decide.** A fault check that is skipped
when its probability is zero shifts every downstream random value, so seed 42 with
faults enabled becomes a different universe from seed 42 without. Then nothing is
comparable and shrinking is fiction. Precisely: within a single event, the number of
draws must not depend on which faults fire. The total across a run is NOT
config-invariant and cannot be, which is why Rule 3 exists.

**3. Key faults semantically, never by time or draw index.** `("crash", node, 2)`
survives shrinking. `("crash", t=4711)` is meaningless the moment one earlier fault
is removed.

Full reasoning, and the assertions that guard each rule, are in `SKILL.md`.

## Install as a skill

Clone into the skill directory your agent reads. The repository itself is the skill
folder because `SKILL.md` is at its root.

```bash
# Claude Code
mkdir -p ~/.claude/skills
git clone https://github.com/juleskuy/deterministic-simulation-testing \
  ~/.claude/skills/deterministic-simulation-testing

# Hermes
mkdir -p "$HOME/AppData/Local/hermes/skills"
git clone https://github.com/juleskuy/deterministic-simulation-testing \
  "$HOME/AppData/Local/hermes/skills/deterministic-simulation-testing"
```

For Codex, Cursor, or another Agent Skills-compatible client, clone the same folder
into that client's configured skills root. This repo uses the plain
[agentskills.io](https://agentskills.io) layout. Ask the agent to run
`scripts/demo_bug.py`; it will show the technique on real output rather than
summarising it.

## Use the simulator directly

```python
from sim import Sim, Node, Faults, search, shrink, InvariantViolation, MS

class Server(Node):
    def on_message(self, src, msg):
        self.disk.write(msg[1], msg[2])
        self.disk.fsync()                 # durable. without this, a crash loses it

def build(sim):
    sim.add(Server(sim, "s"))
    sim.schedule(MS, lambda: sim.send("c", "s", ("put", "k", "v")))
    sim.invariant(lambda: ...)            # re-checked after EVERY event
    return None

found = search(build, verify, range(1000), 5000 * MS,
               {"drop": 0.1, "crash": 0.02})
if found:
    seed, journal, err, _ = found
    minimal, sim, err = shrink(build, verify, 5000 * MS, seed, journal)
```

`Disk` tells the truth about `fsync`: `write` lands in a volatile page cache, only
`fsync` makes bytes durable, `crash` discards the cache, and `read` sees the cache
first. That last detail is why lost-write bugs pass unit tests.

## Honest limits

A simulator tests your logic against your model of the world. It cannot find a bug in
the Postgres wire protocol, the kernel, or your disk firmware, and it cannot see code
paths the simulation replaced with a stub. Keep a thin real-integration tier for
those, and say which is which when reporting results.

`10,000 seeds, no violation` is a claim with content. `it is correct` is not.

The scanner has a stated blind spot it prints on every run: `for x in items:` cannot
be judged lexically, because the type of `items` is unknown. Set iteration is the
highest-frequency nondeterminism source in real code, so bare loops are a manual
audit. A lexical scan narrows the search; only `test_same_seed_same_bytes` proves
anything.

The documentation and implementation were reviewed together. The resulting fixes
have regression tests so the documented behavior and code stay aligned.

## License

MIT
