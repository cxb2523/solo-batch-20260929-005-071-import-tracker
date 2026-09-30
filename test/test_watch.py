"""
Tests for the persistent incremental watcher (import_tracker.watch)
"""

# Standard
import json
import os
import shutil
import threading
import urllib.request

# Third Party
import pytest

# Local
from import_tracker import constants
from import_tracker.watch import (
    ImportIndex,
    TrackedImport,
    WatcherService,
    build_handler,
    read_settled_source,
    resolve_relative,
    resolve_roots,
    serve,
)


## Fixtures / helpers #########################################################


@pytest.fixture
def package_root(tmp_path):
    """Build a small package and return its on-disk root directory"""
    root = tmp_path / "mylib"
    (root / "sub").mkdir(parents=True)
    files = {
        "__init__.py": "import os\nfrom . import a, b\n",
        "a.py": "import sys\nfrom .sub import child\n",
        "b.py": "from .a import thing\nimport requests\n",
        "sub/__init__.py": "",
        "sub/child.py": "import json\nfrom .. import a\n",
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def write(path, content):
    path.write_text(content, encoding="utf-8")
    os.utime(str(path), None)


def atomic_write(path, content):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(tmp, str(path))


def make_index(root, *extra):
    roots = [("mylib", str(root))]
    roots.extend(extra)
    index = ImportIndex(roots)
    index.start()
    return index


## TrackedImport semantics ####################################################


def test_tracked_import_is_frozen_value_object():
    record = TrackedImport(
        module="os",
        alias=("os",),
        group="g",
        category=constants.CATEGORY_STDLIB,
        source="g.m",
        optional=False,
    )
    assert record.module == "os"
    with pytest.raises(Exception):
        record.module = "sys"


def test_default_track_module_builds_record(package_root):
    index = make_index(package_root)
    states = [
        state for state in index._files.values() if state.module == "mylib.a"
    ]
    assert states, "mylib.a should be scanned"
    records = states[0].records
    assert all(isinstance(r, TrackedImport) for r in records)
    modules = {r.module for r in records}
    assert "sys" in modules
    assert "mylib.sub.child" in modules


## Relative import resolution #################################################


def test_resolve_relative_levels():
    assert resolve_relative(0, "os.path", "pkg.sub") == "os.path"
    assert resolve_relative(1, "x", "pkg.sub") == "pkg.sub.x"
    assert resolve_relative(1, None, "pkg.sub") == "pkg.sub"
    assert resolve_relative(2, "x", "pkg.sub") == "pkg.x"
    assert resolve_relative(3, "x", "pkg.sub") is None
    assert resolve_relative(1, "x", "") is None


def test_unresolvable_relative_is_unknown(tmp_path):
    root = tmp_path / "u"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "m.py").write_text("from .... import nope\n", encoding="utf-8")
    index = ImportIndex([("u", str(root))])
    index.start()
    modules = index.snapshot()["groups"]["u"]["modules"]
    assert constants.UNKNOWN_MODULE in modules
    assert modules[constants.UNKNOWN_MODULE]["aliases"] == ["nope"]


## Outer aggregation: counts + alias union, keyed by module ###################


def test_counts_accumulate_and_aliases_union(package_root):
    index = make_index(package_root)
    modules = index.snapshot()["groups"]["mylib"]["modules"]
    # os imported once in __init__
    assert modules["os"]["count"] == 1
    assert modules["os"]["aliases"] == ["os"]
    # requests imported once in b
    assert modules["requests"]["count"] == 1


def test_one_statement_one_count_with_multiple_aliases(package_root):
    # Add a module with a single from-import binding several names; the count
    # stays 1 and all aliases are listed.
    write(
        package_root / "a.py",
        "from os import path, sep as s\n",
    )
    index = make_index(package_root)
    modules = index.snapshot()["groups"]["mylib"]["modules"]
    assert modules["os"]["count"] == 2  # __init__ + a.py
    assert set(modules["os"]["aliases"]) == {"os", "path", "s"}


def test_groups_are_isolated(tmp_path):
    g1 = tmp_path / "g1"
    g2 = tmp_path / "g2"
    for grp in (g1, g2):
        grp.mkdir()
        (grp / "__init__.py").write_text("import shared_dep\n", encoding="utf-8")
    index = ImportIndex([("g1", str(g1)), ("g2", str(g2))])
    index.start()
    snap = index.snapshot()
    assert snap["groups"]["g1"]["modules"]["shared_dep"]["count"] == 1
    assert snap["groups"]["g2"]["modules"]["shared_dep"]["count"] == 1
    assert snap["total_imports"] == (
        snap["groups"]["g1"]["total_imports"]
        + snap["groups"]["g2"]["total_imports"]
    )


## Incremental == cold full re-scan ###########################################


def assert_incremental_equals_full(index):
    incremental = index.snapshot()
    full = index.full_rescan()
    assert incremental["groups"] == full["groups"]


def test_repeated_edits_match_cold_rescan(package_root):
    index = make_index(package_root)
    for i in range(5):
        write(
            package_root / "a.py",
            f"import sys\nimport os\nfrom .sub import child\nVALUE = {i}\n",
        )
        index.notify_changed(str(package_root / "a.py"))
        assert_incremental_equals_full(index)


def test_added_file_matches_cold_rescan(package_root):
    index = make_index(package_root)
    write(package_root / "c.py", "from . import a\nimport numpy as np\n")
    index.notify_changed(str(package_root / "c.py"))
    assert_incremental_equals_full(index)
    modules = index.snapshot()["groups"]["mylib"]["modules"]
    assert modules["numpy"]["count"] == 1
    assert modules["numpy"]["aliases"] == ["np"]


