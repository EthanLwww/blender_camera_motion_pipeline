"""Standalone headless video renderer for generated camera-motion sequences.

Designed for a render farm: no GUI, no clicking, everything through command line
arguments or a config file, non-zero exit code on failure, resumable.

Three invocation shapes are supported::

    # 1. render the file Blender already has open
    blender -b sequence_000001.blend -P render_sequences.py -- \
        --output "D:\\render_output" --video-format mp4

    # 2. scan a whole generated tree
    blender -b -P render_sequences.py -- \
        --input-root "D:\\generated_sequences" --output-root "D:\\render_output" --recursive

    # 3. render an explicit list
    blender -b -P render_sequences.py -- \
        --input "D:\\gen\\s1\\m1\\sequence_000001" --input "D:\\gen\\s1\\m1\\sequence_000002"

Each rendered sequence produces exactly three files in its own output folder::

    render_output/<scene>/<motion>/<sequence_id>/
        <sequence_id>.mp4
        <sequence_id>.json              (reference-compatible JSON details)
        <sequence_id>_camera.txt        (per-frame world-to-camera trajectory)

Everything here is import-safe and importable: the module can be loaded by the
test suite with ``--run-tests`` style flags without touching the network or the
file system until a function is called.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback

# --------------------------------------------------------------------------
# make ``blender_motion_pipeline`` importable when this file is run by Blender
# --------------------------------------------------------------------------
# ``import blender_motion_pipeline`` needs the directory *containing* the
# package on ``sys.path``.  Walking upwards from this file finds it whether the
# script is run in place (``<package>/render/render_sequences.py``) or copied
# somewhere else beside the package.
def _ensure_package_importable(start: str) -> str:
    current = os.path.abspath(start)
    for _ in range(6):
        if os.path.isdir(os.path.join(current, "blender_motion_pipeline")):
            if current not in sys.path:
                sys.path.insert(0, current)
            return current
        if os.path.basename(current) == "blender_motion_pipeline":
            parent = os.path.dirname(current)
            if parent not in sys.path:
                sys.path.insert(0, parent)
            return parent
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    if start not in sys.path:
        sys.path.insert(0, start)
    return start


_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = _ensure_package_importable(_HERE)

from blender_motion_pipeline.core.camera_animation import PAYLOAD_KEY, apply_payload  # noqa: E402
from blender_motion_pipeline.core.sequence_manager import SequenceManager, SequenceInfo  # noqa: E402
from blender_motion_pipeline.io.json_io import load_json_file, save_json_file  # noqa: E402
from blender_motion_pipeline.io.path_utils import (  # noqa: E402
    apply_path_mappings,
    ensure_dir,
    normalize_path,
    parse_path_mappings,
    relative_to,
    safe_filename,
    to_forward_slashes,
)
from blender_motion_pipeline.render.metadata_exporter import (  # noqa: E402
    build_render_metadata,
    sample_camera_trajectory,
    video_extension,
    write_camera_trajectory,
)
from blender_motion_pipeline.utils.logging_utils import (  # noqa: E402
    LEVELS,
    log_environment_summary,
    setup_logging,
)
from blender_motion_pipeline.utils.version import GENERATOR_VERSION  # noqa: E402

LOGGER = setup_logging(level=os.environ.get("MP_LOG_LEVEL", "INFO"))

#: Blender's FFmpeg container identifiers keyed by the short user-facing format.
VIDEO_FORMATS = {
    "mp4": "MPEG4",
    "mkv": "MKV",
    "webm": "WEBM",
    "avi": "AVI",
    "mov": "QUICKTIME",
}

#: Codec identifiers accepted for ``--codec``.
VIDEO_CODECS = ("H264", "H265", "AV1", "MPEG4", "WEBM", "PRORES", "DNXHD")

#: Quality presets for ``--crf``.
CRF_PRESETS = ("LOSSLESS", "PERC_LOSSLESS", "HIGH", "MEDIUM", "LOW", "VERYLOW", "LOWEST")

#: Default name of the per-run summary the panel reads back.
REPORT_NAME = "render_report.json"

#: Engines this build knows about.  Blender 4.2+ renamed EEVEE; 5.x dropped the
#: old identifier, so both spellings are accepted and resolved at runtime.
ENGINE_ALIASES = {    "EEVEE": "BLENDER_EEVEE",
    "BLENDER_EEVEE": "BLENDER_EEVEE",
    "BLENDER_EEVEE_NEXT": "BLENDER_EEVEE",
    "EEVEE_NEXT": "BLENDER_EEVEE",
    "CYCLES": "CYCLES",
    "WORKBENCH": "BLENDER_WORKBENCH",
    "BLENDER_WORKBENCH": "BLENDER_WORKBENCH",
}


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="render_sequences.py",
        description=(
            "Render camera-motion sequences generated by blender_motion_pipeline. "
            "Produces a video, a JSON details file and a camera trajectory TXT per sequence."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        # Blender steals unknown args, so only parse what follows '--' ourselves.
        add_help=True,
    )
    source = parser.add_argument_group("input")
    source.add_argument("--input", action="append", default=[],
                        help="sequence folder, sequence_config.json, or .blend file (repeatable)")
    source.add_argument("--input-root", default="",
                        help="root of a generated sequence tree to scan")
    source.add_argument("--recursive", action="store_true",
                        help="scan --input-root recursively (default when --input-root is given)")
    source.add_argument("--scene-filter", action="append", default=[], metavar="GLOB",
                        help="only render scenes matching this glob (repeatable)")
    source.add_argument("--motion-filter", action="append", default=[], metavar="GLOB",
                        help="only render motions matching this glob (repeatable)")
    source.add_argument("--sequence-filter", action="append", default=[], metavar="GLOB",
                        help="only render sequence ids matching this glob (repeatable)")

    target = parser.add_argument_group("output")
    target.add_argument("--output", default="",
                        help="output folder for a single sequence (or the flat output folder)")
    target.add_argument("--output-root", default="",
                        help="output root; the scene/motion/sequence tree is recreated under it")
    target.add_argument("--flat", action="store_true",
                        help="write every sequence directly into --output-root instead of a tree")
    target.add_argument("--video-format", default="mp4", choices=sorted(VIDEO_FORMATS),
                        help="container format")
    target.add_argument("--codec", default="H264", choices=VIDEO_CODECS, help="video codec")
    target.add_argument("--crf", default="HIGH", choices=CRF_PRESETS,
                        help="constant rate factor preset")
    target.add_argument("--video-bitrate", default="", help="override the bitrate, e.g. 20000k")

    frames = parser.add_argument_group("frames and quality")
    frames.add_argument("--frame-start", type=int, default=None, help="override the first frame")
    frames.add_argument("--frame-end", type=int, default=None, help="override the last frame")
    frames.add_argument("--resolution-x", type=int, default=None)
    frames.add_argument("--resolution-y", type=int, default=None)
    frames.add_argument("--resolution-percentage", type=int, default=None)
    frames.add_argument("--fps", type=float, default=None)
    frames.add_argument("--engine", default="", help="render engine, e.g. BLENDER_EEVEE or CYCLES")
    frames.add_argument("--samples", type=int, default=None, help="engine sample count")
    frames.add_argument("--device", default="", choices=["", "CPU", "GPU"],
                        help="Cycles device override")
    frames.add_argument("--tile-size", type=int, default=None, help="Cycles tile size (where supported)")

    traj = parser.add_argument_group("trajectory export")
    traj.add_argument("--trajectory-mode", default="all_frames", choices=["all_frames", "sampled"],
                      help="export every frame or a subsample")
    traj.add_argument("--trajectory-step", type=int, default=1,
                      help="sampling interval when --trajectory-mode=sampled")

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument("--overwrite", action="store_true",
                           help="re-render even when the expected outputs already exist")
    behaviour.add_argument("--skip-existing", dest="skip_existing", action="store_true", default=None,
                           help="skip sequences whose video already exists (default)")
    behaviour.add_argument("--no-skip-existing", dest="skip_existing", action="store_false",
                           help="render everything, ignoring existing videos unless --overwrite")
    behaviour.add_argument("--dry-run", action="store_true",
                           help="resolve inputs/outputs and validate assets without rendering")
    behaviour.add_argument("--list", dest="list_only", action="store_true",
                           help="list the discovered sequences and exit")
    behaviour.add_argument("--workers", type=int, default=1,
                           help="parallel Blender processes for a multi-sequence job")
    behaviour.add_argument("--keep-frames", action="store_true",
                           help="keep intermediate frame images (only used with --frames-output)")
    behaviour.add_argument("--frames-output", action="store_true",
                           help="also write a PNG sequence next to the video")
    behaviour.add_argument("--log-level", default="INFO", choices=list(LEVELS))
    behaviour.add_argument("--log-file", default="", help="append the run log to this file")
    behaviour.add_argument("--config", default="",
                           help="JSON file supplying defaults for these options")
    behaviour.add_argument("--path-map", action="append", default=[], metavar="FROM=TO",
                           help="remap stored asset paths, e.g. E:\\scenes=/mnt/e/scenes")
    behaviour.add_argument("--check-assets", dest="check_assets", action="store_true", default=True,
                           help="report missing external assets before rendering (default)")
    behaviour.add_argument("--no-check-assets", dest="check_assets", action="store_false",
                           help="skip the missing-asset scan")
    behaviour.add_argument("--asset-report", default="",
                           help="write the pre-flight asset report to this JSON file")
    behaviour.add_argument("--timeout", type=float, default=0.0,
                           help="abort a single sequence after N seconds (0 = no limit)")
    behaviour.add_argument("--report", default="",
                           help="write the run summary to this JSON file "
                                "(default: <output-root>/render_report.json)")
    return parser


def parse_args(argv=None):
    """Parse only the arguments after ``--``.

    Blender consumes its own flags before ``--``, so everything after it belongs
    to this script.  When the script is run under a plain interpreter (tests,
    ``--help``) the whole ``argv`` is used.
    """
    if argv is None:
        argv = sys.argv
    if "--" in argv:
        args = argv[argv.index("--") + 1:]
    else:
        args = [a for a in argv[1:] if not a.startswith("--render")]
    return build_parser().parse_args(args)


def config_defaults(args) -> dict:
    """Optional JSON file supplying defaults; explicit flags win."""
    if not args.config:
        return {}
    payload = load_json_file(args.config, default={}, required=False)
    if not isinstance(payload, dict):
        LOGGER.warning("--config %s is not a JSON object; ignoring it", args.config)
        return {}
    render = payload.get("render") if isinstance(payload.get("render"), dict) else payload
    mapping = {
        "outputRoot": "output_root",
        "output": "output",
        "inputRoot": "input_root",
        "engine": "engine",
        "samples": "samples",
        "resolutionX": "resolution_x",
        "resolutionY": "resolution_y",
        "resolutionPercentage": "resolution_percentage",
        "fps": "fps",
        "videoFormat": "video_format",
        "codec": "codec",
        "constantRateFactor": "crf",
        "recursive": "recursive",
        "overwrite": "overwrite",
        "skipExisting": "skip_existing",
        "dryRun": "dry_run",
        "workers": "workers",
        "trajectoryMode": "trajectory_mode",
        "trajectoryStep": "trajectory_step",
        "keepFrames": "keep_frames",
        "logLevel": "log_level",
        "sceneFilter": "scene_filter",
        "motionFilter": "motion_filter",
        "sequenceFilter": "sequence_filter",
    }
    defaults = {}
    for key, value in render.items():
        target = mapping.get(key, key if key in mapping.values() else None)
        if target:
            defaults[target] = value
    return defaults


def apply_config_defaults(args, defaults: dict) -> None:
    """Fill in unset options from ``defaults`` (CLI flags always win)."""
    parser = build_parser()
    took_action = {
        action.dest: action.default != getattr(args, action.dest)
        for action in parser._actions
        if hasattr(action, "dest")
    }
    for key, value in defaults.items():
        if key not in took_action or took_action[key]:
            continue
        if value is None:
            continue
        try:
            setattr(args, key, value)
        except Exception:
            continue


# --------------------------------------------------------------------------
# sequence discovery
# --------------------------------------------------------------------------
def normalise_args(args):
    """Resolve the tri-state flags that ``main`` and the tests both rely on.

    ``--skip-existing`` is a tri-state (``None`` = "not given") so it can be
    set from a config file; the default is on.  Normalising here rather than
    only in :func:`main` keeps ``resolve_sequences`` / ``select_jobs`` correct
    for any caller.
    """
    if getattr(args, "skip_existing", None) is None:
        args.skip_existing = True
    if getattr(args, "recursive", None) is None:
        args.recursive = False
    return args


def resolve_sequences(args) -> "list[dict]":
    """Turn the CLI arguments into a list of render jobs.

    ``--input-root`` implies a recursive scan: a generated tree is
    ``scene/motion/sequence`` and the useful default is "everything under here".
    ``--input`` remains the way to name one specific sequence folder.
    """
    normalise_args(args)
    jobs: "list[dict]" = []
    for item in args.input:
        jobs.extend(_jobs_from_input(item))
    if args.input_root:
        recursive = bool(args.recursive or not args.flat)
        manager = SequenceManager(args.input_root)
        found = manager.find_sequences()
        if not recursive:
            root = normalize_path(args.input_root)
            found = [info for info in found if os.path.dirname(info.sequence_dir) == root]
        found = manager.filter_sequences(
            found,
            scene_filter=args.scene_filter,
            motion_filter=args.motion_filter,
            sequence_filter=args.sequence_filter,
        )
        for info in found:
            jobs.append(_job_from_info(info, args.input_root))
    return jobs


def _jobs_from_input(item: str) -> "list[dict]":
    path = normalize_path(item)
    if os.path.isdir(path):
        manager = SequenceManager(path)
        infos = manager.find_sequences()
        if infos:
            return [_job_from_info(info, path) for info in infos]
        # A folder can also be a single sequence without a config (hand-made).
        blends = sorted(
            os.path.join(path, name) for name in os.listdir(path) if name.lower().endswith(".blend")
        )
        return [_job_from_blend(blend, path) for blend in blends]
    if path.lower().endswith(".blend") and os.path.isfile(path):
        return [_job_from_blend(path, os.path.dirname(path))]
    if os.path.isfile(path) and os.path.basename(path).lower() == "sequence_config.json":
        return [_job_from_info(_info_from_config(path), os.path.dirname(os.path.dirname(os.path.dirname(path))))]
    if path:
        LOGGER.error("--input %s is not a sequence folder, a .blend file or a sequence_config.json", item)
    return []


def _info_from_config(config_path: str) -> SequenceInfo:
    directory = os.path.dirname(normalize_path(config_path))
    filenames = sorted(os.listdir(directory))
    manager = SequenceManager(os.path.dirname(directory))
    return manager._describe(directory, filenames)  # noqa: SLF001 - same package


def _job_from_info(info: SequenceInfo, root: str) -> dict:
    blend = (info.files.get("blend") or [""])[0]
    config = info.config or {}
    return {
        "sequence_dir": info.sequence_dir,
        "sequence_id": info.sequence_id or os.path.basename(info.sequence_dir),
        "scene_name": info.scene_name,
        "motion_name": info.motion_name,
        "camera_name": info.camera_name,
        "has_character": info.has_character,
        "character_name": info.character_name,
        "character_animation": info.character_animation,
        "frame_start": info.frame_start,
        "frame_end": info.frame_end,
        "fps": info.fps,
        "blend": blend,
        # Animation-only sequences (no scene copy) render by replaying the payload
        # onto the source scene recorded at generation time.
        "source_blend": str((config.get("sequence") or {}).get("source_blend") or ""),
        "animation": info.animation_block,
        "storage_mode": info.storage_mode,
        "config": config,
        "input_root": normalize_path(root),
        "problems": list(info.problems),
    }


def _job_from_blend(blend: str, root: str) -> dict:
    directory = os.path.dirname(normalize_path(blend))
    sequence_id = os.path.splitext(os.path.basename(blend))[0]
    config_path = os.path.join(directory, "sequence_config.json")
    config = load_json_file(config_path, default={}, required=False) or {}
    sequence = config.get("sequence") or {}
    frames = config.get("frames") or {}
    rel = relative_to(directory, root)
    parts = [p for p in rel.split("/") if p and p != "."]
    return {
        "sequence_dir": directory,
        "sequence_id": sequence.get("sequence_id") or sequence_id,
        "scene_name": sequence.get("scene_name") or (parts[0] if parts else safe_filename(sequence_id)),
        "motion_name": sequence.get("motion_name") or (parts[1] if len(parts) > 1 else "motion"),
        "camera_name": sequence.get("camera_name", ""),
        "has_character": bool(sequence.get("has_character")),
        "character_name": sequence.get("character_name", ""),
        "character_animation": sequence.get("character_animation", ""),
        "frame_start": frames.get("frame_start"),
        "frame_end": frames.get("frame_end"),
        "fps": frames.get("fps"),
        "blend": normalize_path(blend),
        "source_blend": str(sequence.get("source_blend") or ""),
        "animation": (config.get("camera_animation") or {}) if isinstance(config.get("camera_animation"), dict) else {},
        "storage_mode": "blend",
        "config": config,
        "input_root": normalize_path(root),
        "problems": [] if config else ["no sequence_config.json beside this .blend"],
    }


def output_dir_for(job: dict, args) -> str:
    """Where the three artifacts for ``job`` go."""
    if args.output:
        base = normalize_path(args.output)
        if args.flat or not args.output_root:
            # A single explicit --output points straight at the sequence folder
            # when only one sequence is being rendered.
            if len(args.input) + (1 if args.input_root else 0) == 1 and not args.flat:
                return base
            return os.path.join(base, safe_filename(job["sequence_id"]))
        return base
    root = normalize_path(args.output_root) if args.output_root else normalize_path(job["sequence_dir"])
    if args.flat:
        return os.path.join(root, safe_filename(job["sequence_id"]))
    return os.path.join(
        root,
        safe_filename(job["scene_name"] or "scene"),
        safe_filename(job["motion_name"] or "motion"),
        safe_filename(job["sequence_id"] or "sequence"),
    )


def expected_outputs(job: dict, args) -> dict:
    directory = output_dir_for(job, args)
    sequence_id = safe_filename(job["sequence_id"] or "sequence")
    container = VIDEO_FORMATS.get(args.video_format, "MPEG4")
    extension = video_extension(container)
    return {
        "video": os.path.join(directory, f"{sequence_id}{extension}"),
        "metadata": os.path.join(directory, f"{sequence_id}.json"),
        "camera_trajectory": os.path.join(directory, f"{sequence_id}_camera.txt"),
        "log": os.path.join(directory, f"{sequence_id}_render_log.txt"),
    }


# --------------------------------------------------------------------------
# pre-flight
# --------------------------------------------------------------------------
def collect_asset_paths():
    """Every external file path the currently loaded blend refers to."""
    import bpy

    from blender_motion_pipeline.io.resource_check import blend_resources_from_bpy

    return blend_resources_from_bpy(bpy.data.filepath or "")


def preflight_assets(mappings, *, check: bool = True) -> dict:
    """Missing-asset scan for the loaded file."""
    from blender_motion_pipeline.io.resource_check import check_blend_resources

    if not check:
        return {"checked": 0, "skipped": True}
    try:
        resources = collect_asset_paths()
    except Exception as exc:
        return {"checked": 0, "error": str(exc)}
    result = check_blend_resources(resources, mappings=mappings)
    return result.as_dict()


def validate_and_remap_paths(mappings) -> dict:
    """Apply path mappings to the loaded file's external references."""
    import bpy

    if not mappings:
        return {"remapped": 0, "mappings": []}
    changed = []
    for image in bpy.data.images:
        if image.packed_file or not image.filepath:
            continue
        mapped = apply_path_mappings(bpy.path.abspath(image.filepath), mappings)
        if mapped and mapped != bpy.path.abspath(image.filepath):
            original = image.filepath
            image.filepath = mapped
            changed.append({"kind": "image", "name": image.name, "from": original, "to": mapped})
    for library in bpy.data.libraries:
        if not library.filepath:
            continue
        absolute = bpy.path.abspath(library.filepath)
        mapped = apply_path_mappings(absolute, mappings)
        if mapped and mapped != absolute:
            original = library.filepath
            library.filepath = mapped
            changed.append({"kind": "library", "name": library.name or "library", "from": original, "to": mapped})
    if changed:
        LOGGER.info("remapped %d external path(s) through --path-map", len(changed))
    return {"remapped": len(changed), "mappings": changed}


