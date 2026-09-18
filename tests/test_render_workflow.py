"""Local-render workflow tests: the panel's render section.

Covers both halves:

* **pure** cases for :class:`RenderRunner` bookkeeping -- job state transitions,
  child-process command construction, report parsing, cancellation and scratch
  cleanup;
* **Blender** cases for the operators behind the "Local render" panel, using
  short (9-frame) sequences so the suite stays fast.

    blender -b -P tests/test_render_workflow.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
if _PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, _PACKAGE_PARENT)

from blender_motion_pipeline.io.json_io import save_json_file  # noqa: E402
from blender_motion_pipeline.render import render_runner  # noqa: E402
from blender_motion_pipeline.tests.harness import Suite, equal, ok  # noqa: E402

WORK = os.path.join(tempfile.gettempdir(), "motion_pipeline_render_test")
BLEND_DIR = os.path.join(WORK, "scenes")
SEQUENCES = os.path.join(WORK, "sequences")
OUTPUT = os.path.join(WORK, "output")

#: Deliberately short so the whole suite renders in seconds.
FRAME_END = 8


def build_scene(path: str) -> str:
    """A minimal scene with one camera looking along +X."""
    import bpy

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 0, FRAME_END
    scene.render.fps = 24
    scene.render.resolution_x, scene.render.resolution_y = 160, 90
    scene.render.engine = "BLENDER_WORKBENCH"
    bpy.ops.mesh.primitive_plane_add(size=20.0, location=(0.0, 0.0, 0.0))
    bpy.ops.object.camera_add(location=(0.0, 0.0, 1.6),
                              rotation=(1.5707963, 0.0, -1.5707963))
    camera = bpy.context.active_object
    camera.data.lens = 35.0
    scene.camera = camera
    scene.frame_set(0)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=path, check_existing=False, compress=False)
    return path


def build_sequence(root: str, scene_name: str, motion: str, index: int, blend: str) -> str:
    """Create a generated-sequence folder (blend copy + sequence_config.json)."""
    directory = os.path.join(root, scene_name, motion, f"sequence_{index:06d}")
    os.makedirs(directory, exist_ok=True)
    target_blend = os.path.join(directory, f"sequence_{index:06d}.blend")
    shutil.copy2(blend, target_blend)
    save_json_file(os.path.join(directory, "sequence_config.json"), {
        "schema_version": 1,
        "sequence": {
            "sequence_id": f"sequence_{index:06d}",
            "scene_name": scene_name,
            "motion_name": motion,
            "camera_name": "Camera",
            "has_character": False,
            "source_blend": blend,
        },
        "frames": {
            "frame_start": 0,
            "frame_end": FRAME_END,
            "frame_count": FRAME_END + 1,
            "fps": 24,
        },
        "render": {"resolution": [160, 90], "fps": 24},
        "generator_version": "1.0.0",
    })
    return directory


def build_suite() -> Suite:
    suite = Suite("test_render_workflow")
    state: dict = {}

    def setup():
        import bpy

        # Keep the suite hermetic: the operators write the "remembered settings"
        # file, and a test run must never touch the user's real one.
        from blender_motion_pipeline.config import panel_state

        os.makedirs(WORK, exist_ok=True)
        os.environ[panel_state.ENV_OVERRIDE] = os.path.join(WORK, "panel_settings.json")
        shutil.rmtree(WORK, ignore_errors=True)
        os.makedirs(BLEND_DIR, exist_ok=True)
        state["blend"] = build_scene(os.path.join(BLEND_DIR, "room.blend"))
        sequences = [
            build_sequence(SEQUENCES, "room", "dolly_in_01_standard", 1, state["blend"]),
            build_sequence(SEQUENCES, "room", "dolly_in_01_standard", 2, state["blend"]),
            build_sequence(SEQUENCES, "room", "pan_right_01_standard", 1, state["blend"]),
        ]
        state["sequences"] = sequences
        bpy.ops.wm.read_factory_settings(use_empty=True)
        print(f"fixtures ready in {WORK} ({len(sequences)} short sequence(s))")

    suite.setup = setup

    def teardown():
        if os.environ.get("MP_KEEP_TEST_OUTPUT"):
            print(f"kept render test output in {WORK}")
            return
        shutil.rmtree(WORK, ignore_errors=True)

    suite.teardown = teardown

    # -- pure: discovery -------------------------------------------------
    @suite.case("discover_sequences finds generated sequences and their blend")
    def _():
        found = render_runner.discover_sequences(SEQUENCES)
        equal(len(found), 3)
        for entry in found:
            ok(entry["blend"] and os.path.isfile(entry["blend"]), entry)
            equal(entry["has_video"], False)
            equal(entry["frame_start"], 0)
            equal(entry["frame_end"], FRAME_END)
        ids = sorted(entry["sequence_id"] for entry in found)
        equal(ids, ["sequence_000001", "sequence_000001", "sequence_000002"])

        # Non-recursive must not descend into scene folders.
        flat = render_runner.discover_sequences(SEQUENCES, recursive=False)
        equal(len(flat), 0)
        deeper = render_runner.discover_sequences(os.path.join(SEQUENCES, "room"), recursive=False)
        equal(len(deeper), 0)

    @suite.case("the render script and Blender executable resolve")
    def _():
        script = render_runner.render_script_path()
        ok(os.path.isfile(script), script)
        equal(os.path.basename(script), "render_sequences.py")
        executable = render_runner.blender_executable()
        ok(os.path.isfile(executable), executable)
        # Must be Blender itself, never the bundled python interpreter.
        ok("python" not in os.path.basename(executable).lower(), executable)

    # -- pure: command construction --------------------------------------
    @suite.case("the child command carries the selected options")
    def _():
        runner = render_runner.RenderRunner(
            output_root=OUTPUT,
            options={
                "engine": "BLENDER_WORKBENCH",
                "resolution_x": 320,
                "resolution_y": 180,
                "fps": 12.5,
                "samples": 8,
                "video_format": "mp4",
                "codec": "H264",
                "crf": "HIGH",
                "trajectory_mode": "sampled",
                "trajectory_step": 4,
                "overwrite": True,
                "flat": False,
                "dry_run": False,
                "path_maps": ["E:\\a=D:\\b"],
            },
        )
        job = render_runner.RenderJob(
            sequence_dir=state["sequences"][0], sequence_id="sequence_000001",
            scene_name="room", motion_name="dolly_in_01_standard", blend=state["blend"],
        )
        command = runner._command(job)
        joined = " ".join(command)
        for needle in (
            "--input", state["sequences"][0], "--output-root", OUTPUT,
            "--resolution-x 320", "--resolution-y 180", "--fps 12.5", "--samples 8",
            "--engine BLENDER_WORKBENCH", "--video-format mp4", "--codec H264",
            "--crf HIGH", "--trajectory-mode sampled", "--trajectory-step 4",
            "--overwrite", "--path-map E:\\a=D:\\b", "--report",
        ):
            ok(needle in joined, f"{needle!r} missing from {joined}")
        # ``--input`` names one sequence, so --recursive must not be added.
        ok("--recursive" not in joined, joined)
        # The scratch report path must be unique, not the shared default name.
        ok(".panel_render_" in joined, joined)
        equal(command.count("-P"), 1)

    @suite.case("a parallel worker command keeps the script arguments after --")
    def _():
        # Regression: the worker command used to append ``-P <script> --`` at the
        # *end*, so Blender parsed ``--input`` as a file to open
        # ("unknown argument, loading as file: --input") and every worker
        # rendered nothing while still looking like it had started.
        from blender_motion_pipeline.render import render_sequences

        # ``parse_args`` strips the program name / everything before ``--``, so
        # build the flags through the parser directly like the other cases do.
        args = render_sequences.build_parser().parse_args(
            ["--workers", "3", "--engine", "BLENDER_EEVEE", "--samples", "32",
             "--resolution-x", "1280", "--resolution-y", "720",
             "--output-root", OUTPUT, "--keep-frames"]
        )
        command = render_sequences.worker_command(
            "blender.exe", "R:/render_sequences.py", ["S:/seq/a", "S:/seq/b"], args
        )
        ok("--" in command, command)
        separator = command.index("--")
        equal(command[separator - 2:separator + 1], ["-P", "R:/render_sequences.py", "--"])
        # Blender's own options must all sit before the separator...
        equal(command[:separator], ["blender.exe", "-b", "-P", "R:/render_sequences.py"])
        # ... and every script option after it.
        for needle in ("--input", "S:/seq/a", "S:/seq/b", "--output-root", OUTPUT,
                       "--engine", "BLENDER_EEVEE", "--samples", "32",
                       "--resolution-x", "1280", "--resolution-y", "720", "--keep-frames"):
            ok(needle in command[separator:], f"{needle!r} missing after --")
        ok("--input" not in command[:separator], command[:separator])
        # Parallel workers hand each process its own explicit list: no rescanning.
        ok("--recursive" not in command, command)
        ok("--workers" not in command, command)

    @suite.case("the output folder follows the documented tree, or is flat")
    def _():
        job = render_runner.RenderJob(
            sequence_dir=state["sequences"][0], sequence_id="sequence_000007",
            scene_name="room", motion_name="dolly_in_01_standard",
        )
        trees = render_runner.RenderRunner(output_root=OUTPUT, options={})
        equal(
            os.path.normcase(trees._output_dir(job)),
            os.path.normcase(os.path.join(OUTPUT, "room", "dolly_in_01_standard", "sequence_000007")),
        )
        flat = render_runner.RenderRunner(output_root=OUTPUT, options={"flat": True})
        equal(os.path.normcase(flat._output_dir(job)),
              os.path.normcase(os.path.join(OUTPUT, "sequence_000007")))

    # -- pure: state machine ---------------------------------------------
    @suite.case("job state transitions: done, failed and cancelled")
    def _():
        # done
        runner = render_runner.RenderRunner(output_root=OUTPUT, options={})
        runner.start([render_runner.RenderJob(
            sequence_dir=state["sequences"][0], sequence_id="sequence_000001",
            scene_name="room", motion_name="dolly_in_01_standard", blend=state["blend"],
        )])
        equal(runner.state, "running")
        equal(runner.fraction, 0.0)
        job = runner.jobs[0]
        os.makedirs(runner._output_dir(job), exist_ok=True)
        runner._collect(job, 0)
        equal(job.state, "done")
        equal(runner.done_count, 1)
        runner._finish("done")
        equal(runner.state, "done")
        equal(runner.fraction, 1.0)
        runner.close()
        ok(not os.path.isfile(runner.report_path), "the scratch report must be swept")

        # failed
        runner = render_runner.RenderRunner(output_root=OUTPUT, options={})
        runner.start([render_runner.RenderJob(
            sequence_dir=state["sequences"][1], sequence_id="sequence_000002",
            scene_name="room", motion_name="dolly_in_01_standard", blend=state["blend"],
        )])
        failed = runner.jobs[0]
        runner._collect(failed, 1)
        equal(failed.state, "failed")
        ok(failed.error, "a failure must carry a reason")
        equal(len(runner.failures()), 1)
        runner._finish("failed")
        equal(runner.state, "failed")
        runner.close()

        # cancel before anything is launched
        runner = render_runner.RenderRunner(output_root=OUTPUT, options={})
        runner.start([render_runner.RenderJob(
            sequence_dir=state["sequences"][2], sequence_id="sequence_000001",
            scene_name="room", motion_name="pan_right_01_standard", blend=state["blend"],
        )])
        runner.cancel("test")
        equal(runner.state, "cancelling")
        ok(runner.step(), "step() must finish the run once cancelling")
        equal(runner.state, "cancelled")

    @suite.case("_collect reads the run report and honours skip")
    def _():
        runner = render_runner.RenderRunner(output_root=OUTPUT, options={})
        runner.start([render_runner.RenderJob(
            sequence_dir=state["sequences"][0], sequence_id="sequence_000001",
            scene_name="room", motion_name="dolly_in_01_standard", blend=state["blend"],
        )])
        job = runner.jobs[0]
        runner._output_dir(job)
        save_json_file(runner.report_path, {
            "totals": {"sequences": 1, "rendered": 0, "failed": 0, "skipped": 1},
            "results": [{
                "sequence_id": "sequence_000001",
                "sequence_dir": job.sequence_dir,
                "ok": False,
                "skipped": True,
                "skip_reason": "video already exists",
                "files": {"video": "X:/already.mp4"},
            }],
        })
        runner._collect(job, 0)
        equal(job.state, "skipped")
        equal(runner.skipped_count, 1)
        runner.close()

        # A failure entry must surface its error message.
        runner = render_runner.RenderRunner(output_root=OUTPUT, options={})
        runner.start([render_runner.RenderJob(
            sequence_dir=state["sequences"][1], sequence_id="sequence_000002",
            scene_name="room", motion_name="dolly_in_01_standard", blend=state["blend"],
        )])
        job = runner.jobs[0]
        save_json_file(runner.report_path, {
            "results": [{
                "sequence_id": "sequence_000002",
                "sequence_dir": job.sequence_dir,
                "ok": False,
                "error": "cannot open the sequence blend",
            }],
        })
        runner._collect(job, 1)
        equal(job.state, "failed")
        equal(job.error, "cannot open the sequence blend")
        runner.close()

    @suite.case("the scratch report is never mistaken for a previous run's result")
    def _():
        first = render_runner.RenderRunner(output_root=OUTPUT, options={})
        first.start([render_runner.RenderJob(
            sequence_dir=state["sequences"][0], sequence_id="sequence_000001",
            scene_name="room", motion_name="dolly_in_01_standard", blend=state["blend"],
        )])
        save_json_file(first.report_path, {"results": [{"sequence_dir": "stale", "ok": True}]})
        stale_path = first.report_path
        ok(os.path.isfile(stale_path), stale_path)

        second = render_runner.RenderRunner(output_root=OUTPUT, options={})
        second.start([render_runner.RenderJob(
            sequence_dir=state["sequences"][1], sequence_id="sequence_000002",
            scene_name="room", motion_name="dolly_in_01_standard", blend=state["blend"],
        )])
        ok(second.report_path != stale_path, "each run needs its own report file")
        job = second.jobs[0]
        # No report for this run yet: the stale file must not be consulted.
        second._collect(job, 1)
        equal(job.state, "failed")
        equal(job.error, "Blender exited with code 1")
        second.close()
        try:
            os.remove(stale_path)
        except OSError:
            pass

    @suite.case("configure() sweeps the previous run's scratch report")
    def _():
        first = render_runner.configure(OUTPUT, {"engine": "BLENDER_WORKBENCH"})
        first.start([render_runner.RenderJob(
            sequence_dir=state["sequences"][0], sequence_id="sequence_000001",
            scene_name="room", motion_name="dolly_in_01_standard", blend=state["blend"],
        )])
        leftover = first.report_path
        save_json_file(leftover, {"results": []})
        ok(os.path.isfile(leftover), leftover)
        second = render_runner.configure(OUTPUT, {"engine": "BLENDER_WORKBENCH"})
        ok(second is not first, "configure must build a fresh runner")
        ok(not os.path.isfile(leftover), f"{leftover} should have been swept")
        ok(second.report_path != leftover, "the new run needs its own report path")
        second.close()

    # -- Blender: operators ----------------------------------------------
    @suite.case("Load sequences fills the list and pre-fills the save folder")
    def _():
        import bpy

        from blender_motion_pipeline import registration
        from blender_motion_pipeline import operators as render_operators

        registration.register_all()
        try:
            group = bpy.context.scene.mpp
            group.render_list.clear()
            group.render_input_root = SEQUENCES
            group.render_output_root = ""
            group.render_recursive = True
            result = bpy.ops.mpp.load_render_sequences()
            ok("FINISHED" in result, result)
            equal(len(group.render_list), 3)
            equal(group.render_list_index, 0)
            ok(group.render_output_root, "the save folder must be pre-filled")
            ok("3 sequence" in group.last_report, group.last_report)
            for item in group.render_list:
                equal(item.state, "pending")
                ok(item.blend and os.path.isfile(item.blend), item.blend)

            # A bad folder must report, not crash.
            group.render_input_root = os.path.join(WORK, "nope")
            result = bpy.ops.mpp.load_render_sequences()
            ok("CANCELLED" in result, result)
        finally:
            registration.unregister_all()

    @suite.case("a single sequence folder can be rendered with no list at all")
    def _():
        import bpy

        from blender_motion_pipeline import registration
        from blender_motion_pipeline import operators as render_operators

        registration.register_all()
        try:
            group = bpy.context.scene.mpp
            group.render_list.clear()
            group.render_input_root = ""
            group.render_sequence_dir = state["sequences"][0]
            group.render_output_root = os.path.join(OUTPUT, "single")
            group.render_overwrite = True
            group.render_engine_choice = "BLENDER_WORKBENCH"
            group.render_override_resolution = True
            group.render_res_x, group.render_res_y = 160, 90
            result = bpy.ops.mpp.load_render_sequences()
            ok("FINISHED" in result, result)
            equal(len(group.render_list), 1)
        finally:
            registration.unregister_all()

    @suite.case("Render all renders the list, updates rows and writes triples")
    def _():
        import bpy

        from blender_motion_pipeline import registration
        from blender_motion_pipeline import operators as render_operators

        registration.register_all()
        try:
            group = bpy.context.scene.mpp
            group.render_list.clear()
            group.render_sequence_dir = ""
            group.render_input_root = SEQUENCES
            group.render_output_root = os.path.join(OUTPUT, "all")
            group.render_recursive = True
            group.render_overwrite = True
            group.render_engine_choice = "BLENDER_WORKBENCH"
            group.render_override_resolution = True
            group.render_res_x, group.render_res_y = 160, 90
            group.render_write_png = False
            group.render_dry_run = False

            ok("FINISHED" in bpy.ops.mpp.load_render_sequences())
            equal(len(group.render_list), 3)

            result = bpy.ops.mpp.render_all_sequences()
            ok("FINISHED" in result, result)
            equal(render_runner.snapshot()["total"], 3)
            # The list must keep every row: an earlier bug truncated it to the
            # submitted subset, which made a second run render only one sequence.
            equal(len(group.render_list), 3)

            deadline = time.time() + 300
            while render_runner.is_running() and time.time() < deadline:
                render_operators._render_tick()
                time.sleep(0.2)
            snapshot = render_runner.snapshot()
            equal(snapshot["state"], "done", render_runner.failures())
            equal(snapshot["total"], 3)
            equal(snapshot["done"], 3, render_runner.failures())
            equal(snapshot["failed"], 0, render_runner.failures())
            equal(len(group.render_list), 3)
            for item in group.render_list:
                equal(item.state, "done", f"{item.label()}: {item.detail}")

            # Every sequence must have the full triple, with canonical names.
            output = os.path.join(OUTPUT, "all")
            videos = []
            for current, _dirs, files in os.walk(output):
                videos.extend(os.path.join(current, n) for n in files if n.endswith(".mp4"))
            equal(len(videos), 3)
            for video in videos:
                stem = os.path.splitext(video)[0]
                ok(os.path.isfile(stem + ".json"), stem + ".json")
                ok(os.path.isfile(stem + "_camera.txt"), stem + "_camera.txt")
                ok(os.path.getsize(video) > 0, video)
                equal(os.path.basename(video).count("-"), 0,
                      "Blender's frame-range suffix must have been normalised away")
            ok(os.path.isfile(os.path.join(output, "panel_render.log")), "render log")
            stray = [n for n in os.listdir(output) if n.startswith(".panel_render_")]
            equal(stray, [], "the scratch report must be cleaned up")

            # A rendered tree must be self-describing, so re-scanning it reports
            # the finished videos.  This is what makes "render an already
            # rendered folder" a no-op instead of a re-render.
            # (Loading it into the *render list* would be wrong: the render
            # output has no .blend, so those rows could never be re-rendered.)
            group.render_overwrite = False
            equal(group.render_list[0].state, "done")
            rescanned = render_runner.discover_sequences(output)
            equal(len(rescanned), 3)
            for entry in rescanned:
                equal(entry["has_video"], True, entry["sequence_dir"])
            equal(render_runner.configure(output, {}).output_root, output)
        finally:
            registration.unregister_all()

    @suite.case("the render panel mirrors per-sequence failures")
    def _():
        import bpy

        from blender_motion_pipeline import registration
        from blender_motion_pipeline import operators as render_operators

        registration.register_all()
        try:
            broken = os.path.join(WORK, "broken", "room", "motion", "sequence_000001")
            os.makedirs(broken, exist_ok=True)
            save_json_file(os.path.join(broken, "sequence_config.json"), {
                "sequence": {"sequence_id": "sequence_000001", "scene_name": "room",
                             "motion_name": "motion", "camera_name": "Camera"},
                "frames": {"frame_start": 0, "frame_end": 1, "fps": 24},
            })
            with open(os.path.join(broken, "sequence_000001.blend"), "w",
                      encoding="utf-8") as handle:
                handle.write("not a blend file")

            group = bpy.context.scene.mpp
            group.render_list.clear()
            group.render_sequence_dir = ""
            group.render_input_root = os.path.join(WORK, "broken")
            group.render_output_root = os.path.join(OUTPUT, "broken")
            group.render_overwrite = True
            group.render_engine_choice = "BLENDER_WORKBENCH"
            group.render_override_resolution = False
            ok("FINISHED" in bpy.ops.mpp.load_render_sequences())
            equal(len(group.render_list), 1)

            ok("FINISHED" in bpy.ops.mpp.render_all_sequences())
            deadline = time.time() + 300
            while render_runner.is_running() and time.time() < deadline:
                render_operators._render_tick()
                time.sleep(0.2)
            snapshot = render_runner.snapshot()
            equal(snapshot["failed"], 1, snapshot)
            equal(group.render_list[0].state, "failed")
            ok(group.render_list[0].detail, "the row must show why it failed")
            failures = render_runner.failures()
            equal(len(failures), 1)
            ok(failures[0]["error"], failures)
            ok("render:" in group.last_report, group.last_report)
        finally:
            registration.unregister_all()

    @suite.case("Stop render is safe when idle and cancels when running")
    def _():
        import bpy

        from blender_motion_pipeline import registration
        from blender_motion_pipeline import operators as render_operators

        registration.register_all()
        try:
            group = bpy.context.scene.mpp
            ok("FINISHED" in bpy.ops.mpp.stop_render(),
               "stopping an idle render must be a no-op, not an error")

            # Start a real render, then cancel it before it can finish.
            group.render_list.clear()
            group.render_input_root = SEQUENCES
            group.render_output_root = os.path.join(OUTPUT, "cancel")
            group.render_overwrite = True
            group.render_engine_choice = "BLENDER_WORKBENCH"
            group.render_override_resolution = True
            group.render_res_x, group.render_res_y = 1920, 1080   # slow on purpose
            ok("FINISHED" in bpy.ops.mpp.load_render_sequences())
            ok("FINISHED" in bpy.ops.mpp.render_all_sequences())
            ok(render_runner.is_running(), "the render must have started")
            equal(group.render_status, "running")

            ok("FINISHED" in bpy.ops.mpp.stop_render())
            equal(group.render_status, "cancelling")
            deadline = time.time() + 120
            while render_runner.is_running() and time.time() < deadline:
                render_operators._render_tick()
                time.sleep(0.2)
            equal(render_runner.snapshot()["state"], "cancelled")
            equal(group.render_status, "cancelled")
        finally:
            registration.unregister_all()

    @suite.case("the quick render button loads the list when it is empty")
    def _():
        import bpy

        from blender_motion_pipeline import registration
        from blender_motion_pipeline import operators as render_operators

        registration.register_all()
        try:
            group = bpy.context.scene.mpp
            group.render_list.clear()
            group.render_input_root = SEQUENCES
            group.render_output_root = os.path.join(OUTPUT, "quick")
            group.render_overwrite = True
            group.render_engine_choice = "BLENDER_WORKBENCH"
            group.render_override_resolution = True
            group.render_res_x, group.render_res_y = 160, 90

            equal(len(group.render_list), 0)
            result = bpy.ops.mpp.quick_render()
            ok("FINISHED" in result, result)
            equal(len(group.render_list), 3, "the quick button must load the list itself")
            equal(render_runner.snapshot()["total"], 3)
            deadline = time.time() + 300
            while render_runner.is_running() and time.time() < deadline:
                render_operators._render_tick()
                time.sleep(0.2)
            equal(render_runner.snapshot()["done"], 3,
                   render_runner.failures())

            # With nothing configured it must explain what is missing.
            group.render_list.clear()
            group.render_input_root = ""
            group.render_sequence_dir = ""
            result = bpy.ops.mpp.quick_render()
            ok("CANCELLED" in result, result)
            ok("Sequence root" in group.last_report, group.last_report)
        finally:
            registration.unregister_all()

    @suite.case("dry-run validates the render without writing a video")
    def _():
        import bpy

        from blender_motion_pipeline import registration
        from blender_motion_pipeline import operators as render_operators

        registration.register_all()
        try:
            group = bpy.context.scene.mpp
            group.render_list.clear()
            group.render_input_root = SEQUENCES
            group.render_output_root = os.path.join(OUTPUT, "dry")
            group.render_sequence_dir = ""
            group.render_dry_run = True
            group.render_overwrite = True
            group.render_engine_choice = "BLENDER_WORKBENCH"
            ok("FINISHED" in bpy.ops.mpp.load_render_sequences())
            ok("FINISHED" in bpy.ops.mpp.render_all_sequences())
            deadline = time.time() + 300
            while render_runner.is_running() and time.time() < deadline:
                render_operators._render_tick()
                time.sleep(0.2)
            equal(render_runner.snapshot()["failed"], 0,
                   render_runner.failures())
            videos = []
            for current, _dirs, files in os.walk(os.path.join(OUTPUT, "dry")):
                videos.extend(n for n in files if n.endswith(".mp4"))
            equal(videos, [], "a dry run must not produce a video")
        finally:
            registration.unregister_all()

    @suite.case("an interrupted render's leftover does not count as a finished video")
    def _():
        # Measured case: killing Blender mid-render left a 0-byte
        # ``sequence_000001_0000-0080.mp4`` (Blender's own name; no moov atom, so it
        # will not play) and nothing else in the folder.  The panel listed the
        # sequence as ``skipped / video already exists`` and ``--skip-existing``
        # refused to render it, so it could never be produced from the UI.
        import bpy

        from blender_motion_pipeline.core.sequence_manager import SequenceManager
        from blender_motion_pipeline.render import render_sequences as rs

        sequence_dir = state["sequences"][0]
        output = os.path.join(OUTPUT, "interrupted")
        shutil.rmtree(output, ignore_errors=True)
        rendered_dir = os.path.join(output, "room", "dolly_in_01_standard", "sequence_000001")
        os.makedirs(rendered_dir, exist_ok=True)
        # What a render that was killed after copying the config leaves behind.
        shutil.copy2(os.path.join(sequence_dir, "sequence_config.json"),
                     os.path.join(rendered_dir, "sequence_config.json"))
        leftover = os.path.join(rendered_dir, "sequence_000001_0000-0080.mp4")
        with open(leftover, "wb"):
            pass                                  # 0 bytes, exactly like a killed render
        equal(os.path.getsize(leftover), 0)

        # Discovery must call it unfinished, not done.
        info = [item for item in SequenceManager(output).find_sequences()
                if item.sequence_id == "sequence_000001"]
        equal(len(info), 1)
        equal(info[0].has_video(), False, "a 0-byte video is not a finished render")
        equal(info[0].has_partial_video(), True)
        ok(any("interrupted render" in problem for problem in info[0].problems),
           info[0].problems)

        # The render list must therefore offer it as work to do, not as skipped.
        # ``--output-root`` recreates the scene/motion/sequence tree, which is what
        # the panel passes and where the leftover sits.
        args = rs.build_parser().parse_args([
            "--input", sequence_dir, "--output-root", output,
            "--engine", "BLENDER_WORKBENCH", "--resolution-x", "160", "--resolution-y", "90",
            "--log-level", "ERROR",
        ])
        jobs = rs.resolve_sequences(args)
        equal(len(jobs), 1)
        selected = rs.select_jobs(jobs, args)
        equal(selected[0].get("skip_reason", ""), "",
              "an unfinished video must not trigger --skip-existing")
        equal(selected[0].get("partial_video"), leftover)

        # A dry run must not touch it...
        dry_args = rs.build_parser().parse_args([
            "--input", sequence_dir, "--output-root", output, "--dry-run", "--log-level", "ERROR",
        ])
        rs.render_sequence(rs.resolve_sequences(dry_args)[0], dry_args, mappings=[])
        ok(os.path.isfile(leftover), "a dry run must not delete anything")

        # ... and a real render must replace it with a finished video.
        rs.render_sequence(selected[0], args, mappings=[])
        finished = os.path.join(rendered_dir, "sequence_000001.mp4")
        ok(os.path.isfile(finished), finished)
        ok(os.path.getsize(finished) > 0, "the finished video must not be empty")
        equal(os.path.isfile(leftover), False, "the unfinished leftover must be gone")
        info = [item for item in SequenceManager(output).find_sequences()
                if item.sequence_id == "sequence_000001"]
        equal(info[0].has_video(), True)
        equal(info[0].has_partial_video(), False)

    @suite.case("Clear empties the render list")
    def _():
        import bpy

        from blender_motion_pipeline import registration
        from blender_motion_pipeline import operators as render_operators

        registration.register_all()
        try:
            group = bpy.context.scene.mpp
            group.render_input_root = SEQUENCES
            bpy.ops.mpp.load_render_sequences()
            ok(len(group.render_list) > 0)
            ok("FINISHED" in bpy.ops.mpp.clear_render_list())
            equal(len(group.render_list), 0)
            equal(group.render_list_index, -1)
        finally:
            registration.unregister_all()

    return suite


def main() -> int:
    return build_suite().run()


if __name__ == "__main__":
    sys.exit(main())
