"""
Shared constants across the various parts of the library
"""

# Standard
import os
import sys

# The name of this package (import_tracker)
THIS_PACKAGE = sys.modules[__name__].__package__.partition(".")[0]

# Labels for direct vs transitive dependencies
TYPE_DIRECT = "direct"
TYPE_TRANSITIVE = "transitive"

# Info section headers
INFO_TYPE = "type"
INFO_STACK = "stack"
INFO_OPTIONAL = "optional"
# Name recorded for a relative import that cannot be restored to an absolute
# module name (e.g. the import level walks past the root package)
UNKNOWN_MODULE = "unknown"

# Classification categories for statically tracked imports
CATEGORY_UNKNOWN = "unknown"
CATEGORY_LOCAL = "local"
CATEGORY_STDLIB = "stdlib"
CATEGORY_THIRD_PARTY = "third_party"
CATEGORY_SETUP_TOOL = "setup_tool"


def classify_import(module_name, root_package):
    """Classify a resolved imported module name relative to the watched root

    The categories are deliberately coarse: imports belonging to the watched
    package are ``local``, standard library modules are ``stdlib`` and
    everything else is ``third_party``. Setup tooling (setuptools, wheel, etc.)
    is classified separately by ``setup_tools.is_setup_tool``.
    """
    if module_name == UNKNOWN_MODULE:
        return CATEGORY_UNKNOWN
    base_name = module_name.partition(".")[0]
    if base_name == root_package:
        return CATEGORY_LOCAL
    stdlib_names = getattr(sys, "stdlib_module_names", None)
    if stdlib_names is not None and base_name in stdlib_names:
        return CATEGORY_STDLIB
    if stdlib_names is None and _is_stdlib_fallback(base_name):
        return CATEGORY_STDLIB
    return CATEGORY_THIRD_PARTY



def _is_stdlib_fallback(base_name):
    """Fallback standard-library detection for Python versions that do not
    expose ``sys.stdlib_module_names`` (below 3.10)
    """
    import sysconfig

    std_path = os.path.realpath(os.path.dirname(os.__file__))
    paths = sysconfig.get_paths()
    purelib = os.path.realpath(paths.get("stdlib", std_path))
    platlib = os.path.realpath(paths.get("platstdlib", std_path))
    for base_dir in {std_path, purelib, platlib}:
        for suffix in (".py", ""):
            if os.path.exists(os.path.join(base_dir, base_name + suffix)):
                return True
        if os.path.exists(os.path.join(base_dir, base_name, "__init__.py")):
            return True
    return False
