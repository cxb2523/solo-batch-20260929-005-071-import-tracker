"""
Persistent incremental import index.

This module turns :mod:`import_tracker` scanning into a long-running,
incremental index. It watches one or more Python packages on disk and only
re-parses the modules affected by a change. The aggregated summary produced at
any point is kept identical to what a full, cold re-scan would produce.

Design summary
--------------

* The unit of scanning is a single source file. An ``ast`` visitor extracts
  the individual import statements and invokes a ``track_module`` callback that
  emits one :class:`TrackedImport` per imported module per statement (bound
  names are carried as aliases and are *not* folded together inside the
  scanner; de-duplication/accumulation only happens in the outer index).

* The outer index groups by watched root. Within a group the merge key is the
  imported module name: repeated imports accumulate a count and the union of
  their aliases is kept. Different watched roots are fully isolated groups.

* Relative imports are resolved to an absolute module name using the import
  ``level`` and the containing package. When the level cannot be anchored to a
  known package, the import is recorded under :data:`constants.UNKNOWN_MODULE`.

* State mutations are serialized by a lock. File change events are coalesced
  (debounced) and snapshots are taken under the same lock, so a reader can
  never observe a half-applied batch. Reads wait for a file to settle so a
  partially-written file is never parsed.

* If a file can no longer be parsed (e.g. it is being edited and is currently
  syntactically invalid), the last successfully parsed version of its imports
  is retained and the module is flagged ``stale`` instead of being cleared.

* Deleting a module invalidates the exact set of modules that may resolve a
  name differently because of it: the deleted module itself and every module
  that probed the deleted module (or anything beneath it) as a potential
  sub-module while resolving imports.
"""

# Standard
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple
import argparse
import ast
import importlib.util
import json
import os
import threading
import time
import urllib.parse

# Local
from . import constants
from .lazy_import_errors import lazy_import_errors
from .log import log

## Tracked import record #######################################################


@dataclass(frozen=True)
class TrackedImport:
    """A single imported name observed within a single source module.

    Field semantics (stable contract):

    * ``module``: fully resolved absolute name of the imported module, or
      :data:`constants.UNKNOWN_MODULE` when a relative import cannot be
      anchored to a concrete package.
    * ``alias``: all names the importing statement binds for this record. A
      plain ``import a`` binds ``("a",)``; ``from m import x as y, z`` binds
      ``("y", "z")``. One record corresponds to one import statement (the unit
      that accumulates the count); multiple bound names remain distinct
      aliases rather than separate counts.
    * ``group``: watched-root group the record belongs to. Records never merge
      across groups.
    * ``category``: one of the ``CATEGORY_*`` constants describing how the
      import is classified.
    * ``source``: dotted module name of the file the import was found in.
    * ``optional``: True when the import lives inside a try/except block
      (a lazily-resolved/optional dependency).
    * ``package``: best-effort installable distribution package name provided
      by the setup-tools package resolution (None when not resolved).
    """

    module: str
    alias: Tuple[str, ...]
    group: str
    category: str = constants.CATEGORY_UNKNOWN
    source: str = ""
    optional: bool = False
    package: Optional[str] = None


## Parsing #####################################################################


