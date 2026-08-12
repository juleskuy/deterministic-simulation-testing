# Nondeterminism: complete taxonomy and elimination

Rule 1 is that exactly one seeded RNG drives every nondeterministic decision. This
file is the checklist for finding the ones you did not think of.

Run `scripts/scan_nondeterminism.py` first. It is a lexical scanner, so it finds
the common shapes and misses anything hidden behind an abstraction. Then verify
empirically: same seed, twice, byte-identical event trace. The scanner narrows the
search; only the empirical check proves anything.

**Where the scanner is weakest, stated up front:** category 3 below, iteration
order. `for x in items:` cannot be judged lexically because the type of `items` is
unknown, and that is the single most common shape in real code. The scanner catches
`list(set(...))`, `next(iter(...))`, comprehensions over set literals, set-operation
results, and suspiciously named containers (`seen`, `visited`, `ids`), and it prints
this limitation on every run. Bare loops over an unknown container are a manual
audit, or better, a reason to switch the container to an ordered type so the
question cannot arise.

## The eight categories

### 1. Time

Every clock read is nondeterminism. All of them become `sim.now`.

| Language | Hunt for | Replace with |
|---|---|---|
| Python | `time.time`, `time.monotonic`, `time.perf_counter`, `datetime.now`, `datetime.utcnow` | a `Clock` object reading `sim.now` |
| Rust | `Instant::now`, `SystemTime::now`, `tokio::time::*` | injected clock trait; `madsim`/`turmoil` provide one |
| Go | `time.Now`, `time.Since`, `time.After`, `time.Tick` | a `Clock` interface |
| JS | `Date.now`, `new Date()`, `performance.now`, `setTimeout`, `setInterval` | injected `now()`; sim-scheduled timers |
| JVM | `System.currentTimeMillis`, `System.nanoTime`, `Instant.now` | `java.time.Clock` injected |
| .NET | `DateTime.Now`, `DateTime.UtcNow`, `Stopwatch` | `TimeProvider` (net8+) or your own |

Second-order hits people miss: log formatters that stamp lines, cache TTL checks,
JWT `exp` validation, rate limiter windows, exponential backoff jitter, metrics
timestamps, HTTP `Date` headers, and database `NOW()`/`CURRENT_TIMESTAMP` evaluated
server-side.

Timezone and DST are a distinct hazard even with a virtual clock. Pin an explicit
timezone; never rely on the host's. A simulation that runs across a DST boundary in
the host's local zone is not reproducible on a machine in another zone.

### 2. Randomness and entropy

One seeded generator, threaded through explicitly. Global or thread-local state
is the enemy, because two runs share a process and diverge.

- Python: `random.*` module functions use one hidden global. `random.Random(seed)`
  instance is fine. `os.urandom`, `secrets`, `uuid.uuid4` are OS entropy and can
  never be seeded; derive ids from the seeded rng instead.
- Rust: `thread_rng()` is per-thread and reseeded from the OS. Use
  `StdRng::seed_from_u64`. Note `rand`'s `SmallRng` output is not stable across
  crate versions, so pin the version if a journal must replay months later.
- Go: `math/rand` top-level functions share global state and, since Go 1.20, are
  auto-seeded randomly. Use `rand.New(rand.NewSource(seed))`. `crypto/rand` cannot
  be seeded at all.
- JVM: `Math.random()` and `ThreadLocalRandom` are unseedable in practice. `new
  Random(seed)` is reproducible and specified in the Javadoc, which is rare and
  useful. `SecureRandom` is not.
- JS: `Math.random()` has no seed by specification. Ship a small PRNG such as
  mulberry32 or xoshiro128\*\*, and treat it as part of the system under test.

UUIDs deserve their own line. v4 is pure entropy, v1 and v7 embed a timestamp, and
both break replay. Under simulation, generate ids from the seeded rng or from a
monotonic counter, and keep the real generator behind an injectable interface.

### 3. Iteration order

The most under-appreciated category, because the code looks pure.

- **Go** randomises map range order deliberately, to stop people depending on it,
  and `select` chooses uniformly at random among ready cases. Both are specified
  behavior, not implementation detail.
- **Python** `dict` preserves insertion order since 3.7, so dicts are safe if
  insertion order is deterministic. `set` and `frozenset` are NOT: ordering depends
  on hash values, and `str`/`bytes` hashing is randomised per process unless
  `PYTHONHASHSEED` is fixed. Never iterate a set; sort it.
- **Rust** `HashMap`/`HashSet` use a randomly seeded SipHash per process. Use
  `BTreeMap`/`BTreeSet`, or a fixed-seed hasher.