# --------------------------------------------------------------------------
# scene / render configuration
# --------------------------------------------------------------------------
def resolve_engine(requested: str) -> str:
    """Map a friendly engine name onto an identifier this Blender accepts."""
    if not requested:
        return ""
    candidate = ENGINE_ALIASES.get(requested.strip().upper(), requested.strip())
    try:
        import bpy

        original = bpy.context.scene.render.engine
        try:
            bpy.context.scene.render.engine = candidate
        except Exception:
            return original
        # Blender silently accepts some invalid values; confirm the read-back.
        applied = bpy.context.scene.render.engine
        bpy.context.scene.render.engine = original
        return applied
    except Exception:
        return candidate


def configure_render(scene, args, *, engine: str) -> dict:
    """Apply resolution/fps/engine settings; return what was applied."""
    render = scene.render
    applied = {
        "engine": render.engine,
        "resolution": [int(render.resolution_x), int(render.resolution_y)],
        "resolution_percentage": int(render.resolution_percentage),
        "fps": float(render.fps) / float(render.fps_base or 1.0),
        "samples": None,
    }
    if args.resolution_x:
        render.resolution_x = int(args.resolution_x)
    if args.resolution_y:
        render.resolution_y = int(args.resolution_y)
    if args.resolution_percentage:
        render.resolution_percentage = max(1, min(100, int(args.resolution_percentage)))
    if args.fps:
        render.fps = int(round(args.fps))
        render.fps_base = 1.0

    if engine:
        try:
            render.engine = engine
        except Exception as exc:
            LOGGER.warning("engine %r is unavailable (%s); keeping %s", engine, exc, render.engine)

    samples_applied = None
    if args.samples:
        current = render.engine
        if current == "CYCLES":
            try:
                scene.cycles.samples = max(1, int(args.samples))
                samples_applied = int(scene.cycles.samples)
            except Exception:
                pass
        elif current.startswith("BLENDER_EEVEE"):
            for attribute in ("taa_render_samples", "taa_samples"):
                if hasattr(scene.eevee, attribute):
                    try:
                        setattr(scene.eevee, attribute, max(1, int(args.samples)))
                        samples_applied = int(getattr(scene.eevee, attribute))
                        break
                    except Exception:
                        continue
        elif current == "BLENDER_WORKBENCH":
            for attribute in ("aa_samples", "aa_samples_viewport"):
                if hasattr(scene.display, attribute):
                    try:
                        setattr(scene.display, attribute, max(1, min(32, int(args.samples))))
                        samples_applied = int(getattr(scene.display, attribute))
                        break
                    except Exception:
                        continue
    applied["samples"] = samples_applied

    if args.device:
        try:
            scene.cycles.device = args.device
        except Exception:
            LOGGER.warning("this engine has no Cycles device setting; --device ignored")
    if args.tile_size:
        for owner, attribute in ((getattr(scene, "cycles", None), "tile_size"),
                                 (getattr(scene, "cycles", None), "tile_size_render")):
            if owner is not None and hasattr(owner, attribute):
                try:
                    setattr(owner, attribute, int(args.tile_size))
                    break
                except Exception:
                    continue

    applied["engine"] = render.engine
    applied["resolution"] = [int(render.resolution_x), int(render.resolution_y)]
    applied["resolution_percentage"] = int(render.resolution_percentage)
    applied["fps"] = float(render.fps) / float(render.fps_base or 1.0)
    return applied