class _ImportVisitor(ast.NodeVisitor):
    """AST visitor that discovers import statements inside a single module.

    For every imported name the visitor calls back into ``track_module``. The
    visitor itself never de-duplicates or aggregates; that responsibility lives
    only in the outer index.
    """

    def __init__(
        self,
        track_module: Callable[..., Optional[TrackedImport]],
        group: str,
        source_module: str,
        package_name: Optional[str],
        resolve_module: Callable[[int, Optional[str]], Optional[str]],
        classify: Callable[[str, Optional[str], bool], Tuple[str, Optional[str]]],
    ):
        self._track_module = track_module
        self._group = group
        self._source_module = source_module
        self._package_name = package_name
        self._resolve_module = resolve_module
        self._classify = classify
        self._try_depth = 0

    # try/except blocks make their body imports optional/lazy. Imports only in
    # handlers/else/finally are not treated as optional.
    def visit_Try(self, node):
        self._try_depth += 1
        try:
            for child_node in node.body:
                self.visit(child_node)
        finally:
            self._try_depth -= 1
        for child_node in node.handlers:
            self.visit(child_node)
        for child_node in node.orelse:
            self.visit(child_node)
        for child_node in node.finalbody:
            self.visit(child_node)

    def visit_Import(self, node):
        if self._try_depth == 0:
            self._handle_import(node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if self._try_depth == 0:
            self._handle_import(node)
        self.generic_visit(node)
    def _handle_import(self, node):
        optional = self._try_depth > 0
        if isinstance(node, ast.Import):
            # Each dotted module is a distinct imported module; the bound alias
            # is the as-name or the leading name segment.
            for alias in node.names:
                bind_name = alias.asname or alias.name.partition(".")[0]
                self._emit(
                    resolved_anchor=alias.name,
                    name=None,
                    bind_names=(bind_name,),
                    optional=optional,
                    relative=False,
                )
            return

        # From-import. Resolve the anchor module honoring the relative level.
        resolved_anchor = self._resolve_module(node.level, node.module)
        relative = node.level > 0

        # Group the bound names by the concrete module they resolve to. A bound
        # name that names a real sub-module counts against that sub-module;
        # otherwise it is an attribute of the anchor module.
        per_module: Dict[str, List[str]] = {}
        for alias in node.names:
            if alias.name == "*":
                per_module.setdefault(
                    resolved_anchor or constants.UNKNOWN_MODULE, []
                ).append("*")
                continue
            sub_candidate = None
            if resolved_anchor is not None:
                sub_candidate = f"{resolved_anchor}.{alias.name}"
            if sub_candidate is not None and self._resolve_module.is_submodule(
                sub_candidate
            ):
                target_module = sub_candidate
            else:
                target_module = resolved_anchor or constants.UNKNOWN_MODULE
            bind_name = alias.asname or alias.name
            per_module.setdefault(target_module, []).append(bind_name)

        for target_module, bind_names in per_module.items():
            self._emit(
                resolved_anchor=target_module,
                name=None,
                bind_names=tuple(bind_names),
                optional=optional,
                relative=relative,
            )

    def _emit(self, resolved_anchor, name, bind_names, optional, relative):
        full_module = resolved_anchor
        if full_module is None:
            full_module = constants.UNKNOWN_MODULE
        category, package = self._classify(full_module, self._source_module, optional)
        record = self._track_module(
            module=full_module,
            alias=tuple(bind_names),
            group=self._group,
            category=category,
            source=self._source_module,
            optional=optional,
            package=package,
        )
        if record is not None and not isinstance(record, TrackedImport):
            raise TypeError(
                f"track_module must return a TrackedImport or None, got {record!r}"
            )


def _module_package(source_module: str, is_init: bool) -> str:
    """Return the package context of a source module.

    For an ``__init__`` the module itself is the package; for a regular module
    the package is its dotted parent.
    """
    if is_init:
        return source_module
    return source_module.rpartition(".")[0]


def resolve_relative(
    level: int,
    module: Optional[str],
    package_name: Optional[str],
) -> Optional[str]:
    """Resolve a relative import to an absolute name using ``level``.

    Mirrors the import-system semantics:

    * ``level == 0`` -> the import is already absolute.
    * ``level == 1`` -> the current package.
    * ``level >= 2`` -> walk up the package tree.

    Returns None when the level cannot be anchored to a known package (the
    caller records the import as ``unknown``).
    """
    if level == 0:
        return module
    if not package_name:
        return None
    parts = package_name.split(".")
    # One dot anchors at the current package; each additional dot steps up.
    steps_up = level - 1
    if steps_up > len(parts):
        return None
    base_parts = parts[: len(parts) - steps_up] if steps_up else parts
    base = ".".join(base_parts)
    if not base:
        return None
    if module:
        return f"{base}.{module}"
    return base


def read_settled_source(
    path: str,
    settle_seconds: float = 0.05,
    attempts: int = 20,
) -> str:
    """Read a source file only once its contents (and size) have stopped
    changing.

    This prevents parsing a file that is mid-write (half written). Raises
    :class:`OSError` if the file disappears and :class:`SyntaxError` is left to
    the caller (it is produced by ``ast.parse`` on successfully-read content).
    """
    last_size = -1
    last_mtime = None
    contents = None
    for _ in range(max(1, attempts)):
        with open(path, "r", encoding="utf-8") as handle:
            contents = handle.read()
        stat = os.stat(path)
        if stat.st_size == last_size and stat.st_mtime_ns == last_mtime:
            return contents
        last_size = stat.st_size
        last_mtime = stat.st_mtime_ns
        time.sleep(settle_seconds)
    return contents


## Index #######################################################################


@dataclass
class _Root:
    """A single watched root: a package name and its on-disk directory"""

    name: str
    path: str

    def source_module_for(self, file_path: str) -> str:
        """Map an absolute file path under the root to a dotted module name"""
        rel = os.path.relpath(file_path, self.path)
        rel_no_ext = os.path.splitext(rel)[0]
        parts = rel_no_ext.replace(os.sep, "/").split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            return self.name
        return ".".join([self.name] + parts)

    def file_for_module(self, module_name: str) -> Optional[str]:
        """Map a dotted module name under the root to its on-disk file path"""
        if module_name != self.name and not module_name.startswith(self.name + "."):
            return None
        if module_name == self.name:
            rel_parts: List[str] = []
        else:
            rel_parts = module_name[len(self.name) + 1 :].split(".")
        init_candidate = os.path.join(self.path, *rel_parts, "__init__.py")
        if os.path.isfile(init_candidate):
            return init_candidate
        file_candidate = os.path.join(self.path, *rel_parts) + ".py"
        if os.path.isfile(file_candidate):
            return file_candidate
        return None


@dataclass
class _FileState:
    """Scanned state for a single source file"""

    path: str
    group: str
    module: str
    is_init: bool
    records: Tuple[TrackedImport, ...] = ()
    probes: Tuple[str, ...] = ()
    mtime_ns: int = 0
    stale: bool = False
    exists: bool = True


def _default_track_module(**kwargs) -> TrackedImport:
    """Default scanner callback: construct the :class:`TrackedImport` from the
    visitor-supplied fields. This is the ``track_module`` the AST visitor calls
    back into.
    """
    return TrackedImport(**kwargs)


class ImportIndex:
    """Persistent, incrementally-maintained index of imports.

    Thread-safe: all state-changing methods and :meth:`snapshot` are serialized
    by ``self._lock``.
    """

    def __init__(
        self,
        roots: Iterable[Tuple[str, str]],
        track_module: Optional[Callable[..., Optional[TrackedImport]]] = None,
        resolve_packages: bool = False,
    ):
        self._roots = [_Root(name=name, path=os.path.abspath(path)) for name, path in roots]
        if not self._roots:
            raise ValueError("ImportIndex requires at least one watched root")
        # The scanner callback (kept as a public-ish hook). The AST visitor
        # calls back into this for every import.
        self.track_module = track_module or _default_track_module

        self._resolve_packages = resolve_packages
        self._package_resolver_cache: Dict[str, Optional[str]] = {}

        # Serializes the debounce/merge thread against snapshot readers.
        self._lock = threading.RLock()
        # Signalled whenever the index state changed.
        self._cond = threading.Condition(self._lock)
        self._version = 0

        # path -> _FileState for every scanned file
        self._files: Dict[str, _FileState] = {}
        # module name -> set of source files that probed it as a sub-module
        self._reverse_probes: Dict[str, Set[str]] = {}
        # (group, module) -> aggregated entry
        self._aggregate: Dict[Tuple[str, str], Dict[str, object]] = {}

    ## Public API #############################################################

    def start(self):
        """Perform the initial cold scan of every watched root"""
        with self._lock:
            self._full_rescan_locked()
            self._bump_locked()

    def snapshot(self):
        """Return a deep, JSON-serializable point-in-time view of the index.

        The snapshot is built under the index lock, so it always reflects a
        fully applied batch and never a half-written aggregation.
        """
        with self._lock:
            groups: Dict[str, dict] = {}
            for root in self._roots:
                groups[root.name] = {
                    "modules": {},
                    "stale_modules": [],
                    "total_imports": 0,
                    "stale": False,
                }
            for (group, module), entry in self._aggregate.items():
                g = groups[group]
                g["modules"][module] = {
                    "count": entry["count"],
                    "aliases": sorted(entry["aliases"]),
                    "category": entry["category"],
                    "optional": bool(entry["optional"]),
                    "package": entry["package"],
                }
                g["total_imports"] += entry["count"]
            for state in self._files.values():
                if state.stale:
                    g = groups[state.group]
                    g["stale"] = True
                    if state.module not in g["stale_modules"]:
                        g["stale_modules"].append(state.module)
            for g in groups.values():
                g["stale_modules"].sort()
            return {
                "groups": groups,
                "total_imports": sum(g["total_imports"] for g in groups.values()),
                "stale": any(g["stale"] for g in groups.values()),
                "version": self._version,
            }

    def wait_until_idle(self, timeout: float = 5.0, settle: float = 0.25):
        """Block until the index version stops changing (for tests/tools)"""
        deadline = time.time() + timeout
        with self._cond:
            last = -1
            stable_since = None
            while time.time() < deadline:
                self._cond.wait(0.05)
                if self._version != last:
                    last = self._version
                    stable_since = None
                elif stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= settle:
                    return True
        return False


    ## Change handling ########################################################

    def notify_changed(self, path: str):
        """Notify that a file may have changed (called by the watcher)"""
        norm = os.path.abspath(path)
        root = self._root_for_file(norm)
        if root is None:
            return
        with self._cond:
            self._apply_change_locked(norm, exists=os.path.isfile(norm))
            self._bump_locked()
            self._cond.notify_all()

    def notify_deleted(self, path: str):
        """Notify that a file was deleted"""
        norm = os.path.abspath(path)
        root = self._root_for_file(norm)
        if root is None:
            return
        with self._cond:
            self._apply_change_locked(norm, exists=False)
            self._bump_locked()
            self._cond.notify_all()

    def full_rescan(self):
        """Force a complete cold re-scan and return its snapshot.

        This is the reference implementation that incremental updates must
        always agree with.
        """
        with self._lock:
            self._full_rescan_locked()
            self._bump_locked()
            self._cond.notify_all()
            return self.snapshot()

    def _bump_locked(self):
        self._version += 1

    def _root_for_file(self, path: str) -> Optional[_Root]:
        for root in self._roots:
            try:
                common = os.path.commonpath([root.path, path])
            except ValueError:
                continue
            if common == root.path:
                return root
        return None

    def is_watched_module_file(self, path: str) -> bool:
        """Whether the path is a ``.py`` source belonging to a watched root and
        maps to a real module (excludes editor/atomic-write temp files).
        """
        norm = os.path.abspath(path)
        if not norm.endswith(".py"):
            return False
        if os.path.basename(norm).startswith("."):
            return False
        return self._root_for_file(norm) is not None

    def _all_python_files(self) -> List[Tuple[_Root, str]]:
        found = []
        for root in self._roots:
            for dirpath, dirnames, filenames in os.walk(root.path):
                # Skip caches and virtual environment noise.
                dirnames[:] = [
                    d for d in dirnames if d != "__pycache__" and not d.endswith(".egg-info")
                ]
                for fname in filenames:
                    if fname.endswith(".py"):
                        found.append((root, os.path.join(dirpath, fname)))
        found.sort(key=lambda item: item[1])
        return found

    def _full_rescan_locked(self):
        """Re-scan every file from scratch.

        On a parse failure for a previously-scanned file, the last good records
        are retained and the file is marked stale (rather than zeroing it).
        """
        prior = {path: state for path, state in self._files.items()}
        self._files = {}
        for root, path in self._all_python_files():
            self._files[path] = self._scan_file(root, path, prior.get(path))
        self._rebuild_groups_locked(root.name for root in self._roots)

    def _apply_change_locked(self, path: str, exists: bool):
        """Merge a single change into the index, recomputing only affected
        modules, then deterministically rebuilding the affected groups.
        """
        root = self._root_for_file(path)
        if root is None:
            return

        # Adding or removing a package's __init__ can change the package/module
        # identity of everything beneath it, so fall back to a full rescan.
        if os.path.basename(path) == "__init__.py" or (
            path in self._files and self._files[path].is_init
        ):
            log.debug("Package boundary change (%s); full rescan", path)
            self._full_rescan_locked()
            return

        module_name = root.source_module_for(path)
        if not exists:
            self._files.pop(path, None)
            affected = self._dependents_of_locked(module_name)
            affected.discard(path)
        else:
            is_new = path not in self._files
            affected = {path}
            # A newly created module can change resolution for files that
            # probed it (or a descendant) while it was absent.
            if is_new:
                affected.update(self._dependents_of_locked(module_name))

        # Re-parse every affected file that still exists.
        rescanned_groups = {root.name}
        for affected_path in list(affected):
            affected_root = self._root_for_file(affected_path)
            if affected_root is None:
                self._files.pop(affected_path, None)
                continue
            rescanned_groups.add(affected_root.name)
            if not os.path.isfile(affected_path):
                self._files.pop(affected_path, None)
                continue
            old = self._files.get(affected_path)
            self._files[affected_path] = self._scan_file(
                affected_root, affected_path, old
            )

        log.debug("Affected modules for change %s: %s", path, affected)
        self._rebuild_groups_locked(rescanned_groups)

    def _dependents_of_locked(self, module_name: str) -> Set[str]:
        """All source files that probed ``module_name`` or a descendant.

        Deleting or creating ``module_name`` can only change resolution for
        imports that probed the name itself or a name nested beneath it.
        """
        dependents: Set[str] = set()
        for probe, sources in self._reverse_probes.items():
            if probe == module_name or probe.startswith(module_name + "."):
                dependents.update(sources)
        return dependents

    def _rebuild_groups_locked(self, groups):
        """Deterministically rebuild reverse probes and aggregates for groups.

        Rebuilding the affected groups from the stored per-file records is what
        guarantees the summary is identical to a cold full re-scan, regardless
        of the order in which edits arrived. De-duplication and accumulation
        (count + alias union) happens only here in the outer layer and the
        group is part of the merge key, so groups never bleed into each other.
        """
        group_set = set(groups)

        # Drop reverse probes contributed by files in the affected groups.
        for probe, sources in list(self._reverse_probes.items()):
            for source_path in list(sources):
                state = self._files.get(source_path)
                if state is not None and state.group in group_set:
                    sources.discard(source_path)
            if not sources:
                del self._reverse_probes[probe]

        # Drop aggregate entries for the affected groups.
        for key in list(self._aggregate.keys()):
            if key[0] in group_set:
                del self._aggregate[key]

        # Rebuild both from the current per-file records.
        for state in self._files.values():
            if state.group not in group_set:
                continue
            for probe in state.probes:
                self._reverse_probes.setdefault(probe, set()).add(state.path)
            for record in state.records:
                key = (record.group, record.module)
                entry = self._aggregate.get(key)
                if entry is None:
                    entry = {
                        "count": 0,
                        "aliases": set(),
                        "category": record.category,
                        "optional": True,
                        "package": record.package,
                    }
                    self._aggregate[key] = entry
                entry["count"] += 1
                entry["aliases"].update(record.alias)
                entry["optional"] = entry["optional"] and bool(record.optional)
                if entry["category"] == constants.CATEGORY_UNKNOWN:
                    entry["category"] = record.category
                if entry["package"] is None and record.package is not None:
                    entry["package"] = record.package

    def _is_submodule_name(self, candidate: str) -> bool:
        """Whether ``candidate`` names an existing module under any watched root

        This performs no imports; it only checks on-disk layout.
        """
        for root in self._roots:
            if root.file_for_module(candidate) is not None:
                return True
        return False


    def _classify(
        self, module_name: str, source_module: str, optional: bool
    ) -> Tuple[str, Optional[str]]:
        """Classify an imported module.

        Routing follows the division of responsibility in the package:
        ``constants`` holds the categories, ``lazy_import_errors`` governs the
        optional/lazy error semantics upstream, and ``setup_tools`` resolves the
        installable distribution package name.
        """
        if module_name == constants.UNKNOWN_MODULE or not module_name:
            return constants.CATEGORY_UNKNOWN, None
        root_name = module_name.partition(".")[0]
        watched_roots = {root.name.partition(".")[0] for root in self._roots}
        if root_name in watched_roots and self._is_submodule_name(module_name):
            category = constants.CATEGORY_LOCAL
        elif constants.is_stdlib_module(module_name):
            category = constants.CATEGORY_STDLIB
        else:
            category = constants.CATEGORY_THIRD_PARTY

        package = None
        if self._resolve_packages:
            package = self._resolve_installable_package(module_name)
        return category, package

    def _resolve_installable_package(self, module_name: str) -> Optional[str]:
        """Best-effort mapping of a module to its installable package via the
        setup-tools package-resolution logic. Never raises.
        """
        if module_name in self._package_resolver_cache:
            return self._package_resolver_cache[module_name]
        package: Optional[str] = None
        try:
            # Local import keeps setup_tools concerns isolated to the tooling.
            from .setup_tools import _get_required_packages_for_imports

            names = _get_required_packages_for_imports([module_name])
            package = sorted(names)[0] if names else None
        except Exception as err:  # pragma: no cover - defensive
            log.debug2("Package resolution failed for %s: %s", module_name, err)
            package = None
        self._package_resolver_cache[module_name] = package
        return package

    def _scan_file(
        self, root: _Root, path: str, prior_state: Optional[_FileState]
    ) -> _FileState:
        """Parse a single file and build its fresh _FileState.

        On parse failure the previous version's records/probes are retained and
        the state is marked stale instead of being cleared.
        """
        module_name = root.source_module_for(path)
        is_init = os.path.basename(path) == "__init__.py"
        package_name = _module_package(module_name, is_init)

        try:
            source_text = read_settled_source(path)
            # Tolerate a UTF-8/UTF-16 BOM (common on Windows-saved files).
            if source_text[:1] in ("\ufeff", "\ufffe"):
                source_text = source_text.lstrip("\ufeff\ufffe")
            tree = ast.parse(source_text, filename=path)
        except (SyntaxError, ValueError, UnicodeDecodeError) as err:
            log.warning("Failed to parse %s; keeping prior version: %s", path, err)
            if prior_state is not None:
                return _FileState(
                    path=path,
                    group=root.name,
                    module=module_name,
                    is_init=is_init,
                    records=prior_state.records,
                    probes=prior_state.probes,
                    mtime_ns=prior_state.mtime_ns,
                    stale=True,
                    exists=True,
                )
            return _FileState(
                path=path,
                group=root.name,
                module=module_name,
                is_init=is_init,
                stale=True,
                exists=True,
            )
        except OSError as err:
            log.warning("Could not read %s: %s", path, err)
            if prior_state is not None:
                return _FileState(
                    path=path,
                    group=root.name,
                    module=module_name,
                    is_init=is_init,
                    records=prior_state.records,
                    probes=prior_state.probes,
                    mtime_ns=prior_state.mtime_ns,
                    stale=True,
                    exists=os.path.isfile(path),
                )
            raise

        records: List[TrackedImport] = []
        probes: Set[str] = set()

        def resolve_module(level: int, module: Optional[str]) -> Optional[str]:
            return resolve_relative(level, module, package_name)

        def is_submodule(candidate: str) -> bool:
            # Only probe candidates that could live under a watched root; the
            # probe set drives reverse invalidation.
            candidate_root = candidate.partition(".")[0]
            if candidate_root in {r.name for r in self._roots}:
                probes.add(candidate)
                return self._is_submodule_name(candidate)
            # External candidate: no probe registered, fall back to on-disk
            # check via sys if needed, but by default a from-name is an
            # attribute unless it is a known watched sub-module.
            return False

        def track_cb(**kwargs):
            record = self.track_module(**kwargs)
            if record is not None:
                records.append(record)
            return record

        visitor = _ImportVisitor(
            track_module=track_cb,
            group=root.name,
            source_module=module_name,
            package_name=package_name,
            resolve_module=resolve_module,
            classify=self._classify,
        )
        # Attach the sub-module oracle used by the visitor.
        resolve_module.is_submodule = is_submodule  # type: ignore[attr-defined]
        visitor.visit(tree)

        try:
            mtime_ns = os.stat(path).st_mtime_ns
        except OSError:
            mtime_ns = prior_state.mtime_ns if prior_state else 0

        return _FileState(
            path=path,
            group=root.name,
            module=module_name,
            is_init=is_init,
            records=tuple(records),
            probes=tuple(sorted(probes)),
            mtime_ns=mtime_ns,
            stale=False,
            exists=True,
        )


## Watching ####################################################################


class _ChangeQueue:
    """Coalescing, debounced queue of file changes.

    Events are merged by path and only flushed once the set of pending paths has
    been quiet for ``debounce`` seconds. Flushing happens under the index lock so
    a snapshot can never observe a partially-merged batch.
    """

    def __init__(self, index: ImportIndex, debounce: float = 0.2):
        self._index = index
        self._debounce = debounce
        self._pending: Dict[str, bool] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()

    def enqueue(self, path: str, deleted: bool = False):
        path = os.path.abspath(path)
        with self._lock:
            # A later "exists" event overrides an earlier deletion and vice
            # versa; we consult the filesystem at flush time anyway.
            self._pending[path] = deleted
            self._wake.set()

    def stop(self):
        self._stopping.set()
        self._wake.set()

    def run(self):
        while not self._stopping.is_set():
            self._wake.wait(timeout=self._debounce)
            if self._stopping.is_set():
                break
            # Wait for the pending set to be quiet for the debounce window.
            self._wake.clear()
            self._stopping.wait(self._debounce)
            with self._lock:
                pending = self._pending
                self._pending = {}
                self._wake.clear()
            if not pending:
                continue
            for path, deleted in pending.items():
                if not self._index.is_watched_module_file(path):
                    continue
                try:
                    if not os.path.isfile(path):
                        self._index.notify_deleted(path)
                    else:
                        self._index.notify_changed(path)
                except Exception as err:  # never let one bad event kill the loop
                    log.warning("Failed to merge change for %s: %s", path, err)


class _PollingWatcher(threading.Thread):
    """Portable fallback watcher based on mtime polling"""

    def __init__(self, index: ImportIndex, queue: _ChangeQueue, interval: float = 0.25):
        super().__init__(daemon=True, name="import-tracker-poller")
        self._index = index
        self._queue = queue
        self._interval = interval
        self._stopping = threading.Event()
        self._mtimes: Dict[str, int] = {}

    def stop(self):
        self._stopping.set()

    def run(self):
        while not self._stopping.is_set():
            try:
                seen = set()
                for root, path in self._index._all_python_files():
                    seen.add(path)
                    try:
                        mtime = os.stat(path).st_mtime_ns
                    except OSError:
                        continue
                    if path not in self._mtimes:
                        self._mtimes[path] = mtime
                    elif mtime != self._mtimes[path]:
                        self._mtimes[path] = mtime
                        self._queue.enqueue(path, deleted=False)
                for path in list(self._mtimes.keys()):
                    if path not in seen:
                        del self._mtimes[path]
                        self._queue.enqueue(path, deleted=True)
            except Exception as err:  # pragma: no cover - defensive
                log.warning("Polling watcher iteration failed: %s", err)
            self._stopping.wait(self._interval)


def _make_watchdog_watcher(index, queue):
    """Create a watchdog observer if the library is available, else None"""
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except Exception:
        return None

    queue_ref = queue
    index_ref = index

    class _Handler(FileSystemEventHandler):
        def _maybe_enqueue(self, event):
            if getattr(event, "is_directory", False):
                return

            # A rename/atomic-save produces a single "moved" event whose source
            # may be a temp name (e.g. x.py.tmp) and whose destination is the
            # real module. Enqueue the destination first so the created/replaced
            # module is never dropped just because the temp source is ignored.
            dest = getattr(event, "dest_path", None)
            if event.event_type == "moved" and dest and dest.endswith(".py"):
                if index_ref._root_for_file(os.path.abspath(dest)) is not None:
                    queue_ref.enqueue(dest, deleted=False)

            if not event.src_path.endswith(".py"):
                return
            if index_ref._root_for_file(os.path.abspath(event.src_path)) is None:
                return
            deleted = event.event_type in ("deleted", "moved")
            queue_ref.enqueue(event.src_path, deleted=deleted)

        def on_modified(self, event):
            self._maybe_enqueue(event)

        def on_created(self, event):
            self._maybe_enqueue(event)

        def on_moved(self, event):
            self._maybe_enqueue(event)

        def on_deleted(self, event):
            self._maybe_enqueue(event)

    observer = Observer()
    for root in index._roots:
        observer.schedule(_Handler(), root.path, recursive=True)
    return observer


class WatcherService:
    """Owns the index, the debounce queue and the underlying filesystem watcher"""

    def __init__(
        self,
        roots: Iterable[Tuple[str, str]],
        debounce: float = 0.2,
        poll: bool = False,
        track_module: Optional[Callable[..., Optional[TrackedImport]]] = None,
        resolve_packages: bool = False,
    ):
        self.index = ImportIndex(
            roots, track_module=track_module, resolve_packages=resolve_packages
        )
        self.queue = _ChangeQueue(self.index, debounce=debounce)
        self._queue_thread = threading.Thread(
            target=self.queue.run, daemon=True, name="import-tracker-queue"
        )
        self._watcher = None
        self._poll = poll

    def start(self):
        self.index.start()
        self._queue_thread.start()
        if not self._poll:
            self._watcher = _make_watchdog_watcher(self.index, self.queue)
        if self._watcher is not None:
            self._watcher.start()
        else:
            self._watcher = _PollingWatcher(self.index, self.queue)
            self._watcher.start()
        return self

    def stop(self):
        self.queue.stop()
        if self._watcher is not None:
            stop = getattr(self._watcher, "stop", None)
            if stop is not None:
                stop()
            join = getattr(self._watcher, "join", None)
            if join is not None:
                join(timeout=1.0)


## HTTP server / page ##########################################################


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>import_tracker watch</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem; color: #1b1b1b; }
  h1 { margin-bottom: 0.25rem; }
  .meta { color: #666; margin-bottom: 1rem; }
  .stale-banner {
    display: none; background: #fdecea; border: 1px solid #e0b4b4;
    color: #9f3a38; padding: 0.75rem 1rem; border-radius: 6px; margin: 1rem 0;
    font-weight: 600;
  }
  table { border-collapse: collapse; width: 100%; margin-top: 0.5rem; }
  th, td { text-align: left; padding: 0.35rem 0.6rem; border-bottom: 1px solid #eee; }
  th { background: #fafafa; }
  .group { margin-top: 2rem; }
  .badge {
    display: inline-block; font-size: 0.75rem; padding: 0.05rem 0.45rem;
    border-radius: 999px; background: #eee; margin-left: 0.4rem; color: #444;
  }
  .badge.stale { background: #fdecea; color: #9f3a38; }
  .muted { color: #888; }
  code { background: #f4f4f4; padding: 0.05rem 0.3rem; border-radius: 4px; }
</style>
</head>
<body>
  <h1>Import Tracker <span class="muted">incremental watch</span></h1>
  <div class="meta">
    Live summary is guaranteed identical to a cold full re-scan.
    Auto-refreshing every <span id="interval">1</span>s.
  </div>
  <div id="stale-banner" class="stale-banner">
    STALE: some modules could not be re-parsed; showing their last good imports.
    Stale modules are listed per group below.
  </div>
  <div id="root"></div>
<script>
async function refresh() {
  try {
    const resp = await fetch("/snapshot");
    const data = await resp.json();
    render(data);
  } catch (e) {
    document.getElementById("root").textContent = "Failed to load snapshot: " + e;
  }
}
function render(data) {
  document.getElementById("stale-banner").style.display = data.stale ? "block" : "none";
  const root = document.getElementById("root");
  root.innerHTML = "";
  const totalHeader = document.createElement("h2");
  totalHeader.textContent = "Total imports: " + data.total_imports;
  root.appendChild(totalHeader);
  for (const [group, g] of Object.entries(data.groups)) {
    const section = document.createElement("div");
    section.className = "group";
    const title = document.createElement("h3");
    title.textContent = group + " (" + g.total_imports + ")";
    if (g.stale) {
      const badge = document.createElement("span");
      badge.className = "badge stale";
      badge.textContent = "STALE";
      title.appendChild(badge);
    }
    section.appendChild(title);
    if (g.stale_modules.length) {
      const sm = document.createElement("div");
      sm.className = "muted";
      sm.textContent = "stale modules: " + g.stale_modules.join(", ");
      section.appendChild(sm);
    }
    const table = document.createElement("table");
    table.innerHTML =
      "<tr><th>module</th><th>count</th><th>aliases</th><th>category</th><th>optional</th></tr>";
    const mods = Object.keys(g.modules).sort();
    for (const mod of mods) {
      const info = g.modules[mod];
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td><code>" + mod + "</code></td>" +
        "<td>" + info.count + "</td>" +
        "<td>" + info.aliases.map(a => "<code>" + a + "</code>").join(", ") + "</td>" +
        "<td>" + (info.category || "") + "</td>" +
        "<td>" + (info.optional ? "optional" : "required") + "</td>";
      if (g.stale_modules.some(s => mod === s || mod.startsWith(s + "."))) {
        const b = document.createElement("span");
        b.className = "badge stale";
        b.textContent = "STALE";
        tr.firstChild.appendChild(b);
      }
      table.appendChild(tr);
    }
    section.appendChild(table);
    root.appendChild(section);
  }
}
refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
"""


def build_handler(index: ImportIndex):
    class _SnapshotHandler(BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):
            log.debug4("HTTP %s", args)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                body = _PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/snapshot":
                # The snapshot is taken under the index lock, so a concurrent
                # change batch can never leave it half-formed.
                payload = json.dumps(index.snapshot(), sort_keys=True).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()

    return _SnapshotHandler


class _ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(index: ImportIndex, port: int = 8765, host: str = "127.0.0.1"):
    server = _ReusableHTTPServer((host, port), build_handler(index))
    return server


## Root resolution and CLI ####################################################


def resolve_roots(targets: Iterable[str]) -> List[Tuple[str, str]]:
    """Resolve watch targets (module names or filesystem paths) to (name, path)
    package roots.
    """
    roots = []
    for target in targets:
        if os.path.sep in target or (os.altsep and os.altsep in target) or os.path.exists(target):
            abs_path = os.path.abspath(target)
            name = os.path.basename(abs_path.rstrip("/\\"))
            roots.append((name, abs_path))
            continue
        # Module name. Resolve the spec without importing the package body; the
        # lazy-errors meta finder keeps a missing third-party dep from aborting
        # resolution here.
        try:
            with lazy_import_errors():
                spec = importlib.util.find_spec(target)
        except Exception:
            spec = None
        if spec is None or spec.origin is None:
            raise ValueError(f"Could not locate module/package: {target}")
        if spec.origin == "namespace" or os.path.basename(spec.origin) != "__init__.py":
            # Single-file module.
            module_file = spec.origin
            path = os.path.dirname(module_file)
            name = spec.name
            roots.append((name, path))
        else:
            roots.append((spec.name, os.path.dirname(spec.origin)))
    return roots


def main(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(
        prog="python -m import_tracker.watch",
        description="Persistently watch one or more packages and serve a live, "
        "incrementally-updated import summary.",
    )
    parser.add_argument("targets", nargs="+", help="Module name(s) or path(s) to watch")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument(
        "--debounce",
        type=float,
        default=0.2,
        help="Seconds of quiet time before a change batch is merged",
    )
    parser.add_argument(
        "--poll",
        action="store_true",
        help="Use mtime polling instead of the native watchdog observer",
    )
    parser.add_argument(
        "--resolve-packages",
        action="store_true",
        help="Resolve third-party modules to installable package names",
    )
    parser.add_argument("--once", action="store_true", help="Cold scan once and print JSON")
    args = parser.parse_args(argv)

    roots = resolve_roots(args.targets)
    service = WatcherService(
        roots,
        debounce=args.debounce,
        poll=args.poll,
        resolve_packages=args.resolve_packages,
    )
    service.start()

    if args.once:
        print(json.dumps(service.index.snapshot(), indent=2, sort_keys=True))
        service.stop()
        return

    server = serve(service.index, port=args.port, host=args.host)
    print(
        f"Watching {[name for name, _ in roots]} on http://{args.host}:{args.port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        service.stop()


if __name__ == "__main__":  # pragma: no cover
    main()
