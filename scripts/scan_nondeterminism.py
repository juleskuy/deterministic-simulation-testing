#!/usr/bin/env python3
"""Find nondeterminism in code you intend to simulate.

Rule 1 of deterministic simulation testing is that exactly one seeded RNG drives
every nondeterministic decision. Rule 1 is violated in places people do not think
to look: a log line that reads the wall clock, a set iteration, a default object
hash. Each one silently breaks replay.

This lexical scanner reports candidates for review. A clean result does not prove a
file is deterministic.

Usage:
    scan_nondeterminism.py <path> [...]      # scan files or directories
    scan_nondeterminism.py src --json
    scan_nondeterminism.py src --lang go     # force language
Exit 1 if any HIGH-severity hit, else 0.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

__version__ = "1.0.0"

HIGH, MED, LOW = "high", "med", "low"

EXT_LANG = {
    ".py": "python", ".rs": "rust", ".go": "go", ".js": "js", ".mjs": "js",
    ".cjs": "js", ".ts": "js", ".tsx": "js", ".jsx": "js", ".java": "jvm",
    ".kt": "jvm", ".scala": "jvm", ".c": "c", ".cc": "c", ".cpp": "c",
    ".h": "c", ".hpp": "c", ".cs": "dotnet", ".rb": "ruby", ".ex": "beam",
    ".exs": "beam",
}
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "target", "dist",
    "build", ".next", ".tox", "vendor", ".mypy_cache", ".pytest_cache",
}

# (regex, severity, what breaks, how to fix)
RULES: dict[str, list[tuple[str, str, str, str]]] = {
    "python": [
        (r"\b(?:time\.time|time\.monotonic|time\.perf_counter)\s*\(",
         HIGH, "wall/monotonic clock", "read sim.now"),
        (r"\bdatetime\.(?:now|utcnow|today)\s*\(",
         HIGH, "wall clock", "inject a clock; derive from sim.now"),
        (r"\brandom\.(?!Random\b)[a-z_]+\s*\(",
         HIGH, "global random state", "use the injected rng instance"),
        (r"\bfrom\s+random\s+import\s+(?!Random\b)",
         HIGH, "bare random functions imported", "import Random and thread one instance"),
        (r"\b(?:os\.urandom|secrets\.|uuid\.uuid4|uuid\.uuid1)\s*\(?",
         HIGH, "OS entropy / time-based uuid", "derive ids from the seeded rng"),
        (r"\bfor\s+\w+\s+in\s+(?!sorted\b)\w+\s*\.\s*(?:keys|values|items)\s*\(\s*\)",
         MED, "dict iteration order", "sort the keys before iterating"),
        # Set iteration is the category nondeterminism.md calls the most
        # under-appreciated, so it gets every shape people actually write, not
        # just the literal `for x in set(...)` form.
        (r"\bfor\s+\w+\s+in\s+(?:set|frozenset)\s*\(",
         HIGH, "set iteration order", "sort, or use a list/dict"),
        (r"\b(?:list|tuple|enumerate|reversed)\s*\(\s*(?:set|frozenset)\s*\(",
         HIGH, "set iteration order", "sorted(...) instead of list(set(...))"),
        (r"\bnext\s*\(\s*iter\s*\(\s*(?:set|frozenset)?\s*\w*",
         HIGH, "arbitrary element from an unordered container", "sorted(...)[0]"),
        (r"\.\s*(?:join|extend)\s*\(\s*(?:set|frozenset)\s*\(",
         HIGH, "set iteration order", "sort before joining"),
        (r"\bfor\s+\w+\s+in\s+\w*(?:set|keys|ids|seen|visited|pending|remaining)\b\s*:",
         MED, "iteration of a likely-unordered container", "sort, or use an ordered type"),
        (r"\[[^\]]*\bfor\s+\w+\s+in\s+(?:set|frozenset)\s*\(",
         HIGH, "set iteration order in a comprehension", "iterate sorted(...)"),
        (r"\[[^\]]*\bfor\s+\w+\s+in\s+\w*(?:set|seen|visited|ids)\b",
         MED, "comprehension over a likely-unordered container", "iterate sorted(...)"),
        (r"\bfor\s+\w+\s+in\s+\w+\s*[|&^-]\s*\w+\s*:",
         HIGH, "iteration over a set operation result", "sort the result"),
        (r"\b(?:min|max|sum)\s*\(\s*(?:set|frozenset)\s*\(",
         LOW, "order-independent reduction over a set", "safe unless a tie-break is order-dependent"),
        (r"\bthreading\.(?:Thread|Timer)\s*\(|\bmultiprocessing\.",
         HIGH, "OS thread scheduling", "single-threaded executor + event queue"),
        (r"\basyncio\.(?:create_task|gather|wait|sleep|get_event_loop)\s*\(",
         MED, "task interleaving", "run tasks on the sim executor"),
        (r"\bconcurrent\.futures\.|\bThreadPoolExecutor\b|\bProcessPoolExecutor\b",
         HIGH, "pool scheduling", "single-threaded executor"),
        (r"\bos\.(?:listdir|scandir|walk)\s*\(",
         MED, "filesystem readdir order", "sort the results"),
        (r"\bglob\.(?:glob|iglob)\s*\(",
         MED, "glob order is filesystem order", "sorted(glob.glob(...))"),
        (r"\bhash\s*\(\s*(?!['\"0-9])",
         MED, "PYTHONHASHSEED / address-dependent hash", "hash stable bytes explicitly"),
        (r"\bid\s*\(\s*\w",
         MED, "address-dependent identity", "use an explicit stable id"),
        (r"\bgc\.|\bweakref\.",
         LOW, "GC timing observable", "do not branch on collection"),
        (r"\bos\.environ|\bos\.getenv\s*\(",
         LOW, "environment-dependent behavior", "pass config explicitly"),
        (r"\bos\.cpu_count\s*\(|\bmultiprocessing\.cpu_count\s*\(",
         MED, "host CPU count changes behavior", "pass a fixed parallelism"),
        (r"\blocale\.|\bstr\.format_map\b|\btime\.strftime\s*\(",
         LOW, "locale/timezone dependent formatting", "format in a fixed locale/UTC"),
        (r"\btzlocal\b|\bdatetime\.astimezone\s*\(\s*\)",
         MED, "host timezone", "pin an explicit tz"),
    ],
    "rust": [
        (r"\b(?:Instant|SystemTime)::now\s*\(",
         HIGH, "clock", "inject a clock trait; madsim/turmoil provide one"),
        (r"\bthread_rng\s*\(|\brand::random\s*(?:::<[^>]*>)?\s*\(",
         HIGH, "thread-local entropy", "StdRng::seed_from_u64(seed)"),
        (r"\bUuid::new_v4\s*\(",
         HIGH, "random uuid", "derive from the seeded rng"),
        (r"\bfor\s+\w+\s+in\s+(?:&|&mut\s+)?\w+\s*(?:\.iter\s*\(\s*\))?\s*\{",
         LOW, "iteration of a possibly-unordered map", "BTreeMap, or sort first"),
        (r"\bHashMap::new\b|\bHashSet::new\b",
         MED, "randomly seeded hasher", "BTreeMap/BTreeSet, or a fixed hasher"),
        (r"\bthread::spawn\s*\(|\btokio::spawn\s*\(",
         HIGH, "OS/tokio scheduling", "madsim or turmoil deterministic runtime"),
        (r"\btokio::time::(?:sleep|timeout)\s*\(",
         MED, "real timers", "deterministic runtime time"),
        (r"\bas\s+\*const\b|\bas\s+usize\b.*\bptr\b|\bAtomicPtr\b",
         LOW, "pointer-derived value", "never observe addresses"),
        (r"\bstd::env::(?:var|vars)\s*\(",
         LOW, "environment", "pass config explicitly"),
        (r"\bavailable_parallelism\s*\(",
         MED, "host CPU count", "fixed parallelism"),
    ],
    "go": [
        (r"\btime\.(?:Now|Since)\s*\(",
         HIGH, "clock", "inject a Clock interface"),
        (r"\brand\.(?:Int|Intn|Float64|Perm|Shuffle|Read)\b",
         HIGH, "global rand", "rand.New(rand.NewSource(seed))"),
        (r"\bfor\s+\w+(?:\s*,\s*\w+)?\s*:?=\s*range\s+\w+\s*\{",
         MED, "map range order is randomised by design", "collect keys, sort, iterate"),
        (r"\bgo\s+func\s*\(|\bgo\s+\w+\s*\(",
         HIGH, "goroutine scheduling", "funnel into one goroutine + event channel"),
        (r"\bselect\s*\{",
         HIGH, "select picks a ready case at random", "deterministic queue instead"),
        (r"\bsync\.(?:WaitGroup|Mutex|RWMutex|Once)\b",
         LOW, "lock acquisition order", "ordering must not affect the result"),
        (r"\buuid\.New\s*\(|\bcrypto/rand\b",
         HIGH, "entropy", "seeded generator"),
        (r"\bos\.(?:Getenv|Environ)\s*\(",
         LOW, "environment", "explicit config"),
        (r"\bruntime\.(?:NumCPU|GOMAXPROCS|GC)\s*\(",
         MED, "host/runtime dependent", "fixed values; GOMAXPROCS=1 is not sufficient"),
        (r"\bfilepath\.(?:Walk|WalkDir)\s*\(|\bos\.ReadDir\s*\(",
         MED, "readdir order", "sort the entries"),
    ],
    "js": [
        (r"\bDate\.now\s*\(|\bnew\s+Date\s*\(\s*\)|\bperformance\.now\s*\(",
         HIGH, "clock", "inject a now() that reads sim time"),
        (r"\bMath\.random\s*\(",
         HIGH, "unseedable PRNG", "a seeded PRNG such as mulberry32"),
        (r"\bcrypto\.(?:randomUUID|getRandomValues|randomBytes)\s*\(",
         HIGH, "entropy", "derive ids from the seeded rng"),
        (r"\b(?:setTimeout|setInterval|setImmediate|queueMicrotask)\s*\(",
         HIGH, "real timers and microtask interleaving", "sim.schedule / fake timers"),
        (r"\bPromise\.(?:all|race|any|allSettled)\s*\(",
         MED, "settlement order", "explicit ordering, deterministic executor"),
        (r"\bObject\.keys\s*\(|\bfor\s*\(\s*const\s+\w+\s+in\s+",
         MED, "integer-like keys reorder; prototype chain", "sort, or use Map"),
        (r"\bnew\s+Set\s*\(|\bnew\s+WeakMap\s*\(|\bnew\s+WeakSet\s*\(",
         LOW, "insertion/GC dependent", "sort on read; never observe GC"),
        (r"\bprocess\.env\b|\bnavigator\.|\bwindow\.",
         LOW, "environment", "explicit config"),
        (r"\btoLocaleString\s*\(|\bIntl\.",
         MED, "locale/timezone dependent", "fixed locale and UTC"),
        (r"\bWorker\s*\(|\bworker_threads\b|\bcluster\b",
         HIGH, "real concurrency", "single-threaded executor"),
    ],
    "jvm": [
        (r"\b(?:System\.currentTimeMillis|System\.nanoTime)\s*\(",
         HIGH, "clock", "inject java.time.Clock"),
        (r"\b(?:Instant|LocalDateTime|LocalDate|ZonedDateTime)\.now\s*\(",
         HIGH, "clock", "Clock.fixed / injected clock"),
        (r"\bnew\s+Random\s*\(\s*\)|\bMath\.random\s*\(|\bThreadLocalRandom\b",
         HIGH, "unseeded/thread-local PRNG", "new Random(seed) threaded through"),
        (r"\bUUID\.randomUUID\s*\(",
         HIGH, "random uuid", "seeded generator"),
        (r"\bnew\s+(?:HashMap|HashSet)\s*[<(]",
         MED, "iteration order", "LinkedHashMap/TreeMap"),
        (r"\bidentityHashCode\b|\bhashCode\s*\(\s*\)",
         MED, "address-dependent hash", "stable explicit hashing"),
        (r"\bnew\s+Thread\s*\(|\bExecutors\.|\bCompletableFuture\.|\bparallelStream\s*\(",
         HIGH, "thread scheduling", "single-threaded executor"),
        (r"\bSystem\.getenv\s*\(|\bSystem\.getProperty\s*\(",
         LOW, "environment", "explicit config"),
        (r"\bavailableProcessors\s*\(",
         MED, "host CPU count", "fixed parallelism"),
    ],
    "c": [
        (r"\b(?:time|clock|gettimeofday|clock_gettime)\s*\(",
         HIGH, "clock", "virtual clock"),
        (r"\b(?:rand|random|arc4random|drand48)\s*\(",
         HIGH, "PRNG state", "seeded generator threaded through"),
        (r"\b(?:pthread_create|std::thread|std::async)\b",
         HIGH, "thread scheduling", "single-threaded executor"),
        (r"\bgetenv\s*\(",
         LOW, "environment", "explicit config"),
        (r"%p\b|\bunordered_(?:map|set)\b",
         MED, "address printing / unordered container", "ordered containers; never print pointers"),
        (r"\bmalloc\s*\(|\bnew\s+\w+\[",
         LOW, "allocation address observable", "never branch on addresses"),
    ],
    "dotnet": [
        (r"\bDateTime\.(?:Now|UtcNow)\b|\bStopwatch\.GetTimestamp\s*\(",
         HIGH, "clock", "inject a time provider"),
        (r"\bnew\s+Random\s*\(\s*\)|\bRandom\.Shared\b|\bRandomNumberGenerator\b",
         HIGH, "unseeded PRNG", "new Random(seed)"),
        (r"\bGuid\.NewGuid\s*\(",
         HIGH, "random guid", "seeded generator"),
        (r"\bTask\.Run\s*\(|\bTask\.WhenAll\s*\(|\bParallel\.",
         HIGH, "thread pool scheduling", "single-threaded scheduler"),
        (r"\bnew\s+(?:Dictionary|HashSet)\s*<",
         MED, "iteration order", "SortedDictionary, or sort on read"),
    ],
    "ruby": [
        (r"\b(?:Time\.now|Time\.at|DateTime\.now)\b",
         HIGH, "clock", "inject a clock"),
        (r"\b(?:rand|srand|SecureRandom\.)\b",
         HIGH, "PRNG/entropy", "Random.new(seed)"),
        (r"\bThread\.new\b",
         HIGH, "thread scheduling", "single-threaded executor"),
        (r"\bObject#object_id\b|\b\.object_id\b",
         MED, "address-dependent id", "explicit stable id"),
    ],
    "beam": [
        (r"\b(?:System\.monotonic_time|System\.system_time|DateTime\.utc_now|:os\.timestamp)\b",
         HIGH, "clock", "inject a clock"),
        (r"(?::rand\.|\bEnum\.random\b|:crypto\.strong_rand_bytes)",
         HIGH, "PRNG/entropy", "seeded :rand state"),
        (r"\b(?:spawn|Task\.async|Task\.start|Task\.await)\b",
         HIGH, "process scheduling", "deterministic driver process"),
        (r"\bMap\.keys\s*\(|\bEnum\.each\s*\(\s*%\{",
         MED, "map order is unspecified", "Enum.sort before iterating"),
    ],
}

# Comment prefixes per language, for cheap false-positive suppression.
COMMENT = {
    "python": ("#",), "ruby": ("#",), "go": ("//",), "rust": ("//",),
    "js": ("//",), "jvm": ("//",), "c": ("//",), "dotnet": ("//",), "beam": ("#",),
}

# Docstrings and block comments are prose. Scanning them reports the
# documentation ABOUT a hazard as the hazard itself.
BLOCK = {
    "python": [(r'"""', r'"""'), (r"'''", r"'''")],
    "ruby": [(r"^=begin", r"^=end")],
    "beam": [(r'"""', r'"""')],
    "go": [(r"/\*", r"\*/")],
    "rust": [(r"/\*", r"\*/")],
    "js": [(r"/\*", r"\*/")],
    "jvm": [(r"/\*", r"\*/")],
    "c": [(r"/\*", r"\*/")],
    "dotnet": [(r"/\*", r"\*/")],
}


def blank_blocks(text: str, lang: str) -> str:
    """Blank out block comments and docstrings, preserving line numbers."""
    pairs = BLOCK.get(lang, [])
    if not pairs:
        return text
    lines = text.splitlines()
    out: list[str] = []
    closer: str | None = None
    for line in lines:
        if closer is not None:
            hit = re.search(closer, line)
            out.append("")
            if hit:
                closer = None
            continue
        opened = None
        for op, cl in pairs:
            m = re.search(op, line)
            if m and (opened is None or m.start() < opened[0]):
                opened = (m.start(), op, cl, m.end())
        if opened is None:
            out.append(line)
            continue
        _, op, cl, end = opened
        # A single-line docstring or comment: opener and closer on the same line.
        if re.search(cl, line[end:]):
            out.append("")
        else:
            out.append("")
            closer = cl
    return "\n".join(out)


def scan_text(text: str, lang: str) -> list[dict]:
    rules = [(re.compile(p), sev, what, fix) for p, sev, what, fix in RULES.get(lang, [])]
    prefixes = COMMENT.get(lang, ())
    scannable = blank_blocks(text, lang).splitlines()
    original = text.splitlines()
    out: list[dict] = []
    for lineno, line in enumerate(scannable, 1):
        if not line.strip():
            continue
        stripped = line.strip()
        if prefixes and stripped.startswith(prefixes):
            continue
        # Sign-off may sit on the line itself or on the line above it.
        if "dst:allow" in line or (lineno >= 2 and "dst:allow" in original[lineno - 2]):
            continue
        for pat, sev, what, fix in rules:
            if pat.search(line):
                out.append({
                    "line": lineno, "severity": sev, "issue": what,
                    "fix": fix, "code": stripped[:100],
                })
    return out


def iter_files(paths: list[Path], forced: str | None):
    for p in paths:
        if p.is_file():
            lang = forced or EXT_LANG.get(p.suffix)
            if lang:
                yield p, lang
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if not f.is_file() or any(d in SKIP_DIRS for d in f.parts):
                    continue
                lang = forced or EXT_LANG.get(f.suffix)
                if lang:
                    yield f, lang


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scan_nondeterminism", description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--lang", choices=sorted(RULES), help="force a language")
    ap.add_argument("--min", choices=[HIGH, MED, LOW], default=LOW, help="minimum severity to report")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)

    rank = {HIGH: 0, MED: 1, LOW: 2}
    floor = rank[a.min]
    results: list[dict] = []
    langs: set[str] = set()
    scanned = 0

    for path, lang in iter_files([Path(p).expanduser() for p in a.paths], a.lang):
        scanned += 1
        langs.add(lang)
        text = path.read_text(encoding="utf-8", errors="replace")
        for hit in scan_text(text, lang):
            if rank[hit["severity"]] <= floor:
                results.append({"file": str(path), "lang": lang, **hit})

    counts = {s: sum(1 for r in results if r["severity"] == s) for s in (HIGH, MED, LOW)}

    if a.json:
        json.dump({"version": __version__, "files_scanned": scanned,
                   "languages": sorted(langs), "counts": counts,
                   "findings": results}, sys.stdout, indent=2)
        print()
    else:
        print(f"scan_nondeterminism {__version__}")
        print(f"{scanned} files, languages: {', '.join(sorted(langs)) or 'none'}")
        print(f"{counts[HIGH]} high, {counts[MED]} med, {counts[LOW]} low\n")
        by_file: dict[str, list[dict]] = {}
        for r in results:
            by_file.setdefault(r["file"], []).append(r)
        for fname in sorted(by_file, key=lambda f: -sum(
                1 for r in by_file[f] if r["severity"] == HIGH)):
            print(fname)
            for r in sorted(by_file[fname], key=lambda r: (rank[r["severity"]], r["line"])):
                print(f"  {r['severity'].upper():<4} :{r['line']:<5} {r['issue']}")
                print(f"       {r['code']}")
                print(f"       fix: {r['fix']}")
            print()
        if counts[HIGH]:
            print("Every HIGH must be eliminated or routed through the seeded rng before")
            print("simulation results mean anything. Mark reviewed lines with `dst:allow`.")
        elif scanned:
            print("No HIGH findings. This is a lexical scan, not a proof: verify")
            print("determinism empirically by asserting one seed produces identical bytes.")
        if scanned:
            print()
            print("KNOWN BLIND SPOT: `for x in items:` cannot be judged lexically, because")
            print("the type of `items` is unknown. Set iteration is the highest-frequency")
            print("nondeterminism source in real code, so audit bare loops by hand or")
            print("switch the containers to ordered types.")

    return 1 if counts[HIGH] else 0


if __name__ == "__main__":
    sys.exit(main())
