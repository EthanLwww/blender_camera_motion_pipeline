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
    """Absolute path of the add-on package folder containing ``start`` (or "").

    A folder qualifies when it holds **both** ``__init__.py`` and ``_bootstrap.py``,
    and the *nearest* one wins.  A bare ``__init__.py`` is not enough, in either
    direction:

    * ``render/`` and ``core/`` are subpackages, so the nearest ``__init__.py`` may
      be one level too deep;
    * this machine's ``.../scripts/addons/`` has an ``__init__.py`` too, so the
      *outermost* one is one level too high.

    Getting it wrong registered the historical name against a folder with no
    submodules, and the scripts died with
    ``No module named 'blender_motion_pipeline.config'``.
    """
    current = os.path.dirname(os.path.abspath(start))
    for _ in range(MAX_DEPTH):
        if os.path.isfile(os.path.join(current, "__init__.py")) and os.path.isfile(
            os.path.join(current, "_bootstrap.py")
        ):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return ""


def child_package_root(directory: str) -> str:
    """A package folder *inside* ``directory`` (or "").

    Generated project folders ship the package as a subfolder next to the render
    script, so ``<project>/blender_camera_motion_pipeline`` is the package root
    for a script placed at ``<project>/render_sequences.py``.
    """
    base = os.path.abspath(directory)
    if not os.path.isdir(base):
        return ""
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return ""
    for name in names:
        if name.startswith(".") or name == "__pycache__":
            continue
        candidate = os.path.join(base, name)
        if os.path.isfile(os.path.join(candidate, "__init__.py")) and os.path.isfile(
            os.path.join(candidate, "_bootstrap.py")
        ):
            return candidate
    return ""


def locate(start: str) -> str:
    """Path of the ``_bootstrap.py`` that serves a script at ``start`` (or "").

    Covers every shape this project ships in: inside the package
    (``<pkg>/tests/x.py``), in a subfolder of it (``<pkg>/render/x.py``), next to
    the package (``<farm>/x.py`` beside ``<farm>/<pkg>/``) and inside a generated
    project folder (``<project>/x.py`` beside ``<project>/<pkg>/``).
    """
    here = os.path.abspath(start)
    if not os.path.isdir(here):
        here = os.path.dirname(here)
    current = here
    for _ in range(MAX_DEPTH):
        beside = os.path.join(current, "_bootstrap.py")
        if os.path.isfile(beside):
            return beside
        nested = child_package_root(current)
        if nested:
            return os.path.join(nested, "_bootstrap.py")
        parent = os.path.dirname(current)
        if parent == current:
            break
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
        # A generated project folder puts the script at the project root and the
        # package in a subfolder of it.
        base = os.path.abspath(start)
        root = child_package_root(base if os.path.isdir(base) else os.path.dirname(base))
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