def set_output_media_type(settings, media_type: str) -> str:
    """Select IMAGE vs VIDEO output across Blender versions.

    Blender 5.2 split ``image_settings.file_format`` behind a new
    ``media_type`` switch: ``FFMPEG`` only appears in the ``file_format`` enum
    once ``media_type`` is ``VIDEO``.  On 4.x and earlier the attribute does not
    exist and setting the format alone is correct, so this is a no-op there.
    """
    if not hasattr(settings, "media_type"):
        return ""
    try:
        settings.media_type = media_type
        return str(settings.media_type)
    except Exception as exc:
        LOGGER.debug("could not set media_type=%s (%s)", media_type, exc)
        return ""


def configure_video_output(scene, args, *, video_path: str, frames_dir: str = "") -> None:
    """Point Blender's FFmpeg writer at ``video_path``."""
    render = scene.render
    set_output_media_type(render.image_settings, "VIDEO")
    render.image_settings.file_format = "FFMPEG"
    render.ffmpeg.format = VIDEO_FORMATS.get(args.video_format, "MPEG4")
    render.ffmpeg.codec = args.codec
    render.ffmpeg.constant_rate_factor = args.crf
    render.ffmpeg.audio_codec = "NONE"
    if args.video_bitrate and hasattr(render.ffmpeg, "video_bitrate"):
        render.ffmpeg.video_bitrate = int(str(args.video_bitrate).rstrip("kK"))
    ensure_dir(os.path.dirname(video_path))
    stem = os.path.splitext(os.path.basename(video_path))[0]
    render.filepath = os.path.join(os.path.dirname(video_path), stem + "_")
    if frames_dir:
        render.filepath = ""
    render.use_file_extension = True
    render.use_overwrite = bool(args.overwrite)


