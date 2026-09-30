"""
Static, file-based incremental import index

Unlike :mod:`import_tracker.import_tracker`, which executes modules and walks
their bytecode, this module scans package *sources* with the ``ast`` module.
That makes it safe to run against a tree of files that is being edited and
allows recomputing only the modules affected by a change.

The public data carrier is :class:`TrackedImport`, whose field semantics are
part of the package's contract. The visitor emits one raw
:class:`TrackedImport` for every import alias; all counting, deduplication and
alias accumulation happens in the outer :class:`IncrementalIndex`, keyed by
importing group and imported module name.
"""

# Standard
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple
import ast
import os
import threading

# Local
from . import constants
from .log import log


class TrackedImport(NamedTuple):
    """A single raw import observation

    Fields are not collapsed by the visitor: one of these is emitted per import
    alias. The index merges these on the outside, grouping by importing module
    and merging records with the same resolved ``module`` name.

    group: the fully-qualified name of the importing module (isolation unit)
    module: resolved absolute imported module name, or constants.UNKNOWN_MODULE
    alias: local binding name used for the import (may equal the module name)
    level: relative import level (0 for absolute imports)
    optional: True when the import sits inside a try/except block
    line: source line number of the import statement
    """

    group: str
    module: str
    alias: str
    level: int = 0
    optional: bool = False
    line: int = 0


## Path helpers ###############################################################


def normalize_path(path: str) -> str:
    """Normalize a path into a case-consistent, absolute, real path used as a
    key in the index"""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def discover_python_files(root_dir: str) -> List[str]:
    """Find every importable python file under the given root directory"""
    python_files = []
    for dir_path, dir_names, file_names in os.walk(root_dir):
        dir_names[:] = [
            dir_name
            for dir_name in dir_names
            if dir_name != "__pycache__" and not dir_name.startswith(".")
        ]
        for file_name in file_names:
            if file_name.endswith(".py"):
                python_files.append(normalize_path(os.path.join(dir_path, file_name)))
    return sorted(python_files)


def path_to_module_name(path: str, root_dir: str, root_package: str) -> Optional[str]:
    """Convert a python file path into its fully-qualified module name relative
    to the watched package root, or None if the file is outside of it"""
    norm_path = normalize_path(path)
    norm_root = normalize_path(root_dir)
    rel_path = os.path.relpath(norm_path, norm_root)
    if rel_path.startswith(".."):
        return None
    no_ext, ext = os.path.splitext(rel_path)
    if ext != ".py":
        return None
    parts = no_ext.split(os.sep)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return root_package
    return ".".join([root_package] + parts)


def module_to_candidate_paths(
    module_name: str, root_dir: str, root_package: Optional[str] = None
) -> List[str]:
    """Return the file paths that could define the given module name (either a
    package __init__ or a sibling module file)

    ``root_dir`` is the directory that maps to ``root_package`` itself, so the
    leading package name is stripped from the relative path.
    """
    parts = module_name.split(".")
    if root_package is not None:
        package_parts = root_package.split(".")
        if parts[: len(package_parts)] == package_parts and len(parts) > len(
            package_parts
        ):
            parts = parts[len(package_parts) :]
        elif parts == package_parts:
            parts = []
    if not parts:
        return [normalize_path(os.path.join(root_dir, "__init__.py"))]
    rel_module = os.path.join(*parts)
    return [
        normalize_path(os.path.join(root_dir, rel_module, "__init__.py")),
        normalize_path(os.path.join(root_dir, rel_module + ".py")),
    ]


def parent_package_name(module_name: str) -> str:
    """Get the dotted parent package name of a module"""
    return module_name.rpartition(".")[0]


## AST visitor ################################################################


