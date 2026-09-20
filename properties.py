"""Blender property groups backing the sidebar panel.

Flat, serialisable and intentionally close to the dataclasses in
``config.models``: :meth:`MPP_SceneProperties.to_config` and
:meth:`MPP_SceneProperties.from_config` are the only translation layer, so the
UI can never drift away from what the CLI accepts.

The 80-entry motion template list is *not* mirrored into RNA.  It is loaded on
demand into a plain Python list held in ``panel_state``, which keeps scene files
small and avoids rebuilding a huge collection property on every redraw.
"""

from __future__ import annotations

import os

from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import PropertyGroup

from .config.models import (
    CHARACTER_MODE_BOTH,
    CHARACTER_MODE_LABELS,
    CHARACTER_MODE_NONE,
    CHARACTER_MODE_WITH,
    BatchConfig,
    ConfigError,
)
from .io.path_utils import normalize_path, to_forward_slashes
from .utils.logging_utils import get_logger

LOGGER = get_logger("properties")

#: Defaults of the panel-only fields, for the "is anything configured?" check.
_PANEL_DEFAULTS = {
    "camera_selection": "all",
    "recursive_scan": False,
    "missing_only": False,
}

#: Output sizes the panel offers.  The label spells out the pixels, so "1K" never
#: means two different things to two people; ``scene`` keeps the historical
#: behaviour of following whatever resolution the source ``.blend`` has, and
#: ``custom`` shows a size that arrived from a config file or the CLI.
RESOLUTION_PRESETS = (
    ("720p", "720p  (1280 x 720)", "HD ready - the pipeline default"),
    ("1080p", "1080p  (1920 x 1080)", "Full HD"),
    ("1k", "1K square  (1024 x 1024)", "Square 1K"),
    ("2k", "2K  (2048 x 1080)", "DCI 2K"),
    ("4k", "4K  (3840 x 2160)", "Ultra HD"),
    ("scene", "Follow the source scene", "Render at whatever the source .blend uses"),
    ("custom", "Custom size (from a config file)", "Set outside the panel"),
)

#: Preset identifier -> ``(width, height)``.
RESOLUTION_SIZES = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1k": (1024, 1024),
    "2k": (2048, 1080),
    "4k": (3840, 2160),
}

#: RNA stores floats in single precision, so ``0.2`` reads back as
#: ``0.20000000298023224``.  Comparing defaults needs a tolerance.
_FLOAT_TOLERANCE = 1e-6


