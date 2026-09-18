"""Remember the panel configuration in a file that outlives the scene.

The panel's settings live on ``scene.mpp``, which is *per file*: a batch run opens
every queued ``.blend``, so the scene the user configured is replaced and the next
scene starts from defaults.  Measured on this build: 11 of 14 configured fields
were lost the moment another scene was opened, which is why the settings had to be
re-entered after every run.

This module keeps a copy of the configuration in the user's Blender config folder
(``<config>/blender_motion_pipeline/panel_settings.json``) so it survives scene
changes, file changes and Blender restarts:

* :func:`save` writes it (atomically, so a crash cannot leave a truncated file);
* :func:`load` reads it back, tolerating a missing or corrupt file;
* :func:`clear` forgets it.

Nothing here touches ``bpy`` at import time, so the round trip is unit-testable
without Blender; the default path simply falls back to the home directory when
``bpy`` is unavailable.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

from ..io.json_io import load_json_file, save_json_file
from ..utils.logging_utils import get_logger
from ..utils.version import GENERATOR_VERSION

LOGGER = get_logger("panel_state")

#: Bumped when the payload's meaning changes; older files are still readable when
#: the keys they carry are unchanged, newer ones are ignored rather than guessed at.
STATE_SCHEMA = 1

#: File name inside the user's config folder.
STATE_FILENAME = "panel_settings.json"

#: Env override, used by the tests and by anyone who wants a portable profile.
ENV_OVERRIDE = "MPP_PANEL_SETTINGS"


def default_state_dir() -> str:
    """``<Blender config>/blender_motion_pipeline``, or a home fallback."""
    try:
        import bpy

        base = bpy.utils.user_resource("CONFIG", path="", create=True)
        if base:
            return os.path.join(base, "blender_motion_pipeline")
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), ".blender_motion_pipeline")


def state_path(path: str = "") -> str:
    """Where the remembered settings live."""
    override = os.environ.get(ENV_OVERRIDE, "")
    if path:
        return os.path.abspath(path)
    if override:
        return os.path.abspath(override)
    return os.path.join(default_state_dir(), STATE_FILENAME)


def wrap(payload: dict, *, source: str = "") -> dict:
    """Add the envelope (schema, version, timestamp) around a settings payload."""
    from ..io.manifest import utc_now_iso

    return {
        "schema": STATE_SCHEMA,
        "generator_version": GENERATOR_VERSION,
        "saved_utc": utc_now_iso(),
        "source": source,
        "settings": dict(payload or {}),
    }


def unwrap(payload: Any) -> dict:
    """Return the settings dict from a stored payload (``{}`` when unusable)."""
    if not isinstance(payload, dict):
        return {}
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        return {}
    try:
        schema = int(payload.get("schema") or 0)
    except (TypeError, ValueError):
        schema = 0
    if schema > STATE_SCHEMA:
        LOGGER.warning(
            "the remembered settings were written by a newer build (schema %s > %s); ignoring them",
            schema, STATE_SCHEMA,
        )
        return {}
    return settings


def save(payload: dict, path: str = "", *, source: str = "") -> str:
    """Write the settings; returns the file path.  Never raises for IO problems."""
    target = state_path(path)
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        # Write through a temporary file so an interrupted save cannot truncate
        # the settings the user relies on.
        handle, temporary = tempfile.mkstemp(
            prefix=".panel_settings_", suffix=".json", dir=os.path.dirname(target) or "."
        )
        os.close(handle)
        try:
            save_json_file(temporary, wrap(payload, source=source))
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError:
                    pass
    except Exception as exc:
        LOGGER.warning("could not remember the panel settings in %s: %s", target, exc)
        return ""
    LOGGER.debug("panel settings saved to %s", target)
    return target


def load(path: str = "") -> dict:
    """Read the remembered settings; ``{}`` when there are none (or unreadable)."""
    target = state_path(path)
    if not os.path.isfile(target):
        return {}
    try:
        payload = load_json_file(target)
    except Exception as exc:
        LOGGER.warning("could not read %s (%s); starting from defaults", target, exc)
        return {}
    return unwrap(payload)


def describe(path: str = "") -> dict:
    """What the UI needs to show about the stored settings."""
    target = state_path(path)
    info = {"path": target, "exists": os.path.isfile(target), "saved_utc": "", "fields": 0}
    if not info["exists"]:
        return info
    try:
        payload = load_json_file(target)
    except Exception:
        return info
    if isinstance(payload, dict):
        info["saved_utc"] = str(payload.get("saved_utc") or "")
        settings = payload.get("settings")
        if isinstance(settings, dict):
            info["fields"] = sum(
                len(value) if isinstance(value, dict) else 1 for value in settings.values()
            )
    return info


def clear(path: str = "") -> bool:
    """Forget the remembered settings; ``True`` when a file was removed."""
    target = state_path(path)
    try:
        if os.path.isfile(target):
            os.remove(target)
            LOGGER.info("forgot the remembered panel settings in %s", target)
            return True
    except OSError as exc:
        LOGGER.warning("could not remove %s: %s", target, exc)
    return False