def configure_frames_output(scene, args, *, frames_dir: str) -> None:
    """Switch the scene to a PNG sequence in ``frames_dir``."""
    render = scene.render
    set_output_media_type(render.image_settings, "IMAGE")
    render.image_settings.file_format = "PNG"
    render.image_settings.color_mode = "RGB"
    ensure_dir(frames_dir)
    render.filepath = os.path.join(frames_dir, "frame_")


def install_dummy_render_result():
    """Give ``Render Result`` a backing image so background renders stop warning.

    A stock ``blender -b`` startup has an empty ``Render Result`` datablock, and
    every render then logs ``Cannot write to Render Result: no buffer``.  Harmless
    for FFmpeg output, but it fills the farm logs, so it is silenced once here.
    """
    import bpy

    try:
        image = bpy.data.images.get("Render Result")
    except Exception:
        return
    if image is None:
        return
    try:
        if image.size[0] == 0 or image.size[1] == 0:
            image.scale(32, 32)
    except Exception:
        pass


# --------------------------------------------------------------------------
# one sequence
# --------------------------------------------------------------------------
def find_sequence_camera(config: dict):
    """Pick the camera the trajectory should be exported from."""
    import bpy

    scene = bpy.context.scene
    wanted = (config.get("sequence") or {}).get("camera_name") or ""
    if wanted:
        obj = bpy.data.objects.get(wanted)
        if obj is not None and obj.type == "CAMERA":
            return obj
    if scene.camera is not None:
        return scene.camera
    for obj in scene.objects:
        if obj.type == "CAMERA":
            return obj
    return None


