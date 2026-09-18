"""UI-free pipeline core.

Importing this package must stay cheap and must not require ``bpy``: the CLI and
the render script import modules from here directly, and the pure test suite
imports the sub-modules that are Blender-agnostic.

``batch_runner`` and ``blender_context`` *do* import ``bpy`` at call time (never
at import time), so they are re-exported lazily via :pep:`562`.
"""

from __future__ import annotations

from .scene_loader import (
    SceneEntry,
    SceneLoadResult,
    load_blend_file,
    load_scene_list,
    merge_scene_entries,
    missing_scene_entries,
    open_scene_for_generation,
    save_scene_list,
    scan_directory,
    scene_name_for,
)
from .sequence_generator import (
    SequenceGenerator,
    SequenceRequest,
    SequenceResult,
    find_sequence_files,
    load_sequence_config,
)
from .sequence_manager import SequenceInfo, SequenceManager

__all__ = [
    "BatchReport",
    "BatchRunner",
    "SceneEntry",
    "SceneLoadResult",
    "SceneOutcome",
    "SequenceGenerator",
    "SequenceInfo",
    "SequenceManager",
    "SequenceRequest",
    "SequenceResult",
    "find_sequence_files",
    "load_blend_file",
    "load_scene_list",
    "load_sequence_config",
    "merge_scene_entries",
    "missing_scene_entries",
    "open_scene_for_generation",
    "run_batch",
    "save_scene_list",
    "scan_directory",
    "scene_name_for",
]

_LAZY = {
    "BatchReport": (".batch_runner", "BatchReport"),
    "BatchRunner": (".batch_runner", "BatchRunner"),
    "SceneOutcome": (".batch_runner", "SceneOutcome"),
    "run_batch": (".batch_runner", "run_batch"),
}


def __getattr__(name: str):
    """Import ``batch_runner`` only when it is actually used."""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0], package=__name__)
    return getattr(module, target[1])


def __dir__():
    return sorted(set(__all__))
