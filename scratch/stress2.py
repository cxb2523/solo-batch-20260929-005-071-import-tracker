import os, sys, tempfile, random, shutil
sys.path.insert(0, os.getcwd())
from import_tracker.index import (
    IncrementalIndex, discover_python_files, parse_file,
)

random.seed(2026)
SNIPPETS = [
    "import os\n",
    "import json as j\n",
    "import setuptools\n",
    "import wheel.cli\n",
    "import yaml.alpha\n",
    "from . import mod\n",
    "from . import mod, sibling\n",
    "from .. import other\n",
    "from ..sub2 import thing\n",
    "from .... import nope\n",
    "from .mod import Thing, Other as O\n",
    "try:\n    import optional_dep_xyz\nexcept ImportError:\n    pass\n",
    "from . import *\n",
    "def broken(:\n",
    "",
    "import csv\nimport csv as csv2\n",
    "import unknown_pkg_xyz\n",
]

def oracle_counts(root_dir, root_package, retained):
    """Stateful full rescan: parse each current file; on failure retain the
    last successful records and flag stale. Mirrors the spec contract."""
    live_paths = set(discover_python_files(root_dir))
    for path in list(retained):
        if path not in live_paths:
            del retained[path]
    records = []
    stale_paths = set()
    for path in live_paths:
        try:
            parsed = parse_file(path, root_dir, root_package)
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            stale_paths.add(path)
            records.extend(retained.get(path, ()))
            continue
        if parsed is None:
            continue
        recs = tuple(parsed[2].records)
        retained[path] = recs
        records.extend(recs)
    probe = IncrementalIndex(root_dir, root_package)
    return probe._aggregate_records_locked(records, stale_paths), retained

def current_counts(idx):
    return idx.current_counts()

def comparable(c):
    return (c["imports"], c["modules"], c["categories"],
            {k: (v["count"], v["aliases"], v["optional"], v["category"])
             for k, v in c["modules_map"].items()},
            set(c["stale_groups"]))

fails = 0
for trial in range(60):
    work = tempfile.mkdtemp()
    pkg = os.path.join(work, "mylib")
    os.makedirs(pkg)
    def disk_path(rel):
        return os.path.join(pkg, rel.replace("/", os.sep))
    def write(rel, text):
        full = disk_path(rel)
        os.makedirs(os.path.dirname(full) or pkg, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
    write("__init__.py", "")
    idx = IncrementalIndex(pkg, "mylib")
    idx.cold_start()
    retained = {p: tuple(st.records) for p, st in idx._states.items()}
    changed = []
    existing = {"__init__.py"}
    for step in range(80):
        roll = random.random()
        if roll < 0.55 or len(existing) < 2:
            rel = random.choice(["", "sub/", "sub2/", "deep/x/"]) + random.choice(
                ["mod", "sibling", "thing", "other", "leaf"]) + ".py"
            text = "".join(random.choice(SNIPPETS)
                           for _ in range(random.randrange(1, 4)))
            write(rel, text)
            existing.add(rel)
            changed.append(disk_path(rel))
        elif roll < 0.85:
            candidates = [r for r in existing if r != "__init__.py"]
            if candidates:
                rel = random.choice(candidates)
                os.remove(disk_path(rel))
                existing.discard(rel)
                changed.append(disk_path(rel))
        else:
            target = random.choice(["sub", "sub2", "deep"])
            tpath = disk_path(target)
            if os.path.isdir(tpath):
                for r in [r for r in existing if r.startswith(target + "/")]:
                    changed.append(disk_path(r))
                    existing.discard(r)
                shutil.rmtree(tpath)
        if changed and random.random() < 0.75:
            idx.apply_changes(changed)
            changed = []
            exp, retained = oracle_counts(pkg, "mylib", retained)
            if comparable(current_counts(idx)) != comparable(exp):
                print("MISMATCH trial", trial, "step", step)
                print("cur:", comparable(current_counts(idx)))
                print("exp:", comparable(exp))
                fails += 1
                break
    if fails:
        break
    idx.apply_changes(changed)
    exp, retained = oracle_counts(pkg, "mylib", retained)
    if comparable(current_counts(idx)) != comparable(exp):
        print("FINAL MISMATCH trial", trial)
        print("cur:", comparable(current_counts(idx)))
        print("exp:", comparable(exp))
        fails += 1
    shutil.rmtree(work, ignore_errors=True)
print("DONE fails =", fails)
sys.exit(1 if fails else 0)