def test_delete_reverse_dependency_invalidation(package_root):
    index = make_index(package_root)
    # While sub/child exists, a.py records a local mylib.sub.child import.
    modules = index.snapshot()["groups"]["mylib"]["modules"]
    assert "mylib.sub.child" in modules

    child = package_root / "sub" / "child.py"
    child.unlink()
    index.notify_deleted(str(child))
    assert_incremental_equals_full(index)
    modules = index.snapshot()["groups"]["mylib"]["modules"]
    assert "mylib.sub.child" not in modules
    # 'child' now resolves as an attribute of mylib.sub
    assert "mylib.sub" in modules


def test_delete_module_removes_its_imports(package_root):
    index = make_index(package_root)
    b = package_root / "b.py"
    b.unlink()
    index.notify_deleted(str(b))
    assert_incremental_equals_full(index)
    modules = index.snapshot()["groups"]["mylib"]["modules"]
    assert "requests" not in modules


## Stale handling #############################################################


def test_parse_failure_keeps_prior_and_marks_stale(package_root):
    index = make_index(package_root)
    before = index.snapshot()
    assert not before["stale"]

    write(package_root / "a.py", "def broken( :\n")
    index.notify_changed(str(package_root / "a.py"))
    stale_snap = index.snapshot()
    assert stale_snap["stale"] is True
    assert "mylib.a" in stale_snap["groups"]["mylib"]["stale_modules"]

    # Prior imports retained, not zeroed.
    modules = stale_snap["groups"]["mylib"]["modules"]
    assert "sys" in modules
    assert "mylib.sub.child" in modules

    # Recover -> stale clears and counts match full rescan.
    write(package_root / "a.py", "import sys\nfrom .sub import child\n")
    index.notify_changed(str(package_root / "a.py"))
    recovered = index.snapshot()
    assert recovered["stale"] is False
    assert_incremental_equals_full(index)


def test_atomic_temp_files_are_ignored(package_root):
    index = make_index(package_root)
    atomic_write(package_root / "a.py", "import sys\n")
    index.notify_changed(str(package_root / "a.py") + ".tmp")
    assert not index.snapshot()["stale"]


## Snapshot serialization & stability #########################################


def test_snapshot_is_json_serializable_and_sorted(package_root):
    index = make_index(package_root)
    payload = json.dumps(index.snapshot(), sort_keys=True)
    assert json.loads(payload)["total_imports"] > 0


def test_read_settled_source(package_root):
    path = package_root / "a.py"
    assert "import sys" in read_settled_source(str(path))


## Live service / HTTP ########################################################


def _http_get(url):
    with urllib.request.urlopen(url, timeout=5) as handle:
        return handle.read().decode("utf-8")


def test_watch_service_live_counts_and_stale_page(package_root):
    service = WatcherService([("mylib", str(package_root))], debounce=0.1)
    service.start()
    server = serve(service.index, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        assert "<html" in _http_get(base + "/")

        cold = json.loads(_http_get(base + "/snapshot"))

        # Repeatedly edit the same file; live counts must equal a cold scan.
        for i in range(4):
            atomic_write(
                package_root / "a.py",
                f"import sys\nimport os\nfrom .sub import child\nV={i}\n",
            )
            assert service.index.wait_until_idle(timeout=5, settle=0.25)
            live = json.loads(_http_get(base + "/snapshot"))
            assert live["groups"] == service.index.full_rescan()["groups"]

        # Break the file -> stale marker visible on the page and in JSON.
        atomic_write(package_root / "a.py", "def broken( :\n")
        assert service.index.wait_until_idle(timeout=5, settle=0.25)
        broken = json.loads(_http_get(base + "/snapshot"))
        assert broken["stale"] is True
        assert "mylib.a" in broken["groups"]["mylib"]["stale_modules"]
        page = _http_get(base + "/")
        assert "STALE" in page

        # Recover.
        atomic_write(package_root / "a.py", "import sys\nfrom .sub import child\n")
        assert service.index.wait_until_idle(timeout=5, settle=0.25)
        recovered = json.loads(_http_get(base + "/snapshot"))
        assert recovered["stale"] is False
    finally:
        server.shutdown()
        service.stop()


def test_resolve_roots_for_path_and_module(package_root):
    name, path = resolve_roots([str(package_root)])[0]
    assert name == "mylib"
    assert os.path.normpath(path) == os.path.normpath(str(package_root))




def test_atomic_rename_create_is_indexed(tmp_path):
    # Many editors save by writing a temp file then renaming it over the
    # target. The watcher must index the destination of that move.
    root = tmp_path / "atomlib"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    service = WatcherService([("atomlib", str(root))], debounce=0.1)
    service.start()
    try:
        target = root / "newmod.py"
        tmp_target = str(target) + ".tmp"
        with open(tmp_target, "w", encoding="utf-8") as handle:
            handle.write("import textwrap\n")
        os.replace(tmp_target, str(target))
        assert service.index.wait_until_idle(timeout=5, settle=0.3)
        modules = service.index.snapshot()["groups"]["atomlib"]["modules"]
        assert "textwrap" in modules
        assert service.index.snapshot()["groups"] == (
            service.index.full_rescan()["groups"]
        )
    finally:
        service.stop()

def teardown_module(module):
    # Best-effort cleanup if any test leaked an observer
    pass


def test_bom_tolerated(package_root):
    # Files saved on Windows may carry a UTF-8 BOM; they must parse cleanly.
    target = package_root / 'bommod.py'
    bom = b'\xef\xbb\xbf'
    target.write_bytes(bom + b'import textwrap\n')
    index = make_index(package_root)
    modules = index.snapshot()['groups']['mylib']['modules']
    assert not index.snapshot()['stale']
    assert 'textwrap' in modules
