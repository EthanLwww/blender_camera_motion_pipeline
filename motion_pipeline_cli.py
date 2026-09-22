"""Headless sequence-generation CLI (the counterpart to ``render_sequences.py``).

Runs *inside* Blender, because generating a sequence requires a live scene::

    blender -b -P motion_pipeline_cli.py -- \
        --config batch.json --scenes "E:\\scenes\\room001.blend" --output-root "D:\\projects"

    blender -b "E:\\scenes\\room001.blend" -P motion_pipeline_cli.py -- \
        --output-root "D:\\projects" --motion-filter "dolly_*" --dry-run

    blender -b -P motion_pipeline_cli.py -- --print-config

``--output-root`` is a **project folder**, not a sequence folder: the run creates
``<output-root>/blender_camera_<date>/`` holding ``sequence/`` (the sequence tree),
``scene/`` (a copy of every source ``.blend``) and ``video/``, plus the headless
renderer, the package it imports and a README with the exact render command.  That
folder is what gets zipped to a render node.  ``--sequence-root`` still writes the
bare sequence tree when a script wants exactly that.

Every stage of the pipeline is reachable without the GUI, which is what makes the
"generate on a workstation, render on a farm" split in the brief possible.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(_HERE)
if _PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, _PACKAGE_PARENT)

# The add-on folder may be called anything -- every module inside it uses relative
# imports -- but this script has to import it by name.  ``_bootstrap`` is loaded by
# path (so it works before the package is importable) and makes the name used below
# resolve to whatever this folder is actually called.
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("_mpp_bootstrap", os.path.join(_HERE, "_bootstrap.py"))
_bootstrap = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)
PACKAGE_NAME = _bootstrap.bootstrap(__file__)

from blender_motion_pipeline.config.defaults import (  # noqa: E402
    default_config,
    load_config_file,
    save_config_file,
)
from blender_motion_pipeline.config.models import (  # noqa: E402
    CHARACTER_MODE_BOTH,
    CHARACTER_MODE_NONE,
    CHARACTER_MODE_WITH,
    BatchConfig,
    ConfigError,
    validate_batch_config,
)
from blender_motion_pipeline.core.scene_loader import (  # noqa: E402
    SceneEntry,
    describe_entries,
    load_scene_list,
    merge_scene_entries,
    save_scene_list,
    scan_directory,
)
from blender_motion_pipeline.core.sequence_manager import SequenceManager  # noqa: E402
from blender_motion_pipeline.io.json_io import save_json_file  # noqa: E402
from blender_motion_pipeline.io.path_utils import (  # noqa: E402
    normalize_path,
    parse_path_mappings,
    to_forward_slashes,
)
from blender_motion_pipeline.utils.logging_utils import (  # noqa: E402
    LEVELS,
    add_file_handler,
    log_environment_summary,
    setup_logging,
)
from blender_motion_pipeline.utils.version import GENERATOR_VERSION  # noqa: E402

LOGGER = setup_logging(level=os.environ.get("MP_LOG_LEVEL", "INFO"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="motion_pipeline_cli.py",
        description="Generate camera-motion sequences from .blend scenes without the GUI.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # Set by --sequence-root: write the bare sequence tree instead of a project folder.
    parser.set_defaults(no_project_layout=False)
    source = parser.add_argument_group("scenes")
    source.add_argument("--scenes", action="append", default=[], metavar="PATH",
                        help=".blend file to process (repeatable)")
    source.add_argument("--scene-dir", action="append", default=[], metavar="DIR",
                        help="folder to scan for .blend files (repeatable)")
    source.add_argument("--scene-list", default="",
                        help="JSON scene list previously written by --save-scene-list")
    source.add_argument("--recursive", action="store_true", help="scan --scene-dir recursively")
    source.add_argument("--save-scene-list", default="", help="write the resolved scene list here")
    source.add_argument("--include-current", action="store_true",
                        help="also process the file Blender currently has open")
    source.add_argument("--camera-selection", default="all",
                        help="'all', comma separated camera names, or comma separated indices")

    config_group = parser.add_argument_group("configuration")
    config_group.add_argument("--config", default="", help="batch configuration JSON")
    config_group.add_argument("--save-config", default="", help="write the effective configuration here")
    config_group.add_argument("--print-config", action="store_true", help="print the effective config and exit")

    motion = parser.add_argument_group("motion")
    motion.add_argument("--templates", default="", help="motion template JSON (defaults to auto-discovery)")
    motion.add_argument("--motion-filter", action="append", default=[], metavar="GLOB",
                        help="only use templates matching this glob (repeatable)")
    motion.add_argument("--frames", default="", metavar="START:END",
                        help="override the sequence frame range, e.g. 1:120")
    motion.add_argument("--fps", type=float, default=None, help="override the sequence fps")
    motion.add_argument("--interpolation", default="", choices=["", "LINEAR", "BEZIER", "CONSTANT"])
    motion.add_argument("--frame-scale", type=float, default=None,
                        help="scale template frame numbers (0.5 = half speed)")

    output = parser.add_argument_group("output")
    output.add_argument("--output-root", default="",
                        help="project folder: <root>/blender_camera_<date>/{sequence,scene,video} is created in it")
    output.add_argument("--sequence-root", default="",
                        help="write the sequence tree straight into this folder (no project layout)")
    output.add_argument("--overwrite", action="store_true", help="regenerate sequences that already exist")
    output.add_argument("--no-resume", dest="resume", action="store_false", default=None,
                        help="do not reuse existing artifacts")
    output.add_argument("--no-sequence-blend", dest="save_sequence_blend", action="store_false", default=None,
                        help="accepted for backwards compatibility; sequence .blend copies were removed")
    output.add_argument("--no-validation-report", dest="save_validation_report",
                        action="store_false", default=None, help="skip validation_report.json")

    character = parser.add_argument_group("character")
    character.add_argument("--character-mode", default="",
                           choices=["", CHARACTER_MODE_NONE, CHARACTER_MODE_WITH, CHARACTER_MODE_BOTH],
                           help="character handling mode")
    character.add_argument("--character-root", default="", help="character asset root or manifest.json")
    character.add_argument("--animation-root", default="", help="animation library root")
    character.add_argument("--character-provider", default="",
                           help="auto | blender | null | unreal_metahuman")

    validation = parser.add_argument_group("validation and search")
    validation.add_argument("--no-validation", dest="validation_enabled", action="store_false", default=None,
                            help="disable geometry validation entirely")
    validation.add_argument("--sample-step", type=int, default=None, help="validation sampling interval")
    validation.add_argument("--clearance", type=float, default=None, help="minimum camera clearance (metres)")
    validation.add_argument("--search-radius", default="", metavar="MIN:MAX",
                            help="spherical search radius range, e.g. 0.5:4.0")
    validation.add_argument("--search-candidates", type=int, default=None, help="candidate positions to try")
    validation.add_argument("--search-seed", type=int, default=None, help="search random seed")
    validation.add_argument("--no-search", dest="search_enabled", action="store_false", default=None,
                            help="never move the camera; fail instead")

    render_defaults = parser.add_argument_group("render defaults recorded in sequence_config.json")
    render_defaults.add_argument("--engine", default="", help="engine to record for the renderer")
    render_defaults.add_argument("--resolution", default="", metavar="X:Y")
    render_defaults.add_argument("--video-format", default="", help="mp4 | mkv | webm | avi")

    composite = parser.add_argument_group(
        "compound shots (several atomic moves at once, in segments)"
    )
    composite.add_argument("--compound", action="store_true", default=None,
                           help="also generate spatio-temporal compound sequences")
    composite.add_argument("--no-compound", dest="compound", action="store_false", default=None,
                           help="generate only the single-move sequences (default)")
    composite.add_argument("--compound-simultaneous", type=int, default=None, metavar="N",
                           help="how many atomic moves may run at the same moment (1-5)")
    composite.add_argument("--compound-segments", type=int, default=None, metavar="N",
                           help="how many segments a compound video may be split into "
                                "(each segment lasts at least 0.5 s)")
    composite.add_argument("--compound-per-camera", type=int, default=None, metavar="N",
                           help="how many compound sequences one camera gets (character/"
                                "animation variants are spread over them, not multiplied)")
    composite.add_argument("--compound-random", dest="compound_random", action="store_true",
                           default=None,
                           help="the two counts above are maxima of per-sequence draws (default)")
    composite.add_argument("--no-compound-random", dest="compound_random", action="store_false",
                           default=None,
                           help="the two counts above are fixed: every segment holds exactly "
                                "that many moves and every video exactly that many segments")
    composite.add_argument("--compound-templates", default="", metavar="PATH",
                           help="atomic motion document (default: the bundled "
                                "templates/atomic_motion_templates.json)")
    composite.add_argument("--compound-seed", type=int, default=None,
                           help="seed for the draws (reproducible)")
    composite.add_argument("--compound-output", default="",
                           choices=["", "with_base", "only_compound", "only_base"],
                           help="what to write: compounds with the single-move shots, compounds "
                                "only, or single moves only")
    composite.add_argument("--duration", type=float, default=None, metavar="SECONDS",
                           help="video length of every sequence (fixed mode)")
    composite.add_argument("--duration-mode", default="", choices=["", "fixed", "random"],
                           help="fixed = one length for every sequence; random = each sequence "
                                "draws its own length from --duration-min/--duration-max")
    composite.add_argument("--duration-min", type=float, default=None, metavar="SECONDS",
                           help="shortest sequence length in random mode")
    composite.add_argument("--duration-max", type=float, default=None, metavar="SECONDS",
                           help="longest sequence length in random mode")

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument("--dry-run", action="store_true",
                           help="resolve everything and report the matrix without generating")
    behaviour.add_argument("--list", dest="list_only", action="store_true", help="list discovered scenes")
    behaviour.add_argument("--check-only", action="store_true",
                           help="validate the configuration and scenes, then exit")
    behaviour.add_argument("--path-map", action="append", default=[], metavar="FROM=TO")
    behaviour.add_argument("--log-level", default="INFO", choices=list(LEVELS))
    behaviour.add_argument("--log-file", default="")
    behaviour.add_argument("--report", default="", help="write the batch report JSON here")
    behaviour.add_argument("--quiet", action="store_true", help="only log warnings and errors")
    return parser


def parse_args(argv=None):
    if argv is None:
        argv = sys.argv
    args = argv[argv.index("--") + 1:] if "--" in argv else argv[1:]
    return build_parser().parse_args(args)


# --------------------------------------------------------------------------
# configuration assembly
# --------------------------------------------------------------------------
def build_config(args) -> BatchConfig:
    """Merge defaults + --config + individual flags into one config object."""
    if args.config:
        config = load_config_file(args.config)
    else:
        config = default_config()

    if args.output_root:
        config.batch.output_root = normalize_path(args.output_root)
    if args.sequence_root:
        # Escape hatch for scripted runs that want the bare sequence tree.
        config.batch.output_root = normalize_path(args.sequence_root)
        args.no_project_layout = True
    if args.character_mode:
        config.batch.mode = args.character_mode
    if args.character_root:
        config.batch.character_asset_root = normalize_path(args.character_root)
    if args.animation_root:
        config.batch.animation_asset_root = normalize_path(args.animation_root)
    if args.character_provider:
        config.batch.character_provider = args.character_provider
    if args.overwrite:
        config.batch.overwrite = True
    if args.resume is not None:
        config.batch.resume = bool(args.resume)
    # ``--no-sequence-blend`` is a no-op: sequences are always animation-only, so
    # no per-sequence ``.blend`` is written and nothing needs configuring.
    if args.save_validation_report is not None:
        config.batch.save_validation_report = bool(args.save_validation_report)
    if args.path_map:
        config.batch.path_mappings = [
            {"from": src, "to": dst} for src, dst in parse_path_mappings(args.path_map)
        ]

    if args.templates:
        config.motion.template_path = normalize_path(args.templates)
    if args.motion_filter:
        config.motion.template_names = list(args.motion_filter)
    if args.interpolation:
        config.motion.interpolation = args.interpolation
    if args.frame_scale is not None:
        config.motion.frame_scale = float(args.frame_scale)

    if args.validation_enabled is not None:        config.validation.enabled = bool(args.validation_enabled)
    if args.sample_step is not None:
        config.validation.sample_step = int(args.sample_step)
    if args.clearance is not None:
        config.validation.clearance = float(args.clearance)

    if args.search_enabled is not None:
        config.search.enabled = bool(args.search_enabled)
    if args.search_radius:
        low, _, high = args.search_radius.partition(":")
        config.search.min_radius = float(low or 0.0)
        config.search.max_radius = float(high or low or 1.0)
    if args.search_candidates is not None:
        config.search.candidate_count = int(args.search_candidates)
    if args.search_seed is not None:
        config.search.random_seed = int(args.search_seed)

    if args.engine:
        config.render.engine = args.engine
    if args.resolution:
        width, _, height = args.resolution.partition(":")
        if width:
            config.render.resolution_x = int(width)
        if height:
            config.render.resolution_y = int(height)
        # Asking for a size here means "stamp it into the sequence": the headless
        # renderer then uses it instead of the source scene's own resolution.
        config.render.resolution_explicit = True
    if args.video_format:
        config.render.video_format = args.video_format
    if args.fps:
        config.render.fps = float(args.fps)
        config.motion.unit_scale.fps = float(args.fps)
        config.render.log_level = args.log_level
    if args.log_level:
        config.render.log_level = args.log_level

    if args.frames:
        start_text, _, end_text = args.frames.partition(":")
        if start_text.strip():
            config.motion.frame_start = int(start_text)
        if end_text.strip():
            config.motion.frame_end = int(end_text)

    if args.compound is not None:
        config.composite.enabled = bool(args.compound)
    if args.compound_simultaneous is not None:
        config.composite.max_simultaneous = int(args.compound_simultaneous)
    if args.compound_segments is not None:
        config.composite.max_segments = int(args.compound_segments)
    if args.compound_per_camera is not None:
        config.composite.sequences_per_camera = int(args.compound_per_camera)
    if args.compound_random is not None:
        config.composite.random = bool(args.compound_random)
    if args.compound_templates:
        config.composite.template_path = args.compound_templates
    if args.compound_seed is not None:
        config.composite.seed = int(args.compound_seed)
    if args.compound_output:
        config.composite.output_mode = args.compound_output
    if args.duration is not None:
        config.composite.duration = float(args.duration)
    if args.duration_mode:
        config.composite.duration_mode = args.duration_mode
    if args.duration_min is not None:
        config.composite.duration_min = float(args.duration_min)
    if args.duration_max is not None:
        config.composite.duration_max = float(args.duration_max)
    if config.composite.enabled:
        config.composite.validate()

    return config


def resolve_entries(args) -> "tuple[list[SceneEntry], list[str]]":
    """Collect scenes from the CLI, the scene list file and the open file."""
    entries: "list[SceneEntry]" = []
    warnings: "list[str]" = []

    if args.scene_list:
        loaded, list_warnings = load_scene_list(args.scene_list)
        warnings.extend(list_warnings)
        entries.extend(loaded)

    for directory in args.scene_dir:
        try:
            found = scan_directory(directory, recursive=args.recursive)
        except NotADirectoryError as exc:
            warnings.append(str(exc))
            continue
        if not found:
            warnings.append(f"no .blend files found in {directory}")
        entries, problems = merge_scene_entries(entries, found)
        warnings.extend(problems)

    if args.scenes:
        entries, problems = merge_scene_entries(entries, args.scenes)
        warnings.extend(problems)

    if args.include_current:
        import bpy

        current = bpy.data.filepath
        if current:
            entries, problems = merge_scene_entries(entries, [current])
            warnings.extend(problems)
        else:
            warnings.append("--include-current was given but no file is open")

    # Never let a 0.2 second "no scenes" failure surprise the operator.
    if not entries:
        import bpy

        if bpy.data.filepath and not args.scene_dir and not args.scenes:
            entries, problems = merge_scene_entries(entries, [bpy.data.filepath])
            warnings.extend(problems)
            warnings.append("no scenes were specified; falling back to the currently open file")
    return entries, warnings


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def command_print_config(config: BatchConfig) -> int:
    from blender_motion_pipeline.config.models import describe_config

    print(describe_config(config))
    print("\nJSON:")
    print(json.dumps(config.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_validate(config: BatchConfig, entries, args) -> int:
    """Configuration + scene pre-flight, no generation."""
    from blender_motion_pipeline.camera.motion_templates import MotionTemplateLibrary
    from blender_motion_pipeline.core import blender_context as bctx
    from blender_motion_pipeline.core.scene_loader import open_scene_for_generation

    problems = validate_batch_config(config, require_output=not args.dry_run)
    print("configuration:")
    for problem in problems:
        print(f"  PROBLEM: {problem}")
    if not problems:
        print("  ok")

    report = {"configuration_problems": problems, "scenes": [], "templates": {}}

    try:
        library = MotionTemplateLibrary.from_config(config.motion, logger=LOGGER)
        report["templates"] = library.manifest()
        print(f"\nmotion templates: {len(library)} from {library.source}")
        for warning in library.warnings:
            print(f"  WARNING: {warning}")
        print(f"  {', '.join(library.names[:8])}{' ...' if len(library) > 8 else ''}")
    except Exception as exc:
        problems.append(f"motion templates: {exc}")
        print(f"\nmotion templates: FAILED -- {exc}")

    print(f"\nscenes ({len(entries)}):")
    for entry in entries:
        if not entry.exists:
            print(f"  MISSING: {entry.path}")
            report["scenes"].append({"path": to_forward_slashes(entry.path), "ok": False,
                                     "error": "file does not exist"})
            continue
        load = open_scene_for_generation(entry)
        if not load.ok:
            print(f"  FAILED : {entry.path} -- {load.error}")
            report["scenes"].append({"path": to_forward_slashes(entry.path), "ok": False, "error": load.error})
            continue
        info = bctx.scene_report()
        context = bctx.build_scene_context(logger=LOGGER)
        info["ok"] = bool(context.cameras)
        info["validated_camera_count"] = len(context.cameras)
        info["ray_caster"] = getattr(context.ray_caster, "description", "none")
        info["scene_warnings"] = list(context.warnings)
        if not context.cameras:
            info["error"] = "scene has no camera"
        report["scenes"].append(info)
        flag = "ok    " if info["ok"] else "no cam"
        print(f"  {flag}: {os.path.basename(entry.path)} -- scene={info['scene_name']!r} "
              f"cameras={info['camera_count']} meshes={info['mesh_count']} "
              f"frames={info['frame_range'][0]}..{info['frame_range'][1]}")

    if args.report:
        save_json_file(args.report, report)
        print(f"\nreport written to {to_forward_slashes(args.report)}")
    return 0 if not problems else 1


def command_run(config: BatchConfig, entries, args) -> int:
    from blender_motion_pipeline.core.batch_runner import BatchRunner
    from blender_motion_pipeline.core.project import ProjectError, create_project
    from blender_motion_pipeline.utils.task_control import TaskController

    task = TaskController(name="cli")

    def progress(snapshot):
        if args.quiet:
            return
        stage = snapshot.get("stage") or ""
        print(
            f"\r  [{snapshot['fraction'] * 100:5.1f}%] {snapshot['completed']}/{snapshot['total']} "
            f"{stage[:60]:60s}",
            end="", flush=True,
        )

    runner = BatchRunner(
        config,
        output_root=config.batch.output_root,
        scene_entries=entries,
        logger=LOGGER,
        task=task,
        camera_selection=args.camera_selection,
    )
    problems = runner.preflight()
    hard = [p for p in problems if "does not exist" not in p and "no scenes" not in p]
    if hard:
        print("configuration problems:")
        for problem in hard:
            print(f"  PROBLEM: {problem}")
        return 1
    for problem in problems:
        LOGGER.warning("%s", problem)

    if args.dry_run:
        # A dry run must not touch the disk, so the project folder is only
        # created for a real run.
        return _dry_run_matrix(config, entries, runner, args)

    # A run writes one slim project folder (sequence tree + scene copies, data only)
    # unless --sequence-root asked for a bare sequence tree.
    if not getattr(args, "no_project_layout", False):
        from blender_motion_pipeline.core.project import ProjectError, create_project

        try:
            runner.project_layout = create_project(config.batch.output_root, logger=LOGGER)
        except ProjectError as exc:
            print(f"  PROBLEM: {exc}")
            return 1
        runner.project_root = runner.project_layout.root
        runner.output_root = runner.project_layout.sequence_root
        print(f"project folder: {runner.project_layout.root}")
        print(runner.project_layout.describe(indent="    "))

    report = runner.run()
    print()
    print(report.summary_text())
    if args.report:
        save_json_file(args.report, report.to_dict())
    return 0 if report.ok else 1


def _dry_run_matrix(config: BatchConfig, entries, runner, args=None) -> int:
    """Report the scene x motion x camera x character matrix without writing."""
    from blender_motion_pipeline.camera.motion_templates import MotionTemplateLibrary
    from blender_motion_pipeline.character.base_provider import character_variants
    from blender_motion_pipeline.core import blender_context as bctx
    from blender_motion_pipeline.core.scene_loader import open_scene_for_generation

    try:
        library = MotionTemplateLibrary.from_config(config.motion, logger=LOGGER)
    except Exception as exc:
        print(f"motion templates could not be loaded: {exc}")
        return 1
    provider = runner.build_provider()
    variants = character_variants(config.batch.mode, provider, logger=LOGGER)

    print(
        f"\ndry run: {len(entries)} scene(s) x {len(library)} motion(s) x "
        f"{len(variants)} character variant(s)"
    )
    if getattr(args, "no_project_layout", False):
        print(f"  sequence root : {to_forward_slashes(config.batch.output_root)}")
    else:
        from blender_motion_pipeline.core.project import project_folder_name

        print(f"  project root  : {to_forward_slashes(config.batch.output_root)}")
        print(
            "  project folder: "
            f"{to_forward_slashes(os.path.join(config.batch.output_root, project_folder_name()))}"
        )
    print(f"  templates     : {library.source} ({len(library)})")
    print(f"  provider      : {provider.name} / {provider.status()}")
    motion_count = len(library)
    composite = getattr(config, "composite", None)
    if composite is not None and composite.enabled:
        from blender_motion_pipeline.camera import motion_composite as mc

        atoms, atomic_source = mc.load_atomic_library(
            template_path=composite.template_path,
            fps=float(config.motion.unit_scale.fps),
            logger=LOGGER,
        )
        if not atoms:
            print(f"  compound      : PROBLEM: no atomic motions in {atomic_source}")
            return 1
        low, high = composite.effective_duration_range()
        segments = mc.max_segments_for(low, requested=composite.max_segments)
        print(
            f"  compound      : {len(atoms)} atom(s) from {to_forward_slashes(atomic_source)}"
        )
        print(
            f"      layout    : up to {segments} segment(s) of at least "
            f"{mc.MIN_SEGMENT_SECONDS:g} s"
            + (f" (capped from {composite.max_segments})"
               if segments < int(composite.max_segments) else "")
            + f", up to {composite.max_simultaneous} move(s) at once "
            f"({'random' if composite.random else 'fixed'} counts)"
        )
        print(
            f"      video     : {low:g} s" + (f"-{high:g} s per sequence (random)"
                                              if high != low else " (fixed)")
            + f" @ {config.motion.unit_scale.fps:g} fps"
            f" -> {int(round(low * config.motion.unit_scale.fps))}"
            f"..{int(round(high * config.motion.unit_scale.fps))} frame(s)"
        )
        print(
            f"      output    : {composite.output_mode} seed={composite.seed}"
            f" compounds_per_camera={composite.sequences_per_camera}"
        )
        for index, atom in enumerate(atoms):
            if index >= 6:
                print(f"      ... {len(atoms) - 6} more atom(s)")
                break
            print(f"      {atom.name:28s} {', '.join(atom.channels) or '-'}")
        # A sample plan, so the layout can be eyeballed before a run.
        sample_rng = random.Random(mc.consecutive_seed(composite.seed, 1))
        try:
            sample = mc.plan_compound(
                atoms,
                duration_seconds=mc.plan_duration(
                    mode=composite.duration_mode, duration=composite.duration,
                    minimum=composite.duration_min, maximum=composite.duration_max,
                    rng=sample_rng,
                ),
                fps=float(config.motion.unit_scale.fps),
                max_simultaneous=composite.max_simultaneous,
                max_segments=composite.max_segments,
                randomize=bool(composite.random),
                rng=sample_rng,
                seed=composite.seed,
                source=atomic_source,
            )
            print(f"      example   : {mc.describe_plan(sample)}")
            motion_count = len(atoms) + (1 if composite.want_compound() else 0)
        except Exception as exc:
            print(f"  compound      : PROBLEM: {exc}")
            return 1
        if not composite.want_base():
            motion_count = 1 if composite.want_compound() else 0
    total = 0
    for entry in entries:
        if not entry.exists:
            print(f"  MISSING : {to_forward_slashes(entry.path)}")
            continue
        load = open_scene_for_generation(entry)
        if not load.ok:
            print(f"  FAILED  : {to_forward_slashes(entry.path)} -- {load.error}")
            continue
        cameras = bctx.scene_camera_names()
        if not cameras:
            print(f"  NO CAM  : {os.path.basename(entry.path)} has no camera; it would be skipped")
            continue
        count = len(cameras) * motion_count * len(variants)
        total += count
        print(
            f"  {os.path.basename(entry.path)}: {len(cameras)} camera(s) x {motion_count} motion(s) "
            f"x {len(variants)} variant(s) = {count} sequence(s)"
        )
    print(f"\nwould generate {total} sequence(s)")
    return 0


def command_list(entries) -> int:
    print(describe_entries(entries))
    for entry in entries:
        state = "ok     " if entry.exists else "MISSING"
        print(f"  [{state}] {to_forward_slashes(entry.path)}")
    return 0


def command_summary(output_root: str) -> int:
    summary = SequenceManager(output_root).summary()
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.log_file:
        add_file_handler(args.log_file)
    if args.quiet:
        LOGGER.setLevel(30)
    else:
        LOGGER.setLevel(getattr(__import__("logging"), args.log_level, 20))

    try:
        config = build_config(args)
    except (ConfigError, ValueError) as exc:
        print(f"configuration error: {exc}")
        return 2

    if args.print_config:
        return command_print_config(config)

    log_environment_summary(LOGGER)
    LOGGER.info("motion_pipeline_cli.py %s", GENERATOR_VERSION)

    if args.save_config:
        save_config_file(args.save_config, config)
        LOGGER.info("effective configuration written to %s", to_forward_slashes(args.save_config))

    entries, warnings = resolve_entries(args)
    for warning in warnings:
        LOGGER.warning("%s", warning)
    for entry in entries:
        entry.enabled = True

    if args.save_scene_list:
        save_scene_list(args.save_scene_list, entries)
        LOGGER.info("scene list written to %s", to_forward_slashes(args.save_scene_list))

    if args.list_only:
        return command_list(entries)

    if not entries:
        print("no scenes to process: pass --scenes, --scene-dir or --scene-list")
        return 2

    if args.check_only:
        return command_validate(config, entries, args)

    return command_run(config, entries, args)


if __name__ == "__main__":
    sys.exit(main())
