"""Default configuration plus discovery of the reference template file.

The project brief points at
``E:\\UE\\DataGenScenes\\Plugins\\MetaHumanScenePipeline\\Templates\\camera_motion_templates.json``
but explicitly warns that the real layout may differ, so discovery probes a
list of candidates and also accepts an override through the
``MOTION_PIPELINE_TEMPLATES`` environment variable.

The copy that ships with the add-on lives in the package's own ``templates/``
folder, beside the code that reads it:
``<package>/templates/camera_motion_templates.json`` (plus the light and test
sets and the pre-migration ``*.unreal_backup.json`` originals).  Builds before
that folder existed kept the same files in ``config/``, which is still probed --
last -- so an install that was upgraded in place and left a copy behind keeps
working.
"""

from __future__ import annotations

import os

from ..io.json_io import JsonError, load_json_file
from ..io.path_utils import normalize_path
from .models import BatchConfig, ConfigError

#: Filenames that are accepted as a motion template document.
TEMPLATE_FILENAMES = (
    "camera_motion_templates.json",
    "motion_templates.json",
    "camera_templates.json",
)

#: Directories probed, in order, when ``motion.template_path`` is empty.
TEMPLATE_DIR_CANDIDATES = (
    r"E:\UE\DataGenScenes\Plugins\MetaHumanScenePipeline\Templates",
    r"E:\UE\MetaHumanScenePipeline\Templates",
    r"E:\UE\DataGenScenes\Plugins\MetaHumanScenePipeline\Config",
    r"E:\VSCode\CameraCtrl",
)

#: The dedicated folder the add-on ships its template documents in.
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUNDLED_DIR = os.path.join(_HERE, "templates")
#: Where they used to live; probed after the dedicated folder, never before it.
_LEGACY_BUNDLED_DIR = os.path.join(_HERE, "config")
#: Bundled probes, in order.
BUNDLED_DIRS = (_BUNDLED_DIR, _LEGACY_BUNDLED_DIR)


def discover_template_path() -> str:
    """Best-effort absolute path of a motion template JSON, or ``''``.

    Probe order matters: the user's own template set wins over the copy bundled
    with the add-on.  Returning the bundled copy first would make the panel show
    a path inside Blender's add-ons folder, which is confusing (and hides the
    fact that a real template set is being used).  The bundled copy -- which is
    byte-identical to the reference document -- is only the fallback, so a fresh
    machine with no template set still works out of the box.
    """
    override = os.environ.get("MOTION_PIPELINE_TEMPLATES", "").strip()
    if override:
        candidate = normalize_path(override)
        if os.path.isfile(candidate):
            return candidate
    for directory in (*TEMPLATE_DIR_CANDIDATES, *BUNDLED_DIRS):
        if not os.path.isdir(directory):
            continue
        for filename in TEMPLATE_FILENAMES:
            candidate = os.path.join(directory, filename)
            if os.path.isfile(candidate):
                return normalize_path(candidate)
    return ""


def load_discovered_templates() -> tuple[list, str]:
    """Return ``(templates, path)`` from discovery, or ``([], '')``."""
    path = discover_template_path()
    if not path:
        return [], ""
    try:
        payload = load_json_file(path)
    except JsonError:
        return [], path
    if isinstance(payload, list):
        return payload, path
    return [], path


def default_config() -> BatchConfig:
    """A runnable configuration: embed the discovered templates if possible."""
    config = BatchConfig()
    templates, path = load_discovered_templates()
    if templates:
        config.motion.template_path = path
    config.render.output_root = ""
    return config


def default_config_dict() -> dict:
    return default_config().to_dict()


def load_config_file(path: str, *, base: "BatchConfig | None" = None) -> BatchConfig:
    """Read a config JSON and merge it onto ``base`` (or the defaults)."""
    payload = load_json_file(path)
    if not isinstance(payload, dict):
        raise ConfigError(f"{path}: configuration root must be a JSON object")
    if base is None:
        base = default_config()
    config = BatchConfig.from_dict(payload, base=base)
    config.warnings.insert(0, f"loaded configuration from {normalize_path(path)}")
    return config


def save_config_file(path: str, config: BatchConfig, *, include_scenes: bool = True) -> str:
    from ..io.json_io import save_json_file

    return save_json_file(path, config.to_dict(include_scenes=include_scenes))
