"""Build the run config for the 41-move dataset, from the three paths an operator has.

    python skills/generate-41-shots/make_run_config.py \\
        --scenes /data/scenes --output /data/sequences --items /data/item \\
        --region "-2.0,0.01,2.95:9.6,5.3,5.7" --anchor "-1.4,-1.2,0.02" \\
        --run

Three inputs are required, and they are the whole interface:

* ``--scenes``  a ``.blend`` file **or a folder** (walked recursively), so one command
  covers a whole folder of scenes;
* ``--output``  where the project folder (sequence tree + scene copies) is written;
* ``--items``   a folder of ``.blend`` models (or ``--item PATH[::LABEL[::SCALE]]``),
  the focus objects that become one axis of the matrix.

The camera-region box and the focus anchor are **not optional and never derived here**:
run ``inspect_scene.py`` on the scene first, look at the report, and pass the two
numbers you chose (``--region``/``--anchor``, or an object/empty you placed yourself
with ``--region-object``/``--anchor-object``).  That is deliberate -- "inside the scene"
and "where the subject stands" are judgements about the room, and a fitted box or an
auto-searched anchor quietly produces a dataset nobody asked for.

The generated config is what ``motion_pipeline_cli.py --config`` consumes, so a run is
reproducible from the file alone:

    blender -b -noaudio --factory-startup -P motion_pipeline_cli.py -- \\
        --config <output>/run_config.json --report <output>/batch_report.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = os.path.dirname(os.path.dirname(HERE))
DEFAULT_TEMPLATES = os.path.join(PACKAGE, "templates", "camera_motion_templates_41.json")
CLI = os.path.join(PACKAGE, "motion_pipeline_cli.py")
CHUNK = 250


def scene_files(paths, *, recursive: bool = True) -> "list[str]":
    """Every ``.blend`` named, or every one inside the folders named."""
    found: "list[str]" = []
    for entry in paths:
        entry = os.path.abspath(entry)
        if os.path.isfile(entry):
            if entry.lower().endswith(".blend"):
                found.append(entry)
            continue
        if os.path.isdir(entry):
            for root, _dirs, files in os.walk(entry):
                for name in sorted(files):
                    if name.lower().endswith(".blend") and not name.endswith(".blend1"):
                        found.append(os.path.join(root, name))
                if not recursive:
                    break
    ordered = []
    for path in found:
        if path not in ordered:
            ordered.append(path)
    return ordered


def item_entries(folder: str, *, label_from: str = "file", scale: float = 1.0,
                 rotation=(0.0, 0.0, 0.0), recursive: bool = True) -> "list[dict]":
    """One focus-model record per ``.blend`` in ``folder``."""
    models = []
    for path in scene_files([folder], recursive=recursive):
        stem = os.path.splitext(os.path.basename(path))[0]
        label = stem[:-6] if label_from == "file" and stem.endswith("_blend") else stem
        models.append({"id": "", "path": path.replace("\\", "/"), "label": label,
                       "object_name": "", "scale": float(scale),
                       "rotation": [float(v) for v in rotation], "enabled": True})
    return models


def parse_item(spec: str) -> dict:
    """``PATH[::LABEL[::SCALE]]`` -> one focus-model record."""
    parts = [part.strip() for part in str(spec).split("::")]
    if len(parts) > 3:
        raise SystemExit(f"{spec!r}: expected PATH[::LABEL[::SCALE]]")
    path = os.path.abspath(parts[0]) if parts[0] else ""
    if not path:
        raise SystemExit(f"{spec!r}: a focus model needs a .blend path")
    if not os.path.isfile(path):
        raise SystemExit(f"focus model not found: {path}")
    label = parts[1] if len(parts) > 1 and parts[1] else os.path.splitext(
        os.path.basename(path))[0]
    scale = 1.0
    if len(parts) > 2 and parts[2]:
        try:
            scale = float(parts[2])
        except ValueError:
            raise SystemExit(f"{spec!r}: scale {parts[2]!r} is not a number") from None
    return {"id": "", "path": path.replace("\\", "/"), "label": label, "object_name": "",
            "scale": scale, "rotation": [0.0, 0.0, 0.0], "enabled": True}


def parse_vec(text: str, *, count: int, what: str) -> "list[float]":
    parts = [part for part in str(text).replace(",", " ").split() if part]
    if len(parts) != count:
        raise SystemExit(f"--{what}: expected {count} numbers, got {text!r}")
    try:
        return [float(part) for part in parts]
    except ValueError:
        raise SystemExit(f"--{what}: every value has to be a number ({text!r})") from None


def parse_region_box(text: str) -> "tuple[list[float], list[float]]":
    """``cx,cy,cz:sx,sy,sz`` -> ``(center, size)``."""
    if ":" not in str(text):
        raise SystemExit(f"--region: expected 'cx,cy,cz:sx,sy,sz', got {text!r}")
    center_text, _, size_text = str(text).partition(":")
    center = parse_vec(center_text, count=3, what="region center")
    size = parse_vec(size_text, count=3, what="region size")
    if any(value <= 0.0 for value in size):
        raise SystemExit(f"--region: the size has to be positive, got {size!r}")
    return center, size


def parse_resolution(text: str) -> "tuple[int, int]":
    value = str(text).lower().replace("*", "x")
    if "x" not in value:
        raise SystemExit(f"--resolution: expected WxH, got {text!r}")
    width, _, height = value.partition("x")
    try:
        return int(width), int(height)
    except ValueError:
        raise SystemExit(f"--resolution: expected WxH, got {text!r}") from None


def build_config(args) -> dict:
    scenes = scene_files(args.scenes, recursive=not args.non_recursive)
    if not scenes:
        raise SystemExit("no .blend scenes found under: " + ", ".join(args.scenes))

    models = [parse_item(spec) for spec in args.item]
    if args.items:
        models = item_entries(args.items, label_from=args.item_label,
                              scale=args.item_scale,
                              rotation=parse_vec(args.item_rotation, count=3,
                                                 what="item-rotation")
                              if args.item_rotation else (0.0, 0.0, 0.0),
                              recursive=not args.non_recursive) + models
    if not models:
        raise SystemExit(
            "no focus objects: pass --items <folder of .blend models> or "
            "--item PATH[::LABEL[::SCALE]] (or run with --no-focus-objects to "
            "generate the plain camera matrix)"
        )

    # The box and the anchor are the operator's decision -- refuse to invent them.
    if args.region_object:
        region = {"mode": "object", "object_name": args.region_object}
    elif args.region:
        center, size = parse_region_box(args.region)
        region = {"mode": "numbers", "center": center, "size": size}
    else:
        raise SystemExit(
            "no camera region: pass --region 'cx,cy,cz:sx,sy,sz' or "
            "--region-object NAME.  Run inspect_scene.py first and pick the box from "
            "its report -- an auto-fitted box would include the environment dome and "
            "every object outside the room."
        )
    region.update({"rotation": parse_vec(args.region_rotation, count=3,
                                         what="region-rotation")
                   if args.region_rotation else [0.0, 0.0, 0.0],
                   "inset": float(args.region_inset), "margin": float(args.region_margin),
                   "attempts": [8, 8, 4, 3], "strict": bool(args.region_strict)})

    if args.anchor_object:
        anchor_mode, anchor_location, anchor_object = "object", [0.0, 0.0, 0.0], args.anchor_object
    elif args.anchor:
        anchor_mode, anchor_location, anchor_object = "numbers", parse_vec(
            args.anchor, count=3, what="anchor"), ""
    else:
        raise SystemExit(
            "no focus anchor: pass --anchor 'x,y,z' or --anchor-object NAME.  Run "
            "inspect_scene.py first and pick a candidate whose orbit stays inside the "
            "room -- the plugin's automatic spot is deliberately not used here."
        )

    resolution = parse_resolution(args.resolution) if args.resolution else None
    render = {"engine": args.engine, "fps": int(args.fps)}
    if resolution:
        # ``resolution_explicit`` is what makes the *sequences* record this size: without
        # it every sequence follows the source scene (a 2010x2010 bedroom produced
        # 2010x2010 videos no matter what this config said) and the render node has to be
        # told the size again on the command line.
        render.update({"resolution_x": resolution[0], "resolution_y": resolution[1],
                       "resolution_explicit": True})
    if args.samples:
        render["samples"] = int(args.samples)

    return {
        "schema_version": 1,
        "batch": {"output_root": os.path.abspath(args.output).replace("\\", "/"),
                  "mode": "none", "keep_reports": not args.drop_reports,
                  "resume": True, "overwrite": False},
        "motion": {"template_path": os.path.abspath(args.templates).replace("\\", "/"),
                   "frame_start": int(args.frame_start),
                   "interpolation": args.interpolation},
        # ``obstruction_distance`` is how far ahead of the lens a surface counts as
        # blocking the shot.  The default (0.5 m) is tight for an interior: an orbit that
        # swings near a wall is flagged frame after frame, the camera search then moves
        # the camera away from the subject to "fix" it.  0.3 m still catches a camera
        # staring at a wall and leaves a shot that merely passes close to one alone.
        "validation": {"enabled": not args.no_validation, "sample_step": int(args.sample_step),
                       "obstruction_distance": 0.3},
        "search": {"enabled": not args.no_search},
        "render": render,
        "composite": {"enabled": False},
        "region": region,
        "focus": {"mode": "models", "models": models,
                  "anchor_mode": anchor_mode, "anchor_object": anchor_object,
                  "anchor_location": anchor_location,
                  "anchor_clearance": float(args.anchor_clearance),
                  "keep_visible": not args.no_keep_visible,
                  "visible_ratio": float(args.visible_ratio),
                  "strict": bool(args.focus_strict)},
        "scenes": [{"path": path.replace("\\", "/"), "enabled": True} for path in scenes],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_argument_group("the three inputs")
    source.add_argument("--scenes", action="append", default=[], required=True,
                        help="a .blend file or a folder to walk (repeatable)")
    source.add_argument("--output", required=True, help="where the project folder goes")
    source.add_argument("--items", default="", help="folder of .blend focus models")
    source.add_argument("--item", action="append", default=[],
                        metavar="PATH[::LABEL[::SCALE]]",
                        help="one focus model, named explicitly (repeatable)")
    source.add_argument("--non-recursive", action="store_true",
                        help="only look at the top level of the folders given")

    placement = parser.add_argument_group("the box and the anchor (never guessed)")
    placement.add_argument("--region", default="", metavar="CX,CY,CZ:SX,SY,SZ")
    placement.add_argument("--region-object", default="", metavar="NAME")
    placement.add_argument("--region-rotation", default="", metavar="RX,RY,RZ")
    placement.add_argument("--region-inset", type=float, default=0.0)
    placement.add_argument("--region-margin", type=float, default=0.25)
    placement.add_argument("--region-strict", action="store_true")
    placement.add_argument("--anchor", default="", metavar="X,Y,Z")
    placement.add_argument("--anchor-object", default="", metavar="NAME")
    placement.add_argument("--anchor-clearance", type=float, default=0.5)

    quality = parser.add_argument_group("templates and render records")
    quality.add_argument("--templates", default=DEFAULT_TEMPLATES)
    quality.add_argument("--frame-start", type=int, default=0)
    quality.add_argument("--interpolation", default="BEZIER",
                         choices=["BEZIER", "LINEAR", "CONSTANT"])
    quality.add_argument("--fps", type=float, default=24.0)
    quality.add_argument("--resolution", default="1280x720", metavar="WxH",
                         help="recorded for the renderer; 'none' keeps the scene's own")
    quality.add_argument("--engine", default="BLENDER_EEVEE")
    quality.add_argument("--samples", type=int, default=32)
    quality.add_argument("--sample-step", type=int, default=10)
    quality.add_argument("--no-validation", action="store_true")
    quality.add_argument("--no-search", action="store_true")
    quality.add_argument("--no-keep-visible", action="store_true")
    quality.add_argument("--visible-ratio", type=float, default=0.95)
    quality.add_argument("--focus-strict", action="store_true")
    quality.add_argument("--drop-reports", action="store_true",
                         help="do not keep per-sequence logs/validation reports")

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument("--config-out", default="")
    behaviour.add_argument("--run", action="store_true",
                           help="also run the generation now")
    behaviour.add_argument("--blender", default="",
                           help="Blender binary to run with (default: PATH)")
    behaviour.add_argument("--dry-run", action="store_true",
                           help="with --run, resolve the matrix without generating")
    args = parser.parse_args(argv)

    if args.resolution.lower() in ("none", "scene", ""):
        args.resolution = ""
    config = build_config(args)
    target = args.config_out or os.path.join(os.path.abspath(args.output), "run_config.json")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    scenes = config["scenes"]
    models = config["focus"]["models"]
    resolution = f"{config['render'].get('resolution_x')}x{config['render'].get('resolution_y')}"
    print(f"config   : {target}")
    print(f"scenes   : {len(scenes)}")
    for entry in scenes[:5]:
        print(f"           {entry['path']}")
    if len(scenes) > 5:
        print(f"           ... {len(scenes) - 5} more")
    print(f"focus    : {len(models)} model(s) -> " + ", ".join(m['label'] for m in models))
    print(f"anchor   : {config['focus']['anchor_mode']} "
          + (config['focus']['anchor_object'] or
             str(config['focus']['anchor_location'])))
    print(f"region   : {config['region']['mode']} "
          + (config['region'].get('object_name', '') or
             f"center={config['region'].get('center')} size={config['region'].get('size')}")
          + f" margin={config['region']['margin']}")
    print(f"templates: {config['motion']['template_path']}")
    print(f"render   : {config['render']['engine']} @{config['render']['fps']:g} fps"
          + (f" {resolution}" if args.resolution else " (scene resolution)"))
    print(f"expect   : {len(scenes)} scene(s) x 41 motion(s) x camera(s) x {len(models)} "
          "focus object(s)")

    verify = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_run.py")
    if not args.run:
        print("\nto generate:\n"
              f"  blender -b -noaudio --factory-startup -P {CLI} -- \\\n"
              f"      --config \"{target}\" --report \"{os.path.dirname(target)}"
              "/batch_report.json\"")
        print(f"to check it: python \"{verify}\" --run \"{os.path.abspath(args.output)}\"")
        return 0

    blender = args.blender or shutil.which("blender") or ""
    if not blender:
        print("blender is not on PATH; pass --blender /path/to/blender", file=sys.stderr)
        return 2
    command = [blender, "-b", "-noaudio", "--factory-startup", "-P", CLI, "--",
               "--config", target,
               "--report", os.path.join(os.path.dirname(target), "batch_report.json")]
    if args.dry_run:
        command.append("--dry-run")
    print("\nrunning: " + " ".join(f'"{part}"' if " " in part else part for part in command))
    completed = subprocess.run(command)
    print(f"\nto check what came out:\n  python \"{verify}\" --run "
          f"\"{os.path.abspath(args.output)}\"")
    return int(completed.returncode)


if __name__ == "__main__":
    sys.exit(main())
