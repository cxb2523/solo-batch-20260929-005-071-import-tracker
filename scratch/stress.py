import os, sys, tempfile, random, shutil
sys.path.insert(0, os.getcwd())
from import_tracker.index import (
    discover_python_files, parse_file,
)
from import_tracker.constants import UNKNOWN_MODULE

def ground_truth(root_dir, root_package, last_good):
    """Fresh rescan semantics: parse every current file; unparseable files keep
    their last-good records and are reported stale (same as the index)."""
    records = []
    stale = set()
    for path in discover_python_files(root_dir):
        try:
            parsed = parse_file(path, root_dir, root_package)
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            stale.add(path)
            records.extend(last_good.get(path, ()))
            continue
        if parsed is None:
            continue
        _, _, visitor = parsed
        recs = visitor.records
        last_good[path] = recs
        records.extend(recs)
    # mirror outer aggregation from index
    from import_tracker.setup_tools import is_setup_tool
    from import_tracker import constants
    merged = {}
    categories = {}
    def classify(module):
        c = constants.classify_import(module, root_package)
        if c == constants.CATEGORY_THIRD_PARTY and is_setup_tool(module):
            c = constants.CATEGORY_SETUP_TOOL
        return c
    for r in records:
        e = merged.setdefault((r.group, r.module),
                              {"count": 0, "aliases": set(), "opt": True,
                               "category": classify(r.module)})
        e["count"] += 1
        e["aliases"].add(r.alias)
        if not r.optional:
            e["opt"] = False
        categories[e["category"]] = categories.get(e["category"], 0) + 1
    groups = {}
    for (group, module), e in merged.items():
        groups.setdefault(group, {})[module] = (
            e["count"], tuple(sorted(e["aliases"])), e["opt"], e["category"])
    stale_groups = set()
    for p in stale:
        rel = os.path.relpath(p, root_dir)
        no_ext = os.path.splitext(rel)[0]
        parts = no_ext.split(os.sep)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        stale_groups.add(".".join([root_package] + parts))
    all_groups = set(groups) | stale_groups
    for path in discover_python_files(root_dir):
        rel = os.path.relpath(path, root_dir)
        no_ext = os.path.splitext(rel)[0]
        parts = no_ext.split(os.sep)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        all_groups.add(".".join([root_package] + parts))
    out = {}
    for g in sorted(all_groups):
        mods = groups.get(g, {})
        out[g] = (g in stale_groups,
                  sorted((m, c, a, o, cat) for m, (c, a, o, cat) in mods.items()))
    total = sum(c for _, mods in out.items() for _, c, _, _, _ in mods[1])
    return total, len(merged), dict(sorted(categories.items())), out

def index_comparable(idx):
    s = idx.snapshot()
    return (s["totals"]["imports"], s["totals"]["modules"], s["categories"],
            {g: (info["stale"],
                 sorted((m["module"], m["count"], m["aliases"],
                         m["optional"], m["category"]) for m in info["modules"]))
             for g, info in s["groups"].items()})

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

random.seed(2026)
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
    from import_tracker.index import IncrementalIndex
    idx = IncrementalIndex(pkg, "mylib")
    idx.cold_start()
    last_good = {p: tuple(idx._states[p].records) for p in idx._states}
    changed = []
    existing = {"__init__.py"}
    for step in range(80):
        roll = random.random()
        _pre_actions = []
        if roll < 0.55 or len(existing) < 2:
            rel = random.choice(["", "sub/", "sub2/", "deep/x/"]) + random.choice(
                ["mod", "sibling", "thing", "other", "leaf"]) + ".py"
            text = "".join(random.choice(SNIPPETS)
                           for _ in range(random.randrange(1, 4)))
            write(rel, text)
            existing.add(rel)
            changed.append(disk_path(rel))
            if "thing" in rel:
                print(f"trial{trial} step{step} WRITE {rel} broken={'def broken' in text} text={text!r}")
        elif roll < 0.85:
            rel = random.choice([r for r in existing if r != "__init__.py"])
            if rel:
                os.remove(disk_path(rel))
                existing.discard(rel)
                changed.append(disk_path(rel))
                last_good.pop(disk_path(rel), None)
                if "thing" in rel:
                    print(f"trial{trial} step{step} DELETE FILE {rel}")
        else:
            target = random.choice(["sub", "sub2", "deep"])
            tpath = disk_path(target)
            if os.path.isdir(tpath):
                for r in [r for r in existing if r.startswith(target + "/")]:
                    changed.append(disk_path(r))
                    last_good.pop(disk_path(r), None)
                    existing.discard(r)
                shutil.rmtree(tpath)
                if "sub2" in target:
                    print(f"trial{trial} step{step} DELETE DIR {target}")
        if changed and random.random() < 0.75:
            if any("thing" in os.path.relpath(c, pkg) for c in changed):
                print(f"trial{trial} step{step} APPLY {[os.path.relpath(c, pkg) for c in changed]}")
            idx.apply_changes(changed)
            changed = []
            exp = ground_truth(pkg, "mylib", last_good)
            cur = index_comparable(idx)
            if cur != exp:
                print("MISMATCH trial", trial, "step", step)
                print("cur:", cur)
                print("exp:", exp)
                print("changed:", [os.path.relpath(c, pkg) for c in changed])
                print("disk files:", sorted(os.path.relpath(f, pkg).replace(os.sep, "/") for f in discover_python_files(pkg)))
                for k, v in sorted(idx._reverse_deps.items()):
                    print("rdep", os.path.relpath(k, pkg).replace(os.sep, "/"),
                          "<-", sorted(os.path.relpath(d, pkg).replace(os.sep, "/") for d in v))
                for fpath, st in sorted(idx._states.items()):
                    print("state", os.path.relpath(fpath, pkg).replace(os.sep, "/"),
                          "stale" if st.stale else "ok",
                          [r.module for r in st.records],
                          "edges:", [os.path.relpath(e, pkg).replace(os.sep, "/") for e in st.out_edges])
                fails += 1
                break
    if not fails:
        idx.apply_changes(changed)
        exp = ground_truth(pkg, "mylib", last_good)
        if index_comparable(idx) != exp:
            print("FINAL MISMATCH trial", trial)
            print("cur:", index_comparable(idx))
            print("exp:", exp)
            fails += 1
    shutil.rmtree(work, ignore_errors=True)
    if fails:
        break
print("DONE fails =", fails)
sys.exit(1 if fails else 0)