class ImportVisitor(ast.NodeVisitor):
    """AST visitor that emits one raw TrackedImport per import alias

    The optional ``track_module`` callback is invoked for every importing module
    before its imports are collected, mirroring the way the bytecode-based
    tracker hooks into module tracking. Relative imports are restored to their
    absolute names using the import level and the importing module's package;
    imports that cannot be resolved (e.g. ``from .... import x``) are recorded
    under constants.UNKNOWN_MODULE.
    """

    def __init__(
        self,
        module_name: str,
        is_package_init: bool,
        root_dir: str,
        root_package: str,
        *,
        track_module: Optional[Callable[[str, Optional[str]], None]] = None,
        module_exists: Optional[Callable[[str], bool]] = None,
    ):
        self._module_name = module_name
        self._package_name = (
            module_name if is_package_init else parent_package_name(module_name)
        )
        self._root_dir = root_dir
        self._root_package = root_package
        self._track_module = track_module
        self._module_exists = module_exists or self._default_module_exists
        self._records: List[TrackedImport] = []
        self._optional_lines = set()
        self._local_targets: Set[str] = set()
        self._potential_targets: Set[str] = set()
        self._from_targets: Dict[int, Set[str]] = {}

    def visit(self, node):
        """Fire the track_module callback when entering the module root"""
        if (
            self._track_module is not None
            and isinstance(node, ast.Module)
            and not self._records
            and not self._optional_lines
        ):
            self._track_module(self._module_name, None)
        super().visit(node)

    def visit_Try(self, node):
        """Track import statement lines that are protected by a try/except"""
        if node.handlers:
            for child in node.body:
                self._collect_import_lines(child)
        self.generic_visit(node)

    def _collect_import_lines(self, node):
        for child in ast.walk(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                end_line = getattr(child, "end_lineno", child.lineno) or child.lineno
                self._optional_lines.update(range(child.lineno, end_line + 1))

    def visit_Import(self, node):
        optional = node.lineno in self._optional_lines
        for alias in node.names:
            module_name = alias.name
            self._records.append(
                TrackedImport(
                    group=self._module_name,
                    module=module_name,
                    alias=alias.asname or module_name.partition(".")[0],
                    level=0,
                    optional=optional,
                    line=node.lineno,
                )
            )
            if module_name.partition(".")[0] == self._root_package:
                if self._module_exists(module_name):
                    self._local_targets.add(module_name)
                else:
                    # The file may be created later; watch its candidate paths
                    # so its creation invalidates this dependent.
                    self._potential_targets.add(module_name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        optional = node.lineno in self._optional_lines
        resolved = self._resolve_from_module(node)
        line_targets = self._from_targets.setdefault(node.lineno, set())

        # Resolve the module each alias actually binds to. Names that resolve to
        # a local submodule file/dir map to that module; non-module attributes
        # map back to the resolved anchor module. If a relative from-import
        # points at a local name that no longer exists, the anchor package is
        # used (a fresh full scan of the current tree resolves the same way).
        for alias in node.names:
            resolved_module, edge_target, potential = self._resolve_alias(
                node, resolved, alias
            )
            if edge_target is not None:
                line_targets.add(edge_target)
            self._potential_targets.update(potential)
            self._records.append(
                TrackedImport(
                    group=self._module_name,
                    module=resolved_module,
                    alias=alias.asname or alias.name,
                    level=node.level or 0,
                    optional=optional,
                    line=node.lineno,
                )
            )
        self._local_targets.update(line_targets)
        self.generic_visit(node)

    def _resolve_alias(self, node, resolved, alias):
        """Resolve a single ImportFrom alias

        Returns a tuple ``(module_name, edge_target, potential_targets)``:

        module_name: aggregate key (may be constants.UNKNOWN_MODULE)
        edge_target: existing local module this alias depends on, or None
        potential_targets: local module names that are currently missing but
            could later be created, in which case this dependent must be
            recomputed (file creation is otherwise invisible to reverse deps).

        ``from pkg import name`` binds ``name`` to the submodule ``pkg.name``
        when that module exists, otherwise ``name`` is an attribute of ``pkg``.
        For relative imports, if neither the submodule nor the anchor package
        currently exists in the watched tree, the name is recorded as unknown.
        """
        level = node.level or 0
        if alias.name == "*":
            return resolved, self._local_edge(resolved, level), set()
        if resolved == constants.UNKNOWN_MODULE:
            return resolved, None, set()
        submodule_candidate = f"{resolved}.{alias.name}"
        is_local_submodule = (
            submodule_candidate.partition(".")[0] == self._root_package
        )
        submodule_exists = self._module_exists(submodule_candidate)
        anchor_exists = level == 0 or self._module_exists(resolved)
        if submodule_exists:
            return submodule_candidate, submodule_candidate, set()
        potential = set()
        if is_local_submodule:
            # If the submodule file appears later, this import would resolve to
            # it, so its creation must invalidate this group.
            potential.add(submodule_candidate)
        if level > 0 and not anchor_exists:
            # The anchor package itself is also missing. For relative imports
            # both the submodule and the anchor are unresolved: record unknown
            # but keep watching every missing local name in the chain.
            if resolved.partition(".")[0] == self._root_package:
                potential.add(resolved)
            return constants.UNKNOWN_MODULE, None, potential
        # The imported name is an attribute of the anchor module (or a module
        # external to the watched tree)
        return (
            resolved,
            self._local_edge(resolved, level, anchor_exists),
            potential,
        )

    def _local_edge(self, resolved, level, anchor_exists=None):
        if resolved == constants.UNKNOWN_MODULE:
            return None
        if resolved.partition(".")[0] != self._root_package:
            return None
        if anchor_exists is None:
            anchor_exists = self._module_exists(resolved)
        if level > 0 and not anchor_exists:
            return None
        if level == 0 and not anchor_exists:
            return None
        return resolved

    def _default_module_exists(self, module_name):
        return any(
            os.path.exists(path)
            for path in module_to_candidate_paths(
                module_name, self._root_dir, self._root_package
            )
        )

    def _resolve_from_module(self, node):
        """Restore the absolute module name of an ImportFrom statement

        Absolute imports are returned as-is. Relative imports walk the package
        chain using ``level``. When the level walks past the root package the
        import cannot be restored and constants.UNKNOWN_MODULE is returned.
        """
        module_name = node.module or ""
        level = node.level or 0
        if level == 0:
            return module_name

        package_parts = (
            self._package_name.split(".") if self._package_name else []
        )
        # level 1 -> current package; level 2 -> parent package, ...
        keep = len(package_parts) - (level - 1)
        # The anchor must still contain at least the root package. A level
        # that walks past the top-level package cannot be restored and would
        # raise ImportError at runtime, so record it as unknown.
        if keep < 1:
            return constants.UNKNOWN_MODULE
        base_parts = package_parts[:keep]
        if module_name:
            base_parts = base_parts + module_name.split(".")
        return ".".join(base_parts)

    @property
    def records(self) -> Tuple[TrackedImport, ...]:
        return tuple(self._records)

    @property
    def local_targets(self) -> Set[str]:
        """Set of fully-qualified local module names this module depends on"""
        return self._local_targets

    @property
    def potential_targets(self) -> Set[str]:
        """Local module names this import could bind to if they were created

        These are currently missing, so they carry no dependency records, but
        the index watches their candidate paths to invalidate this module when
        one of them appears on disk.
        """
        return set(self._potential_targets)

    @property
    def line_targets(self) -> Dict[int, Set[str]]:
        return {line: set(targets) for line, targets in self._from_targets.items()}


def parse_file(
    path: str,
    root_dir: str,
    root_package: str,
    *,
    track_module: Optional[Callable[[str, Optional[str]], None]] = None,
    module_exists: Optional[Callable[[str], bool]] = None,
) -> Optional[Tuple[str, bool, ImportVisitor]]:
    """Parse a single python file, returning (module_name, is_package, visitor)

    Returns None if the file cannot be mapped to a module name. Raises
    SyntaxError (and OSError) so callers can distinguish parse failures.
    """
    module_name = path_to_module_name(path, root_dir, root_package)
    if module_name is None:
        return None
    is_package = os.path.splitext(os.path.basename(path))[0] == "__init__"
    with open(path, "r", encoding="utf-8") as handle:
        # Read the full file before parsing so a file caught mid-write either
        # parses fully or fails and is treated as stale rather than being
        # partially indexed.
        source = handle.read()
    tree = ast.parse(source, filename=path)
    visitor = ImportVisitor(
        module_name,
        is_package,
        normalize_path(root_dir),
        root_package,
        track_module=track_module,
        module_exists=module_exists,
    )
    visitor.visit(tree)
    return module_name, is_package, visitor

## Outer aggregation #########################################################


class _ModuleState:
    """Indexed state for a single python file"""

    __slots__ = ("records", "out_edges", "stale", "mtime_ns", "size")

    def __init__(self, records, out_edges, stale, mtime_ns, size):
        self.records = records
        self.out_edges = out_edges
        self.stale = stale
        self.mtime_ns = mtime_ns
        self.size = size


class ModuleAggregate(NamedTuple):
    """Outer-layer merge of all TrackedImports for one (group, module) pair"""

    group: str
    module: str
    count: int
    aliases: Tuple[str, ...]
    optional: bool
    category: str
    stale: bool


class IncrementalIndex:
    """Persistent incremental index over a single package source tree

    All public methods are thread-safe. File reads, debounce merging and
    snapshot reads go through :attr:`lock`, so a snapshot can never observe a
    half-applied update. Parse failures retain the previous records and mark
    the affected group ``stale`` instead of clearing its counts.
    """

    def __init__(
        self,
        root_dir: str,
        root_package: str,
        *,
        track_module: Optional[Callable[[str, Optional[str]], None]] = None,
        lazy_import_errors: Optional[Callable] = None,
    ):
        self._root_dir = normalize_path(root_dir)
        self._root_package = root_package
        self._track_module = track_module
        self._lazy_import_errors = lazy_import_errors
        self.lock = threading.RLock()
        self._states: Dict[str, _ModuleState] = {}
        self._path_by_module: Dict[str, str] = {}
        self._reverse_deps: Dict[str, Set[str]] = {}
        self._version = 0

    ## Lifecycle #############################################################

    def cold_start(self) -> None:
        """Build the index from a complete scan of the root directory"""
        with self.lock:
            paths = discover_python_files(self._root_dir)
            self._states.clear()
            self._path_by_module.clear()
            self._reverse_deps.clear()
            # Pre-register every module name so relative imports resolve
            # correctly regardless of the order files are parsed in.
            for path in paths:
                module_name = path_to_module_name(
                    path, self._root_dir, self._root_package
                )
                if module_name is not None:
                    self._path_by_module[module_name] = path
            for path in paths:
                self._recompute_locked(path, deleted=False)
            self._version += 1
            log.debug("Cold start indexed %d files", len(paths))

    def apply_changes(self, changed_paths: Iterable[str]) -> List[str]:
        """Merge a batch of file changes and recompute affected modules

        Returns the sorted list of paths that were actually recomputed,
        including reverse-dependency invalidation.
        """
        with self.lock:
            # Retry files that previously failed to parse. A file whose syntax
            # is now valid must converge with a fresh full scan even if no
            # reverse edge could ever point at it (a file that never parsed
            # successfully carries no edges). Files still unparseable keep
            # their previous records and stale flag.
            stale_retry = {
                stale_path
                for stale_path, state in self._states.items()
                if state.stale and os.path.exists(stale_path)
            }
            affected = self._collect_affected_locked(changed_paths)
            affected.update(stale_retry)
            recomputed = sorted(affected)

            # Deletions are processed first (so dependents see the shrunken
            # tree). A deleted target's retained reverse-dependency entry is
            # what pulls its former dependents into this batch, so it is only
            # pruned after those dependents have been recomputed.
            deleted_paths = {
                path for path in affected if not os.path.exists(path)
            }
            existing_paths = sorted(affected - deleted_paths)
            for path in sorted(deleted_paths):
                self._recompute_locked(path, deleted=True)
            for path in existing_paths:
                self._recompute_locked(path, deleted=False)
            # Reverse-dependency entries for deleted targets are retained as
            # tombstones so a later recreation pulls the former dependents back
            # into the affected set. Only prune entries whose dependents are
            # themselves gone, and only while the target stays absent (a target
            # deleted and re-created within the batch keeps its entry).
            for deleted_path in sorted(deleted_paths):
                if os.path.exists(deleted_path):
                    continue
                dependents = self._reverse_deps.get(deleted_path)
                if dependents is None:
                    continue
                live_dependents = {
                    dependent for dependent in dependents if os.path.exists(dependent)
                }
                if live_dependents:
                    dependents.intersection_update(live_dependents)
                else:
                    del self._reverse_deps[deleted_path]
            if affected:
                self._version += 1
            log.debug2("Applied %d changes: %s", len(affected), recomputed)
            return recomputed

    def _collect_affected_locked(self, changed_paths):
        affected = set()
        queue = []
        for raw_path in changed_paths:
            path = normalize_path(raw_path)
            if path not in affected:
                affected.add(path)
                queue.append(path)
        while queue:
            path = queue.pop(0)
            for dependent in self._reverse_deps.get(path, set()):
                if dependent not in affected:
                    affected.add(dependent)
                    queue.append(dependent)
        return affected

    ## Recompute #############################################################

    def _recompute_locked(self, path: str, *, deleted: bool) -> None:
        if deleted:
            self._remove_state_locked(path)
            return

        module_name = path_to_module_name(path, self._root_dir, self._root_package)
        if module_name is None:
            self._remove_state_locked(path)
            return
        self._path_by_module.setdefault(module_name, path)

        previous = self._states.get(path)
        try:
            stat = os.stat(path)
            parsed = parse_file(
                path,
                self._root_dir,
                self._root_package,
                track_module=self._safe_track_module,
                module_exists=self._module_exists_locked,
            )
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError) as err:
            # The file may be caught mid-write or it may currently contain
            # invalid syntax. Retain the previous records and mark stale so the
            # aggregate counts are not zeroed.
            log.debug2("Stale parse for %s: %s", path, err)
            if previous is not None:
                previous.stale = True
            else:
                self._states[path] = _ModuleState((), set(), True, None, None)
                self._path_by_module.setdefault(module_name, path)
            return

        module_name, is_package, visitor = parsed
        if previous is not None:
            self._disconnect_edges_locked(path, previous.out_edges)
        out_edges = set()
        for target_module in visitor.local_targets:
            target_path = self._module_path_locked(target_module)
            if target_path is not None and target_path != path:
                out_edges.add(target_path)
                self._reverse_deps.setdefault(target_path, set()).add(path)
        # Watch candidate paths of local modules that are currently missing.
        # If such a file is created later it must invalidate this dependent,
        # even though no reverse edge existed while the target was absent.
        # Missing targets are retained as tombstones by edge disconnection and
        # self-heal once the file appears (or are pruned with their dependents).
        for target_module in visitor.potential_targets:
            for candidate_path in module_to_candidate_paths(
                target_module, self._root_dir, self._root_package
            ):
                if candidate_path == path:
                    continue
                existing_target = self._module_path_locked(target_module)
                if existing_target is not None:
                    out_edges.add(existing_target)
                    self._reverse_deps.setdefault(existing_target, set()).add(path)
                else:
                    out_edges.add(candidate_path)
                    self._reverse_deps.setdefault(candidate_path, set()).add(path)
        state = _ModuleState(
            records=tuple(visitor.records),
            out_edges=out_edges,
            stale=False,
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
        )
        self._states[path] = state
        self._path_by_module[module_name] = path

    def _remove_state_locked(self, path: str) -> None:
        state = self._states.pop(path, None)
        if state is not None:
            self._disconnect_edges_locked(path, state.out_edges)
        dead_modules = [
            module_name
            for module_name, module_path in self._path_by_module.items()
            if module_path == path
        ]
        for module_name in dead_modules:
            del self._path_by_module[module_name]
        # NOTE: The reverse-dependency entry for ``path`` (where it is the
        # target of other modules' edges) is intentionally retained as a
        # tombstone. If the file is re-created, its former dependents must still
        # be invalidated and re-resolved; end-of-batch cleanup prunes the entry
        # once none of those dependents exist either. Each surviving dependent
        # rebuilds its own out-edges when recomputed, which prunes edges that
        # no longer apply to targets that still exist.

    def _disconnect_edges_locked(self, path: str, edges: Set[str]) -> None:
        for target_path in edges:
            # Edges pointing at a target that no longer exists are retained as
            # tombstones: if the target is ever re-created, this dependent must
            # be invalidated and re-resolved. The batch cleanup prunes tombstones
            # whose dependents have also disappeared.
            if not os.path.exists(target_path):
                continue
            dependents = self._reverse_deps.get(target_path)
            if dependents is not None:
                dependents.discard(path)
                if not dependents:
                    del self._reverse_deps[target_path]

    def _module_exists_locked(self, module_name: str) -> bool:
        """Whether the given local module currently exists as a live (indexed
        or on-disk) file within the watched tree"""
        return self._module_path_locked(module_name) is not None

    def _module_path_locked(self, module_name: str) -> Optional[str]:
        existing = self._path_by_module.get(module_name)
        if existing is not None and os.path.exists(existing):
            return existing
        for candidate in module_to_candidate_paths(
            module_name, self._root_dir, self._root_package
        ):
            if os.path.exists(candidate):
                return candidate
        return None

    def _safe_track_module(self, module_name, package_name):
        if self._track_module is None:
            return
        try:
            self._track_module(module_name, package_name)
        except Exception as err:  # pragma: no cover - defensive logging hook
            log.debug2("track_module callback failed for %s: %s", module_name, err)

    ## Snapshot / aggregation ################################################

    def snapshot(self):
        """Get an immutable, consistent summary of the current index

        The structure is::

            {
                "root_package": str,
                "version": int,
                "totals": {"imports": N, "modules": N, "stale_groups": N},
                "categories": {category: count, ...},
                "groups": {
                    group_name: {
                        "stale": bool,
                        "imports": N,
                        "modules": [
                            {"module": name, "count": N,
                             "aliases": [...], "optional": bool,
                             "category": str}
                        ],
                    },
                },
            }

        Counts are produced purely in the outer layer: records are merged on
        (group, module), counts accumulate per occurrence and every distinct
        alias is listed. Groups are isolated (merging never crosses groups).
        """
        with self.lock:
            groups: Dict[str, Dict[str, Dict[str, object]]] = {}
            category_totals: Dict[str, int] = {}
            total_imports = 0

            for path, state in self._states.items():
                group_aggregates = groups.setdefault(
                    self._group_name_locked(path),
                    {"stale": False, "aggregates": {}},
                )
                if state.stale:
                    group_aggregates["stale"] = True
                for record in state.records:
                    merged = group_aggregates["aggregates"].setdefault(
                        record.module,
                        {
                            "count": 0,
                            "aliases": [],
                            "optional_all": True,
                            "category": self._classify(record.module),
                        },
                    )
                    merged["count"] += 1
                    if record.alias not in merged["aliases"]:
                        merged["aliases"].append(record.alias)
                    if not record.optional:
                        merged["optional_all"] = False
                    total_imports += 1
                    category_totals[merged["category"]] = (
                        category_totals.get(merged["category"], 0) + 1
                    )

            groups_out = {}
            unique_modules = set()
            stale_groups = 0
            for group_name in sorted(groups):
                group_data = groups[group_name]
                modules_out = []
                for module_name in sorted(group_data["aggregates"]):
                    merged = group_data["aggregates"][module_name]
                    unique_modules.add((group_name, module_name))
                    modules_out.append(
                        ModuleAggregate(
                            group=group_name,
                            module=module_name,
                            count=merged["count"],
                            aliases=tuple(sorted(merged["aliases"])),
                            optional=bool(merged["optional_all"]),
                            category=merged["category"],
                            stale=bool(group_data["stale"]),
                        )._asdict()
                    )
                group_imports = sum(module["count"] for module in modules_out)
                if group_data["stale"]:
                    stale_groups += 1
                groups_out[group_name] = {
                    "stale": bool(group_data["stale"]),
                    "imports": group_imports,
                    "modules": modules_out,
                }

            return {
                "root_package": self._root_package,
                "version": self._version,
                "totals": {
                    "imports": total_imports,
                    "modules": len(unique_modules),
                    "stale_groups": stale_groups,
                },
                "categories": {
                    category: category_totals.get(category, 0)
                    for category in (
                        constants.CATEGORY_UNKNOWN,
                        constants.CATEGORY_LOCAL,
                        constants.CATEGORY_STDLIB,
                        constants.CATEGORY_THIRD_PARTY,
                        constants.CATEGORY_SETUP_TOOL,
                    )
                    if category_totals.get(category, 0)
                },
                "groups": groups_out,
            }

    def _group_name_locked(self, path: str) -> str:
        module_name = path_to_module_name(
            path, self._root_dir, self._root_package
        )
        return module_name if module_name is not None else normalize_path(path)

    def _classify(self, module_name: str) -> str:
        from .setup_tools import is_setup_tool

        category = constants.classify_import(module_name, self._root_package)
        if category == constants.CATEGORY_THIRD_PARTY and is_setup_tool(module_name):
            return constants.CATEGORY_SETUP_TOOL
        return category

    ## Consistency verification ##############################################

    def full_rescan_counts(self):
        """Compute the expected aggregate counts from a fresh full scan

        This does not mutate the live index. Stale files are read using their
        retained records so the comparison is made against the same set of
        sources.
        """
        with self.lock:
            records = []
            stale_paths = set()
            for path, state in self._states.items():
                if state.stale:
                    stale_paths.add(path)
                    records.extend(state.records)
                    continue
                try:
                    parsed = parse_file(
                        path,
                        self._root_dir,
                        self._root_package,
                        track_module=None,
                    )
                except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
                    records.extend(state.records)
                    stale_paths.add(path)
                    continue
                if parsed is not None:
                    _, _, visitor = parsed
                    records.extend(visitor.records)
            return self._aggregate_records_locked(records, stale_paths)

    def current_counts(self):
        """Aggregate the live retained records using the outer merge"""
        with self.lock:
            records = []
            stale_paths = set()
            for path, state in self._states.items():
                records.extend(state.records)
                if state.stale:
                    stale_paths.add(path)
            return self._aggregate_records_locked(records, stale_paths)

    def _aggregate_records_locked(self, records, stale_paths):
        stale_groups = {self._group_name_locked(path) for path in stale_paths}
        merged: Dict[Tuple[str, str], Dict[str, object]] = {}
        category_totals: Dict[str, int] = {}
        for record in records:
            key = (record.group, record.module)
            entry = merged.setdefault(
                key,
                {
                    "count": 0,
                    "aliases": set(),
                    "optional_all": True,
                    "category": self._classify(record.module),
                },
            )
            entry["count"] += 1
            entry["aliases"].add(record.alias)
            if not record.optional:
                entry["optional_all"] = False
            category_totals[entry["category"]] = (
                category_totals.get(entry["category"], 0) + 1
            )

        modules = {}
        for (group_name, module_name), entry in merged.items():
            modules[(group_name, module_name)] = {
                "count": entry["count"],
                "aliases": tuple(sorted(entry["aliases"])),
                "optional": bool(entry["optional_all"]),
                "category": entry["category"],
            }
        return {
            "imports": sum(entry["count"] for entry in modules.values()),
            "modules": len(modules),
            "categories": dict(sorted(category_totals.items())),
            "stale_groups": frozenset(sorted(stale_groups)),
            "modules_map": modules,
        }

    def verify_against_full_rescan(self):
        """Return True if the incremental aggregate equals a fresh full scan,
        entry for entry"""
        with self.lock:
            current = self.current_counts()
            full = self.full_rescan_counts()
            comparable = ("imports", "modules", "categories", "stale_groups")
            return all(current[key] == full[key] for key in comparable) and (
                current["modules_map"] == full["modules_map"]
            )

    ## Introspection #########################################################

    @property
    def version(self) -> int:
        with self.lock:
            return self._version

    @property
    def root_dir(self) -> str:
        return self._root_dir

    @property
    def root_package(self) -> str:
        return self._root_package

    def known_files(self):
        with self.lock:
            return sorted(self._states)
