import os, sys, tempfile, random, shutil
sys.path.insert(0, os.getcwd())
from import_tracker.index import IncrementalIndex, discover_python_files

SNIPPETS = [
    "import os\n", "import json as j\n", "import setuptools\n",
    "import wheel.cli\n", "import yaml.alpha\n", "from . import mod\n",
    "from . import mod, sibling\n", "from .. import other\n",
    "from ..sub2 import thing\n", "from .... import nope\n",
    "from .mod import Thing, Other as O\n",
    "try:\n    import optional_dep_xyz\nexcept ImportError:\n    pass\n",
    "from . import *\n", "def broken(:\n", "",
    "import csv\nimport csv as csv2\n", "import unknown_pkg_xyz\n",
    "from ... import nope2\n", "import mylib\n", "import mylib.sub2.mod\n",
]

def run_seed(seed, trials, steps):
    random.seed(seed)
    for trial in range(trials):
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
        if not idx.verify_against_full_rescan():
            print("seed", seed, "trial", trial, "cold mismatch")
            shutil.rmtree(work, ignore_errors=True); return 1
        changed = []
        existing = {"__init__.py"}
        for step in range(steps):
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
            if changed and random.random() < 0.7:
                idx.apply_changes(changed)
                changed = []
                if not idx.verify_against_full_rescan():
                    print("seed", seed, "trial", trial, "step", step, "mismatch")
                    shutil.rmtree(work, ignore_errors=True); return 1
        idx.apply_changes(changed)
        if not idx.verify_against_full_rescan():
            print("seed", seed, "trial", trial, "final mismatch")
            shutil.rmtree(work, ignore_errors=True); return 1
        shutil.rmtree(work, ignore_errors=True)
    return 0

bad = 0
for seed in (1, 7, 42, 99, 2026, 55555):
    r = run_seed(seed, 20, 60)
    print("seed", seed, "ok" if r == 0 else "FAIL")
    bad += r
print("ALL DONE", "fails" if bad else "passing")
sys.exit(bad)
