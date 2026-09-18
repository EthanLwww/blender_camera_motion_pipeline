"""Import the add-on package whatever its folder happens to be called.

Every module *inside* the package uses relative imports, so the add-on itself is
name-agnostic: rename ``blender_motion_pipeline`` to anything and the panel keeps
working.  The scripts that live inside it are a different story -- the CLI, the
headless renderer and the test suites are run as standalone files
(``blender -b -P <package>/motion_pipeline_cli.py``) and therefore have to import
the package *by name*.  Hardcoding that name is what breaks when the folder is
renamed: after this project's folder became ``blender_camera_motion_pipeline``,
both entry points died with::

    ModuleNotFoundError: No module named 'blender_motion_pipeline'

This module is loaded **by file path** (so it works before the package is
importable).  It finds the package root by walking up from the caller and
registers a *stub package* under the historical name whose ``__path__`` points at
the real folder.  Python then loads every submodule from disk under that name, so
the process ends up with exactly one copy of each module and one instance of each
singleton (``core.ui_task``, the registries, ...).

Nothing is imported eagerly: walking and importing the whole package here would
execute modules just because they exist (``tests/verify_end_to_end.py`` and
``tests/make_e2e_scenes.py`` do real work on import-time side effects).
"""

from __future__ import annotations

import importlib.machinery
import os
import sys
import types

#: The name the scripts and tests import; kept working no matter the folder name.
LEGACY_NAME = "blender_motion_pipeline"

#: How far up to look for the package root (``render/render_sequences.py`` is two
#: levels below it, a test suite one).
MAX_DEPTH = 4


def package_root(start: str) -> str:
    """Absolute path of the package folder that contains ``start`` (or "")."""
    current = os.path.dirname(os.path.abspath(start))
    for _ in range(MAX_DEPTH):
        if os.path.isfile(os.path.join(current, "__init__.py")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return ""
        current = parent
    return ""


def already_importable(name: str) -> bool:
    """True when ``name`` can be imported without help."""
    if name in sys.modules:
        return True
    try:
        return importlib.machinery.PathFinder.find_spec(name) is not None
    except Exception:
        return False


def bootstrap(start: str, *, alias: str = LEGACY_NAME) -> str:
    """Make ``import <alias>`` work from a script inside the package.

    ``start`` is normally ``__file__`` of the calling script, or the path of this
    file.  Returns the real package name, or ``""`` when no package root was found
    (which leaves the caller's imports to fail with Python's own error).
    """
    root = package_root(start)
    if not root:
        return ""
    parent = os.path.dirname(root)
    if parent and parent not in sys.path:
        sys.path.insert(0, parent)
    name = os.path.basename(root)
    if not name:
        return ""
    if name == alias or already_importable(alias):
        # Nothing to bridge: the folder has the expected name, or something else
        # (an installed copy) already provides it.
        return name

    stub = types.ModuleType(alias)
    stub.__path__ = [root]
    stub.__package__ = alias
    stub.__file__ = os.path.join(root, "__init__.py")
    stub.__spec__ = importlib.machinery.ModuleSpec(
        alias, loader=None, is_package=True, origin=stub.__file__
    )
    stub.__spec__.submodule_search_locations = [root]
    stub.__doc__ = f"Alias for the add-on package at {root!r}."
    sys.modules[alias] = stub
    return name


def bootstrap_from_file(caller_file: str, **kwargs) -> str:
    """``bootstrap`` for a script, using its own location."""
    return bootstrap(caller_file, **kwargs)
