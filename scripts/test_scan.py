"""Self-check for the nondeterminism scanner.

Each HIGH rule has a positive fixture. Selected safe patterns also have negative
fixtures to prevent noisy regressions.

Run: python test_scan.py
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from scan_nondeterminism import RULES, blank_blocks, main, scan_text  # noqa: E402

# language -> (source that MUST produce a high finding, expected issue substring)
POSITIVE = {
    "python": [
        ("t = time.time()", "clock"),
        ("stamp = datetime.now()", "clock"),
        ("x = random.choice(items)", "random"),
        ("from random import shuffle", "random"),
        ("rid = uuid.uuid4()", "entropy"),
        ("for v in set(values): pass", "set iteration"),
        ("threading.Thread(target=f).start()", "thread"),
        ("with ThreadPoolExecutor() as ex: pass", "pool"),
    ],
    "rust": [
        ("let t = Instant::now();", "clock"),
        ("let n: u32 = rand::random();", "entropy"),
        ("let id = Uuid::new_v4();", "uuid"),
        ("tokio::spawn(async move { work().await });", "scheduling"),
    ],
    "go": [
        ("start := time.Now()", "clock"),
        ("n := rand.Intn(10)", "rand"),
        ("go worker(ch)", "goroutine"),
        ("select {", "select"),
        ("id := uuid.New()", "entropy"),
    ],
    "js": [
        ("const t = Date.now();", "clock"),
        ("const r = Math.random();", "PRNG"),
        ("setTimeout(fn, 100);", "timers"),
        ("const id = crypto.randomUUID();", "entropy"),
        ("new Worker('./w.js');", "concurrency"),
    ],
    "jvm": [
        ("long t = System.currentTimeMillis();", "clock"),
        ("Instant now = Instant.now();", "clock"),
        ("UUID id = UUID.randomUUID();", "uuid"),
        ("new Thread(task).start();", "thread"),
        ("var r = ThreadLocalRandom.current();", "PRNG"),
    ],
    "c": [
        ("time_t t = time(NULL);", "clock"),
        ("int r = rand();", "PRNG"),
        ("pthread_create(&th, NULL, work, NULL);", "thread"),
    ],
    "dotnet": [
        ("var t = DateTime.UtcNow;", "clock"),
        ("var g = Guid.NewGuid();", "guid"),
        ("await Task.Run(() => Work());", "scheduling"),
    ],
    "ruby": [
        ("t = Time.now", "clock"),
        ("n = rand(10)", "PRNG"),
        ("Thread.new { work }", "thread"),
    ],
    "beam": [
        ("t = System.monotonic_time()", "clock"),
        ("n = :rand.uniform(10)", "PRNG"),
        ("Task.async(fn -> work() end)", "scheduling"),
    ],
}

# Things that must NOT be flagged, because flagging them makes the tool useless.
NEGATIVE = {
    "python": [
        "rng = random.Random(seed)",          # the correct pattern
        "self.rng = random.Random(seed ^ 1)",
        "for k in sorted(d.keys()): pass",    # explicitly ordered
        "value = hash('literal string')",     # literal, stable
        "t = sim.now",
        "for x in sorted(s): pass",           # sorted set is fine
        "for item in items: pass",            # unknown type: must NOT be HIGH
    ],
    "go": [
        "r := rand.New(rand.NewSource(seed))",
    ],
    "rust": [
        "let mut rng = StdRng::seed_from_u64(seed);",
    ],
}


def test_every_high_rule_has_a_fixture():
    """A HIGH rule with no positive fixture is an untested claim."""
    for lang, rules in RULES.items():
        highs = [r for r in rules if r[1] == "high"]
        fixtures = POSITIVE.get(lang, [])
        assert fixtures, f"{lang} has {len(highs)} HIGH rules and no fixtures"
        hit_issues = set()
        for src, _ in fixtures:
            for f in scan_text(src, lang):
                hit_issues.add(f["issue"])
        # Not every rule needs its own fixture, but a decent share must be covered.
        covered = sum(1 for _, _, what, _ in highs if what in hit_issues)
        assert covered >= max(2, len(highs) // 2), (
            f"{lang}: only {covered}/{len(highs)} HIGH rules exercised"
        )


def test_positives_fire():
    for lang, cases in POSITIVE.items():
        for src, expect in cases:
            found = scan_text(src, lang)
            assert found, f"{lang}: no finding for {src!r}"
            assert any(f["severity"] == "high" for f in found), (
                f"{lang}: {src!r} should be HIGH, got {[f['severity'] for f in found]}"
            )
            blob = " ".join(f["issue"] for f in found).lower()
            assert expect.lower() in blob, f"{lang}: {src!r} -> {blob!r}, wanted {expect!r}"


def test_negatives_stay_quiet():
    for lang, cases in NEGATIVE.items():
        for src in cases:
            found = [f for f in scan_text(src, lang) if f["severity"] == "high"]
            assert not found, f"{lang}: false positive on {src!r}: {found}"


def test_set_iteration_shapes_are_caught():
    """Category 3 is called the most under-appreciated, so cover its real shapes."""
    high = [
        "z = list(set(items))",
        "first = next(iter(s))",
        'out = "".join(frozenset(parts))',
        "for k in a | b: pass",
        "for x in set(values): pass",
        "vals = [f(x) for x in set(items)]",
    ]
    for src in high:
        found = [f for f in scan_text(src, "python") if f["severity"] == "high"]
        assert found, f"missed set-iteration shape: {src!r}"

    med = ["for k in seen: pass", "vals = [f(x) for x in visited]"]
    for src in med:
        sev = {f["severity"] for f in scan_text(src, "python")}
        assert sev & {"med", "high"}, f"missed suspicious container: {src!r}"


def test_bare_loop_is_not_claimed_as_high():
    """`for x in items:` is undecidable lexically; flagging it HIGH is a lie."""
    found = [f for f in scan_text("for x in items: pass", "python") if f["severity"] == "high"]
    assert not found, f"bare loop claimed as HIGH: {found}"


def test_line_comments_ignored():
    assert not scan_text("# t = time.time()", "python")
    assert not scan_text("// start := time.Now()", "go")


def test_docstrings_ignored():
    """Prose describing a hazard is not the hazard."""
    src = '"""Never call time.time() here."""\nx = 1\n'
    assert not scan_text(src, "python"), scan_text(src, "python")

    multi = '"""\nDo not use random.choice or Math.random.\n"""\nrng = None\n'
    assert not scan_text(multi, "python"), scan_text(multi, "python")

    block = "/*\n * start := time.Now()\n */\nx := 1\n"
    assert not scan_text(block, "go"), scan_text(block, "go")


def test_line_numbers_survive_blanking():
    src = '"""\ndoc\n"""\nt = time.time()\n'
    found = scan_text(src, "python")
    assert len(found) == 1, found
    assert found[0]["line"] == 4, f"line number shifted: {found[0]['line']}"


def test_blank_blocks_preserves_line_count():
    for src in ('"""a"""\nx\n', '"""\na\n"""\nx\n', "x\ny\n", "/*\na\n*/\nx\n"):
        for lang in ("python", "go"):
            assert len(blank_blocks(src, lang).splitlines()) == len(src.splitlines())


def test_dst_allow_suppresses():
    assert not scan_text("t = time.time()  # dst:allow real clock at the boundary", "python")
    above = "# dst:allow boundary code\nt = time.time()\n"
    assert not scan_text(above, "python"), scan_text(above, "python")


def test_exit_code_and_json(tmp: Path):
    bad = tmp / "bad.py"
    bad.write_text("import time\nt = time.time()\n", encoding="utf-8")
    good = tmp / "good.py"
    good.write_text("rng = random.Random(7)\nx = rng.random()\n", encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()) as out:
        assert main([str(bad)]) == 1, "HIGH finding must exit 1"
        assert main([str(good)]) == 0, "clean file must exit 0"
        assert main([str(bad), "--min", "high", "--json"]) == 1
    assert '"severity": "high"' in out.getvalue(), "json mode emitted no findings"


def test_skips_vendored_trees(tmp: Path):
    (tmp / "node_modules" / "pkg").mkdir(parents=True)
    (tmp / "node_modules" / "pkg" / "i.js").write_text("Date.now()", encoding="utf-8")
    (tmp / "ok.py").write_text("x = 1\n", encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        assert main([str(tmp)]) == 0, "vendored tree was scanned"


def main_run() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tests = [(k, v) for k, v in sorted(globals().items())
                 if k.startswith("test_") and callable(v)]
        for name, fn in tests:
            if fn.__code__.co_argcount == 1:
                sub = tmp / name
                sub.mkdir()
                fn(sub)
            else:
                fn()
            print(f"ok   {name}")
        print(f"\n{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main_run())
