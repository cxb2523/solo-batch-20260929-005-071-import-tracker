import json, subprocess, sys, time, os, urllib.request

ROOT = os.getcwd()
TARGET = os.path.join(ROOT, "import_tracker", "watch.py")
BASE = open(os.path.join(ROOT, "scratch", "watch_orig.py"), encoding="utf-8").read()

def fetch(path="/snapshot"):
    with urllib.request.urlopen(f"http://127.0.0.1:8771{path}", timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))

def cold_truth():
    code = (
        "import json,sys;"
        "sys.path.insert(0, r'%s');"
        "from import_tracker.index import IncrementalIndex;"
        "import import_tracker, os;"
        "root=os.path.dirname(import_tracker.__file__);"
        "idx=IncrementalIndex(root,'import_tracker');"
        "idx.cold_start();"
        "print(json.dumps(idx.snapshot(), default=list))"
    ) % ROOT
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return json.loads(out.stdout.strip())

def comparable(s):
    groups = {}
    for g, info in s["groups"].items():
        groups[g] = (
            info["stale"],
            sorted(
                (m["module"], m["count"], tuple(m["aliases"]),
                 m["optional"], m["category"])
                for m in info["modules"]
            ),
        )
    return (
        s["totals"]["imports"],
        s["totals"]["modules"],
        tuple(sorted(s["categories"].items())),
        s["totals"]["stale_groups"],
        tuple(sorted(groups.items())),
    )

def wait_settle(predicate, timeout=10.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fetch()
        if predicate(last):
            return last
        time.sleep(0.25)
    return last

variants = [
    BASE + "\nimport collections\n",
    BASE + "\nimport collections\nimport collections as col2\n",
    BASE + "\ntry:\n    import nonexistent_xyz_pkg_99\nexcept ImportError:\n    pass\n",
    BASE + "\nfrom . import constants as c99\n",
    BASE,
]

results = []
for i, text in enumerate(variants):
    with open(TARGET, "w", encoding="utf-8") as fh:
        fh.write(text)
    time.sleep(1.2)  # let poller + debounce run
    live = fetch()
    truth = cold_truth()
    ok = comparable(live) == comparable(truth)
    results.append((i, live["version"], live["totals"], dict(live["categories"]), ok))
    print(f"variant {i}: version={live['version']} totals={live['totals']} "
          f"categories={live['categories']} match={ok}")
    if not ok:
        lc, tc = comparable(live), comparable(truth)
        for a, b in zip(lc, tc):
            if a != b:
                print("  LIVE:", str(a)[:500])
                print("  COLD:", str(b)[:500])

# Now break syntax: snapshot must retain previous counts and show STALE
with open(TARGET, "w", encoding="utf-8") as fh:
    fh.write("def this_is_broken(:\n")
time.sleep(1.2)
broken = fetch()
g = broken["groups"].get("import_tracker.watch", {})
print("broken totals:", broken["totals"], "watch stale:", g.get("stale"),
      "watch imports:", g.get("imports"))
results.append(("broken", broken["totals"], g.get("stale"), g.get("imports")))

# Restore valid content: must converge back to the cold baseline
with open(TARGET, "w", encoding="utf-8") as fh:
    fh.write(BASE)
time.sleep(1.2)
healed = fetch()
truth = cold_truth()
healed_ok = comparable(healed) == comparable(truth)
print("healed match:", healed_ok, healed["totals"], dict(healed["categories"]))
print("watch_error:", fetch().get("watch_error"))
all_ok = all(r[-1] is True for r in results if isinstance(r[-1], bool)) and healed_ok
print("ALL_OK", all_ok)