def _same_value(left, right) -> bool:
    """Equality that tolerates single-precision float noise."""
    if isinstance(left, bool) or isinstance(right, bool):
        return bool(left) == bool(right)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= _FLOAT_TOLERANCE * max(
            1.0, abs(float(left)), abs(float(right))
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _same_value(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(
            _same_value(left[key], right[key]) for key in left
        )
    return left == right

#: Status values shown per row of the scene list.
SCENE_STATUS_ITEMS = (
    ("pending", "Pending", "Queued, not inspected yet"),
    ("ok", "Ready", "File exists and a camera was found"),
    ("missing", "Missing", "The .blend file is not on disk"),
    ("no_camera", "No camera", "The file contains no camera"),
    ("load_failed", "Load failed", "Blender could not open the file"),
    ("generated", "Generated", "Sequences were produced"),
    ("failed", "Failed", "Generation reported an error"),
)

#: Task lifecycle shown in the panel.
TASK_STATES = (
    ("idle", "Idle", "Nothing is running"),
    ("preparing", "Preparing", "Loading configuration and templates"),
    ("running", "Running", "Generating sequences"),
    ("cancelling", "Cancelling", "Waiting for the current sequence to stop"),
    ("done", "Done", "The last run finished"),
    ("failed", "Failed", "The last run reported errors"),
    ("cancelled", "Cancelled", "The last run was cancelled"),
)

TRAJECTORY_MODES = (
    ("all_frames", "All frames", "Export a trajectory row for every frame"),
    ("sampled", "Sampled", "Export every N-th frame"),
)

#: Per-sequence render states shown in the render list.
RENDER_STATES = (
    ("pending", "Pending", "Queued for rendering"),
    ("running", "Rendering", "Blender is rendering this sequence now"),
    ("done", "Done", "Video, JSON and trajectory written"),
    ("skipped", "Skipped", "A video already exists"),
    ("failed", "Failed", "Rendering reported an error"),
)

#: Render engines offered in the panel.  The renderer resolves aliases, so a
#: choice that this Blender build does not know still falls back safely.
RENDER_ENGINES = (
    ("BLENDER_EEVEE", "EEVEE", "Fast rasteriser; the default"),
    ("CYCLES", "Cycles", "Physically based; needs a device setting"),
    ("BLENDER_WORKBENCH", "Workbench", "Viewport-style solid shading"),
)


class MPP_RenderItem(PropertyGroup):
    """One sequence row in the local-render list."""

    sequence_dir: StringProperty(
        name="Sequence folder",
        description="Folder containing sequence_config.json",
        default="",
    )
    sequence_id: StringProperty(name="Sequence", default="")
    scene_name: StringProperty(name="Scene", default="")
    motion_name: StringProperty(name="Motion", default="")
    blend: StringProperty(name="Blend", default="")
    #: ``blend`` (self-contained scene copy) or ``animation`` (the renderer replays
    #: the stored camera animation onto the source scene).
    storage_mode: StringProperty(name="Storage", default="blend")
    state: EnumProperty(name="State", items=RENDER_STATES, default="pending")
    detail: StringProperty(name="Detail", default="")

    def label(self) -> str:
        parts = [self.scene_name, self.motion_name, self.sequence_id]
        text = " / ".join(part for part in parts if part)
        return text or os.path.basename(self.sequence_dir or "") or "(unnamed)"

    def renderable(self) -> bool:
        """A blend on disk, or an animation-only sequence that has a source scene."""
        if self.blend and os.path.isfile(self.blend):
            return True
        return self.storage_mode == "animation"


class MPP_SceneListItem(PropertyGroup):
    """One queued ``.blend`` file."""

    path: StringProperty(
        name="Path",
        description="Absolute path to the .blend file",
        default="",
        subtype="FILE_PATH",
    )
    enabled: BoolProperty(name="Enabled", default=True)
    status: EnumProperty(name="Status", items=SCENE_STATUS_ITEMS, default="pending")
    note: StringProperty(name="Note", default="")
    camera_count: IntProperty(name="Cameras", default=0, min=0)
    detail: StringProperty(name="Detail", default="")

    def label(self) -> str:
        return os.path.basename(self.path) or "(unnamed)"

    def directory(self) -> str:
        return os.path.dirname(self.path) if self.path else ""


class MPP_SceneProperties(PropertyGroup):
    """Everything the Motion Pipeline panel edits."""

    # -- scene list ------------------------------------------------------
    scene_list: CollectionProperty(type=MPP_SceneListItem)
    scene_list_index: IntProperty(name="Selected scene", default=-1, min=-1)
    #: Deliberately a plain string, not FILE_PATH: RNA validates that subtype
    #: before the operator runs, which would make "this file is missing" (a case
    #: the brief explicitly asks us to report) impossible to handle in Python.
    file_path: StringProperty(
        name="Scene file",
        description="Pick a .blend file, then press Add file",
        default="",
    )
    directory: StringProperty(
        name="Folder",
        description="Scan this folder for .blend files",
        default="",
        subtype="DIR_PATH",
    )
    recursive_scan: BoolProperty(
        name="Recursive",
        description="Also scan sub-folders",
        default=False,
    )
    #: Plain string for the same reason as ``file_path``: loading a stale list
    #: must produce a message, not an RNA error.
    scene_list_file: StringProperty(
        name="Scene list file",
        description="Where to save/load the scene list",
        default="",
    )
    missing_only: BoolProperty(
        name="Missing only",
        description="Show only scenes whose file is missing",
        default=False,
    )

    # -- character -------------------------------------------------------
    character_mode: EnumProperty(
        name="Character mode",
        description="How the character dimension of the sequence matrix is handled",
        items=(
            (CHARACTER_MODE_NONE, CHARACTER_MODE_LABELS[CHARACTER_MODE_NONE],
             "Generate one character-free sequence per camera/motion"),
            (CHARACTER_MODE_WITH, CHARACTER_MODE_LABELS[CHARACTER_MODE_WITH],
             "Only generate sequences that contain a character"),
            (CHARACTER_MODE_BOTH, CHARACTER_MODE_LABELS[CHARACTER_MODE_BOTH],
             "Generate both a character-free and a with-character sequence"),
        ),
        default=CHARACTER_MODE_NONE,
    )
    character_asset_root: StringProperty(
        name="Character library",
        description="Folder (or manifest.json) describing Blender character assets",
        default="",
        subtype="DIR_PATH",
    )
    animation_asset_root: StringProperty(
        name="Animation library",
        description="Folder containing the animation clips for those characters",
        default="",
        subtype="DIR_PATH",
    )
    character_provider: EnumProperty(
        name="Provider",
        description="Which character adapter to use",
        items=(
            ("auto", "Automatic", "Use the Blender adapter when a library is configured"),
            ("blender", "Blender .blend library", "Append characters from .blend files"),
            ("null", "None (characters unavailable)", "Keep the interface but generate no characters"),
            ("unreal_metahuman", "Unreal MetaHuman (not supported)", "Documents the platform boundary"),
        ),
        default="auto",
    )

    # -- motion templates ------------------------------------------------
    template_path: StringProperty(
        name="Motion templates",
        description="Camera motion template JSON (leave empty to auto-discover)",
        default="",
        subtype="FILE_PATH",
    )
    motion_names: StringProperty(
        name="Motion filter",
        description="Comma separated template ids, or a glob such as dolly_*, pan_* (empty = all)",
        default="",
    )
    motion_count: IntProperty(name="Templates loaded", default=0, min=0)
    template_source: StringProperty(name="Template source", default="")
    frame_start: IntProperty(
        name="First frame",
        description="Scene frame the first template key is mapped to",
        default=0, min=0,
    )
    fps: FloatProperty(name="FPS", default=24.0, min=1.0, max=240.0)
    interpolation: EnumProperty(
        name="Interpolation",
        items=(
            ("BEZIER", "Bezier", "Smooth ease in/out between keyframes"),
            ("LINEAR", "Linear", "Constant speed between keyframes"),
            ("CONSTANT", "Constant", "Hold each keyframe (stepped)"),
        ),
        default="BEZIER",
    )

    # -- camera validation ----------------------------------------------
    validation_enabled: BoolProperty(
        name="Validate cameras",
        description="Check clipping, occlusion, framing and jumps before generating",
        default=True,
    )
    validation_sample_step: IntProperty(
        name="Sample step",
        description="Validate every N-th frame (first and last are always checked)",
        default=10, min=1, max=1000,
    )
    clearance: FloatProperty(
        name="Minimum clearance (m)",
        description="How far the camera body must stay from geometry",
        default=0.25, min=0.0, max=50.0, precision=3,
    )
    obstruction_distance: FloatProperty(
        name="Blocked-shot distance (m)",
        description="A surface closer than this in front of the lens counts as a blocked shot",
        default=1.0, min=0.0, max=100.0, precision=3,
    )
    max_position_jump: FloatProperty(
        name="Max move per frame (m)",
        description="Reject camera moves faster than this between two frames",
        default=2.0, min=0.0, max=1000.0, precision=3,
    )
    max_rotation_jump_deg: FloatProperty(
        name="Max turn per frame (deg)",
        description="Reject camera turns faster than this between two frames",
        default=45.0, min=0.0, max=360.0,
    )
    check_character_visibility: BoolProperty(
        name="Check character visibility",
        description="Reject shots where the character is hidden or out of frame",
        default=True,
    )
    min_character_visible_ratio: FloatProperty(
        name="Min visible fraction",
        description="Share of the character that must stay visible",
        default=0.05, min=0.0, max=1.0, precision=3,
    )
    check_character_overlap: BoolProperty(
        name="Check character overlap",
        description="Reject placements where the character is inside scene geometry",
        default=True,
    )
    save_validation_report: BoolProperty(
        name="Save validation report",
        description="Write validation_report.json next to each sequence",
        default=True,
    )

    # -- spherical search ------------------------------------------------
    search_enabled: BoolProperty(
        name="Auto-adjust camera",
        description="Search a sphere around the camera when validation fails",
        default=True,
    )
    search_min_radius: FloatProperty(
        name="Min offset radius (m)",
        description="Smallest distance to try from the original camera position",
        default=0.2, min=0.0, max=1000.0, precision=3,
    )
    search_max_radius: FloatProperty(
        name="Max offset radius (m)",
        description="Largest distance to try from the original camera position",
        default=3.0, min=0.0, max=1000.0, precision=3,
    )
    search_candidate_count: IntProperty(
        name="Candidate positions",
        description="How many candidate positions to generate",
        default=64, min=1, max=20000,
    )
    search_azimuth_samples: IntProperty(
        name="Horizontal samples",
        description="How many positions around the horizon",
        default=12, min=1, max=360,
    )
    search_elevation_samples: IntProperty(
        name="Vertical samples",
        description="How many elevation bands",
        default=5, min=1, max=180,
    )
    search_shell_only: BoolProperty(
        name="Shell only",
        description="Search only the outer sphere instead of the whole volume",
        default=False,
    )
    search_max_retries: IntProperty(
        name="Max retries",
        description="How many search passes to allow",
        default=2, min=0, max=50,
    )
    search_random_seed: IntProperty(
        name="Random seed",
        description="Makes the search reproducible",
        default=1234,
    )
    search_allow_rotation: BoolProperty(
        name="Allow rotation adjustment",
        description="Let the search turn the camera slightly to keep the subject framed",
        default=True,
    )
    search_max_rotation_deg: FloatProperty(
        name="Max rotation change (deg)",
        description="Upper bound on how far the search may turn the camera",
        default=25.0, min=0.0, max=180.0,
    )
    search_allow_focal: BoolProperty(
        name="Allow focal adjustment",
        description="Let the search change the focal length slightly",
        default=True,
    )
    search_focal_steps: FloatProperty(
        name="Focal step (%)",
        description="How much the focal length may change per step",
        default=3.0, min=0.0, max=50.0,
    )
    search_max_output: IntProperty(
        name="Max accepted positions",
        description="Stop after accepting this many adjusted cameras",
        default=1, min=0, max=100,
    )

    # -- output ----------------------------------------------------------
    output_root: StringProperty(
        name="Project folder",
        description=(
            "Folder the project is written into. Generation creates "
            "blender_camera_<date>/ inside it, holding sequence/ (the sequence tree), "
            "scene/ (a copy of every source .blend) and video/ (render output), plus "
            "the headless render toolkit"
        ),
        default="",
        subtype="DIR_PATH",
    )
    overwrite: BoolProperty(
        name="Overwrite existing",
        description="Regenerate sequences even when their files already exist",
        default=False,
    )
    resume: BoolProperty(
        name="Reuse existing sequences",
        description="Skip a combination whose artifacts are already complete",
        default=True,
    )
    verbose_logging: BoolProperty(
        name="Verbose logging",
        description="Write a detailed generation_log.txt per sequence",
        default=True,
    )
    camera_selection: StringProperty(
        name="Cameras",
        description="'all', comma separated camera names, or comma separated indices",
        default="all",
    )

    # -- render defaults (recorded for the headless renderer) -------------
    sequence_resolution: EnumProperty(
        name="Sequence resolution",
        description=(
            "Output size recorded in every sequence, so a headless render uses it "
            "instead of the source scene's own render resolution"
        ),
        items=RESOLUTION_PRESETS,
        default="720p",
    )
    #: The numbers the preset resolves to (and where a config file's custom size
    #: lands).  Kept on the group so the panel, a saved config and the CLI all
    #: agree on one representation; the enum above is the user-facing facet.
    sequence_res_x: IntProperty(name="Width", default=1280, min=16, max=16384)
    sequence_res_y: IntProperty(name="Height", default=720, min=16, max=16384)
    sequence_res_percentage: IntProperty(name="Resolution %", default=100, min=1, max=100)
    render_engine: EnumProperty(
        name="Engine",
        description=(
            "Render engine recorded in sequence_config.json, so a headless render "
            "reproduces it. Pick from the list; the identifier is written to the file"
        ),
        items=RENDER_ENGINES,
        default="BLENDER_EEVEE",
    )
    render_samples: IntProperty(name="Samples", default=32, min=1, max=100000)
    render_fps: FloatProperty(name="Render FPS", default=24.0, min=1.0, max=240.0)
    video_format: EnumProperty(
        name="Video format",
        items=(
            ("mp4", "MP4", "H.264 in an MP4 container"),
            ("mkv", "Matroska", "H.264 in Matroska"),
            ("webm", "WebM", "VP9/AV1 in WebM"),
            ("avi", "AVI", "Legacy AVI container"),
        ),
        default="mp4",
    )
    trajectory_mode: EnumProperty(name="Trajectory", items=TRAJECTORY_MODES, default="all_frames")
    trajectory_step: IntProperty(name="Trajectory step", default=1, min=1, max=1000)

    # -- compound shots (澶嶅悎杩愰暅) ----------------------------------------
    compound_enabled: BoolProperty(
        name="Compound shots",
        description=(
            "Also generate compound sequences: several base camera moves played one "
            "after another inside the same total frame range (0-80 frames), each part "
            "starting where the previous one ended"
        ),
        default=False,
    )
    compound_mode: EnumProperty(
        name="Compound type",
        description="How the base templates are combined",
        items=(
            ("full", "Full compound",
             "Every ordering of every loaded template: n! sequences"),
            ("partial", "Partial compound",
             "Sequences of x distinct templates, a chosen number of them, drawn from "
             "the x! * C(n, x) possible orderings"),
        ),
        default="full",
    )
    compound_types: IntProperty(
        name="Templates per sequence",
        description="x: how many distinct base templates one compound sequence contains",
        default=2, min=2, max=10,
    )
    compound_count: IntProperty(
        name="Sequence count",
        description=(
            "How many distinct compounds a partial compound generates (at most "
            "x! * C(n, x); random but seeded, so a re-run reproduces the same set)"
        ),
        default=12, min=1, max=100000,
    )
    compound_seed: IntProperty(
        name="Random seed",
        description="Seed for the partial compound draw, so runs are reproducible",
        default=1234, min=0,
    )
    compound_output: EnumProperty(
        name="Compound output",
        description="What the run writes",
        items=(
            ("with_base", "With base shots",
             "Generate the compound sequences together with the single-template ones"),
            ("only_compound", "Compound shots only",
             "Generate only the compound sequences"),
            ("only_base", "Base shots only",
             "Ignore the compound configuration and generate only the single-template ones"),
        ),
        default="with_base",
    )

    # -- local rendering --------------------------------------------------
    render_list: CollectionProperty(type=MPP_RenderItem)
    render_list_index: IntProperty(name="Selected sequence", default=-1, min=-1)
    render_input_root: StringProperty(
        name="Sequence root",
        description="Folder containing generated sequences (scene/motion/sequence)",
        default="",
        subtype="DIR_PATH",
    )
    render_sequence_dir: StringProperty(
        name="Sequence folder",
        description="Render this one sequence folder",
        default="",
        subtype="DIR_PATH",
    )
    render_recursive: BoolProperty(
        name="Recursive",
        description="Scan sub-folders of the sequence root",
        default=True,
    )
    render_output_root: StringProperty(
        name="Save to",
        description="Where the videos, JSON and trajectory files are written",
        default="",
        subtype="DIR_PATH",
    )
    render_flat: BoolProperty(
        name="Flat output",
        description="Write every sequence directly into the save folder "
                    "instead of the scene/motion/sequence tree",
        default=False,
    )
    render_overwrite: BoolProperty(
        name="Overwrite existing",
        description="Re-render even when a video already exists",
        default=False,
    )
    render_engine_choice: EnumProperty(
        name="Engine",
        description="Render engine for the local render",
        items=RENDER_ENGINES,
        default="BLENDER_EEVEE",
    )
    render_override_resolution: BoolProperty(
        name="Override resolution",
        description="Use the panel resolution instead of the sequence's own setting",
        default=False,
    )
    render_res_x: IntProperty(name="X", default=1280, min=16, max=16384)
    render_res_y: IntProperty(name="Y", default=720, min=16, max=16384)
    render_override_fps: BoolProperty(
        name="Override FPS",
        description="Use the panel frame rate instead of the sequence's own setting",
        default=False,
    )
    render_fps_choice: FloatProperty(name="FPS", default=24.0, min=1.0, max=240.0)
    render_samples_override: BoolProperty(
        name="Override samples",
        description="Use the panel sample count instead of the engine default",
        default=False,
    )
    render_samples_choice: IntProperty(name="Samples", default=32, min=1, max=100000)
    render_device: EnumProperty(
        name="Device",
        description="Cycles compute device (ignored by other engines)",
        # NOTE: the identifier must be non-empty -- Blender rejects "" here and
        # logs "current value '0' matches no enum in 'MPP_SceneProperties'".
        items=(
            ("DEFAULT", "Default", "Leave the engine's own setting alone"),
            ("CPU", "CPU", "Render on the processor"),
            ("GPU", "GPU", "Render on the graphics device"),
        ),
        default="DEFAULT",
    )
    render_codec: EnumProperty(
        name="Codec",
        items=(
            ("H264", "H.264", "Widely compatible"),
            ("H265", "H.265 / HEVC", "Smaller files"),
            ("AV1", "AV1", "Newest, needs a recent ffmpeg"),
            ("PRORES", "ProRes", "Editing-friendly intermediate"),
            ("MPEG4", "MPEG-4", "Legacy"),
        ),
        default="H264",
    )
    render_crf: EnumProperty(
        name="Quality",
        items=(
            ("LOSSLESS", "Lossless", "Largest files"),
            ("PERC_LOSSLESS", "Perceptually lossless", "Very high quality"),
            ("HIGH", "High", "Recommended default"),
            ("MEDIUM", "Medium", "Balanced"),
            ("LOW", "Low", "Smaller files"),
            ("VERYLOW", "Very low", "Smallest files"),
        ),
        default="HIGH",
    )
    render_dry_run: BoolProperty(
        name="Check only",
        description="Resolve inputs and outputs without rendering",
        default=False,
    )
    render_write_png: BoolProperty(
        name="Also write PNG frames",
        description="Keep a PNG sequence beside the video (needs ffmpeg on PATH)",
        default=False,
    )
    render_keep_png: BoolProperty(
        name="Keep PNG frames",
        description="Do not delete the PNG sequence after encoding",
        default=False,
    )
    render_status: StringProperty(name="Render status", default="")
    render_progress: FloatProperty(name="Render progress", default=0.0, min=0.0, max=1.0)
    render_current: StringProperty(name="Rendering", default="")
    render_log_path: StringProperty(name="Render log", default="")

    # -- remembered settings (read-only for the user) ---------------------
    settings_path: StringProperty(name="Settings file", default="")
    settings_saved_utc: StringProperty(name="Saved", default="")

    # -- live status (read-only for the user) ----------------------------
    task_state: EnumProperty(name="Task", items=TASK_STATES, default="idle")
    progress_text: StringProperty(name="Progress", default="")
    progress_fraction: FloatProperty(name="Progress", default=0.0, min=0.0, max=1.0)
    last_report: StringProperty(name="Last report", default="")
    last_output_root: StringProperty(name="Last output", default="")
    last_project_folder: StringProperty(
        name="Last project folder",
        description="The dated project folder the last run wrote into",
        default="",
    )
    generated_count: IntProperty(name="Generated", default=0, min=0)
    failed_count: IntProperty(name="Failed", default=0, min=0)
    skipped_count: IntProperty(name="Skipped", default=0, min=0)
    character_status: StringProperty(name="Character status", default="")

    # ------------------------------------------------------------------
    # translation to / from the CLI config
    # ------------------------------------------------------------------
    def to_config(self) -> BatchConfig:
        """Build a :class:`BatchConfig` from the panel values."""
        from .config.defaults import default_config

        config = default_config()
        config.batch.output_root = normalize_path(self.output_root) if self.output_root else ""
        config.batch.mode = self.character_mode
        config.batch.overwrite = bool(self.overwrite)
        config.batch.resume = bool(self.resume)
        config.batch.save_validation_report = bool(self.save_validation_report)
        config.batch.verbose = bool(self.verbose_logging)
        config.composite.enabled = bool(self.compound_enabled)
        config.composite.mode = self.compound_mode
        config.composite.types_per_sequence = int(self.compound_types)
        config.composite.sequence_count = int(self.compound_count)
        config.composite.seed = int(self.compound_seed)
        config.composite.output_mode = self.compound_output
        config.batch.character_asset_root = (
            normalize_path(self.character_asset_root) if self.character_asset_root else ""
        )
        config.batch.animation_asset_root = (
            normalize_path(self.animation_asset_root) if self.animation_asset_root else ""
        )
        config.batch.character_provider = self.character_provider

        config.motion.template_path = normalize_path(self.template_path) if self.template_path else ""
        config.motion.template_names = parse_motion_filter(self.motion_names)
        config.motion.frame_start = int(self.frame_start)
        config.motion.interpolation = self.interpolation
        config.motion.unit_scale.fps = float(self.fps)

        config.validation.enabled = bool(self.validation_enabled)
        config.validation.sample_step = max(1, int(self.validation_sample_step))
        config.validation.clearance = float(self.clearance)
        config.validation.obstruction_distance = float(self.obstruction_distance)
        config.validation.max_position_jump = float(self.max_position_jump)
        config.validation.max_rotation_jump_deg = float(self.max_rotation_jump_deg)
        config.validation.check_character_visibility = bool(self.check_character_visibility)
        config.validation.min_character_visible_ratio = float(self.min_character_visible_ratio)
        config.validation.check_character_overlap = bool(self.check_character_overlap)

        config.search.enabled = bool(self.search_enabled)
        low = float(self.search_min_radius)
        high = float(self.search_max_radius)
        if high < low:                       # keep the model valid even mid-edit
            low, high = high, low
        config.search.min_radius = low
        config.search.max_radius = high
        config.search.candidate_count = max(1, int(self.search_candidate_count))
        config.search.azimuth_samples = max(1, int(self.search_azimuth_samples))
        config.search.elevation_samples = max(1, int(self.search_elevation_samples))
        config.search.shell_only = bool(self.search_shell_only)
        config.search.max_retries = max(0, int(self.search_max_retries))
        config.search.random_seed = int(self.search_random_seed)
        config.search.allow_rotation_adjust = bool(self.search_allow_rotation)
        config.search.max_rotation_adjust_deg = float(self.search_max_rotation_deg)
        config.search.allow_focal_adjust = bool(self.search_allow_focal)
        config.search.focal_adjust_steps = float(self.search_focal_steps)
        config.search.max_output_candidates = max(0, int(self.search_max_output))

        config.render.engine = self.render_engine
        config.render.samples = max(1, int(self.render_samples))
        config.render.fps = float(self.render_fps)
        config.render.video_format = self.video_format
        config.render.trajectory_mode = self.trajectory_mode
        config.render.trajectory_step = max(1, int(self.trajectory_step))
        config.render.resolution_explicit = self._resolution_explicit()
        width, height, percentage = self._resolution_numbers()
        config.render.resolution_x = width
        config.render.resolution_y = height
        config.render.resolution_percentage = percentage
        config.render.output_root = config.batch.output_root
        return config

    def _resolution_explicit(self) -> bool:
        """Does this sequence dictate its output size?"""
        return self.sequence_resolution not in ("scene",)

    def _resolution_numbers(self) -> "tuple[int, int, int]":
        """``(width, height, percentage)`` the current preset stands for."""
        preset = self.sequence_resolution
        size = RESOLUTION_SIZES.get(preset)
        if size is None:
            # ``scene`` and ``custom`` keep whatever numbers are on the group.
            return (
                max(16, int(self.sequence_res_x)),
                max(16, int(self.sequence_res_y)),
                max(1, min(100, int(self.sequence_res_percentage))),
            )
        return size[0], size[1], 100

    def resolution_summary(self) -> str:
        """One line for the panel saying what this sequence will render at."""
        if self.sequence_resolution == "scene":
            return "Sequences follow the source scene's own resolution."
        width, height, percentage = self._resolution_numbers()
        if percentage != 100:
            return f"Sequences record {width} x {height} at {percentage}% ({int(round(width * percentage / 100))} x {int(round(height * percentage / 100))})."
        return f"Sequences record {width} x {height}."

    # -- project folder ---------------------------------------------------
    def project_folder(self) -> str:
        """The dated project folder this configuration writes into ("" when unset)."""
        from .core.project import project_folder_name

        if not self.output_root:
            return ""
        return os.path.join(normalize_path(self.output_root), project_folder_name())

    def project_summary(self) -> str:
        """Multi-line preview of what generation is about to create."""
        if not self.output_root:
            return "No project folder is set yet: pick the folder the project is written into."
        root = self.project_folder()
        return "\n".join([
            f"Project folder: {to_forward_slashes(root)}",
            "  sequence/  the sequence tree the renderer reads",
            "  scene/     a copy of every source .blend (what the sequences replay onto)",
            "  video/     render output",
            "  render_sequences.py + the package: render this folder on any machine",
        ])

    # -- compound shots ---------------------------------------------------
    def compound_counts(self) -> "tuple[int, int]":
        """``(planned, space)`` compound sequences for the current settings.

        ``space`` is how many distinct compounds exist (``n!`` for a full compound,
        ``x! * C(n, x)`` for a partial one) and ``planned`` how many will be
        generated, so the panel can show "12 of 90" before a run starts.
        """
        from .camera import motion_composite as mc
        from .config.models import CompositeSection

        total = int(self.motion_count)
        if total < 2:
            return 0, 0
        if self.compound_mode == "full":
            space = mc.factorial(total)
            return (0, space) if space > CompositeSection().max_full_sequences else (space, space)
        x = int(self.compound_types)
        if x > total or x < 2:
            return 0, 0
        space = mc.ordered_count(total, x)
        return min(int(self.compound_count), space), space

    def compound_ok(self) -> bool:
        """Can this configuration actually run?"""
        if not self.compound_enabled:
            return True
        planned, space = self.compound_counts()
        return planned > 0 and space > 0

    def composite_summary(self) -> str:
        """Multi-line description of the compound plan (panel label)."""
        from .camera import motion_composite as mc
        from .config.models import CompositeSection

        if not self.compound_enabled:
            return "Compound shots are off: one sequence per template."
        output = {
            "with_base": "together with the base shots",
            "only_compound": "compound shots only",
            "only_base": "base shots only -- nothing compound will be written",
        }.get(self.compound_output, self.compound_output)
        total = int(self.motion_count)
        if total < 2:
            return (f"Compound shots need at least 2 loaded templates (currently {total}).\n"
                    f"Load motion templates, or widen the Motion filter. Output: {output}.")
        if self.compound_mode == "full":
            count = mc.factorial(total)
            limit = CompositeSection().max_full_sequences
            if count > limit:
                return (f"Full compound of {total} templates = {count} sequences (n!), above the "
                        f"{limit} limit.\nNarrow the template set with the Motion filter, or switch "
                        f"to Partial compound.")
            return (f"Full compound: {count} sequence(s) = {total}!\n"
                    f"Each part plays in its own window of the same total frame range a "
                    f"single template uses. Output: {output}.")
        x = int(self.compound_types)
        if x > total:
            return (f"Partial compound needs at least {x} templates, but only {total} are loaded.\n"
                    f"Widen the Motion filter or lower Templates per sequence.")
        space = mc.ordered_count(total, x)
        planned = min(int(self.compound_count), space)
        extra = "" if planned == int(self.compound_count) else f" (capped from {int(self.compound_count)})"
        return (f"Partial compound: {planned}{extra} of {space} distinct {x}-template ordering(s)\n"
                f"= {x}! x C({total},{x}), drawn with seed {self.compound_seed}. "
                f"Output: {output}.")

    def from_config(self, config: BatchConfig) -> None:
        """Push a :class:`BatchConfig` into the panel fields."""
        self.output_root = config.batch.output_root
        self.character_mode = config.batch.mode
        self.overwrite = bool(config.batch.overwrite)
        self.resume = bool(config.batch.resume)
        self.save_validation_report = bool(config.batch.save_validation_report)
        self.verbose_logging = bool(config.batch.verbose)
        self.compound_enabled = bool(config.composite.enabled)
        self.compound_mode = config.composite.mode
        self.compound_types = int(config.composite.types_per_sequence)
        self.compound_count = int(config.composite.sequence_count)
        self.compound_seed = int(config.composite.seed)
        self.compound_output = config.composite.output_mode
        self.character_asset_root = config.batch.character_asset_root
        self.animation_asset_root = config.batch.animation_asset_root
        if config.batch.character_provider in ("auto", "blender", "null", "unreal_metahuman"):
            self.character_provider = config.batch.character_provider

        self.template_path = config.motion.template_path
        self.motion_names = ", ".join(config.motion.template_names)
        self.frame_start = int(config.motion.frame_start)
        self.interpolation = config.motion.interpolation
        self.fps = float(config.motion.unit_scale.fps)

        self.validation_enabled = bool(config.validation.enabled)
        self.validation_sample_step = int(config.validation.sample_step)
        self.clearance = float(config.validation.clearance)
        self.obstruction_distance = float(config.validation.obstruction_distance)
        self.max_position_jump = float(config.validation.max_position_jump)
        self.max_rotation_jump_deg = float(config.validation.max_rotation_jump_deg)
        self.check_character_visibility = bool(config.validation.check_character_visibility)
        self.min_character_visible_ratio = float(config.validation.min_character_visible_ratio)
        self.check_character_overlap = bool(config.validation.check_character_overlap)

        self.search_enabled = bool(config.search.enabled)
        self.search_min_radius = float(config.search.min_radius)
        self.search_max_radius = float(config.search.max_radius)
        self.search_candidate_count = int(config.search.candidate_count)
        self.search_azimuth_samples = int(config.search.azimuth_samples)
        self.search_elevation_samples = int(config.search.elevation_samples)
        self.search_shell_only = bool(config.search.shell_only)
        self.search_max_retries = int(config.search.max_retries)
        self.search_random_seed = int(config.search.random_seed)
        self.search_allow_rotation = bool(config.search.allow_rotation_adjust)
        self.search_max_rotation_deg = float(config.search.max_rotation_adjust_deg)
        self.search_allow_focal = bool(config.search.allow_focal_adjust)
        self.search_focal_steps = float(config.search.focal_adjust_steps)
        self.search_max_output = int(config.search.max_output_candidates)

        # The dropdown only knows the three engines it lists; a config file naming
        # anything else keeps the current choice instead of erroring on an enum.
        if config.render.engine in {identifier for identifier, _label, _tip in RENDER_ENGINES}:
            self.render_engine = config.render.engine
        self.render_samples = int(config.render.samples)
        self.render_fps = float(config.render.fps)
        self.video_format = config.render.video_format
        self.trajectory_mode = config.render.trajectory_mode
        self.trajectory_step = int(config.render.trajectory_step)
        self.sequence_res_x = int(config.render.resolution_x)
        self.sequence_res_y = int(config.render.resolution_y)
        self.sequence_res_percentage = int(config.render.resolution_percentage)
        self.sequence_resolution = self._preset_for(
            bool(config.render.resolution_explicit),
            self.sequence_res_x,
            self.sequence_res_y,
            self.sequence_res_percentage,
        )

    @staticmethod
    def _preset_for(explicit: bool, width: int, height: int, percentage: int) -> str:
        """Which dropdown entry matches a config's resolution.

        A size that matches no preset (a config file or ``--resolution`` wrote it)
        lands on ``custom``: the panel must not silently rewrite the user's numbers
        just because they did not come from the dropdown.
        """
        if not explicit:
            return "scene"
        for name, size in RESOLUTION_SIZES.items():
            if (int(width), int(height)) == size and int(percentage) == 100:
                return name
        return "custom"

    # -- helpers ---------------------------------------------------------
    #: Panel-only fields (not part of BatchConfig) that are still "settings".
    PANEL_FIELDS = (
        "camera_selection",
        "directory",
        "file_path",
        "recursive_scan",
        "scene_list_file",
        "missing_only",
        # The dropdown selection itself: the numbers travel through BatchConfig, but
        # "custom" vs "scene" is only expressible here.
        "sequence_resolution",
    )
    #: Local-render fields worth remembering between runs.
    RENDER_FIELDS = (
        "render_input_root",
        "render_sequence_dir",
        "render_recursive",
        "render_output_root",
        "render_flat",
        "render_overwrite",
        "render_engine_choice",
        "render_override_resolution",
        "render_res_x",
        "render_res_y",
        "render_override_fps",
        "render_fps_choice",
        "render_samples_override",
        "render_samples_choice",
        "render_device",
        "render_codec",
        "render_crf",
        "render_dry_run",
        "render_write_png",
        "render_keep_png",
    )

    def snapshot_settings(self) -> dict:
        """Everything the user configured, as plain JSON-able data.

        Covers the whole ``BatchConfig`` projection plus the panel-only fields and
        the queued scene list, because "my settings" means all of it -- losing the
        scene list after every run is the same annoyance as losing the sliders.
        """
        config = self.to_config()
        panel = {}
        for name in self.PANEL_FIELDS:
            value = getattr(self, name, None)
            if value is not None:
                panel[name] = value
        render = {}
        for name in self.RENDER_FIELDS:
            value = getattr(self, name, None)
            if value is not None:
                render[name] = value
        scenes = [
            {"path": item.path, "enabled": bool(item.enabled)}
            for item in self.scene_list if item.path
        ]
        return {
            "config": config.to_dict(),
            "panel": panel,
            "render": render,
            "scenes": scenes,
        }

    def apply_settings(self, payload: dict) -> "list[str]":
        """Push a :meth:`snapshot_settings` payload back into the panel.

        Returns the names of the sections that were applied.  Unknown or
        unusable entries are skipped rather than raising: a settings file written
        by another version must never block the panel.
        """
        applied: "list[str]" = []
        if not isinstance(payload, dict):
            return applied

        raw_config = payload.get("config")
        if isinstance(raw_config, dict) and raw_config:
            from .config.models import BatchConfig

            try:
                config = BatchConfig.from_dict(raw_config)
                self.from_config(config)
                applied.append("config")
            except Exception as exc:            # never block the panel on this
                LOGGER.warning("remembered config could not be applied: %s", exc)

        panel = payload.get("panel")
        if isinstance(panel, dict) and panel:
            for name, value in panel.items():
                if name in self.PANEL_FIELDS and hasattr(self, name):
                    try:
                        setattr(self, name, value)
                    except Exception:
                        continue
            applied.append("panel")

        render = payload.get("render")
        if isinstance(render, dict) and render:
            for name, value in render.items():
                if name in self.RENDER_FIELDS and hasattr(self, name):
                    try:
                        setattr(self, name, value)
                    except Exception:
                        continue
            applied.append("render")

        scenes = payload.get("scenes")
        if isinstance(scenes, list):
            self.scene_list.clear()
            for entry in scenes:
                if not isinstance(entry, dict):
                    continue
                path = str(entry.get("path") or "")
                if not path:
                    continue
                item = self.scene_list.add()
                item.path = path
                item.enabled = bool(entry.get("enabled", True))
                item.status = "pending"
                item.note = "" if os.path.isfile(path) else "the .blend file does not exist"
            self.scene_list_index = 0 if len(self.scene_list) else -1
            applied.append("scenes")
        return applied

    def is_pristine(self) -> bool:
        """True when nothing here differs from a fresh scene's defaults.

        Used to decide whether a newly opened file's own settings should win over
        the remembered ones: a file that carries a deliberate configuration keeps
        it, a file that has never been configured gets the user's remembered setup.

        Floats are compared with a tolerance because RNA stores single precision:
        a fresh ``clearance`` reads back as ``0.20000000298023224`` for a default of
        ``0.2``, and an exact comparison would call every scene "configured".
        """
        from .config.defaults import default_config

        #: Keys a fresh panel fills in by itself, so their value says nothing about
        #: whether the user configured anything: the template path and output folder
        #: are seeded from preferences, and the sequence resolution is preselected
        #: (720p) -- without ignoring the latter, every new scene would count as
        #: "already configured" and the remembered settings would never be applied.
        seeded = (
            "template_path", "output_root", "input_root",
            "resolution_explicit", "resolution_x", "resolution_y", "resolution_percentage",
        )

        if len(self.scene_list):
            return False
        try:
            current = self.to_config().to_dict()
        except Exception:
            return False
        baseline = default_config().to_dict()
        for section, values in (current or {}).items():
            base = baseline.get(section)
            if not isinstance(values, dict) or not isinstance(base, dict):
                if not _same_value(values, base):
                    return False
                continue
            for key, value in values.items():
                if key in seeded:
                    continue
                if not _same_value(value, base.get(key)):
                    return False
        for name in self.PANEL_FIELDS:
            if name in ("directory", "file_path", "scene_list_file"):
                continue
            default = _PANEL_DEFAULTS.get(name)
            if default is not None and not _same_value(getattr(self, name, None), default):
                return False
        return True

    def scene_paths(self) -> "list[str]":
        return [normalize_path(item.path) for item in self.scene_list if item.path]

    def enabled_scene_paths(self) -> "list[str]":
        return [
            normalize_path(item.path) for item in self.scene_list
            if item.path and item.enabled
        ]

    def find_scene(self, path: str):
        target = os.path.normcase(normalize_path(path))
        for item in self.scene_list:
            if os.path.normcase(normalize_path(item.path)) == target:
                return item
        return None

    def context_text(self) -> str:
        """One-line summary shown above the action buttons."""
        total = len(self.scene_list)
        enabled = sum(1 for item in self.scene_list if item.enabled)
        missing = sum(1 for item in self.scene_list if not os.path.isfile(item.path or ""))
        character = {
            CHARACTER_MODE_NONE: "no character",
            CHARACTER_MODE_WITH: "character only",
            CHARACTER_MODE_BOTH: "with + without character",
        }.get(self.character_mode, self.character_mode)
        return (
            f"{total} scene(s), {enabled} enabled, {missing} missing | "
            f"{self.motion_count} motion template(s) | {character} | "
            f"output: {to_forward_slashes(self.output_root) or '(unset)'}"
        )

    # -- render helpers ---------------------------------------------------
    def render_options(self) -> dict:
        """The option set handed to :class:`render.render_runner.RenderRunner`.

        Only *enabled* overrides are included, so a sequence's own recorded
        resolution/fps/samples survive unless the user explicitly opts in.
        """
        options = {
            "engine": self.render_engine_choice,
            "video_format": self.video_format,
            "codec": self.render_codec,
            "crf": self.render_crf,
            "device": "" if self.render_device == "DEFAULT" else self.render_device,
            "trajectory_mode": self.trajectory_mode,
            "trajectory_step": int(self.trajectory_step),
            "overwrite": bool(self.render_overwrite),
            "flat": bool(self.render_flat),
            "dry_run": bool(self.render_dry_run),
            "keep_frames": bool(self.render_write_png),
            "log_level": "INFO",
        }
        if self.render_override_resolution:
            options["resolution_x"] = int(self.render_res_x)
            options["resolution_y"] = int(self.render_res_y)
        if self.render_override_fps:
            options["fps"] = float(self.render_fps_choice)
        if self.render_samples_override:
            options["samples"] = int(self.render_samples_choice)
        if self.render_write_png and not self.render_keep_png:
            options.pop("keep_frames", None)
            options["frames_output"] = True
        return options

    def render_input(self) -> str:
        """The folder to scan: the explicit sequence folder wins over the root."""
        return normalize_path(self.render_sequence_dir) if self.render_sequence_dir else (
            normalize_path(self.render_input_root) if self.render_input_root else ""
        )

    def render_summary_text(self) -> str:
        total = len(self.render_list)
        done = sum(1 for item in self.render_list if item.state == "done")
        failed = sum(1 for item in self.render_list if item.state == "failed")
        skipped = sum(1 for item in self.render_list if item.state == "skipped")
        return (
            f"{total} sequence(s) listed | {done} rendered, {failed} failed, "
            f"{skipped} skipped | save to: "
            f"{to_forward_slashes(self.render_output_root) or '(unset)'}"
        )


def parse_motion_filter(text: str) -> "list[str]":
    """``"dolly_*, pan_left_01_standard"`` -> ``["dolly_*", "pan_left_01_standard"]``."""
    if not text:
        return []
    parts = []
    for chunk in str(text).replace(";", ",").split(","):
        value = chunk.strip()
        if value:
            parts.append(value)
    return parts


def apply_motion_filter(library, patterns) -> int:
    """Restrict ``library`` in place using glob patterns; returns how many remain."""
    import fnmatch

    patterns = [p for p in (patterns or []) if p]
    if not patterns:
        return len(library)
    wanted = [
        template.name for template in library
        if any(fnmatch.fnmatch(template.name, pattern) for pattern in patterns)
    ]
    if not wanted:
        raise ConfigError(
            f"the motion filter {patterns} matched none of the {len(library)} loaded template(s)"
        )
    library.restrict_to(wanted)
    return len(library)


CLASSES = (
    MPP_RenderItem,
    MPP_SceneListItem,
    MPP_SceneProperties,
)