def load_sequence_scene(job: dict, result: dict) -> "tuple[bool, str]":
    """Open the scene this sequence is rendered from.

    A blend-based sequence carries its own scene copy; an animation-only sequence
    has none, so the source scene recorded at generation time is opened and the
    camera animation is replayed onto it.  Both paths must end up with the same
    camera path -- that equivalence is what ``test_blender_integration`` checks by
    rendering one sequence each way and comparing the trajectories.

    Returns ``(ok, opened_path)``; ``result['error']`` is filled in on failure.
    """
    import bpy

    blend = job.get("blend") or ""
    if blend and os.path.isfile(blend):
        try:
            bpy.ops.wm.open_mainfile(filepath=blend, load_ui=False)
        except TypeError:
            bpy.ops.wm.open_mainfile(filepath=blend)
        except Exception as exc:
            result["error"] = f"cannot open {blend}: {exc}"
            return False, blend
        return True, blend

    if not job.get("animation", {}).get("available"):
        result["error"] = f"sequence .blend not found: {blend or '(none discovered)'}"
        return False, ""

    source = str(job.get("source_blend") or "")
    if not source:
        result["error"] = (
            "animation-only sequence without sequence.source_blend: there is no scene to "
            "replay the camera animation onto"
        )
        return False, ""
    if not os.path.isfile(source):
        result["error"] = f"source scene not found: {source}"
        return False, source
    try:
        bpy.ops.wm.open_mainfile(filepath=source, load_ui=False)
    except TypeError:
        bpy.ops.wm.open_mainfile(filepath=source)
    except Exception as exc:
        result["error"] = f"cannot open source scene {source}: {exc}"
        return False, source

    payload = load_json_file(
        os.path.join(job["sequence_dir"], job["animation"].get("file") or ""),
        default=None, required=False,
    )
    if not isinstance(payload, dict):
        payload = load_json_file(
            os.path.join(job["sequence_dir"], f"{job['sequence_id']}.json"),
            default=None, required=False,
        )
    payload = (payload or {}).get(job["animation"].get("key") or PAYLOAD_KEY) or {}
    summary = apply_payload(payload, config=job.get("config") or {})
    result["animation"] = summary
    for warning in summary.get("warnings") or []:
        result["warnings"].append(warning)
    if not summary.get("applied"):
        result["error"] = "camera animation could not be replayed: " + (
            "; ".join(summary.get("warnings") or []) or "unknown reason"
        )
        return False, source
    LOGGER.info(
        "replayed camera animation onto %s: %d key(s) on %r%s",
        to_forward_slashes(source), int(summary.get("key_count") or 0),
        summary.get("object_name", ""),
        f", muted {', '.join(summary['muted_constraints'])}" if summary.get("muted_constraints") else "",
    )
    return True, source


def render_sequence(job: dict, args, *, mappings, check_assets: bool = True) -> dict:
    """Render one sequence.  Returns a structured result; never raises."""
    import bpy

    started = time.time()
    result = {
        "sequence_id": job["sequence_id"],
        "sequence_dir": to_forward_slashes(job["sequence_dir"]),
        "output_dir": "",
        "blend": to_forward_slashes(job["blend"]),
        "storage_mode": job.get("storage_mode", "blend"),
        "animation": {},
        "ok": False,
        "skipped": False,
        "dry_run": bool(args.dry_run),
        "error": "",
        "files": {},
        "warnings": list(job.get("problems") or []),
        "asset_check": {},
        "render": {},
        "elapsed_seconds": 0.0,
    }
    outputs = expected_outputs(job, args)
    result["output_dir"] = to_forward_slashes(os.path.dirname(outputs["video"]))
    result["files"] = {key: to_forward_slashes(value) for key, value in outputs.items()}

    # -- load ------------------------------------------------------------
    ok, opened = load_sequence_scene(job, result)
    result["scene_file"] = to_forward_slashes(opened)
    if not ok:
        return result

    scene = bpy.context.scene
    install_dummy_render_result()
    # Refresh the config from the file we actually opened: it is authoritative.
    config = load_json_file(
        os.path.join(job["sequence_dir"], "sequence_config.json"), default=None, required=False
    )
    if not isinstance(config, dict):
        config = job.get("config") or {}

    result["asset_check"] = preflight_assets(mappings, check=check_assets)
    if check_assets:
        validate_and_remap_paths(mappings)

    # -- frames ----------------------------------------------------------
    frames_cfg = config.get("frames") or {}
    frame_start = args.frame_start if args.frame_start is not None else frames_cfg.get("frame_start")
    frame_end = args.frame_end if args.frame_end is not None else frames_cfg.get("frame_end")
    if frame_start is None:
        frame_start = scene.frame_start
    if frame_end is None:
        frame_end = scene.frame_end
    frame_start, frame_end = int(frame_start), int(frame_end)
    if frame_end < frame_start:
        result["error"] = f"invalid frame range {frame_start}..{frame_end}"
        return result
    scene.frame_start, scene.frame_end = frame_start, frame_end
    if args.fps:
        scene.render.fps = int(round(args.fps))
        scene.render.fps_base = 1.0

    engine = resolve_engine(args.engine)
    applied = configure_render(scene, args, engine=engine)
    result["render"] = applied
    result["frames"] = {"frame_start": frame_start, "frame_end": frame_end,
                        "frame_count": frame_end - frame_start + 1}

    camera = find_sequence_camera(config)
    if camera is None:
        result["error"] = "the sequence file contains no camera to render from"
        return result
    if scene.camera is None:
        scene.camera = camera

    resolution = (
        int(round(applied["resolution"][0] * applied["resolution_percentage"] / 100.0)),
        int(round(applied["resolution"][1] * applied["resolution_percentage"] / 100.0)),
    )
    sensor_width = float(getattr(getattr(camera, "data", None), "sensor_width", 36.0) or 36.0)

    # -- dry run ---------------------------------------------------------
    if args.dry_run:
        rows = sample_camera_trajectory(
            camera, frame_start=frame_start, frame_end=frame_end,
            step=max(1, args.trajectory_step),
            scene=scene, mode=args.trajectory_mode,
        )
        result["ok"] = True
        result["trajectory_rows"] = len(rows)
        missing = result["asset_check"].get("missing") or []
        if missing:
            result["warnings"].append(f"{len(missing)} external asset(s) are missing")
        LOGGER.info(
            "[dry-run] %s -> %s (%d frame(s), %dx%d, %s)",
            job["sequence_id"], os.path.dirname(outputs["video"]),
            frame_end - frame_start + 1, resolution[0], resolution[1], applied["engine"],
        )
        return result

    ensure_dir(os.path.dirname(outputs["video"]))

    # -- trajectory (before rendering, so it reflects what will be drawn) --
    try:
        rows = sample_camera_trajectory(
            camera, frame_start=frame_start, frame_end=frame_end,
            step=max(1, args.trajectory_step),
            scene=scene, mode=args.trajectory_mode,
        )
    except Exception as exc:
        result["error"] = f"camera trajectory sampling failed: {exc}"
        LOGGER.error("%s: %s", job["sequence_id"], result["error"])
        return result

    scene.frame_set(frame_start)
    fps = float(scene.render.fps) / float(scene.render.fps_base or 1.0)

    # -- render ----------------------------------------------------------
    frames_dir = ""
    if args.frames_output:
        frames_dir = ensure_dir(os.path.join(os.path.dirname(outputs["video"]), "frames"))
        configure_frames_output(scene, args, frames_dir=frames_dir)
    else:
        configure_video_output(scene, args, video_path=outputs["video"])

    try:
        bpy.ops.render.render(animation=True)
    except Exception as exc:
        result["error"] = f"render failed: {exc}\n{traceback.format_exc()}"
        LOGGER.error("%s: render failed: %s", job["sequence_id"], exc)
        return result

    # -- locate the produced video ---------------------------------------
    if frames_dir:
        produced = _encode_frames_to_video(frames_dir, outputs["video"], fps, args)
        if not produced:
            result["error"] = "frame encoding to video failed"
            return result
        if not args.keep_frames:
            shutil.rmtree(frames_dir, ignore_errors=True)
    else:
        produced_video = _locate_written_video(outputs["video"])
        if produced_video is None:
            result["error"] = (
                f"Blender reported success but no video was written near {outputs['video']}"
            )
            return result
        outputs["video"] = produced_video

    # -- sidecars --------------------------------------------------------
    try:
        metadata = build_render_metadata(
            sequence_id=job["sequence_id"],
            scene_name=job["scene_name"],
            motion_name=job["motion_name"],
            camera_name=camera.name,
            # The file the frames actually came from: the sequence's own scene copy,
            # or the shared source scene for an animation-only sequence.
            source_blend=result.get("scene_file") or job["blend"],
            sequence_dir=job["sequence_dir"],
            video_path=outputs["video"],
            rows=rows,
            frame_start=frame_start,
            frame_end=frame_end,
            fps=fps,
            resolution=resolution,
            engine=applied["engine"],
            samples=applied.get("samples"),
            has_character=bool(job.get("has_character")),
            character_name=job.get("character_name", ""),
            character_animation=job.get("character_animation", ""),
            trajectory_mode=args.trajectory_mode,
            trajectory_step=args.trajectory_step,
            sequence_config=config if isinstance(config, dict) else None,
            sensor_width_mm=sensor_width,
            elapsed_seconds=time.time() - started,
            extra={
                "render_host": {"platform": os.name, "argv": sys.argv[1:]},
                "asset_check": result["asset_check"],
            },
        )
        save_json_file(outputs["metadata"], metadata)
        write_camera_trajectory(
            outputs["camera_trajectory"], rows,
            sequence_id=job["sequence_id"],
            scene_name=job["scene_name"],
            motion_name=job["motion_name"],
            camera_name=camera.name,
            frame_start=frame_start,
            frame_end=frame_end,
            fps=fps,
            header_notes=[f"rendered_by=render_sequences.py {GENERATOR_VERSION}"],
        )
    except Exception as exc:
        result["error"] = f"sidecar export failed: {exc}"
        LOGGER.error("%s: %s", job["sequence_id"], result["error"], exc_info=True)
        return result

    _copy_sequence_config(job["sequence_dir"], os.path.dirname(outputs["video"]))
    _write_render_log(outputs["log"], job, result, rows, applied, outputs)
    result["ok"] = True
    result["files"] = {key: to_forward_slashes(value) for key, value in outputs.items()}
    result["frame_count"] = len(rows)
    LOGGER.info(
        "rendered %s -> %s (%d exported frame(s), %.1fs)",
        job["sequence_id"], to_forward_slashes(outputs["video"]), len(rows), time.time() - started,
    )
    return result