- **JVM** `HashMap` order depends on capacity and hash; `LinkedHashMap` and
  `TreeMap` are ordered.
- **JS** `Object` key order is specified but surprising: integer-like keys come
  first in ascending numeric order, then string keys in insertion order. `Map` and
  `Set` are insertion-ordered and safe.
- **Filesystem** `readdir` order is filesystem-defined and differs between ext4,
  APFS, and NTFS, and between two directories with the same contents. Always sort.

### 4. Scheduling and concurrency

You do not control the OS scheduler, so the simulator must own concurrency.

The rule is one thread, one event queue. Threads, goroutines, thread pools, async
task runtimes, and `Promise.all` settlement order all become entries in a priority
queue keyed on virtual time.

Python's GIL does not help. It serialises bytecode execution but switches at
unpredictable points, so interleavings still vary run to run.

`GOMAXPROCS=1` in Go does not help either. Goroutines are still preemptible at
function calls and channel operations, and `select` remains randomised.

Rust has real solutions: `madsim` and `turmoil` provide deterministic executors with
`tokio`-compatible APIs, so production code runs unchanged under simulation. If you
work in Rust, use them rather than rebuilding this.

For everything else, the practical pattern is to restructure the system under test
so that all business logic is a pure function from `(state, event)` to
`(new_state, outgoing_effects)`. The simulator then applies events one at a time.
This restructuring is most of the work in adopting DST, and it improves testability
and reasoning about the code whether or not you finish the simulator.

### 5. Address and identity

Anything derived from a memory address varies per run under ASLR.

- Default `hash()` of an object without `__hash__`, Python `id()`, Ruby `object_id`,
  Java `identityHashCode`, C `%p`, Rust pointer casts.
- Default `__repr__`/`toString` implementations often embed an address, so a log
  line or an error message can leak nondeterminism into a comparison or a hash.
- Sorting by an address-derived key produces a different order every run.

### 6. Environment

- `os.environ` / `System.getenv` / `process.env`
- Locale: number formatting, string collation, case mapping, month names.
- Timezone: as above.
- CPU count: `os.cpu_count`, `runtime.NumCPU`, `availableProcessors`,
  `available_parallelism`. Any behavior that scales with core count changes between
  your laptop and CI.
- Available memory, disk free space, `ulimit`.
- Hostname, PID, current working directory, path separators.
- Library and language versions, when they change hashing, sorting stability, or
  float formatting.

### 7. Floating point

Reproducible only under constraints, and violating them is easy.

- Compiler fast-math flags reassociate operations and change results.
- FMA contraction changes results relative to separate multiply and add.
- x87 80-bit intermediates on 32-bit x86 differ from SSE double precision.
- Reduction order in parallel sums changes results. This is why `parallelStream`
  and Rayon can break bit-identical replay even with everything else fixed.
- Transcendental functions (`sin`, `exp`, `pow`) are not specified bit-exactly and
  differ between libm implementations, so a replay can diverge across platforms.
- Formatting: shortest-round-trip float printing differs by language version.

If exact float reproducibility matters, use integers or fixed-point in the
simulated logic. For money this is mandatory anyway.

### 8. Garbage collection and finalisation

Any behavior that depends on when memory is reclaimed is nondeterministic: weak
references, finalizers, `__del__`, `Drop` ordering across a cycle, cache eviction
driven by memory pressure, and object resurrection. Never branch on collection.

## The empirical check

Everything above is a heuristic. This is the proof:

```python
def test_same_seed_same_bytes():
    for seed in (0, 1, 7, 12345):
        a = run(seed)
        b = run(seed)
        assert a.events == b.events
```

Run it in CI, in a separate process per invocation, on more than one OS. Same-process
runs can hide `PYTHONHASHSEED`-style hazards, because the randomised seed is chosen
once at interpreter startup and shared by both runs.

Two failure signatures and what they mean:

- Diverges in the same process: unfixed logic nondeterminism. Bisect by diffing the
  two event traces and looking at the first differing event.
- Passes in one process, differs across processes: environment or hash seeding.
  Set `PYTHONHASHSEED=0`, pin `TZ` and `LC_ALL`, and re-check.

A useful bisect trick: dump the event trace to a file on divergence and `diff` the
two. The first differing line names the operation that read an unseeded source.

## Sign-off convention

Some nondeterminism is legitimate and lives at the boundary: the real clock in the
production adapter, real entropy in key generation. Mark those lines
`dst:allow <reason>` so the scanner stays quiet and reviewers can see the decision
was deliberate rather than missed. A codebase with zero suppressions and a working
simulator is either very well factored or hiding something.
