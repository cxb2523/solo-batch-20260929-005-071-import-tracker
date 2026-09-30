"""
Shared constants across the various parts of the library
"""

# Standard
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

# Category labels used by the persistent incremental watcher
CATEGORY_STDLIB = "stdlib"
CATEGORY_THIRD_PARTY = "third_party"
CATEGORY_LOCAL = "local"
CATEGORY_UNKNOWN = "unknown"

# Sentinel module name recorded when a relative import cannot be resolved to a
# concrete module name from the available package context
UNKNOWN_MODULE = "unknown"


def is_stdlib_module(module_name):
    """Return whether the (root of the) given module name is part of the
    Python standard library.
    """
    root = module_name.partition(".")[0]
    std_names = getattr(
        sys,
        "stdlib_module_names",
        frozenset(
            [
                "_thread",
                "abc",
                "ast",
                "collections",
                "contextlib",
                "dis",
                "functools",
                "http",
                "importlib",
                "json",
                "logging",
                "os",
                "re",
                "sys",
                "threading",
                "types",
                "typing",
            ]
        ),
    )
    return root in std_names
