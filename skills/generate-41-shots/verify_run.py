"""Check a generated sequence tree and say what is actually in it.

Reads only the JSON the generator wrote -- no Blender, no add-on import -- so it can
be run on any machine that has the output folder, including a render node.

    python verify_run.py --run E:/Blender/output/sequence/sequence_output_0924
    python verify_run.py --run <folder> --expect-cameras 3 --expect-motions 41 \
        --expect-focus 2 --strict

Exit code 0 when the run is structurally sound, 1 otherwise.  ``--strict`` also fails
on the softer findings (a shot whose camera left the region box, a subject that was
not in frame), which are otherwise reported as WARN.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os

ARC_HINTS = ("arc", "orbit")


def _load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def find_project(run_dir: str) -> "str | None":
    """The ``blender_camera_*`` folder inside a run directory, if it is there."""
    if os.path.isdir(os.path.join(run_dir, "sequence")):
        return run_dir
    candidates = sorted(glob.glob(os.path.join(run_dir, "blender_camera_*")))
    candidates = [c for c in candidates if os.path.isdir(os.path.join(c, "sequence"))]
    if candidates:
        return candidates[0]
    # A project folder nested one level deeper still counts.
    nested = sorted(glob.glob(os.path.join(run_dir, "*", "blender_camera_*", "sequence")))
    return os.path.dirname(nested[0]) if nested else None


def collect(project_dir: str):
    """Every sequence under ``project_dir``/sequence, with the fields worth checking.

    The tree is ``sequence/[<scene>/]<motion>/<sequence>/sequence_config.json``; the
    scene level is optional, so the folder names are read from the path itself.
    """
    root = os.path.join(project_dir, "sequence")
    rows = []
    for config_path in sorted(glob.glob(os.path.join(root, "**", "sequence_config.json"),
                                       recursive=True)):
        sequence_path = os.path.dirname(config_path)
        parts = os.path.relpath(sequence_path, root).replace("\\", "/").split("/")
        if len(parts) < 2:
            continue
        motion_dir = parts[-2]
        scene_name = parts[0] if len(parts) >= 3 else ""
        name = parts[-1]
        if True:
            config = _load(config_path) or {}
            sequence = config.get("sequence") or {}
            render = config.get("render") or {}
            region = config.get("region") or {}
            focus = config.get("focus") or {}
            visibility = focus.get("visibility") or {}
            rows.append({
                "scene": scene_name or str(sequence.get("scene_name") or ""),
                "motion": motion_dir,
                "name": name,
                "path": sequence_path,
                "camera": sequence.get("camera_name") or (config.get("camera") or {}).get("name") or "?",
                "focus": focus.get("object") or "",
                "arc": any(hint in motion_dir.lower() for hint in ARC_HINTS),
                "engine": render.get("engine"),
                "resolution": tuple(render.get("effective_resolution") or ()),
                "resolution_explicit": bool(render.get("resolution_explicit")),
                "fps": render.get("fps"),
                "frames": int((config.get("frames") or {}).get("frame_count") or 0),
                "region_stage": region.get("stage"),
                "region_ok": region.get("ok"),
                "region_exit_frames": region.get("exit_frames"),
                "region_scale": region.get("scale"),
                "region_max_excess": region.get("max_excess_m"),
                "visibility": visibility or None,
                "visible_ratio": (None if not visibility
                                  else float(visibility.get("visible_ratio") or 0.0)),
                "visibility_ok": (None if not visibility else bool(visibility.get("ok"))),
                "validated": (config.get("validation") or {}).get("passed"),
                "validation_reasons": (config.get("validation") or {}).get("reasons") or [],
                "has_video": any(f.endswith((".mp4", ".mkv", ".mov"))
                                 for f in os.listdir(sequence_path)),
            })
    return rows


def _counter(rows, key):
    return collections.Counter(row[key] for row in rows if row.get(key) not in (None, ""))


def find_report(run_dir: str, project_dir: str) -> "str | None":
    """``batch_report.json`` -- at the run root, or in the project (CLI default)."""
    for candidate in (os.path.join(run_dir, "batch_report.json"),
                      os.path.join(project_dir, "batch_report.json"),
                      os.path.join(project_dir, "sequence", "batch_report.json")):
        if os.path.isfile(candidate):
            return candidate
    return None


def report(run_dir: str, rows, project_dir: str, args) -> int:
    problems, warnings = [], []

    print("run        : %s" % run_dir)
    print("project    : %s" % project_dir)
    report_path = find_report(run_dir, project_dir)
    batch = _load(report_path) if report_path else None
    if batch:
        totals = batch.get("totals") or {}
        print("batch      : %s" % json.dumps(
            {k: totals.get(k) for k in sorted(totals) if totals.get(k) is not None},
            ensure_ascii=False))
        print("report     : %s" % report_path)
        for scene in batch.get("scenes") or ():
            print("scene      : %s  generated=%s failed=%s skipped=%s (%.1fs)"
                  % (os.path.basename(str(scene.get("path") or scene.get("name") or "?")),
                     scene.get("generated"), scene.get("failed"), scene.get("skipped"),
                     float(scene.get("elapsed_seconds") or 0.0)))
            if int(scene.get("failed") or 0):
                problems.append("scene %s reported %s failed sequence(s)"
                                % (scene.get("name") or scene.get("path"), scene.get("failed")))
        for error in batch.get("errors") or ():
            problems.append("batch error: %s" % (error,))
    else:
        warnings.append("no batch_report.json under %s" % run_dir)

    if not rows:
        problems.append("no sequence_config.json found under %s" % project_dir)
    print("sequences  : %d in %d motion folder(s)"
          % (len(rows), len({row["motion"] for row in rows})))

    for label, key in (("motion", "motion"), ("camera", "camera"), ("focus object", "focus")):
        counts = _counter(rows, key)
        if counts:
            print("%-11s: %s" % (label, ", ".join("%s=%d" % item for item in sorted(counts.items()))))

    engines = _counter(rows, "engine")
    resolutions = collections.Counter(row["resolution"] for row in rows)
    print("render     : %s | resolution %s | fps %s"
          % (", ".join("%s=%d" % item for item in sorted(engines.items())),
             ", ".join("%dx%d=%d" % (r[0], r[1], n) if len(r) == 2 else "%s=%d" % (r, n)
                       for r, n in sorted(resolutions.items())),
             ", ".join("%s" % v for v in sorted({row["fps"] for row in rows}))))
    explicit = sum(1 for row in rows if row["resolution_explicit"])
    if 0 < explicit < len(rows):
        problems.append("resolution_explicit is mixed: %d/%d sequence(s) pin their own size"
                        % (explicit, len(rows)))
    for resolution in resolutions:
        if len(resolution) == 2 and any(int(v) % 2 for v in resolution):
            problems.append("resolution %sx%s has an odd dimension; H.264 needs even numbers"
                            % resolution)

    stages = _counter(rows, "region_stage")
    if stages:
        print("region     : %s" % ", ".join("%s=%d" % item for item in sorted(stages.items())))
    outside = [row for row in rows if row["region_ok"] is False]
    for row in outside[:10]:
        warnings.append("%s/%s: camera left the box (%s, worst %s m, scale %s)"
                        % (row["motion"], row["name"], row["region_stage"],
                           row["region_max_excess"], row["region_scale"]))

    arcs = [row for row in rows if row["arc"] and row["visibility"]]
    if arcs:
        worst = min(arcs, key=lambda row: row["visible_ratio"])
        print("arcs       : %d arc shot(s), subject visible %.0f%% on average, worst %s/%s "
              "at %.0f%%" % (len(arcs),
                             100.0 * sum(row["visible_ratio"] for row in arcs) / len(arcs),
                             worst["motion"], worst["name"], 100.0 * worst["visible_ratio"]))
        for row in arcs:
            if row["visibility_ok"] is False:
                warnings.append("%s/%s: the subject was in frame for %s frame(s) only"
                                % (row["motion"], row["name"],
                                   (row["visibility"] or {}).get("visible_frames")))

    failed_validation = [row for row in rows if row["validated"] is False]
    for row in failed_validation[:10]:
        problems.append("%s/%s: validation failed (%s)"
                        % (row["motion"], row["name"], ", ".join(row["validation_reasons"])))

    frame_counts = _counter(rows, "frames")
    if frame_counts:
        print("lengths    : %s" % ", ".join("%s frames=%d" % item
                                            for item in sorted(frame_counts.items())))
    videos = sum(1 for row in rows if row["has_video"])
    print("videos     : %d sequence folder(s) already hold a video" % videos)

    if args.expect_cameras and len(_counter(rows, "camera")) < args.expect_cameras:
        problems.append("expected %d camera(s), found %d"
                        % (args.expect_cameras, len(_counter(rows, "camera"))))
    if args.expect_motions and len(_counter(rows, "motion")) < args.expect_motions:
        problems.append("expected %d motion folder(s), found %d"
                        % (args.expect_motions, len(_counter(rows, "motion"))))
    if args.expect_focus and len(_counter(rows, "focus")) < args.expect_focus:
        problems.append("expected %d focus object(s), found %d"
                        % (args.expect_focus, len(_counter(rows, "focus"))))
    if args.expect_sequences and len(rows) != args.expect_sequences:
        problems.append("expected %d sequence(s), found %d" % (args.expect_sequences, len(rows)))

    print("-" * 72)
    for warning in warnings:
        print("WARN  %s" % warning)
    for problem in problems:
        print("FAIL  %s" % problem)
    if not problems and not warnings:
        print("OK    every check passed")
    elif not problems:
        print("OK    %d warning(s), nothing structurally wrong" % len(warnings))
    return 1 if problems or (args.strict and warnings) else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verify a generated sequence tree.")
    parser.add_argument("--run", required=True, help="the sequence output folder")
    parser.add_argument("--project", default="", help="the blender_camera_* folder, if known")
    parser.add_argument("--expect-cameras", type=int, default=0)
    parser.add_argument("--expect-motions", type=int, default=0)
    parser.add_argument("--expect-focus", type=int, default=0)
    parser.add_argument("--expect-sequences", type=int, default=0)
    parser.add_argument("--strict", action="store_true",
                        help="also fail on region/subject warnings")
    args = parser.parse_args(argv)

    run_dir = os.path.abspath(args.run)
    if not os.path.isdir(run_dir):
        print("FAIL  %s is not a folder" % run_dir)
        return 1
    project_dir = os.path.abspath(args.project) if args.project else find_project(run_dir)
    if project_dir is None:
        print("FAIL  no sequence folder found in %s (pass --project)" % run_dir)
        return 1
    rows = collect(project_dir)
    if not os.path.isdir(os.path.join(run_dir, "sequence")) and os.path.isdir(project_dir):
        # A project folder was passed directly: read the report from its parent run.
        parent = os.path.dirname(project_dir)
        if os.path.isfile(os.path.join(parent, "batch_report.json")):
            run_dir = parent
    return report(run_dir, rows, project_dir, args)


if __name__ == "__main__":
    raise SystemExit(main())
