"""Typed configuration model.

Design notes
------------
* Dataclasses only (no ``bpy`` import) so the exact same object can be built
  from the add-on UI, from a JSON file, and from ``argparse`` on a render node.
* Every parser is *lenient*: an unknown key is reported as a warning instead of
  aborting a long batch, but a wrong type or out-of-range value is an error.
* ``from_dict`` accepts both ``snake_case`` and ``camelCase`` keys, because the
  Unreal reference project's config files use camelCase.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1

CHARACTER_MODE_NONE = "none"
CHARACTER_MODE_WITH = "with_character"
CHARACTER_MODE_BOTH = "both"

CHARACTER_MODE_LABELS = {
    CHARACTER_MODE_NONE: "No character",
    CHARACTER_MODE_WITH: "Character sequences only",
    CHARACTER_MODE_BOTH: "Both with and without character",
}


class ConfigError(ValueError):
    """Raised when a configuration document cannot be honoured."""


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.title() for part in tail)


def _canonical(text: str) -> str:
    """``outputRoot``/``output-root``/``OUTPUT_ROOT`` -> ``outputroot``."""
    return str(text).replace("-", "_").replace("_", "").lower()


def _key_variants(key: str) -> "tuple[str, ...]":
    """Accepted spellings for the snake_case field ``key``, most specific first.

    ``key`` itself is deliberately checked **last**.  When a partial
    configuration is merged onto a base, the base contributes the snake_case
    spelling and the incoming document contributes whatever the user wrote
    (``outputRoot``, ``OUTPUT_ROOT``...).  Both can therefore be present at
    once, and the user's explicit value has to win.
    """
    camel = _camel(key)
    dashed = key.replace("_", "-")
    return (
        camel,
        camel.lower(),
        dashed,
        dashed.lower(),
        key.upper(),
        key.lower(),
        _canonical(key),
        key,
    )


def _match_keys(raw: dict, key: str) -> "list[str]":
    """All keys in ``raw`` that spell the snake_case field ``key``.

    Ordering is preferred-spelling first, then whatever else matches after
    normalising case and separators.  Returning *all* of them is what lets
    :func:`_pop` clean up an alias after the canonical key already supplied the
    value -- otherwise the leftover alias is later reported as an unknown key.
    """
    found: "list[str]" = []
    for variant in _key_variants(key):
        if variant in raw and variant not in found:
            found.append(variant)
    canonical = _canonical(key)
    for existing in raw:
        if existing not in found and _canonical(existing) == canonical:
            found.append(existing)
    return found


def _match_key(raw: dict, key: str) -> "str | None":
    """First (preferred) spelling of ``key`` present in ``raw``."""
    matches = _match_keys(raw, key)
    return matches[0] if matches else None


def _pop(raw: dict, key: str):
    """Remove every alias of ``key``; return ``(source_key, value)``.

    The value comes from the preferred spelling, so an explicit
    ``outputRoot`` in the incoming document always beats a ``output_root``
    inherited from a base config.
    """
    matches = _match_keys(raw, key)
    if not matches:
        return (None, None)
    source = matches[0]
    value = raw.pop(source)
    for alias in matches[1:]:
        raw.pop(alias, None)
    return (source, value)


def _coerce_scalar(value: Any, kind: type):
    if value is None:
        return None
    if kind is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if kind is int:
        if isinstance(value, bool):
            raise ConfigError(f"expected an integer, got a boolean ({value!r})")
        if isinstance(value, float) and not float(value).is_integer():
            raise ConfigError(f"expected an integer, got {value!r}")
        return int(value)
    if kind is float:
        if isinstance(value, bool):
            raise ConfigError(f"expected a number, got a boolean ({value!r})")
        return float(value)
    if kind is str:
        return str(value)
    return value


def _read_typed(raw: dict, key: str, kind: type, default, warnings: "list[str]"):
    """Pop ``key`` from ``raw`` accepting snake_case, camelCase and PACKED case."""
    _source, value = _pop(raw, key)
    if _source is None:
        return copy.deepcopy(default)
    try:
        return _coerce_scalar(value, kind)
    except (ConfigError, TypeError, ValueError) as exc:
        warnings.append(f"{key}: {exc}; keeping default {default!r}")
        return copy.deepcopy(default)


def _read_choice(raw: dict, key: str, choices, default, warnings: "list[str]"):
    value = _read_typed(raw, key, str, default, warnings)
    if value in choices:
        return value
    normalised = str(value).strip().lower()
    for choice in choices:
        if str(choice).lower() == normalised:
            return choice
    warnings.append(f"{key}: {value!r} is not one of {sorted(map(str, choices))}; keeping {default!r}")
    return default


def _read_list(raw: dict, key: str, default, warnings: "list[str]"):
    _source, value = _pop(raw, key)
    if _source is None or value is None:
        return copy.deepcopy(default)
    if isinstance(value, str):
        return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return list(value)
    warnings.append(f"{key}: expected a list, got {type(value).__name__}; keeping default")
    return copy.deepcopy(default)


def _read_float_list(raw: dict, key: str, default, warnings: "list[str]"):
    values = _read_list(raw, key, default, warnings)
    out = []
    for item in values:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            warnings.append(f"{key}: ignoring non-integer entry {item!r}")
    return out


def _report_unknown(raw: dict, owner: str, warnings: "list[str]") -> None:
    for leftover in sorted(raw):
        warnings.append(f"{owner}: unknown key {leftover!r} ignored")


def _dedupe(items: "list[str]") -> "list[str]":
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _sub_dict(raw: dict, key: str, warnings: "list[str]") -> dict:
    _source, value = _pop(raw, key)
    if _source is None or value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    warnings.append(f"{key}: expected an object, got {type(value).__name__}; ignoring")
    return {}


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------
@dataclass
class TemplateUnitScale:
    """Timeline settings for templates.  There is **no** coordinate conversion.

    Templates are authored in **Blender coordinates**: ``location`` is an offset in
    the camera's own frame in metres (``+X`` right, ``+Y`` up, ``-Z`` forward) and
    ``rotation`` is degrees about the camera's own axes.  Nothing is rescaled, no
    axis is swapped and no sign is flipped, so a template's numbers are exactly the
    numbers Blender uses.

    This class used to carry the Unreal -> Blender mapping (``location_scale``,
    ``location_forward/right/up``, ``yaw/pitch/roll_axis`` and their signs).  Those
    keys are gone on purpose: a config that still sets them gets one warning naming
    the migration script, and the values are ignored.
    """

    fps: float = 24.0
    #: Order the three local rotation components are applied in (Blender's own
    #: ``Euler`` orders; ``XYZ`` applies X first).
    rotation_order: str = "XYZ"

    ROTATION_ORDERS = ("XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX")
    AXES = ("X", "Y", "Z")

    #: Keys from the Unreal-coordinate era; accepted-but-ignored, with a warning.
    REMOVED_KEYS = (
        "location_scale",
        "location_forward",
        "location_right",
        "location_up",
        "yaw_axis",
        "pitch_axis",
        "roll_axis",
        "yaw_sign",
        "pitch_sign",
        "roll_sign",
    )

    @classmethod
    def from_dict(cls, raw: dict, warnings: "list[str]", owner: str = "motion.unit_scale"):
        raw = dict(raw or {})
        legacy = sorted(key for key in cls.REMOVED_KEYS if key in raw)
        for key in legacy:
            raw.pop(key, None)
        instance = cls(
            fps=_read_typed(raw, "fps", float, 24.0, warnings),
            rotation_order=_read_choice(raw, "rotation_order", cls.ROTATION_ORDERS, "XYZ", warnings),
        )
        _report_unknown(raw, owner, warnings)
        if legacy:
            warnings.append(
                f"{owner}: {', '.join(legacy)} no longer do anything -- templates are "
                "authored in Blender coordinates now (metres, +X right / +Y up / -Z "
                "forward, local degrees). Convert an Unreal-coordinate set once with "
                "'python tests/migrate_unreal_templates.py --input <file>'."
            )
        instance.validate()
        return instance

    def validate(self) -> None:
        if self.fps <= 0:
            raise ConfigError("motion.unit_scale.fps must be > 0")
        if self.rotation_order not in self.ROTATION_ORDERS:
            raise ConfigError(
                f"motion.unit_scale.rotation_order must be one of {self.ROTATION_ORDERS}"
            )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TemplateSource:
    """Where the motion templates come from.

    Exactly one of ``path`` / ``inline`` / ``embedded`` is expected.  ``auto``
    makes the loader try the known reference paths from the project brief before
    giving up.
    """

    path: str = ""
    inline: list = field(default_factory=list)
    auto: bool = True

    def to_dict(self) -> dict:
        return {"path": self.path, "inline": self.inline, "auto": self.auto}


@dataclass
class MotionSection:
    template_path: str = ""
    template_data: list = field(default_factory=list)
    template_names: list = field(default_factory=list)
    template_overrides: dict = field(default_factory=dict)
    matrix_source: str = "auto"
    frame_start: int = 0
    #: Optional explicit last frame.  ``None`` means "whatever the template's
    #: last keyframe maps to", which is the normal case.
    frame_end: int | None = None
    frame_scale: float = 1.0
    interpolation: str = "BEZIER"
    unit_scale: TemplateUnitScale = field(default_factory=TemplateUnitScale)

    MATRIX_SOURCES = ("auto", "file", "input")
    INTERPOLATIONS = ("LINEAR", "BEZIER", "CONSTANT")

    @classmethod
    def from_dict(cls, raw: dict, warnings: "list[str]"):
        raw = dict(raw or {})
        instance = cls(
            template_path=_read_typed(raw, "template_path", str, "", warnings),
            template_data=_read_list(raw, "template_data", [], warnings),
            template_names=_read_list(raw, "template_names", [], warnings),
            template_overrides=_sub_dict(raw, "template_overrides", warnings),
            matrix_source=_read_choice(raw, "matrix_source", cls.MATRIX_SOURCES, "auto", warnings),
            frame_start=_read_typed(raw, "frame_start", int, 0, warnings),
            frame_end=_read_typed(raw, "frame_end", int, None, warnings),
            frame_scale=_read_typed(raw, "frame_scale", float, 1.0, warnings),
            interpolation=_read_choice(raw, "interpolation", cls.INTERPOLATIONS, "BEZIER", warnings),
            unit_scale=TemplateUnitScale.from_dict(
                _sub_dict(raw, "unit_scale", warnings), warnings
            ),
        )
        _report_unknown(raw, "motion", warnings)
        instance.validate()
        return instance

    def validate(self) -> None:
        if self.frame_start < 0:
            raise ConfigError("motion.frame_start must be >= 0")
        if self.frame_end is not None and int(self.frame_end) < int(self.frame_start):
            raise ConfigError("motion.frame_end must be >= motion.frame_start")
        if self.frame_scale <= 0:
            raise ConfigError("motion.frame_scale must be > 0")
        if not isinstance(self.template_overrides, dict):
            raise ConfigError("motion.template_overrides must be an object")

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["unit_scale"] = self.unit_scale.to_dict()
        return payload


@dataclass
class ValidationSection:
    enabled: bool = True
    sample_step: int = 10
    extra_sample_frames: list = field(default_factory=list)
    clearance: float = 0.25
    #: How far ahead of the lens a surface may be before the shot is considered
    #: blocked.  Independent of ``clearance`` (which is about the camera body
    #: being *near* geometry): a wall 1 m in front of the lens is a blocked shot
    #: even though the camera itself is in open space.
    obstruction_distance: float = 1.0
    inside_epsilon: float = 0.001
    max_position_jump: float = 2.0
    max_rotation_jump_deg: float = 45.0
    min_clip_start: float = 0.01
    max_clip_end: float = 100000.0
    occlusion_ray_count: int = 9
    #: Relax jump limits by ``sqrt(frame_gap)`` when consecutive validation
    #: samples are more than one frame apart.  Without this, a coarse
    #: ``sample_step`` reports a legitimate multi-frame move as a per-frame jump.
    jump_gap_scale: str = "sqrt"
    check_character_visibility: bool = True
    min_character_visible_ratio: float = 0.05
    min_character_on_screen_frames: float = 0.6
    character_probe_points: int = 27
    check_character_overlap: bool = True

    JUMP_GAP_SCALES = ("none", "sqrt", "linear")

    @classmethod
    def from_dict(cls, raw: dict, warnings: "list[str]"):
        raw = dict(raw or {})
        instance = cls(
            enabled=_read_typed(raw, "enabled", bool, True, warnings),
            sample_step=_read_typed(raw, "sample_step", int, 10, warnings),
            extra_sample_frames=_read_float_list(raw, "extra_sample_frames", [], warnings),
            clearance=_read_typed(raw, "clearance", float, 0.25, warnings),
            obstruction_distance=_read_typed(raw, "obstruction_distance", float, 1.0, warnings),
            inside_epsilon=_read_typed(raw, "inside_epsilon", float, 0.001, warnings),
            max_position_jump=_read_typed(raw, "max_position_jump", float, 2.0, warnings),
            max_rotation_jump_deg=_read_typed(raw, "max_rotation_jump_deg", float, 45.0, warnings),
            min_clip_start=_read_typed(raw, "min_clip_start", float, 0.01, warnings),
            max_clip_end=_read_typed(raw, "max_clip_end", float, 100000.0, warnings),
            occlusion_ray_count=_read_typed(raw, "occlusion_ray_count", int, 9, warnings),
            jump_gap_scale=_read_choice(raw, "jump_gap_scale", cls.JUMP_GAP_SCALES, "sqrt", warnings),
            check_character_visibility=_read_typed(raw, "check_character_visibility", bool, True, warnings),
            min_character_visible_ratio=_read_typed(raw, "min_character_visible_ratio", float, 0.05, warnings),
            min_character_on_screen_frames=_read_typed(raw, "min_character_on_screen_frames", float, 0.6, warnings),
            character_probe_points=_read_typed(raw, "character_probe_points", int, 27, warnings),
            check_character_overlap=_read_typed(raw, "check_character_overlap", bool, True, warnings),
        )
        _report_unknown(raw, "validation", warnings)
        instance.validate()
        return instance

    def validate(self) -> None:
        if self.sample_step < 1:
            raise ConfigError("validation.sample_step must be >= 1")
        if self.clearance < 0:
            raise ConfigError("validation.clearance must be >= 0")
        if self.obstruction_distance < 0:
            raise ConfigError("validation.obstruction_distance must be >= 0")
        if self.max_position_jump < 0 or self.max_rotation_jump_deg < 0:
            raise ConfigError("validation jump limits must be >= 0")
        if self.min_clip_start <= 0:
            raise ConfigError("validation.min_clip_start must be > 0")
        if self.max_clip_end <= self.min_clip_start:
            raise ConfigError("validation.max_clip_end must exceed min_clip_start")
        if self.occlusion_ray_count < 1:
            raise ConfigError("validation.occlusion_ray_count must be >= 1")
        for name in ("min_character_visible_ratio", "min_character_on_screen_frames"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ConfigError(f"validation.{name} must be within [0, 1]")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SearchSection:
    enabled: bool = True
    min_radius: float = 0.2
    max_radius: float = 3.0
    candidate_count: int = 64
    azimuth_samples: int = 12
    elevation_samples: int = 5
    shell_only: bool = False
    max_retries: int = 2
    random_seed: int = 1234
    allow_rotation_adjust: bool = True
    max_rotation_adjust_deg: float = 25.0
    allow_focal_adjust: bool = True
    focal_adjust_steps: float = 3.0
    max_output_candidates: int = 1
    weights: dict = field(default_factory=lambda: {
        "distance": 1.0,
        "clipping": 4.0,
        "character_invisible": 2.0,
        "occlusion": 1.5,
        "rotation_delta": 0.5,
        "focal_delta": 0.25,
    })

    @classmethod
    def from_dict(cls, raw: dict, warnings: "list[str]"):
        raw = dict(raw or {})
        weights_raw = _sub_dict(raw, "weights", warnings) or {}
        defaults = SearchSection().weights
        weights = {}
        for key, default_value in defaults.items():
            weights[key] = float(weights_raw.pop(key, default_value))
        for leftover in sorted(weights_raw):
            warnings.append(f"search.weights: unknown key {leftover!r} ignored")
        instance = cls(
            enabled=_read_typed(raw, "enabled", bool, True, warnings),
            min_radius=_read_typed(raw, "min_radius", float, 0.2, warnings),
            max_radius=_read_typed(raw, "max_radius", float, 3.0, warnings),
            candidate_count=_read_typed(raw, "candidate_count", int, 64, warnings),
            azimuth_samples=_read_typed(raw, "azimuth_samples", int, 12, warnings),
            elevation_samples=_read_typed(raw, "elevation_samples", int, 5, warnings),
            shell_only=_read_typed(raw, "shell_only", bool, False, warnings),
            max_retries=_read_typed(raw, "max_retries", int, 2, warnings),
            random_seed=_read_typed(raw, "random_seed", int, 1234, warnings),
            allow_rotation_adjust=_read_typed(raw, "allow_rotation_adjust", bool, True, warnings),
            max_rotation_adjust_deg=_read_typed(raw, "max_rotation_adjust_deg", float, 25.0, warnings),
            allow_focal_adjust=_read_typed(raw, "allow_focal_adjust", bool, True, warnings),
            focal_adjust_steps=_read_typed(raw, "focal_adjust_steps", float, 3.0, warnings),
            max_output_candidates=_read_typed(raw, "max_output_candidates", int, 1, warnings),
            weights=weights,
        )
        _report_unknown(raw, "search", warnings)
        instance.validate()
        return instance

    def validate(self) -> None:
        if self.min_radius < 0 or self.max_radius < 0:
            raise ConfigError("search radii must be >= 0")
        if self.max_radius < self.min_radius:
            raise ConfigError("search.max_radius must be >= search.min_radius")
        for name in ("candidate_count", "azimuth_samples", "elevation_samples"):
            if getattr(self, name) < 1:
                raise ConfigError(f"search.{name} must be >= 1")
        if self.max_retries < 0:
            raise ConfigError("search.max_retries must be >= 0")
        if self.max_output_candidates < 0:
            raise ConfigError("search.max_output_candidates must be >= 0")
        for key, value in self.weights.items():
            if value < 0:
                raise ConfigError(f"search.weights.{key} must be >= 0")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BatchSection:
    output_root: str = ""
    mode: str = CHARACTER_MODE_NONE
    scene_name_mode: str = "stem"
    overwrite: bool = False
    resume: bool = True
    #: Sequences always store the camera animation, never a copy of the scene: the
    #: renderer replays the payload onto the scene shipped beside it.  There is no
    #: switch any more -- a config file that still sets ``save_sequence_blend`` gets
    #: an "unknown key" warning.
    save_validation_report: bool = True
    verbose: bool = True
    character_asset_root: str = ""
    animation_asset_root: str = ""
    character_provider: str = "auto"
    path_mappings: list = field(default_factory=list)

    SCENE_NAME_MODES = ("stem", "filename")

    @classmethod
    def from_dict(cls, raw: dict, warnings: "list[str]"):
        raw = dict(raw or {})
        mappings_raw = _read_list(raw, "path_mappings", [], warnings)
        mappings = []
        for item in mappings_raw:
            if isinstance(item, dict) and item.get("from") and item.get("to"):
                mappings.append({"from": str(item["from"]), "to": str(item["to"])})
            elif isinstance(item, str) and "=" in item:
                src, dst = item.split("=", 1)
                mappings.append({"from": src.strip(), "to": dst.strip()})
            else:
                warnings.append(f"batch.path_mappings: ignoring unusable entry {item!r}")
        instance = cls(
            output_root=_read_typed(raw, "output_root", str, "", warnings),
            mode=_read_choice(raw, "mode", CHARACTER_MODE_LABELS, CHARACTER_MODE_NONE, warnings),
            scene_name_mode=_read_choice(raw, "scene_name_mode", cls.SCENE_NAME_MODES, "stem", warnings),
            overwrite=_read_typed(raw, "overwrite", bool, False, warnings),
            resume=_read_typed(raw, "resume", bool, True, warnings),
            save_validation_report=_read_typed(raw, "save_validation_report", bool, True, warnings),
            verbose=_read_typed(raw, "verbose", bool, True, warnings),
            character_asset_root=_read_typed(raw, "character_asset_root", str, "", warnings),
            animation_asset_root=_read_typed(raw, "animation_asset_root", str, "", warnings),
            character_provider=_read_typed(raw, "character_provider", str, "auto", warnings),
            path_mappings=mappings,
        )
        _report_unknown(raw, "batch", warnings)
        return instance

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RenderSection:
    engine: str = "BLENDER_EEVEE"
    samples: int = 32
    resolution_x: int = 1280
    resolution_y: int = 720
    resolution_percentage: int = 100
    #: When False the sequence does not dictate a resolution: the renderer keeps
    #: whatever the scene it loads has (the historical behaviour).  Set it True to
    #: stamp ``resolution_x``/``y``/``percentage`` into the sequence, so a headless
    #: render reproduces the size that was chosen at generation time.
    resolution_explicit: bool = False
    fps: float = 24.0
    video_format: str = "mp4"
    codec: str = "H264"
    constant_rate_factor: str = "HIGH"
    input_root: str = ""
    output_root: str = ""
    recursive: bool = False
    overwrite: bool = False
    skip_existing: bool = True
    dry_run: bool = False
    workers: int = 1
    trajectory_mode: str = "all_frames"
    trajectory_step: int = 1
    keep_frames: bool = False
    scene_filter: list = field(default_factory=list)
    motion_filter: list = field(default_factory=list)
    sequence_filter: list = field(default_factory=list)
    log_level: str = "INFO"

    VIDEO_FORMATS = ("mp4", "mkv", "webm", "avi")
    TRAJECTORY_MODES = ("all_frames", "sampled")
    LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

    @classmethod
    def from_dict(cls, raw: dict, warnings: "list[str]"):
        raw = dict(raw or {})
        instance = cls(
            engine=_read_typed(raw, "engine", str, "BLENDER_EEVEE", warnings),
            samples=_read_typed(raw, "samples", int, 32, warnings),
            resolution_x=_read_typed(raw, "resolution_x", int, 1280, warnings),
            resolution_y=_read_typed(raw, "resolution_y", int, 720, warnings),
            resolution_percentage=_read_typed(raw, "resolution_percentage", int, 100, warnings),
            resolution_explicit=_read_typed(raw, "resolution_explicit", bool, False, warnings),
            fps=_read_typed(raw, "fps", float, 24.0, warnings),
            video_format=_read_choice(raw, "video_format", cls.VIDEO_FORMATS, "mp4", warnings),
            codec=_read_typed(raw, "codec", str, "H264", warnings),
            constant_rate_factor=_read_typed(raw, "constant_rate_factor", str, "HIGH", warnings),
            input_root=_read_typed(raw, "input_root", str, "", warnings),
            output_root=_read_typed(raw, "output_root", str, "", warnings),
            recursive=_read_typed(raw, "recursive", bool, False, warnings),
            overwrite=_read_typed(raw, "overwrite", bool, False, warnings),
            skip_existing=_read_typed(raw, "skip_existing", bool, True, warnings),
            dry_run=_read_typed(raw, "dry_run", bool, False, warnings),
            workers=_read_typed(raw, "workers", int, 1, warnings),
            trajectory_mode=_read_choice(raw, "trajectory_mode", cls.TRAJECTORY_MODES, "all_frames", warnings),
            trajectory_step=_read_typed(raw, "trajectory_step", int, 1, warnings),
            keep_frames=_read_typed(raw, "keep_frames", bool, False, warnings),
            scene_filter=_read_list(raw, "scene_filter", [], warnings),
            motion_filter=_read_list(raw, "motion_filter", [], warnings),
            sequence_filter=_read_list(raw, "sequence_filter", [], warnings),
            log_level=_read_choice(raw, "log_level", cls.LOG_LEVELS, "INFO", warnings),
        )
        _report_unknown(raw, "render", warnings)
        instance.validate()
        return instance

    def validate(self) -> None:
        if self.samples < 1:
            raise ConfigError("render.samples must be >= 1")
        if self.resolution_x < 1 or self.resolution_y < 1:
            raise ConfigError("render resolution must be >= 1")
        if not 1 <= self.resolution_percentage <= 100:
            raise ConfigError("render.resolution_percentage must be within [1, 100]")
        if self.fps <= 0:
            raise ConfigError("render.fps must be > 0")
        if self.workers < 1:
            raise ConfigError("render.workers must be >= 1")
        if self.trajectory_step < 1:
            raise ConfigError("render.trajectory_step must be >= 1")

    def to_dict(self) -> dict:
        return asdict(self)


def _section_from_dict(section_cls, raw: dict, base, warnings: "list[str]"):
    """Merge ``raw`` onto ``base`` (same section type) and rebuild it.

    Sections are flat dataclasses, so the cleanest way to honour a *partial*
    override is to serialise the existing section, apply the override on top and
    re-parse the result.  Without this, supplying only ``search.max_radius``
    would silently reset every other search setting to its default.
    """
    merged = asdict(base) if base is not None else {}
    merged.update(raw)
    return section_cls.from_dict(merged, warnings)


@dataclass
class CompositeSection:
    """Compound shots: base templates played one after another in one sequence.

    A compound keeps the **same total frame range** as a single template and only
    concatenates the order, so ``pan_right + hitchcock`` is still 0..80 frames: the
    range is split into windows, one per part, and each part starts where the
    previous one ended.

    * ``mode = "full"``: every ordering of every loaded template -- ``n!`` sequences.
    * ``mode = "partial"``: ``types_per_sequence`` distinct templates per sequence,
      ``sequence_count`` of them, drawn deterministically from the
      ``x! * C(n, x)`` possible orderings.
    * ``output_mode``: whether compounds are generated next to the base shots, on
      their own, or whether only the base shots are generated.
    """

    enabled: bool = False
    mode: str = "full"
    #: x -- how many distinct templates one compound sequence contains (2..10).
    types_per_sequence: int = 2
    #: How many distinct compounds a partial compound should produce.
    sequence_count: int = 12
    #: Seeded so a re-run (and ``--resume``) reproduces the same set.
    seed: int = 1234
    output_mode: str = "with_base"
    #: Safety rails: ``n!`` and ``x! * C(n, x)`` grow far too fast to generate
    #: blindly (80! is not a number of sequences anyone can render).
    max_full_sequences: int = 5040          # 7! -- enough for an 7-template set
    max_partial_sequences: int = 100000

    MAX_TYPES_PER_SEQUENCE = 10

    @classmethod
    def from_dict(cls, raw: dict, warnings: "list[str]"):
        raw = dict(raw or {})
        instance = cls(
            enabled=_read_typed(raw, "enabled", bool, False, warnings),
            mode=_read_choice(raw, "mode", ("full", "partial"), "full", warnings),
            types_per_sequence=_read_typed(raw, "types_per_sequence", int, 2, warnings),
            sequence_count=_read_typed(raw, "sequence_count", int, 12, warnings),
            seed=_read_typed(raw, "seed", int, 1234, warnings),
            output_mode=_read_choice(
                raw, "output_mode", ("with_base", "only_compound", "only_base"),
                "with_base", warnings,
            ),
            max_full_sequences=_read_typed(raw, "max_full_sequences", int, 5040, warnings),
            max_partial_sequences=_read_typed(
                raw, "max_partial_sequences", int, 100000, warnings
            ),
        )
        _report_unknown(raw, "composite", warnings)
        instance.validate()
        return instance

    def validate(self) -> None:
        if self.mode not in ("full", "partial"):
            raise ConfigError("composite.mode must be 'full' or 'partial'")
        if self.output_mode not in ("with_base", "only_compound", "only_base"):
            raise ConfigError(
                "composite.output_mode must be 'with_base', 'only_compound' or 'only_base'"
            )
        if not 2 <= int(self.types_per_sequence) <= self.MAX_TYPES_PER_SEQUENCE:
            raise ConfigError(
                f"composite.types_per_sequence must be between 2 and "
                f"{self.MAX_TYPES_PER_SEQUENCE}"
            )
        if int(self.sequence_count) < 1:
            raise ConfigError("composite.sequence_count must be >= 1")
        if int(self.max_full_sequences) < 1:
            raise ConfigError("composite.max_full_sequences must be >= 1")
        if int(self.max_partial_sequences) < 1:
            raise ConfigError("composite.max_partial_sequences must be >= 1")

    def want_base(self) -> bool:
        """Should the run generate the plain, single-template sequences?"""
        return not (self.enabled and self.output_mode == "only_compound")

    def want_compound(self) -> bool:
        """Should the run generate compound sequences?"""
        return bool(self.enabled) and self.output_mode != "only_base"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BatchConfig:
    """Whole-run configuration."""

    schema_version: int = SCHEMA_VERSION
    batch: BatchSection = field(default_factory=BatchSection)
    motion: MotionSection = field(default_factory=MotionSection)
    validation: ValidationSection = field(default_factory=ValidationSection)
    search: SearchSection = field(default_factory=SearchSection)
    render: RenderSection = field(default_factory=RenderSection)
    composite: CompositeSection = field(default_factory=CompositeSection)
    scenes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self, *, include_scenes: bool = True) -> dict:
        payload = {
            "schema_version": self.schema_version,
            "batch": self.batch.to_dict(),
            "motion": self.motion.to_dict(),
            "validation": self.validation.to_dict(),
            "search": self.search.to_dict(),
            "render": self.render.to_dict(),
            "composite": self.composite.to_dict(),
        }
        if include_scenes:
            payload["scenes"] = list(self.scenes)
        return payload

    def copy(self) -> "BatchConfig":
        clone = BatchConfig.from_dict(self.to_dict())
        clone.warnings = list(self.warnings)
        return clone

    @classmethod
    def from_dict(cls, raw: dict, *, base: "BatchConfig | None" = None):
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigError(f"configuration root must be an object, got {type(raw).__name__}")
        warnings: "list[str]" = []
        source = dict(raw)

        schema_version = _read_typed(source, "schema_version", int, SCHEMA_VERSION, warnings)
        if schema_version > SCHEMA_VERSION:
            warnings.append(
                f"schema_version {schema_version} is newer than supported {SCHEMA_VERSION}; "
                "unknown keys are ignored"
            )

        if base is None:
            instance = cls()
            section_base = cls()
        else:
            warnings.extend(base.warnings)
            instance = base.copy()
            section_base = base

        instance.schema_version = schema_version
        batch_raw = _sub_dict(source, "batch", warnings)
        motion_raw = _sub_dict(source, "motion", warnings)
        validation_raw = _sub_dict(source, "validation", warnings)
        search_raw = _sub_dict(source, "search", warnings)
        render_raw = _sub_dict(source, "render", warnings)
        composite_raw = _sub_dict(source, "composite", warnings)
        scenes_raw = _read_list(source, "scenes", instance.scenes, warnings)

        if batch_raw:
            instance.batch = _section_from_dict(BatchSection, batch_raw, section_base.batch, warnings)
        if motion_raw:
            instance.motion = _section_from_dict(MotionSection, motion_raw, section_base.motion, warnings)
        if validation_raw:
            instance.validation = _section_from_dict(
                ValidationSection, validation_raw, section_base.validation, warnings
            )
        if search_raw:
            instance.search = _section_from_dict(SearchSection, search_raw, section_base.search, warnings)
        if render_raw:
            instance.render = _section_from_dict(RenderSection, render_raw, section_base.render, warnings)
        if composite_raw:
            instance.composite = _section_from_dict(
                CompositeSection, composite_raw, section_base.composite, warnings
            )
        instance.scenes = scenes_raw
        _report_unknown(source, "config", warnings)
        # De-duplicate: re-parsing a section re-emits warnings from its base.
        instance.warnings = _dedupe(warnings)
        return instance


def config_from_dict(raw: dict, *, base: "BatchConfig | None" = None) -> BatchConfig:
    return BatchConfig.from_dict(raw, base=base)


def combine_frame_ranges(entries) -> dict:
    """Merge ``(start, end)`` pairs into a minimal set of disjoint ranges."""
    spans = []
    for start, end in entries:
        if end < start:
            start, end = end, start
        spans.append((int(start), int(end)))
    spans.sort()
    merged: "list[list[int]]" = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return {
        "ranges": [{"start": s, "end": e} for s, e in merged],
        "frame_start": merged[0][0] if merged else 0,
        "frame_end": merged[-1][1] if merged else 0,
        "total_frames": sum(e - s + 1 for s, e in merged),
    }


def validate_batch_config(config: BatchConfig, *, require_output: bool = True) -> "list[str]":
    """Return a list of problems (empty means the config is runnable)."""
    problems: "list[str]" = []
    if require_output and not config.batch.output_root.strip():
        problems.append("batch.output_root is empty; choose an output folder")
    if config.batch.mode not in CHARACTER_MODE_LABELS:
        problems.append(f"batch.mode {config.batch.mode!r} is invalid")
    try:
        config.motion.validate()
        config.validation.validate()
        config.search.validate()
        config.render.validate()
        config.motion.unit_scale.validate()
        config.composite.validate()
    except ConfigError as exc:
        problems.append(str(exc))
    for index, scene in enumerate(config.scenes):
        path = scene.get("path") if isinstance(scene, dict) else scene
        if not path:
            problems.append(f"scenes[{index}] has no path")
    return problems


def describe_config(config: BatchConfig) -> str:
    """Short human readable summary used by CLI ``--print-config``."""
    lines = [
        f"schema_version : {config.schema_version}",
        f"output_root    : {config.batch.output_root or '(unset)'}",
        f"character mode : {config.batch.mode} ({CHARACTER_MODE_LABELS.get(config.batch.mode, '?')})",
        f"templates      : {config.motion.template_path or '(embedded/default search)'}"
        f"  selected={config.motion.template_names or 'all'}",
        f"frame start    : {config.motion.frame_start}  scale={config.motion.frame_scale}"
        f"  interp={config.motion.interpolation}",
        f"template space : Blender (metres, +X right / +Y up / -Z forward, local degrees)"
        f"  fps={config.motion.unit_scale.fps} order={config.motion.unit_scale.rotation_order}",
        f"validation     : enabled={config.validation.enabled} step={config.validation.sample_step}"
        f" clearance={config.validation.clearance}",
        f"camera search  : enabled={config.search.enabled} r=[{config.search.min_radius}, {config.search.max_radius}]"
        f" candidates={config.search.candidate_count} seed={config.search.random_seed}",
        f"render         : {config.render.engine} {config.render.resolution_x}x{config.render.resolution_y}"
        f" @{config.render.fps}fps -> {config.render.video_format}",
        (
            "composite      : enabled"
            f" mode={config.composite.mode}"
            + (f" x={config.composite.types_per_sequence} count={config.composite.sequence_count}"
               if config.composite.mode == "partial" else "")
            + f" output={config.composite.output_mode} seed={config.composite.seed}"
            if config.composite.enabled else "composite      : off"
        ),
        f"scenes         : {len(config.scenes)}",
    ]
    for warning in config.warnings:
        lines.append(f"WARNING: {warning}")
    return "\n".join(lines)
