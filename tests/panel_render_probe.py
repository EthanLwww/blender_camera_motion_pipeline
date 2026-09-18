"""Exercise the panel's local-render workflow headlessly.

Drives the *operators* (not the runner directly) so the test covers exactly what
the sidebar buttons do: load sequences, render all, stop, and the two quick
actions.

    blender -b -P tests/panel_render_probe.py -- <generated-root> <output-root>

Writes a small JSON summary to ``<output-root>/panel_probe.json`` so the caller
can assert on the outcome without parsing stdout.
"""
from __future__ import annotations

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
if _PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, _PACKAGE_PARENT)

import bpy  # noqa: E402

from blender_motion_pipeline import registration  # noqa: E402
from blender_motion_pipeline.io.json_io import save_json_file  # noqa: E402
from blender_motion_pipeline.operators import _render_tick  # noqa: E402
from blender_motion_pipeline.render import render_runner  # noqa: E402


def pump(limit_seconds: float) -> bool:
    """Run the render timer the way Blender's event loop would."""
    deadline = time.time() + limit_seconds
    while render_runner.is_running() and time.time() < deadline:
        _render_tick()
        time.sleep(0.2)
    return not render_runner.is_running()


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if len(argv) < 2:
        print("usage: blender -b -P panel_render_probe.py -- <generated-root> <output-root>")
        return 2
    source_root, output_root = argv[0], argv[1]

    registration.unregister_all()
    registration.register_all()

    summary: dict = {"source_root": source_root, "output_root": output_root}
    group = bpy.context.scene.mpp

    # -- the "Motion templates" default ----------------------------------
    summary["template_path"] = group.template_path
    summary["motion_count"] = group.motion_count

    # -- load sequences ---------------------------------------------------
    group.render_input_root = source_root
    group.render_output_root = output_root
    group.render_recursive = True
    group.render_overwrite = True
    group.render_engine_choice = "BLENDER_WORKBENCH"
    group.render_override_resolution = True
    group.render_res_x, group.render_res_y = 320, 180

    summary["load_result"] = sorted(bpy.ops.mpp.load_render_sequences())
    summary["load_report"] = group.last_report
    summary["listed"] = len(group.render_list)
    summary["auto_output_root"] = group.render_output_root

    # -- render all -------------------------------------------------------
    summary["render_result"] = sorted(bpy.ops.mpp.render_all_sequences())
    summary["jobs_submitted"] = render_runner.snapshot()["total"]
    summary["rows_after_start"] = len(group.render_list)
    summary["finished_in_time"] = pump(600)
    snapshot = render_runner.snapshot()
    summary["snapshot"] = {k: snapshot[k] for k in
                           ("state", "total", "done", "failed", "skipped", "fraction")}
    summary["rows_after_finish"] = len(group.render_list)
    summary["row_states"] = {item.sequence_id: item.state for item in group.render_list}
    summary["panel_report"] = group.last_report
    summary["panel_status"] = group.render_status
    summary["failures"] = render_runner.failures()

    # -- stop must be a no-op when nothing runs ---------------------------
    summary["stop_when_idle"] = sorted(bpy.ops.mpp.stop_render())

    # -- quick render: list is already populated and videos exist ---------
    group.render_overwrite = False
    summary["quick_render"] = sorted(bpy.ops.mpp.quick_render())
    summary["quick_jobs"] = render_runner.snapshot()["total"]
    pump(600)
    summary["quick_snapshot"] = {
        k: render_runner.snapshot()[k]
        for k in ("state", "done", "failed", "skipped", "total")
    }

    # -- what is actually on disk ----------------------------------------
    videos = []
    for current, _dirs, files in os.walk(output_root):
        videos.extend(
            os.path.join(current, name) for name in files
            if name.lower().endswith(".mp4")
        )
    summary["video_count"] = len(videos)
    summary["videos"] = sorted(os.path.basename(path) for path in videos)
    summary["stray_variants"] = sorted(
        os.path.basename(path) for path in videos
        if "_" in os.path.basename(path).rsplit(".", 1)[0]
        and os.path.basename(path).rsplit(".", 1)[0].split("_")[-1].count("-") == 1
    )
    summary["triples_complete"] = all(
        os.path.isfile(os.path.splitext(path)[0] + ".json")
        and os.path.isfile(os.path.splitext(path)[0] + "_camera.txt")
        for path in videos
    )
    summary["render_log_exists"] = os.path.isfile(os.path.join(output_root, "panel_render.log"))
    summary["scratch_report_left_behind"] = sorted(
        name for name in os.listdir(output_root) if name.startswith(".panel_render_")
    ) if os.path.isdir(output_root) else []

    registration.unregister_all()
    target = os.path.join(output_root, "panel_probe.json")
    os.makedirs(output_root, exist_ok=True)
    save_json_file(target, summary)

    print("\n================ PANEL RENDER PROBE ================")
    for key in (
        "template_path", "motion_count", "load_result", "listed", "jobs_submitted",
        "rows_after_start", "finished_in_time", "snapshot", "rows_after_finish",
        "row_states", "stop_when_idle", "quick_render", "quick_jobs",
        "quick_snapshot", "video_count", "videos", "stray_variants",
        "triples_complete", "render_log_exists", "scratch_report_left_behind",
    ):
        print(f"  {key}: {summary.get(key)}")
    print(f"  summary written to {target}")
    print("===================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