def _locate_written_video(expected: str) -> "str | None":
    """Find what Blender actually wrote and normalise it to ``expected``.

    Blender's FFmpeg writer always appends the rendered frame range to the file
    stem (``sequence_000001`` -> ``sequence_000001_0000-0080.mp4``).  The
    pipeline promises a stable ``<sequence_id>.mp4`` beside its sidecars, so the
    file is renamed into place and every other variant of the same stem is
    removed -- re-rendering must not leave ``<id>.mp4`` *and*
    ``<id>_0000-0080.mp4`` sitting next to each other.

    Returns the final path, or ``None`` when nothing was written.
    """
    if os.path.isfile(expected):
        # A previous run already produced the canonical name; still sweep any
        # suffixed leftovers so the folder stays predictable.
        _remove_video_variants(expected, keep=expected)
        return expected
    directory = os.path.dirname(expected)
    stem = os.path.splitext(os.path.basename(expected))[0]
    extension = os.path.splitext(expected)[1].lower()
    if not os.path.isdir(directory):
        return None

    candidates = []
    for name in os.listdir(directory):
        lowered = name.lower()
        if not lowered.endswith(extension):
            continue
        if not lowered.startswith(stem.lower()):
            continue
        candidates.append(os.path.join(directory, name))
    if not candidates:
        return None
    # Prefer the shortest name: the plain stem beats "<stem>_0000-0080".
    candidates.sort(key=lambda path: (len(os.path.basename(path)), os.path.basename(path)))
    chosen = candidates[0]
    try:
        if os.path.normcase(chosen) != os.path.normcase(expected):
            if os.path.exists(expected):
                os.remove(expected)
            os.replace(chosen, expected)
            LOGGER.info(
                "Blender wrote %s; renamed it to %s",
                os.path.basename(chosen), os.path.basename(expected),
            )
        _remove_video_variants(expected, keep=expected)
        return expected
    except OSError as exc:
        LOGGER.warning(
            "could not rename %s to %s (%s); keeping the written name",
            os.path.basename(chosen), os.path.basename(expected), exc,
        )
        return chosen


def _remove_video_variants(expected: str, *, keep: str) -> "list[str]":
    """Delete other ``<stem>*<ext>`` videos beside ``expected``.

    ``keep`` is never touched, and only files sharing the expected stem *and*
    extension are considered, so an unrelated video in the same folder is left
    alone.
    """
    directory = os.path.dirname(expected)
    if not os.path.isdir(directory):
        return []
    stem = os.path.splitext(os.path.basename(expected))[0].lower()
    extension = os.path.splitext(expected)[1].lower()
    keep_key = os.path.normcase(keep)
    removed = []
    for name in os.listdir(directory):
        lowered = name.lower()
        if not lowered.endswith(extension) or not lowered.startswith(stem):
            continue
        path = os.path.join(directory, name)
        if os.path.normcase(path) == keep_key or not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            continue
    if removed:
        LOGGER.info(
            "removed %d leftover video variant(s): %s",
            len(removed), ", ".join(os.path.basename(path) for path in removed),
        )
    return removed


def _existing_video_for(expected: str) -> "str | None":
    """An already-rendered video for ``expected``, tolerating Blender's naming."""
    if os.path.isfile(expected):
        return expected
    directory = os.path.dirname(expected)
    stem = os.path.splitext(os.path.basename(expected))[0]
    extension = os.path.splitext(expected)[1].lower()
    if not os.path.isdir(directory):
        return None
    for name in sorted(os.listdir(directory)):
        lowered = name.lower()
        if lowered.startswith(stem.lower()) and lowered.endswith(extension):
            return os.path.join(directory, name)
    return None


def _encode_frames_to_video(frames_dir: str, video_path: str, fps: float, args) -> bool:
    """ffmpeg-encode a PNG sequence (only used with --frames-output)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        LOGGER.error(
            "ffmpeg is not on PATH; cannot encode %s into a video. "
            "Re-run without --frames-output to use Blender's built-in encoder.", frames_dir,
        )
        return False
    pattern = os.path.join(frames_dir, "frame_%04d.png")
    command = [
        ffmpeg, "-y", "-framerate", f"{fps:g}", "-start_number", "1",
        "-i", pattern, "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-crf", "18", "-preset", "medium", video_path,
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=3600)
    except Exception as exc:
        LOGGER.error("ffmpeg invocation failed: %s", exc)
        return False
    if completed.returncode != 0:
        LOGGER.error("ffmpeg failed (%d): %s", completed.returncode, (completed.stderr or "")[-2000:])
        return False
    return os.path.isfile(video_path)


def _copy_sequence_config(sequence_dir: str, output_dir: str) -> str:
    """Copy ``sequence_config.json`` beside the rendered artifacts.

    Without this the render output folder holds a video but no *marker*, so any
    scanner (the panel's "Load sequences", `--input-root` on a later run, the
    sequence manager) cannot see the sequence.  A rendered tree must be
    self-describing for the same reason the generated tree is.
    """
    source = os.path.join(normalize_path(sequence_dir), "sequence_config.json")
    if not os.path.isfile(source):
        return ""
    target = os.path.join(normalize_path(output_dir), "sequence_config.json")
    if os.path.normcase(source) == os.path.normcase(target):
        return target
    try:
        ensure_dir(output_dir)
        shutil.copy2(source, target)
        return target
    except OSError as exc:
        LOGGER.warning("could not copy sequence_config.json into %s: %s", output_dir, exc)
        return ""


def _write_render_log(path: str, job: dict, result: dict, rows, applied: dict, outputs: dict) -> None:
    ensure_dir(os.path.dirname(path) or ".")
    lines = [
        f"sequence_id      : {job['sequence_id']}",
        f"source sequence  : {to_forward_slashes(job['sequence_dir'])}",
        f"storage mode     : {job.get('storage_mode', 'blend')}",
        f"sequence blend   : {to_forward_slashes(job['blend']) or '(none: animation-only)'}",
        f"scene rendered   : {to_forward_slashes(result.get('scene_file') or job['blend'])}",
        f"scene / motion   : {job['scene_name']} / {job['motion_name']}",
        f"character        : {'yes' if job.get('has_character') else 'no'}"
        + (f" ({job.get('character_name')} / {job.get('character_animation')})" if job.get("has_character") else ""),
        f"frames           : {result.get('frames', {}).get('frame_start')}"
        f"..{result.get('frames', {}).get('frame_end')}"
        f" ({result.get('frames', {}).get('frame_count')} frame(s))",
        f"trajectory rows  : {len(rows)} (mode={rows and 'ok' or 'empty'})",
        f"engine           : {applied.get('engine')} samples={applied.get('samples')}",
        f"resolution       : {applied.get('resolution')} @ {applied.get('resolution_percentage')}%",
        f"fps              : {applied.get('fps')}",
        f"status           : {'ok' if result.get('ok') else result.get('error') or 'failed'}",
        "",
        "artifacts:",
    ]
    for key, value in sorted(outputs.items()):
        lines.append(f"  {key:18s}: {to_forward_slashes(value)}")
    for warning in result.get("warnings") or []:
        lines.append(f"  WARNING: {warning}")
    missing = (result.get("asset_check") or {}).get("missing") or []
    if missing:
        lines.append(f"  missing assets ({len(missing)}):")
        for item in missing[:50]:
            lines.append(f"    - [{item.get('kind')}] {item.get('path')}")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def select_jobs(jobs, args) -> "list[dict]":
    """Apply the resume/skip rules."""
    normalise_args(args)
    selected = []
    seen = set()
    for job in jobs:
        key = os.path.normcase(job["sequence_dir"])
        if key in seen:
            continue
        seen.add(key)
        outputs = expected_outputs(job, args)
        if args.skip_existing and not args.overwrite:
            existing = _existing_video_for(outputs["video"])
            if existing is not None:
                LOGGER.info("skipping %s: %s already exists", job["sequence_id"], to_forward_slashes(existing))
                job["skip_reason"] = "video already exists"
                job["existing_video"] = existing
                selected.append(job)
                continue
        selected.append(job)
    return selected


def summary_path(output_root: str, explicit: str = "") -> str:
    """Where the run summary goes: ``--report`` wins, else the output root."""
    if explicit:
        return normalize_path(explicit)
    return os.path.join(normalize_path(output_root), REPORT_NAME)


def write_batch_summary(output_root: str, results, *, dry_run: bool, extra: dict | None = None,
                        report_path: str = "") -> str:
    ensure_dir(output_root)
    ok = [r for r in results if r.get("ok") and not r.get("skipped")]
    failed = [r for r in results if not r.get("ok") and not r.get("skipped")]
    skipped = [r for r in results if r.get("skipped")]
    payload = {
        "generator_version": GENERATOR_VERSION,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dry_run": bool(dry_run),
        "output_root": to_forward_slashes(output_root),
        "totals": {
            "sequences": len(results),
            "rendered": len(ok),
            "failed": len(failed),
            "skipped": len(skipped),
        },
        "rendered": [{"sequence_id": r["sequence_id"], "video": (r.get("files") or {}).get("video"),
                      "output_dir": r.get("output_dir")} for r in ok],
        "failed": [{"sequence_id": r["sequence_id"], "error": r.get("error"),
                    "output_dir": r.get("output_dir")} for r in failed],
        "skipped": [{"sequence_id": r["sequence_id"], "reason": r.get("skip_reason", "already rendered")}
                    for r in skipped],
        "results": results,
    }
    if extra:
        payload.update(extra)
    path = summary_path(output_root, report_path)
    save_json_file(path, payload)
    return path


def run_render(args, *, mappings) -> int:
    """Top-level render driver.  Returns the process exit code."""
    normalise_args(args)
    jobs = resolve_sequences(args)
    if not jobs:
        LOGGER.error(
            "no sequences to render. Provide --input / --input-root, or run this script "
            "with a sequence .blend already open."
        )
        return 2

    if args.list_only:
        print(f"{len(jobs)} sequence(s):")
        for job in jobs:
            outputs = expected_outputs(job, args)
            state = "rendered" if os.path.isfile(outputs["video"]) else "pending"
            scene = to_forward_slashes(job["blend"]) or (
                "animation-only -> " + (to_forward_slashes(job.get("source_blend") or "") or "(no source scene)")
            )
            print(f"  [{state:8s}] {job['sequence_id']}  [{job.get('storage_mode', 'blend')}]  {scene}")
            if job.get("problems"):
                for problem in job["problems"]:
                    print(f"             ! {problem}")
        return 0

    jobs = select_jobs(jobs, args)
    pending = [job for job in jobs if not job.get("skip_reason")]
    LOGGER.info(
        "%d sequence(s) discovered: %d to render, %d already done",
        len(jobs), len(pending), len(jobs) - len(pending),
    )

    # -- parallel workers: one Blender process per sequence ---------------
    if args.workers > 1 and len(pending) > 1:
        return _run_parallel(pending, args, mappings)

    results = []
    for index, job in enumerate(jobs, start=1):
        if job.get("skip_reason"):
            results.append({
                "sequence_id": job["sequence_id"],
                "sequence_dir": to_forward_slashes(job["sequence_dir"]),
                "ok": False,
                "skipped": True,
                "skip_reason": job["skip_reason"],
                "files": {k: to_forward_slashes(v) for k, v in expected_outputs(job, args).items()},
                "output_dir": to_forward_slashes(os.path.dirname(expected_outputs(job, args)["video"])),
            })
            continue
        LOGGER.info("[%d/%d] %s", index, len(jobs), job["sequence_id"])
        try:
            results.append(render_sequence(job, args, mappings=mappings, check_assets=args.check_assets))
        except Exception as exc:
            results.append({
                "sequence_id": job["sequence_id"],
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
                "output_dir": to_forward_slashes(os.path.dirname(expected_outputs(job, args)["video"])),
                "files": {},
            })

    output_root = normalize_path(args.output_root or args.output or (jobs[0]["sequence_dir"] if jobs else "."))
    report = write_batch_summary(
        output_root, results, dry_run=args.dry_run, report_path=getattr(args, "report", "")
    )
    failed = [r for r in results if not r.get("ok") and not r.get("skipped")]
    rendered = [r for r in results if r.get("ok") and not r.get("skipped")]
    skipped = [r for r in results if r.get("skipped")]
    LOGGER.info(
        "done: %d rendered, %d failed, %d skipped; report at %s",
        len(rendered), len(failed), len(skipped), to_forward_slashes(report),
    )
    if failed:
        for item in failed:
            LOGGER.error("  FAILED %s: %s", item["sequence_id"], item.get("error"))
        return 1
    if not rendered:
        if skipped:
            # Resumable by design: a fully rendered tree is a success, not an error.
            LOGGER.info(
                "nothing to do: all %d sequence(s) already have a video "
                "(use --overwrite to re-render)",
                len(skipped),
            )
            return 0
        LOGGER.error("nothing was rendered")
        return 1
    return 0


def worker_command(executable: str, script: str, sequence_dirs, args) -> "list[str]":
    """Build the command line for one worker process.

    The order is load-bearing.  Blender parses its **own** options up to the
    standalone ``--``; anything after it belongs to the script.  Emitting the
    script options before ``-P`` makes Blender read ``--input`` as a file name
    (``unknown argument, loading as file: --input``) and the worker renders
    nothing, so ``-P <script> --`` always comes first::

        blender -b -P render_sequences.py -- --input <dir> [--input <dir>] ...
    """
    command = [executable, "-b", "-P", script, "--"]
    for sequence_dir in sequence_dirs:
        command += ["--input", sequence_dir]
    if args.output_root:
        command += ["--output-root", args.output_root]
    if args.output:
        command += ["--output", args.output]
    command += ["--video-format", args.video_format, "--codec", args.codec, "--crf", args.crf]
    command += ["--trajectory-mode", args.trajectory_mode, "--trajectory-step", str(args.trajectory_step)]
    command += ["--log-level", args.log_level]
    if args.flat:
        command += ["--flat"]
    if args.recursive:
        command += ["--recursive"]
    if args.overwrite:
        command += ["--overwrite"]
    if args.dry_run:
        command += ["--dry-run"]
    if args.engine:
        command += ["--engine", args.engine]
    if args.samples:
        command += ["--samples", str(args.samples)]
    if args.resolution_x:
        command += ["--resolution-x", str(args.resolution_x)]
    if args.resolution_y:
        command += ["--resolution-y", str(args.resolution_y)]
    if args.fps:
        command += ["--fps", str(args.fps)]
    if args.frames_output:
        command += ["--frames-output"]
    if getattr(args, "keep_frames", False):
        command += ["--keep-frames"]
    for pair in args.path_map:
        command += ["--path-map", pair]
    return command


def _run_parallel(pending, args, mappings) -> int:
    """Fan out to ``--workers`` Blender subprocesses."""
    import bpy

    executable = bpy.app.binary_path
    if not executable:
        LOGGER.warning("cannot determine the Blender executable; falling back to sequential rendering")
        args.workers = 1
        return run_render(args, mappings=mappings)

    script = os.path.abspath(__file__)
    workers = max(1, int(args.workers))
    batches = [pending[index::workers] for index in range(workers)]
    processes = []
    for batch in batches:
        if not batch:
            continue
        command = worker_command(
            executable, script, [job["sequence_dir"] for job in batch], args
        )
        LOGGER.info(
            "worker: %s",
            " ".join(f'"{part}"' if " " in part else part for part in command[:8]) + " ...",
        )
        try:
            processes.append(subprocess.Popen(command))
        except Exception as exc:
            LOGGER.error("could not start worker: %s", exc)

    exit_code = 0
    for process in processes:
        code = process.wait()
        if code != 0:
            exit_code = code
    LOGGER.info("%d worker process(es) finished", len(processes))
    return exit_code


def main(argv=None) -> int:
    args = parse_args(argv)
    LOGGER.setLevel(getattr(__import__("logging"), args.log_level, 20))
    if args.log_file:
        from blender_motion_pipeline.utils.logging_utils import add_file_handler

        add_file_handler(args.log_file)
    defaults = config_defaults(args)
    if defaults:
        apply_config_defaults(args, defaults)
    args.recursive = bool(args.recursive or args.input_root)
    if args.skip_existing is None:
        args.skip_existing = True
    mappings = parse_path_mappings(args.path_map)
    log_environment_summary(LOGGER)
    LOGGER.info("render_sequences.py %s", GENERATOR_VERSION)
    if mappings:
        LOGGER.info("path mappings: %s", ", ".join(f"{a} -> {b}" for a, b in mappings))

    try:
        exit_code = run_render(args, mappings=mappings)
    except Exception as exc:
        LOGGER.error("unhandled failure: %s", exc, exc_info=True)
        exit_code = 3

    if args.asset_report:
        try:
            save_json_file(args.asset_report, preflight_assets(mappings, check=args.check_assets))
        except Exception as exc:
            LOGGER.warning("could not write --asset-report: %s", exc)
    try:
        import bpy

        bpy.ops.wm.quit_blender()
    except Exception:
        pass
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
