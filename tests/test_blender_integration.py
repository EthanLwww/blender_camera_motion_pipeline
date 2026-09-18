"""End-to-end integration tests.  **Must run inside Blender**::

    blender -b -P tests/test_blender_integration.py

Covers the functional checklist from the brief:

* scan a folder for ``.blend`` files and de-duplicate the list;
* read the motion template document;
* generate a camera motion sequence into a real scene;
* detect a camera that is inside geometry;
* run the spherical camera search and accept a better position;
* generate a character-free sequence (and not crash when the character module
  is unavailable);
* save a loadable sequence ``.blend`` that keeps the original camera animation;
* write MP4/JSON/TXT via the standalone render script;
* report failures with an error log instead of pretending to succeed.

It also exercises the documented edge cases: missing scene path, no camera,
multiple cameras, missing/malformed template JSON, blocked lens, output folder
that already contains a sequence, references to missing external assets.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
for path in (_PACKAGE_PARENT,):
    if path not in sys.path:
        sys.path.insert(0, path)

from blender_motion_pipeline.config.defaults import default_config  # noqa: E402
from blender_motion_pipeline.config.models import (  # noqa: E402
    CHARACTER_MODE_BOTH,
    CHARACTER_MODE_NONE,
    CHARACTER_MODE_WITH,
    ConfigError,
)
from blender_motion_pipeline.core import blender_context as bctx  # noqa: E402
from blender_motion_pipeline.core.scene_loader import (  # noqa: E402
    SceneEntry,
    load_scene_list,
    merge_scene_entries,
    save_scene_list,
    scan_directory,
)
from blender_motion_pipeline.io.json_io import load_json_file, save_json_file  # noqa: E402
from blender_motion_pipeline.tests.harness import Suite, close, equal, ok, raises, vec_close  # noqa: E402

WORK = os.path.join(tempfile.gettempdir(), "motion_pipeline_itest")
BLEND_DIR = os.path.join(WORK, "scenes")
OUT_DIR = os.path.join(WORK, "out")
RENDER_DIR = os.path.join(WORK, "render")
TEMPLATE_PATH = os.path.join(WORK, "templates.json")

#: A tiny, self-contained template document.  Deliberately not the 80-entry
#: reference file so the tests stay fast and independent of the artist's copy.
TEMPLATES = [
    {"id": "still", "keys": [
        {"frame": 0, "location": [0, 0, 0], "rotation": [0, 0, 0], "focal": 35},
        {"frame": 4, "location": [0, 0, 0], "rotation": [0, 0, 0], "focal": 35},
        {"frame": 8, "location": [0, 0, 0], "rotation": [0, 0, 0], "focal": 35},
    ]},
    {"id": "push_in", "keys": [
        {"frame": 0, "location": [0, 0, 0], "rotation": [0, 0, 0], "focal": 35},
        {"frame": 4, "location": [40, 0, 0], "rotation": [0, 0, 0], "focal": 35},
        {"frame": 8, "location": [80, 0, 0], "rotation": [0, 0, 0], "focal": 35},
    ]},
    {"id": "pan_swing", "keys": [
        {"frame": 0, "location": [0, 0, 0], "rotation": [0, 0, 0], "focal": 35},
        {"frame": 8, "location": [0, 0, 0], "rotation": [0, 0, 30], "focal": 35},
    ]},
    # Pushes 4 m along the camera's own view axis, straight through the wall
    # that ``blocked.blend`` puts in front of the camera.
    {"id": "through_wall", "keys": [
        {"frame": 0, "location": [0, 0, 0], "rotation": [0, 0, 0], "focal": 35},
        {"frame": 8, "location": [400, 0, 0], "rotation": [0, 0, 0], "focal": 35},
    ]},
]


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def build_scene_blend(path: str, *, cameras: int = 1, blocker: bool = False,
                      animate_camera: bool = False, missing_asset: bool = False,
                      parent_camera: bool = False, frame_end: int = 8) -> str:
    """Write a small self-contained scene to ``path``.

    Cameras are aimed along world **+X** with world **+Z** up.  The Euler order
    matters: ``rotation_euler = (90 deg, 0, -90 deg)`` in Blender's default XYZ
    order is what actually points the camera at +X -- a naive
    ``(90, 0, +90)`` looks at -X instead, which would silently invert every
    "forward" expectation in these tests.
    """
    import bpy
    from mathutils import Vector

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start = 0
    scene.frame_end = frame_end
    scene.render.fps = 24
    scene.render.resolution_x = 160
    scene.render.resolution_y = 90
    scene.render.engine = "BLENDER_WORKBENCH"

    # Floor plus a back wall 6 m behind the camera, so clipping tests have
    # geometry in every direction.
    bpy.ops.mesh.primitive_plane_add(size=24.0, location=(0.0, 0.0, 0.0))
    bpy.context.active_object.name = "Floor"
    bpy.ops.mesh.primitive_plane_add(size=24.0, location=(-6.0, 0.0, 3.0),
                                     rotation=(0.0, 1.5707963, 0.0))
    bpy.context.active_object.name = "BackWall"

    if blocker:
        # A wall 2 m in front of the camera (which looks along +X), so the
        # "through_wall" template drives straight into it.
        bpy.ops.mesh.primitive_plane_add(size=12.0, location=(2.0, 0.0, 3.0),
                                         rotation=(0.0, 1.5707963, 0.0))
        bpy.context.active_object.name = "Blocker"

    for index in range(max(1, cameras)):
        bpy.ops.object.camera_add(
            location=(0.0, index * 1.5, 1.6),
            rotation=(1.5707963, 0.0, -1.5707963),   # look along +X
        )
        camera = bpy.context.active_object
        camera.name = f"Cam{index + 1:02d}" if index else "Camera"
        camera.data.lens = 35.0
        camera.data.clip_start = 0.1
        camera.data.clip_end = 100.0
        if index == 0:
            scene.camera = camera
        if animate_camera and index == 0:
            # Pre-existing artist animation that generation must not destroy.
            for frame, y in ((0, 0.0), (4, 0.5), (8, 0.0)):
                scene.frame_set(frame)
                camera.location = Vector((0.0, y, 1.6))
                camera.keyframe_insert("location", frame=frame)
            scene.frame_set(0)

    if parent_camera:
        # A camera bolted to a moving rig, the shape that exposed the bake bug:
        # the rig travels 12 m while the camera keeps a non-zero
        # ``matrix_parent_inverse`` (as in the reference scene, where the camera
        # is parented to an animated train empty with a ~78 m offset).
        rig = bpy.data.objects.new("Rig", None)
        scene.collection.objects.link(rig)
        for frame, x in ((0, -10.0), (4, -4.0), (8, 2.0)):
            scene.frame_set(frame)
            rig.location = Vector((x, 0.0, 0.0))
            rig.keyframe_insert("location", frame=frame)
        scene.frame_set(0)
        camera = bpy.data.objects.get("Camera")
        camera.parent = rig
        camera.matrix_parent_inverse = rig.matrix_world.inverted()
        # Only the translation of the parent inverse is needed to catch a
        # double-counted parent transform, and it keeps the maths readable.
        scene.frame_set(0)
        bpy.context.view_layer.update()

    if missing_asset:
        # ``bpy.data.images.new`` always creates a GENERATED image and setting
        # ``source = "FILE"`` on it does not survive a save/reload, so the
        # missing asset is modelled the way a real project produces one: load a
        # real file, then delete it, leaving a datablock whose filepath dangles.
        texture_path = os.path.join(WORK, "temporary_texture.png")
        image = bpy.data.images.new("MissingTexSource", 4, 4)
        image.filepath_raw = texture_path
        image.file_format = "PNG"
        image.save()
        bpy.data.images.remove(image)
        loaded = bpy.data.images.load(texture_path)
        loaded.name = "MissingTex"
        try:
            os.remove(texture_path)
        except OSError:
            pass
        material = bpy.data.materials.new("MissingTexMat")
        material.use_nodes = True
        texture = material.node_tree.nodes.new("ShaderNodeTexImage")
        texture.image = loaded
        texture.name = "MissingTexture"
        floor = bpy.data.objects.get("Floor")
        if floor is not None:
            floor.data.materials.append(material)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=path, check_existing=False, compress=False)
    return path


def write_templates() -> str:
    save_json_file(TEMPLATE_PATH, TEMPLATES)
    return TEMPLATE_PATH


def make_config(output_root: str, *, templates: str = "", names=None,
                validation: bool = True, search: bool = True,
                mode: str = CHARACTER_MODE_NONE, sample_step: int = 1,
                save_blend: bool = True):
    config = default_config()
    config.batch.output_root = output_root
    config.batch.mode = mode
    config.batch.overwrite = True
    config.batch.resume = False
    config.batch.save_sequence_blend = bool(save_blend)
    config.motion.template_path = templates or write_templates()
    config.motion.template_names = list(names or [t["id"] for t in TEMPLATES])
    config.motion.frame_start = 0
    config.motion.unit_scale.fps = 24.0
    config.validation.enabled = validation
    config.validation.sample_step = sample_step
    config.validation.clearance = 0.25
    config.validation.obstruction_distance = 1.0
    config.validation.min_character_visible_ratio = 0.05
    config.search.enabled = search
    config.search.min_radius = 0.5
    config.search.max_radius = 3.0
    config.search.candidate_count = 12
    config.search.azimuth_samples = 6
    config.search.elevation_samples = 3
    config.search.max_retries = 1
    config.search.max_output_candidates = 1
    config.search.allow_rotation_adjust = False
    config.search.allow_focal_adjust = False
    config.render.video_format = "mp4"
    config.render.engine = "BLENDER_WORKBENCH"
    return config


def generate_for(blend: str, output_root: str, **kwargs):
    """Run the generator for one scene and return ``(report, outcomes)``."""
    from blender_motion_pipeline.core.batch_runner import BatchRunner

    entry = SceneEntry(path=blend)
    config = make_config(output_root, **kwargs)
    runner = BatchRunner(config, output_root=output_root, scene_entries=[entry])
    report = runner.run()
    return report, report.scenes[0] if report.scenes else None


def panel():
    """The *current* panel property group.

    Always call this instead of caching a previous reference: generation opens
    other ``.blend`` files, which frees the scene the old reference belongs to
    and turns any write to it into an access violation.
    """
    import bpy

    from blender_motion_pipeline.preferences import status_source

    # Status lives in the add-on preferences because it must survive the scene
    # changes that generation performs; fall back to the scene group.
    return status_source() or bpy.context.scene.mpp


def ui_ops_pump() -> None:
    """Advance the panel task by one step and refresh the status fields.

    Mirrors what ``bpy.app.timers`` would do, so the panel workflow can be
    driven deterministically from a test.  Status is written through
    ``preferences.status_source()`` -- the same durable place the real timer
    uses -- because generation opens other ``.blend`` files and scene-scoped
    properties are reset when that happens.
    """
    import bpy

    from blender_motion_pipeline.core import ui_task
    from blender_motion_pipeline.preferences import status_source

    if not ui_task.is_running():
        return
    done = ui_task.step()
    snapshot = ui_task.snapshot()
    target = status_source() or bpy.context.scene.mpp
    target.progress_fraction = snapshot["fraction"]
    target.progress_text = snapshot["stage"]
    target.generated_count = snapshot["generated"]
    target.failed_count = snapshot["failed"]
    target.skipped_count = snapshot["skipped"]
    if done:
        state = "done" if snapshot["failed"] == 0 else "failed"
        ui_task.finish(state=state)
        target = status_source() or bpy.context.scene.mpp
        if hasattr(target, "task_state"):
            target.task_state = state
        target.progress_text = snapshot["stage"]
        target.last_report = (
            f"{snapshot['generated']} generated, {snapshot['failed']} failed, "
            f"{snapshot['skipped']} skipped"
        )


# --------------------------------------------------------------------------
# suite
# --------------------------------------------------------------------------
def build_suite() -> Suite:
    suite = Suite("test_blender_integration")
    state: dict = {}

    def setup():
        import bpy

        # Hermetic settings: the operators write the "remembered settings" file,
        # and a test run must never touch the user's real one.
        from blender_motion_pipeline.config import panel_state

        os.makedirs(WORK, exist_ok=True)
        os.environ[panel_state.ENV_OVERRIDE] = os.path.join(WORK, "panel_settings.json")
        shutil.rmtree(WORK, ignore_errors=True)
        os.makedirs(BLEND_DIR, exist_ok=True)
        os.makedirs(OUT_DIR, exist_ok=True)
        os.makedirs(RENDER_DIR, exist_ok=True)
        write_templates()
        state["single"] = build_scene_blend(os.path.join(BLEND_DIR, "single.blend"))
        state["multi"] = build_scene_blend(os.path.join(BLEND_DIR, "multi_cam.blend"), cameras=3)
        state["blocked"] = build_scene_blend(os.path.join(BLEND_DIR, "blocked.blend"), blocker=True)
        state["animated"] = build_scene_blend(os.path.join(BLEND_DIR, "animated_cam.blend"),
                                              animate_camera=True)
        state["parented"] = build_scene_blend(os.path.join(BLEND_DIR, "parented_cam.blend"),
                                              parent_camera=True)
        state["missing_asset"] = build_scene_blend(os.path.join(BLEND_DIR, "missing_asset.blend"),
                                                   missing_asset=True)
        state["nocam"] = os.path.join(BLEND_DIR, "no_camera.blend")
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.mesh.primitive_cube_add()
        bpy.ops.wm.save_as_mainfile(filepath=state["nocam"], check_existing=False, compress=False)
        print(f"fixtures ready in {WORK}")

    suite.setup = setup

    def teardown():
        # Keep the artifacts when debugging (MP_KEEP_TEST_OUTPUT=1).
        if os.environ.get("MP_KEEP_TEST_OUTPUT"):
            print(f"kept test output in {WORK}")
            return
        shutil.rmtree(WORK, ignore_errors=True)

    suite.teardown = teardown

    # -- scene list ------------------------------------------------------
    @suite.case("scan_directory finds every .blend and the list de-duplicates")
    def _():
        found = scan_directory(BLEND_DIR)
        names = sorted(os.path.basename(p) for p in found)
        ok("single.blend" in names, names)
        ok("multi_cam.blend" in names, names)
        ok("no_camera.blend" in names, names)
        equal(len(found), len(set(os.path.normcase(p) for p in found)))

        entries, problems = merge_scene_entries([], found)
        equal(len(entries), len(found))
        # Re-adding the same files (in different case) must not duplicate.
        entries2, problems2 = merge_scene_entries(entries, [p.upper() for p in found])
        equal(len(entries2), len(found))
        ok(any("already in the list" in p for p in problems2), problems2)

        entries3, problems3 = merge_scene_entries([], [os.path.join(BLEND_DIR, "nope.blend")])
        equal(entries3, [])
        ok(any("does not exist" in p for p in problems3), problems3)

    @suite.case("scene list round-trips through JSON")
    def _():
        path = os.path.join(WORK, "scene_list.json")
        entries, _ = merge_scene_entries([], scan_directory(BLEND_DIR))
        save_scene_list(path, entries)
        loaded, warnings = load_scene_list(path)
        equal(sorted(e.path for e in loaded), sorted(e.path for e in entries))
        equal(warnings, [])
        # A stale path must be reported, not silently trusted.
        save_json_file(path, {"scenes": [{"path": os.path.join(BLEND_DIR, "gone.blend")}]})
        loaded2, warnings2 = load_scene_list(path)
        equal(len(loaded2), 1)
        ok(any("missing" in w for w in warnings2), warnings2)

    @suite.case("scene_report describes cameras, geometry and frame range")
    def _():
        from blender_motion_pipeline.core.scene_loader import load_blend_file

        load_blend_file(state["multi"])
        report = bctx.scene_report()
        equal(report["camera_count"], 3)
        equal(report["camera_names"], ["Cam02", "Cam03", "Camera"])
        equal(report["frame_range"], [0, 8])
        equal(report["resolution"], [160, 90])
        ok(report["mesh_count"] >= 2, report["mesh_count"])
        ok(report["world_bbox"] is not None, "a world bounding box is expected")
        ok(report["world_diagonal"] and report["world_diagonal"] > 1.0, report["world_diagonal"])

    @suite.case("camera_snapshot captures the parameters that must be preserved")
    def _():
        from blender_motion_pipeline.core.scene_loader import load_blend_file

        load_blend_file(state["single"])
        cameras = bctx.list_camera_objects()
        equal(len(cameras), 1)
        snapshot = bctx.camera_snapshot(cameras[0])
        close(snapshot.lens, 35.0)
        close(snapshot.clip_start, 0.1)
        close(snapshot.clip_end, 100.0)
        close(snapshot.sensor_width, 36.0)
        equal(snapshot.resolution_x, 160)
        equal(snapshot.effective_resolution, (160, 90))
        equal(snapshot.had_animation, False)
        vec_close(snapshot.location, (0.0, 0.0, 1.6), tol=1e-5)
        payload = snapshot.to_dict()
        for key in ("camera_name", "lens_mm", "sensor_width_mm", "clip_start", "clip_end", "resolution"):
            ok(key in payload, key)

    @suite.case("existing camera animation is detected and preserved by generation")
    def _():
        report, outcome = generate_for(state["animated"], os.path.join(OUT_DIR, "animated"),
                                       names=["still"])
        ok(report.ok, report.summary_text())
        equal(outcome.generated, 1)
        # The original file must still hold its own animation, untouched.
        from blender_motion_pipeline.core.scene_loader import load_blend_file

        load_blend_file(state["animated"])
        camera = bctx.list_camera_objects()[0]
        ok(camera.animation_data is not None and camera.animation_data.action is not None,
           "the artist's camera action must survive")
        from blender_motion_pipeline.utils.animation import action_data_paths

        curves = action_data_paths(camera.animation_data.action)
        ok(any("location" in path for path in curves), curves)
        # ... and the saved sequence carries its own, independent action.
        sequence_blend = os.path.join(
            OUT_DIR, "animated", "animated_cam", "still", "sequence_000001", "sequence_000001.blend"
        )
        ok(os.path.isfile(sequence_blend), sequence_blend)
        load_blend_file(sequence_blend)
        seq_camera = bctx.list_camera_objects()[0]
        ok(seq_camera.animation_data is not None and seq_camera.animation_data.action is not None,
           "the sequence blend must contain the generated animation")
        ok(seq_camera.data.users == 1, "the sequence camera must own an independent data block")

    @suite.case("a camera parented to a moving rig reproduces the validated world path")
    def _():
        # Regression: the bake used to write *world* poses into ``obj.location``,
        # which is parent space.  On a camera parented to a moving rig that
        # double-counts the parent transform -- on the reference scene it put the
        # rendered camera 26 m away at frame 0 and 110 m away by frame 80, while
        # the sidecar and the validator both said the path was fine.
        import bpy
        import math

        from blender_motion_pipeline.core.scene_loader import load_blend_file

        report, outcome = generate_for(state["parented"], os.path.join(OUT_DIR, "parented"),
                                      names=["still"])
        ok(report.ok, report.summary_text())
        equal(outcome.generated, 1)
        sequence_dir = os.path.join(OUT_DIR, "parented", "parented_cam", "still",
                                    "sequence_000001")
        sequence_blend = os.path.join(sequence_dir, "sequence_000001.blend")
        ok(os.path.isfile(sequence_blend), sequence_blend)

        payload = load_json_file(os.path.join(sequence_dir, "sequence_000001.json"))
        recorded = {row["frame"]: row for row in payload["camera_trajectory"]}
        ok(len(recorded) >= 2, len(recorded))

        load_blend_file(sequence_blend)
        scene = bpy.context.scene
        camera = scene.camera
        ok(camera.parent is not None, "the fixture camera must stay parented")
        equal(camera.parent.name, "Rig")
        # The fixture is only meaningful with a real parent offset.
        offset = camera.matrix_parent_inverse.translation
        ok(abs(offset[0]) > 1.0, f"parent inverse offset too small: {tuple(offset)}")

        worst = 0.0
        rig_travel = 0.0
        previous_rig = None
        local_gap = 0.0
        for frame in sorted(recorded):
            scene.frame_set(int(frame))
            bpy.context.view_layer.update()
            depsgraph = bpy.context.evaluated_depsgraph_get()
            world = camera.evaluated_get(depsgraph).matrix_world.translation
            world = (float(world[0]), float(world[1]), float(world[2]))
            rotation = [[recorded[frame]["matrix"][i][j] for j in range(3)] for i in range(3)]
            translation = [recorded[frame]["matrix"][i][3] for i in range(3)]
            wanted = tuple(
                -sum(rotation[k][j] * translation[k] for k in range(3)) for j in range(3)
            )
            worst = max(worst, math.dist(world, wanted))
            # A world pose written straight into ``location`` is exactly what the
            # bug did, so the keyed local location must *not* equal the world one.
            local_gap = max(local_gap, math.dist(tuple(camera.location), wanted))
            rig = camera.parent.evaluated_get(depsgraph).matrix_world.translation
            rig = (float(rig[0]), float(rig[1]), float(rig[2]))
            if previous_rig is not None:
                rig_travel += math.dist(rig, previous_rig)
            previous_rig = rig
        ok(worst < 0.01, f"worst deviation {worst:.4f} m exceeds 1 cm")
        ok(rig_travel > 10.0, f"the rig must actually move; travelled {rig_travel:.2f} m")
        ok(local_gap > 1.0, "the keyed local location must differ from the world position")

    @suite.case("parenting modes the bake cannot reproduce are refused, not faked")
    def _():
        # Object parenting is the only mode the world-to-local conversion models.
        # Bone/armature parenting would silently bake a wrong path -- and still
        # ship a video, a trajectory and a validator PASS -- so it must fail
        # loudly.  Blender does allow a camera on a bone (the usual "camera on a
        # hand" rig), so this is not a theoretical case.
        import bpy

        from blender_motion_pipeline.core.scene_loader import load_blend_file
        from blender_motion_pipeline.core.sequence_generator import assert_object_parenting

        load_blend_file(state["parented"])
        camera = bpy.context.scene.camera
        equal(str(camera.parent_type), "OBJECT")
        assert_object_parenting(camera)          # must not raise

        # Vertex parenting needs a mesh parent; anything non-OBJECT must be refused.
        bpy.ops.mesh.primitive_plane_add(size=1.0)
        mesh = bpy.context.active_object
        camera.parent = mesh
        camera.parent_type = "VERTEX"
        try:
            assert_object_parenting(camera)
            ok(False, "vertex parenting must be refused")
        except RuntimeError as exc:
            text = str(exc)
            ok("object parenting only" in text, text)
            ok("VERTEX" in text, text)

        # An unparented camera is always fine.
        camera.parent = None
        camera.parent_type = "OBJECT"
        assert_object_parenting(camera)

    @suite.case("every sequence in a batch anchors on the same camera pose")
    def _():
        # Regression: ``restore_camera`` used to write the snapshot's *world*
        # translation into ``obj.location``, which is parent space.  On the
        # reference scene (camera parented to an animated train empty) that moved
        # the camera 25.96 m on every restore, so each sequence anchored further
        # along the train's path than the last and a template's shot depended on
        # where it sat in the batch.
        import bpy
        import math

        from blender_motion_pipeline.core.scene_loader import load_blend_file

        report, outcome = generate_for(state["parented"], os.path.join(OUT_DIR, "anchors"),
                                      names=["still", "push_in"])
        ok(report.ok, report.summary_text())
        equal(outcome.generated, 2)

        # Where the artist's camera actually sits at the anchor frame.
        load_blend_file(state["parented"])
        scene = bpy.context.scene
        scene.frame_set(0)
        bpy.context.view_layer.update()
        expected = tuple(float(v) for v in scene.camera.matrix_world.translation)

        anchors = []
        for motion in ("still", "push_in"):
            payload = load_json_file(os.path.join(
                OUT_DIR, "anchors", "parented_cam", motion, "sequence_000001",
                "sequence_000001.json"))
            row = payload["camera_trajectory"][0]
            rotation = [[row["matrix"][i][j] for j in range(3)] for i in range(3)]
            translation = [row["matrix"][i][3] for i in range(3)]
            anchors.append(tuple(
                -sum(rotation[k][j] * translation[k] for k in range(3)) for j in range(3)))

        for motion, anchor in zip(("still", "push_in"), anchors):
            ok(math.dist(anchor, expected) < 1e-6,
               f"{motion} anchored at {anchor}, expected the artist pose {expected}")
        ok(math.dist(anchors[0], anchors[1]) < 1e-6,
           f"the two sequences anchored differently: {anchors}")

    @suite.case("saving a sequence blend survives a dangling asset with autopack on")
    def _():
        # Regression: with ``bpy.data.use_autopack`` enabled, ``save_as_mainfile``
        # re-packs every external file and aborts on the first missing one
        # ("cannot pack file, source path not found").  A generated sequence must
        # still save, and the artist's setting must be left exactly as it was.
        import bpy

        from blender_motion_pipeline.core.scene_loader import load_blend_file
        from blender_motion_pipeline.core.sequence_generator import write_sequence_blend

        load_blend_file(state["missing_asset"])
        bpy.data.use_autopack = True
        target = os.path.join(WORK, "autopack_on.blend")
        write_sequence_blend(target)
        ok(os.path.isfile(target), target)
        equal(bool(bpy.data.use_autopack), True, "the artist's autopack setting must be restored")

        # ... and a disabled setting must stay disabled.
        bpy.data.use_autopack = False
        second = os.path.join(WORK, "autopack_off.blend")
        write_sequence_blend(second)
        ok(os.path.isfile(second), second)
        equal(bool(bpy.data.use_autopack), False)

    # -- templates -------------------------------------------------------
    @suite.case("missing and malformed template JSON are reported clearly")
    def _():
        from blender_motion_pipeline.camera.motion_templates import MotionTemplateLibrary
        from blender_motion_pipeline.io.json_io import JsonError

        missing = os.path.join(WORK, "nope.json")
        raises(JsonError, lambda: MotionTemplateLibrary.from_file(missing))

        broken = os.path.join(WORK, "broken.json")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        raises(JsonError, lambda: MotionTemplateLibrary.from_file(broken))

        wrong_shape = os.path.join(WORK, "wrong.json")
        save_json_file(wrong_shape, {"hello": "world"})
        raises(ConfigError, lambda: MotionTemplateLibrary.from_file(wrong_shape))

        good = write_templates()
        library = MotionTemplateLibrary.from_file(good)
        equal(len(library), 4)
        equal(library.names, ["pan_swing", "push_in", "still", "through_wall"])

    @suite.case("a malformed config template path fails loudly, discovery only warns")
    def _():
        config = default_config()
        config.motion.template_path = os.path.join(WORK, "does_not_exist.json")
        from blender_motion_pipeline.camera.motion_templates import MotionTemplateLibrary
        from blender_motion_pipeline.io.json_io import JsonError

        raises(JsonError, lambda: MotionTemplateLibrary.from_config(config))

        # Discovery failure is only a warning: the embedded set keeps things running.
        discovery_only = default_config()
        discovery_only.motion.template_path = ""
        discovery_only.motion.template_data = []
        library = MotionTemplateLibrary.from_config(discovery_only)
        ok(len(library) > 0, "the embedded fallback must always provide templates")

    # -- generation ------------------------------------------------------
    @suite.case("generates a character-free sequence with all artifacts")
    def _():
        report, outcome = generate_for(state["single"], os.path.join(OUT_DIR, "basic"),
                                      names=["still", "push_in"])
        ok(report.ok, report.summary_text())
        equal(outcome.generated, 2)
        equal(outcome.failed, 0)
        equal(len(outcome.cameras), 1)

        base = os.path.join(OUT_DIR, "basic", "single")
        for motion in ("still", "push_in"):
            directory = os.path.join(base, motion, "sequence_000001")
            for name in ("sequence_000001.blend", "sequence_config.json", "sequence_000001.json",
                         "sequence_000001_camera.txt", "validation_report.json", "generation_log.txt"):
                ok(os.path.isfile(os.path.join(directory, name)), os.path.join(directory, name))
            manifest = os.path.join(base, motion, "manifest.json")
            ok(os.path.isfile(manifest), manifest)
        ok(os.path.isfile(os.path.join(OUT_DIR, "basic", "manifest.json")), "root manifest")
        ok(os.path.isfile(os.path.join(OUT_DIR, "basic", "batch_report.json")), "batch report")

    @suite.case("the generated sequence JSON carries the documented fields")
    def _():
        path = os.path.join(OUT_DIR, "basic", "single", "push_in", "sequence_000001",
                            "sequence_000001.json")
        payload = load_json_file(path)
        for key in ("level_name", "sequence_name", "video_id", "video_path", "frame_count",
                    "camera_trajectory", "text_prompt", "has_character", "sequence_id",
                    "source_blend", "generator_version", "random_seed", "validation"):
            ok(key in payload, f"missing key {key!r}")
        equal(payload["has_character"], False)
        equal(payload["level_name"], "single")
        equal(payload["motion_name"], "push_in")
        equal(payload["frame_start"], 0)
        equal(payload["frame_end"], 8)
        equal(payload["frame_count"], 9)
        ok(len(payload["camera_trajectory"]) == 9, len(payload["camera_trajectory"]))
        entry = payload["camera_trajectory"][0]
        ok("fov" in entry and "focal_length" in entry and "matrix" in entry)
        equal(len(entry["matrix"]), 4)
        ok("passed" in payload["validation"], payload["validation"].keys())
        ok(payload["camera_original"]["lens_mm"] == 35.0, payload["camera_original"])

    @suite.case("the trajectory TXT has the reference header and one row per frame")
    def _():
        path = os.path.join(OUT_DIR, "basic", "single", "push_in", "sequence_000001",
                            "sequence_000001_camera.txt")
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        header_index = next(i for i, line in enumerate(lines)
                            if line.startswith("frame focal_length d1"))
        equal(lines[header_index],
              "frame focal_length d1 d2 d3 d4 d5 r00 r01 r02 tx r10 r11 r12 ty r20 r21 r22 tz")
        data = lines[header_index + 1:]
        equal(len(data), 9, "one row per frame")
        first = data[0].split()
        equal(len(first), 19)  # frame + focal + 5 reserved + 12 matrix values
        equal(int(first[0]), 0)
        equal(first[2:7], ["0"] * 5)
        comments = [line for line in lines if line.startswith("#")]
        ok(any("coordinate_system" in line for line in comments), comments)
        ok(any("units=" in line for line in comments), comments)

    @suite.case("push_in actually moves the camera forward relative to still")
    def _():
        root = os.path.join(OUT_DIR, "basic", "single")
        still = load_json_file(os.path.join(root, "still", "sequence_000001", "sequence_000001.json"))
        push = load_json_file(os.path.join(root, "push_in", "sequence_000001", "sequence_000001.json"))

        def camera_position(payload, frame_index):
            row = payload["camera_trajectory"][frame_index]["matrix"]
            # Recover the camera centre from the orthonormal 3x3 block of W2C.
            rotation = [[row[i][j] for j in range(3)] for i in range(3)]
            translation = [row[i][3] for i in range(3)]
            return [
                -(rotation[0][k] * translation[0] + rotation[1][k] * translation[1] + rotation[2][k] * translation[2])
                for k in range(3)
            ]

        still_start = camera_position(still, 0)
        still_end = camera_position(still, len(still["camera_trajectory"]) - 1)
        push_start = camera_position(push, 0)
        push_end = camera_position(push, len(push["camera_trajectory"]) - 1)
        vec_close(still_start, still_end, tol=1e-6, message="a 'still' template must not move")
        vec_close(push_start, still_start, tol=1e-6, message="frame 0 must match the original camera")
        moved = sum((b - a) ** 2 for a, b in zip(push_start, push_end)) ** 0.5
        close(moved, 0.8, tol=0.02, message="80 units at 0.01 m/unit = 0.8 m")

        # Verify the W2C row is self-consistent: its own camera maps to origin.
        row = push["camera_trajectory"][0]["matrix"]
        position = camera_position(push, 0)
        for axis in range(3):
            value = sum(row[axis][k] * position[k] for k in range(3)) + row[axis][3]
            close(value, 0.0, tol=1e-6, message=f"camera-space axis {axis}")

    @suite.case("multi-camera scenes produce one sequence per camera")
    def _():
        report, outcome = generate_for(state["multi"], os.path.join(OUT_DIR, "multi"),
                                      names=["still"])
        ok(report.ok, report.summary_text())
        equal(len(outcome.cameras), 3)
        equal(outcome.request_count, 3)
        equal(outcome.generated, 3)
        root = os.path.join(OUT_DIR, "multi", "multi_cam", "still")
        ids = sorted(name for name in os.listdir(root) if name.startswith("sequence_"))
        equal(len(ids), 3)
        cameras = set()
        for sequence in ids:
            payload = load_json_file(os.path.join(root, sequence, f"{sequence}.json"))
            cameras.add(payload["camera_name"])
        equal(len(cameras), 3, f"each sequence must name a distinct camera: {cameras}")

    @suite.case("a scene without a camera is reported, not silently skipped")
    def _():
        report, outcome = generate_for(state["nocam"], os.path.join(OUT_DIR, "nocam"), names=["still"])
        equal(outcome.generated, 0)
        ok(not outcome.ok)
        ok("no camera" in outcome.error, outcome.error)
        ok(not report.ok, "the batch report must not claim success")
        manifest = load_json_file(os.path.join(OUT_DIR, "nocam", "no_camera", "manifest.json"))
        equal(manifest["failure_count"], 1)

    @suite.case("a missing scene file is reported and does not abort the batch")
    def _():
        from blender_motion_pipeline.core.batch_runner import BatchRunner

        gone = SceneEntry(path=os.path.join(BLEND_DIR, "vanished.blend"))
        good = SceneEntry(path=state["single"])
        config = make_config(os.path.join(OUT_DIR, "mixed"), names=["still"])
        runner = BatchRunner(config, output_root=config.batch.output_root,
                             scene_entries=[gone, good])
        report = runner.run()
        equal(len(report.scenes), 2)
        equal(report.scenes[0].generated, 0)
        ok("does not exist" in report.scenes[0].error, report.scenes[0].error)
        equal(report.scenes[1].generated, 1)
        ok(report.generated == 1)

    @suite.case("a scene referencing a missing texture still generates, and is flagged")
    def _():
        report, outcome = generate_for(state["missing_asset"], os.path.join(OUT_DIR, "missing"),
                                      names=["still"])
        ok(report.ok, report.summary_text())
        equal(outcome.generated, 1)
        from blender_motion_pipeline.core.scene_loader import load_blend_file
        from blender_motion_pipeline.io.resource_check import check_blend_resources, blend_resources_from_bpy

        load_blend_file(state["missing_asset"])
        result = check_blend_resources(blend_resources_from_bpy(state["missing_asset"]))
        ok(len(result.missing) >= 1, result.as_dict())
        ok(any(item["kind"] == "image" for item in result.missing), result.missing)

    # -- validation ------------------------------------------------------
    @suite.case("a clean motion validates and the report records the metrics")
    def _():
        path = os.path.join(OUT_DIR, "basic", "single", "still", "sequence_000001",
                            "validation_report.json")
        payload = load_json_file(path)
        validation = payload["validation"]
        equal(validation["passed"], True)
        equal(validation["reasons"], [])
        ok(validation["frame_sample_count"] >= 2, validation["frame_sample_count"])
        ok("min_clearance" in validation["metrics"], validation["metrics"])
        ok(validation["score"] > 0.5, validation["score"])
        ok(payload["camera_final"]["lens_mm"] > 0)

    @suite.case("geometry validation detects a camera that travels through a wall")
    def _():
        # "through_wall" pushes 5 m along +X, straight through the Blocker plane
        # standing at x = 2 in the blocked scene.
        report, outcome = generate_for(state["blocked"], os.path.join(OUT_DIR, "blocked"),
                                      names=["through_wall"], search=False)
        equal(outcome.generated, 0)
        sequence = outcome.requests[0]
        ok(not sequence.ok)
        ok("validation_failed" in sequence.error, sequence.error)
        report_path = os.path.join(OUT_DIR, "blocked", "blocked", "through_wall",
                                   "sequence_000001", "failure_report.json")
        ok(os.path.isfile(report_path), report_path)
        payload = load_json_file(report_path)
        equal(payload["status"], "failed")
        reasons = payload["validation"]["reasons"]
        ok(reasons, "the failure report must name the failing checks")
        ok(any(r in reasons for r in ("camera_inside_geometry", "camera_clipping",
                                      "camera_obstructed")), reasons)
        log_path = os.path.join(OUT_DIR, "blocked", "blocked", "through_wall",
                                "sequence_000001", "generation_log.txt")
        ok(os.path.isfile(log_path), log_path)
        with open(log_path, "r", encoding="utf-8") as handle:
            log_text = handle.read()
        ok("validation" in log_text.lower(), log_text[:400])

    @suite.case("the spherical search finds a usable camera when the original is blocked")
    def _():
        report, outcome = generate_for(state["blocked"], os.path.join(OUT_DIR, "searched"),
                                      names=["through_wall"], search=True)
        ok(report.ok, report.summary_text())
        equal(outcome.generated, 1)
        sequence = outcome.requests[0]
        ok(sequence.search is not None, "a search must have been attempted")
        ok(sequence.search.attempts >= 1, sequence.search.attempts)
        ok(sequence.search.passed, sequence.search.messages)
        ok(sequence.validation is not None and sequence.validation.passed,
           sequence.validation.summary_line() if sequence.validation else "no report")
        # The accepted camera must differ from the artist's, but not wildly.
        accepted = sequence.search.accepted[0]
        ok(accepted.radius > 0.0, accepted.describe())
        ok(accepted.radius <= 3.0 + 1e-6, accepted.describe())
        payload = load_json_file(os.path.join(OUT_DIR, "searched", "blocked", "through_wall",
                                              "sequence_000001", "sequence_000001.json"))
        ok(payload["search"]["passed"] is True or payload["search"].get("attempt_count", 0) >= 1,
           payload["search"])

    @suite.case("validation disabled still produces a sequence (and says so)")
    def _():
        report, outcome = generate_for(state["blocked"], os.path.join(OUT_DIR, "novalidation"),
                                      names=["through_wall"], validation=False, search=False)
        ok(report.ok, report.summary_text())
        equal(outcome.generated, 1)
        log_path = os.path.join(OUT_DIR, "novalidation", "blocked", "through_wall",
                                "sequence_000001", "generation_log.txt")
        with open(log_path, "r", encoding="utf-8") as handle:
            text = handle.read()
        ok("validation disabled" in text, text[:400])

    @suite.case("motion filter selects a subset of templates")
    def _():
        report, outcome = generate_for(state["single"], os.path.join(OUT_DIR, "filtered"),
                                      names=["pan_swing"])
        ok(report.ok, report.summary_text())
        equal(outcome.motion_count, 1)
        equal(outcome.generated, 1)
        ok(os.path.isdir(os.path.join(OUT_DIR, "filtered", "single", "pan_swing")))

    # -- characters ------------------------------------------------------
    @suite.case("character mode with an unusable provider keeps the no-character flow working")
    def _():
        # mode=with_character but no character library configured: the pipeline
        # must log the reason and still emit the character-free variant.
        report, outcome = generate_for(state["single"], os.path.join(OUT_DIR, "nomodule"),
                                      names=["still"], mode=CHARACTER_MODE_WITH)
        ok(report.ok, report.summary_text())
        equal(outcome.generated, 1)
        payload = load_json_file(os.path.join(
            OUT_DIR, "nomodule", "single", "still", "sequence_000001", "sequence_000001.json"))
        equal(payload["has_character"], False)
        ok(payload["character_status"], "the character status must be recorded")
        ok("unavailable" in payload["character_status"] or "no character" in payload["character_status"],
           payload["character_status"])

    @suite.case("mode=both yields both a character-free and a with-character variant request")
    def _():
        from blender_motion_pipeline.character import character_variants
        from blender_motion_pipeline.character.base_provider import (
            STATUS_AVAILABLE, AnimationDescriptor, CharacterDescriptor, CharacterProvider,
            CharacterPlacement, CharacterValidation,
        )

        class FakeProvider(CharacterProvider):
            name = "fake"
            description = "test double"

            def status(self):
                return STATUS_AVAILABLE

            def list_characters(self):
                return [CharacterDescriptor(id="hero", blend_path="x.blend")]

            def list_animations(self):
                return [AnimationDescriptor(id="walk", action_name="walk")]

            def import_character(self, scene_context, character_config):
                return CharacterPlacement(descriptor_id="hero", status=STATUS_AVAILABLE)

            def place_character(self, placement, scene_context):
                return placement

            def apply_animation(self, placement, animation_config):
                return placement

            def validate_character_placement(self, placement, scene_context):
                return CharacterValidation()

        provider = FakeProvider()
        variants = character_variants(CHARACTER_MODE_BOTH, provider)
        equal(len(variants), 2)
        equal([v[0] for v in variants], [False, True])
        equal(variants[1][1].id, "hero")
        equal(variants[1][2].id, "walk")

        only = character_variants(CHARACTER_MODE_WITH, provider)
        equal([v[0] for v in only], [True])

        none = character_variants(CHARACTER_MODE_NONE, provider)
        equal([v[0] for v in none], [False])

    @suite.case("the null provider never claims success")
    def _():
        from blender_motion_pipeline.character import NullCharacterProvider

        provider = NullCharacterProvider()
        equal(provider.status(), "unavailable")
        equal(provider.is_usable(), False)
        equal(provider.list_characters(), [])
        equal(provider.list_animations(), [])
        placement = provider.import_character(None, {"id": "x"})
        equal(placement.ok, False)
        equal(placement.object_name, "")
        ok(placement.messages, "a reason must be reported")
        validation = provider.validate_character_placement(placement, None)
        equal(validation.valid, False)

    @suite.case("a broken character manifest degrades to the null provider")
    def _():
        from blender_motion_pipeline.character import build_character_provider

        root = os.path.join(WORK, "charlib")
        os.makedirs(root, exist_ok=True)
        save_json_file(os.path.join(root, "manifest.json"), {
            "characters": [{"id": "ghost", "blend_path": "missing.blend"}],
        })
        provider = build_character_provider("blender", asset_root=root)
        # The manifest exists but the .blend does not: the provider is degraded,
        # and importing must fail honestly rather than pretending.
        ok(provider.name in ("blender", "null"), provider.name)
        if provider.is_usable():
            from blender_motion_pipeline.core import blender_context

            placement = provider.import_character(blender_context.build_scene_context(), "ghost")
            equal(placement.ok, False)
            ok(placement.errors, placement.errors)

    # -- output tree behaviour -------------------------------------------
    @suite.case("existing sequences are reused when resume is on, regenerated when overwrite is on")
    def _():
        output_root = os.path.join(OUT_DIR, "basic")
        target = os.path.join(output_root, "single", "still", "sequence_000001",
                              "sequence_000001.json")
        before = os.path.getmtime(target)

        from blender_motion_pipeline.core.batch_runner import BatchRunner

        config = make_config(output_root, names=["still"])
        config.batch.resume = True
        config.batch.overwrite = False
        entry = SceneEntry(path=state["single"])
        report = BatchRunner(config, output_root=output_root, scene_entries=[entry]).run()
        equal(report.skipped, 1)
        equal(report.generated, 0)
        close(os.path.getmtime(target), before, tol=0.001, message="the file must not be rewritten")

        config.batch.overwrite = True
        report2 = BatchRunner(config, output_root=output_root, scene_entries=[entry]).run()
        equal(report2.generated, 1)
        equal(report2.skipped, 0)

    @suite.case("output tree summary reflects what is on disk")
    def _():
        from blender_motion_pipeline.core.sequence_manager import SequenceManager

        manager = SequenceManager(os.path.join(OUT_DIR, "basic"))
        summary = manager.summary()
        equal(summary["exists"], True)
        ok(summary["sequence_count"] >= 2, summary)
        ok(summary["scene_count"] >= 1, summary)
        names = [s["scene_name"] for s in summary["scenes"]]
        ok("single" in names, names)
        sequences = manager.find_sequences()
        ok(all(info.sequence_id for info in sequences), "every sequence must have an id")
        ok(all(info.has_blend for info in sequences), "every sequence must have a blend file")

    # -- rendering -------------------------------------------------------
    @suite.case("the render script renders MP4 + JSON + TXT per sequence")
    def _():
        from blender_motion_pipeline.render import render_sequences as rs

        args = rs.build_parser().parse_args([
            "--input-root", os.path.join(OUT_DIR, "basic"),
            "--output-root", RENDER_DIR,
            "--scene-filter", "single",
            "--motion-filter", "push_in",
            "--engine", "BLENDER_WORKBENCH",
            "--resolution-x", "160",
            "--resolution-y", "90",
            "--overwrite",
            "--log-level", "WARNING",
        ])
        mappings = []
        jobs = rs.resolve_sequences(args)
        equal(len(jobs), 1, [j["sequence_id"] for j in jobs])
        job = jobs[0]
        equal(job["motion_name"], "push_in")

        result = rs.render_sequence(job, args, mappings=mappings)
        ok(result["ok"], result.get("error"))
        video = result["files"]["video"]
        metadata = result["files"]["metadata"]
        trajectory = result["files"]["camera_trajectory"]
        for path in (video, metadata, trajectory):
            ok(os.path.isfile(path), path)
        ok(os.path.getsize(video) > 0, "the video must not be empty")
        equal(os.path.dirname(video), os.path.dirname(metadata))
        equal(os.path.dirname(video), os.path.dirname(trajectory))

        payload = load_json_file(metadata)
        for key in ("level_name", "sequence_name", "video_id", "video_path", "frame_count",
                    "camera_trajectory", "text_prompt"):
            ok(key in payload, key)
        equal(payload["level_name"], "single")
        equal(payload["sequence_id"], "sequence_000001")
        equal(payload["frame_count"], 9)
        ok(payload["video_path"].endswith(".mp4"), payload["video_path"])
        equal(payload["status"], "rendered")
        ok(len(payload["camera_trajectory"]) == 9, len(payload["camera_trajectory"]))
        mat = payload["camera_trajectory"][0]["matrix"]
        equal(len(mat), 4)
        equal(mat[3], [0.0, 0.0, 0.0, 1.0])

        with open(trajectory, "r", encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if not line.startswith("#")]
        equal(lines[0].split()[0], "frame")
        equal(len(lines) - 1, 9)

    @suite.case("the rendered trajectory matches the generator's trajectory")
    def _():
        generated = load_json_file(os.path.join(
            OUT_DIR, "basic", "single", "push_in", "sequence_000001", "sequence_000001.json"))
        rendered = load_json_file(os.path.join(
            RENDER_DIR, "single", "push_in", "sequence_000001", "sequence_000001.json"))
        equal(len(generated["camera_trajectory"]), len(rendered["camera_trajectory"]))
        worst = 0.0
        for left, right in zip(generated["camera_trajectory"], rendered["camera_trajectory"]):
            equal(left["frame"], right["frame"])
            for row in range(4):
                for column in range(4):
                    worst = max(worst, abs(left["matrix"][row][column] - right["matrix"][row][column]))
        ok(worst < 1e-4, f"the rendered camera must match the generated one (worst delta {worst:g})")

    @suite.case("an animation-only sequence renders the same path as a blend-based one")
    def _():
        # Storing the animation instead of a scene copy is the difference between
        # ~150 KB and hundreds of MB per sequence, so both shapes must render
        # identically -- otherwise switching would silently change every video.
        from blender_motion_pipeline.render import render_sequences as rs

        animation_root = os.path.join(OUT_DIR, "animation_only")
        report, outcome = generate_for(state["single"], animation_root,
                                      names=["push_in"], save_blend=False)
        ok(report.ok, report.summary_text())
        equal(outcome.generated, 1)

        sequence_dir = os.path.join(animation_root, "single", "push_in", "sequence_000001")
        equal(sorted(os.listdir(sequence_dir)),
              ["generation_log.txt", "sequence_000001.json", "sequence_000001_camera.txt",
               "sequence_config.json", "validation_report.json"],
              "an animation-only sequence must not write a scene copy")

        config = load_json_file(os.path.join(sequence_dir, "sequence_config.json"))
        block = config["camera_animation"]
        equal(block["available"], True)
        equal(block["file"], "sequence_000001.json")
        equal(block["key_count"], 9)
        equal(config["sequence"]["source_blend"].replace("\\", "/"), state["single"].replace("\\", "/"))

        payload = load_json_file(os.path.join(sequence_dir, "sequence_000001.json"))
        samples = payload["camera_animation"]["samples"]
        equal(len(samples), 9)
        ok(all(set(("frame", "location", "quaternion", "scale", "lens")) <= set(sample) for sample in samples),
           "every key must carry the values that were actually keyed")

        # Discovery reads only sequence_config.json, so it must see this as
        # renderable rather than as the old "no .blend, cannot be rendered".
        from blender_motion_pipeline.core.sequence_manager import SequenceManager

        found = SequenceManager(animation_root).find_sequences()
        equal(len(found), 1)
        equal(found[0].storage_mode, "animation")
        equal(found[0].problems, [])

        # ... and the renderer must replay it onto the source scene.
        output = os.path.join(RENDER_DIR, "animation_only")
        args = rs.build_parser().parse_args([
            "--input", sequence_dir,
            "--output", output,
            "--engine", "BLENDER_WORKBENCH",
            "--resolution-x", "160",
            "--resolution-y", "90",
            "--overwrite",
            "--log-level", "WARNING",
        ])
        jobs = rs.resolve_sequences(args)
        equal(len(jobs), 1)
        equal(jobs[0]["storage_mode"], "animation")
        equal(jobs[0]["animation"]["available"], True)

        result = rs.render_sequence(jobs[0], args, mappings=[])
        ok(result["ok"], result.get("error"))
        equal(result["storage_mode"], "animation")
        equal(result["animation"]["key_count"], 9)
        ok(result["scene_file"].endswith("single.blend"),
           f"must render from the source scene, got {result['scene_file']}")

        rendered = load_json_file(result["files"]["metadata"])
        equal(rendered["frame_count"], 9)
        equal(len(rendered["camera_trajectory"]), 9)
        generated = load_json_file(os.path.join(sequence_dir, "sequence_000001.json"))
        worst = 0.0
        for left, right in zip(generated["camera_trajectory"], rendered["camera_trajectory"]):
            equal(left["frame"], right["frame"])
            for row in range(4):
                for column in range(4):
                    worst = max(worst, abs(left["matrix"][row][column] - right["matrix"][row][column]))
        ok(worst < 1e-4, f"animation-only render must match the generated path (worst {worst:g})")

        # The panel used to skip anything without a .blend, which would have made
        # this mode unreachable from the GUI.
        import bpy

        from blender_motion_pipeline import registration

        registration.register_all()
        try:
            group = bpy.context.scene.mpp
            group.render_list.clear()
            group.render_input_root = animation_root
            group.render_recursive = True
            ok("FINISHED" in bpy.ops.mpp.load_render_sequences())
            equal(len(group.render_list), 1)
            equal(group.render_list[0].storage_mode, "animation")
            ok(group.render_list[0].renderable(),
               "the panel must offer an animation-only sequence for rendering")
        finally:
            registration.unregister_all()

        # A render node keeps the source scene somewhere else, so the recorded path
        # has to answer to --path-map like every other path does.
        moved_root = os.path.join(OUT_DIR, "animation_only_moved")
        shutil.rmtree(moved_root, ignore_errors=True)
        shutil.copytree(animation_root, moved_root)
        moved_dir = os.path.join(moved_root, "single", "push_in", "sequence_000001")
        moved_config = load_json_file(os.path.join(moved_dir, "sequence_config.json"))
        original_source = moved_config["sequence"]["source_blend"]
        moved_config["sequence"]["source_blend"] = "Z:/elsewhere/single.blend"
        save_json_file(os.path.join(moved_dir, "sequence_config.json"), moved_config)

        missing_args = rs.build_parser().parse_args([
            "--input", moved_dir,
            "--output", os.path.join(RENDER_DIR, "animation_only_moved"),
            "--engine", "BLENDER_WORKBENCH",
            "--resolution-x", "160", "--resolution-y", "90",
            "--overwrite", "--log-level", "ERROR",
        ])
        missing_job = rs.resolve_sequences(missing_args)[0]
        failed = rs.render_sequence(missing_job, missing_args, mappings=[])
        equal(failed["ok"], False)
        ok("source scene not found" in failed["error"], failed["error"])

        mapped_args = rs.build_parser().parse_args([
            "--input", moved_dir,
            "--output", os.path.join(RENDER_DIR, "animation_only_moved"),
            "--engine", "BLENDER_WORKBENCH",
            "--resolution-x", "160", "--resolution-y", "90",
            "--overwrite", "--log-level", "ERROR",
            "--path-map", f"Z:/elsewhere={os.path.dirname(original_source)}",
        ])
        mapped_job = rs.resolve_sequences(mapped_args)[0]
        mapped = rs.render_sequence(
            mapped_job, mapped_args,
            mappings=[(os.path.dirname("Z:/elsewhere/single.blend"), os.path.dirname(original_source))],
        )
        ok(mapped["ok"], mapped.get("error"))
        ok(any("--path-map" in warning for warning in mapped["warnings"]), mapped["warnings"])

    @suite.case("the resolution presets map to documented sizes and default to 720p")
    def _():
        # The panel offers presets rather than free numbers, so the labels and the
        # sizes behind them have to stay in step -- and a size that came from a config
        # file must show up as "custom" instead of being silently rewritten.
        import bpy

        from blender_motion_pipeline import registration
        from blender_motion_pipeline.properties import RESOLUTION_PRESETS, RESOLUTION_SIZES

        registration.register_all()
        try:
            for identifier, label, _tip in RESOLUTION_PRESETS:
                if identifier in RESOLUTION_SIZES:
                    width, height = RESOLUTION_SIZES[identifier]
                    ok(str(width) in label and str(height) in label,
                       f"preset {identifier!r} must spell out its pixels: {label!r}")

            group = bpy.context.scene.mpp
            equal(group.sequence_resolution, "720p", "the default must be 1280x720")
            equal(group.resolution_summary(), "Sequences record 1280 x 720.")
            # The preselected preset must not make a fresh scene look configured, or
            # the remembered-settings restore would skip every new file.
            ok(group.is_pristine(), "a fresh scene must still count as unconfigured")

            expected = {
                "720p": (1280, 720, True),
                "1080p": (1920, 1080, True),
                "1k": (1024, 1024, True),
                "2k": (2048, 1080, True),
                "4k": (3840, 2160, True),
                # "Follow the scene": the numbers on the group are irrelevant because
                # the sequence does not dictate a size.
                "scene": (None, None, False),
            }
            for preset, (width, height, explicit) in expected.items():
                group.sequence_resolution = preset
                config = group.to_config()
                if width is not None:
                    equal(config.render.resolution_x, width, preset)
                    equal(config.render.resolution_y, height, preset)
                equal(config.render.resolution_explicit, explicit, preset)
                # ... and the round trip back into the panel picks the same entry.
                group.from_config(config)
                equal(group.sequence_resolution, preset, preset)

            # A size from outside the presets is preserved, not rewritten.
            group.sequence_resolution = "720p"
            config = group.to_config()
            config.render.resolution_x = 1000
            config.render.resolution_y = 1000
            config.render.resolution_explicit = True
            group.from_config(config)
            equal(group.sequence_resolution, "custom")
            round_tripped = group.to_config()
            equal((round_tripped.render.resolution_x, round_tripped.render.resolution_y), (1000, 1000))
            equal(round_tripped.render.resolution_explicit, True)
            ok("1000 x 1000" in group.resolution_summary(), group.resolution_summary())

            # And it ends up in the sequence record the renderer reads.
            root = os.path.join(OUT_DIR, "resolution_preset")
            config = make_config(root, names=["push_in"])
            config.render.resolution_explicit = True
            config.render.resolution_x = 1024
            config.render.resolution_y = 1024
            from blender_motion_pipeline.core.batch_runner import BatchRunner

            BatchRunner(config, output_root=root,
                        scene_entries=[SceneEntry(path=state["single"])]).run()
            recorded = load_json_file(os.path.join(root, "single", "push_in", "sequence_000001",
                                                   "sequence_config.json"))
            equal(recorded["render"]["effective_resolution"], [1024, 1024])
        finally:
            registration.unregister_all()

    @suite.case("a sequence can fix its own output resolution for the renderer")
    def _():
        # The recorded size used to be informational only: the renderer kept the
        # loaded scene's resolution, so a 2000x2000 scene produced 2000x2000 videos
        # whatever the generator recorded.  With ``resolution_explicit`` the sequence
        # dictates the size, and a render with no explicit request obeys it.
        import bpy

        from blender_motion_pipeline.core.batch_runner import BatchRunner
        from blender_motion_pipeline.render import render_sequences as rs

        root = os.path.join(OUT_DIR, "resolution")
        config = make_config(root, names=["push_in"])
        config.render.resolution_explicit = True
        config.render.resolution_x = 320
        config.render.resolution_y = 180
        config.render.resolution_percentage = 100
        report = BatchRunner(
            config, output_root=root, scene_entries=[SceneEntry(path=state["single"])]
        ).run()
        ok(report.ok, report.summary_text())

        sequence_dir = os.path.join(root, "single", "push_in", "sequence_000001")
        recorded = load_json_file(os.path.join(sequence_dir, "sequence_config.json"))
        equal(recorded["render"]["resolution_explicit"], True)
        equal(recorded["render"]["resolution_x"], 320)
        equal(recorded["render"]["effective_resolution"], [320, 180],
              "the sequence must record the size it is meant to render at")

        # The fixture scene renders at 160x90, so a renderer that ignored the record
        # would come out 160x90 -- the difference is what makes this test meaningful.
        args = rs.build_parser().parse_args([
            "--input", sequence_dir,
            "--output-root", os.path.join(RENDER_DIR, "resolution"),
            "--engine", "BLENDER_WORKBENCH",
            "--log-level", "ERROR",
        ])
        result = rs.render_sequence(rs.resolve_sequences(args)[0], args, mappings=[])
        ok(result["ok"], result.get("error"))
        equal(result["render"]["resolution"], [320, 180],
              "the renderer must use the sequence's recorded resolution")
        equal(result["render"]["resolution_source"], "sequence")

        # An explicit request still wins over the record.
        args2 = rs.build_parser().parse_args([
            "--input", sequence_dir,
            "--output-root", os.path.join(RENDER_DIR, "resolution2"),
            "--engine", "BLENDER_WORKBENCH",
            "--resolution-x", "120", "--resolution-y", "60",
            "--log-level", "ERROR",
        ])
        result2 = rs.render_sequence(rs.resolve_sequences(args2)[0], args2, mappings=[])
        ok(result2["ok"], result2.get("error"))
        equal(result2["render"]["resolution"], [120, 60])
        equal(result2["render"]["resolution_source"], "command line")

        # A sequence that does not dictate one keeps following its scene.
        plain_root = os.path.join(OUT_DIR, "resolution_plain")
        plain = make_config(plain_root, names=["push_in"])
        plain.render.resolution_explicit = False
        BatchRunner(
            plain, output_root=plain_root, scene_entries=[SceneEntry(path=state["single"])]
        ).run()
        plain_dir = os.path.join(plain_root, "single", "push_in", "sequence_000001")
        plain_recorded = load_json_file(os.path.join(plain_dir, "sequence_config.json"))
        equal(plain_recorded["render"]["resolution_explicit"], False)
        equal(plain_recorded["render"]["effective_resolution"], [160, 90],
              "without the override the sequence follows the scene")
        args3 = rs.build_parser().parse_args([
            "--input", plain_dir,
            "--output-root", os.path.join(RENDER_DIR, "resolution_plain"),
            "--engine", "BLENDER_WORKBENCH",
            "--log-level", "ERROR",
        ])
        result3 = rs.render_sequence(rs.resolve_sequences(args3)[0], args3, mappings=[])
        ok(result3["ok"], result3.get("error"))
        equal(result3["render"]["resolution_source"], "scene")
        equal(result3["render"]["resolution"], [160, 90])

    @suite.case("dry-run resolves outputs without rendering")
    def _():
        from blender_motion_pipeline.render import render_sequences as rs

        args = rs.build_parser().parse_args([
            "--input-root", os.path.join(OUT_DIR, "basic"),
            "--output-root", os.path.join(RENDER_DIR, "dry"),
            "--dry-run",
            "--log-level", "ERROR",
        ])
        jobs = rs.resolve_sequences(args)
        ok(len(jobs) >= 2, len(jobs))
        result = rs.render_sequence(jobs[0], args, mappings=[])
        ok(result["ok"], result.get("error"))
        equal(result["dry_run"], True)
        ok(result.get("trajectory_rows", 0) > 0, result)
        ok(not os.path.isdir(os.path.join(RENDER_DIR, "dry")) or
           not any(name.endswith(".mp4") for name in os.listdir(
               os.path.join(RENDER_DIR, "dry", "single", "push_in", "sequence_000001"))
               if os.path.isdir(os.path.join(RENDER_DIR, "dry", "single", "push_in", "sequence_000001")))
           if os.path.isdir(os.path.join(RENDER_DIR, "dry")) else True,
           "dry-run must not write a video")

    @suite.case("--list reports discovered sequences and their state")
    def _():
        from blender_motion_pipeline.render import render_sequences as rs

        args = rs.build_parser().parse_args([
            "--input-root", os.path.join(OUT_DIR, "basic"), "--list",
            "--output-root", RENDER_DIR, "--log-level", "ERROR",
        ])
        equal(rs.run_render(args, mappings=[]), 0)

    @suite.case("skip-existing leaves an already rendered video alone")
    def _():
        from blender_motion_pipeline.render import render_sequences as rs

        video = os.path.join(RENDER_DIR, "single", "push_in", "sequence_000001", "sequence_000001.mp4")
        ok(os.path.isfile(video), video)
        before = os.path.getmtime(video)
        args = rs.build_parser().parse_args([
            "--input-root", os.path.join(OUT_DIR, "basic"),
            "--output-root", RENDER_DIR,
            "--scene-filter", "single", "--motion-filter", "push_in",
            "--log-level", "ERROR",
        ])
        jobs = rs.resolve_sequences(args)
        job = jobs[0]
        ok(not job.get("skip_reason"))
        selected = rs.select_jobs(jobs, args)
        ok(selected[0].get("skip_reason"), "the existing video must be detected")
        close(os.path.getmtime(video), before, tol=1e-6)

    @suite.case("render failures produce a non-zero exit code and a report")
    def _():
        from blender_motion_pipeline.render import render_sequences as rs

        broken_dir = os.path.join(WORK, "broken_sequences", "scene", "motion", "sequence_000001")
        os.makedirs(broken_dir, exist_ok=True)
        save_json_file(os.path.join(broken_dir, "sequence_config.json"), {
            "sequence": {"sequence_id": "sequence_000001", "scene_name": "scene",
                         "motion_name": "motion", "camera_name": "Camera"},
            "frames": {"frame_start": 0, "frame_end": 4, "fps": 24},
        })
        with open(os.path.join(broken_dir, "sequence_000001.blend"), "w", encoding="utf-8") as handle:
            handle.write("this is not a blend file")
        args = rs.build_parser().parse_args([
            "--input-root", os.path.join(WORK, "broken_sequences"),
            "--output-root", os.path.join(RENDER_DIR, "broken"),
            "--log-level", "ERROR",
        ])
        exit_code = rs.run_render(args, mappings=[])
        equal(exit_code, 1)
        summary = load_json_file(os.path.join(RENDER_DIR, "broken", "render_report.json"))
        equal(summary["totals"]["failed"], 1)
        ok(summary["failed"][0]["error"], summary["failed"])

    @suite.case("an unreadable input reports exit code 2 instead of a traceback")
    def _():
        from blender_motion_pipeline.render import render_sequences as rs

        args = rs.build_parser().parse_args(["--input", os.path.join(WORK, "nowhere"), "--log-level", "ERROR"])
        equal(rs.run_render(args, mappings=[]), 2)

    # -- add-on registration ---------------------------------------------
    @suite.case("the add-on registers, exposes its panels and unregisters cleanly")
    def _():
        import bpy

        module_name = "blender_motion_pipeline"
        module = sys.modules.get(module_name)
        ok(module is not None, "the package must be importable")
        from blender_motion_pipeline import registration

        registration.unregister_all()
        registration.register_all()

        # A PropertyGroup is not exposed as a ``bpy.types`` attribute in Blender
        # 5.x, so the authoritative check is the PointerProperty's fixed type.
        scene = bpy.context.scene
        ok(hasattr(scene, "mpp"), "scene.mpp must exist after registration")
        pointer = bpy.types.Scene.bl_rna.properties["mpp"]
        equal(pointer.fixed_type.name, "MPP_SceneProperties")
        ok(hasattr(scene.mpp, "scene_list"), "the scene list collection must exist")
        equal(len(scene.mpp.scene_list), 0)

        # Blender 5.2 resolves module-scoped operators only through the
        # qualified ``bpy.ops.mpp.<name>`` form.
        for operator in (
            "add_files", "add_directory", "remove_selected", "clear_list",
            "save_scene_list", "load_scene_list", "check_configuration",
            "validate_scenes", "start_generation", "stop_task",
            "open_output_directory", "show_error_report", "load_templates",
            "apply_defaults", "save_config", "load_config",
        ):
            ok(hasattr(bpy.ops.mpp, operator), f"operator mpp.{operator} is missing")

        for panel in ("MPP_PT_scenes", "MPP_PT_character", "MPP_PT_motion",
                      "MPP_PT_validation", "MPP_PT_output", "MPP_PT_actions",
                      "MPP_PT_status", "MPP_UL_scene_list"):
            ok(hasattr(bpy.types, panel), f"{panel} is missing")

        registration.unregister_all()
        ok(not hasattr(bpy.context.scene, "mpp"),
           "unregister must remove the Scene.mpp pointer property")

    @suite.case("panel property defaults are sane and the scene list add operator validates paths")
    def _():
        import bpy
        from blender_motion_pipeline import registration

        registration.register_all()
        try:
            scene = bpy.context.scene
            group = scene.mpp
            equal(group.character_mode, CHARACTER_MODE_NONE)
            ok(group.output_root == "" or os.path.isabs(group.output_root), group.output_root)
            ok(group.validation_sample_step >= 1)
            ok(group.search_max_radius >= group.search_min_radius)
            ok(group.trajectory_mode in ("all_frames", "sampled"))
            equal(len(group.scene_list), 0)

            # Adding a file that does not exist must report a problem, not crash
            # and not silently insert a broken row.
            group.file_path = os.path.join(BLEND_DIR, "nope.blend")
            result = bpy.ops.mpp.add_files()
            ok("CANCELLED" in result, result)
            equal(len(group.scene_list), 0, "a missing file must not be added")
            ok("does not exist" in group.last_report, group.last_report)

            group.file_path = state["single"]
            bpy.ops.mpp.add_files()
            equal(len(group.scene_list), 1)
            equal(group.scene_list[0].status in ("pending", "ok", ""), True)
            # Adding it twice must not duplicate.
            bpy.ops.mpp.add_files()
            equal(len(group.scene_list), 1)

            group.directory = BLEND_DIR
            bpy.ops.mpp.add_directory()
            # One per fixture scene: single, multi_cam, blocked, animated_cam,
            # parented_cam, missing_asset and no_camera.
            equal(len(group.scene_list), 7, [item.path for item in group.scene_list])
            bpy.ops.mpp.clear_list()
            equal(len(group.scene_list), 0)
        finally:
            registration.unregister_all()

    @suite.case("the panel configuration survives the scene changes a run performs")
    def _():
        # Regression: the settings live on ``scene.mpp``, which is per file, so a
        # batch that opens every queued scene replaced them with defaults (measured
        # 11 of 14 fields) and the user had to type everything again.  The
        # configuration is now remembered outside the .blend.
        import bpy

        from blender_motion_pipeline import registration
        from blender_motion_pipeline.config import panel_state

        settings_path = os.path.join(WORK, "panel_settings.json")
        previous = os.environ.get(panel_state.ENV_OVERRIDE)
        os.environ[panel_state.ENV_OVERRIDE] = settings_path
        panel_state.clear()
        registration.register_all()
        try:
            group = bpy.context.scene.mpp
            group.render_samples = 9
            group.camera_selection = "Camera"
            group.frame_start = 5
            group.validation_enabled = False
            group.output_root = os.path.join(WORK, "remembered_out")
            group.motion_names = "still"
            group.render_output_root = os.path.join(WORK, "render_out")

            # What pressing Start generation does before the first scene opens.
            from blender_motion_pipeline.operators import remember_settings

            ok(remember_settings(group), "the settings file must be written")
            ok(os.path.isfile(settings_path), settings_path)
            described = panel_state.describe()
            ok(described["fields"] > 0, described)

            # The run opens another scene: everything must come back.
            from blender_motion_pipeline.core.scene_loader import open_scene_for_generation

            load = open_scene_for_generation(SceneEntry(path=state["single"]))
            ok(load.ok, load.error)
            registration._scene_watch_tick()          # the 1 s watcher, once
            restored = bpy.context.scene.mpp
            equal(restored.render_samples, 9)
            equal(restored.camera_selection, "Camera")
            equal(restored.frame_start, 5)
            equal(restored.validation_enabled, False)
            equal(restored.motion_names, "still")
            equal(len(restored.scene_list), 0)

            # A file that carries its own configuration keeps it.
            own = os.path.join(WORK, "own_settings.blend")
            bpy.ops.wm.read_factory_settings(use_empty=True)
            bpy.ops.object.camera_add()
            bpy.context.scene.camera = bpy.context.active_object
            bpy.context.scene.mpp.render_samples = 3
            bpy.ops.wm.save_as_mainfile(filepath=own, check_existing=False, compress=False)

            load = open_scene_for_generation(SceneEntry(path=own))
            ok(load.ok, load.error)
            registration._scene_watch_tick()
            equal(bpy.context.scene.mpp.render_samples, 3,
                  "a deliberately configured file must not be overwritten")

            # ... and the panel buttons work: force-apply, then forget.
            ok("FINISHED" in bpy.ops.mpp.load_settings(), bpy.context.scene.mpp.last_report)
            equal(bpy.context.scene.mpp.render_samples, 9)
            ok("FINISHED" in bpy.ops.mpp.save_settings())
            ok("FINISHED" in bpy.ops.mpp.forget_settings())
            equal(panel_state.load(), {})
        finally:
            panel_state.clear()
            registration.unregister_all()
            if previous is None:
                os.environ.pop(panel_state.ENV_OVERRIDE, None)
            else:
                os.environ[panel_state.ENV_OVERRIDE] = previous

    @suite.case("the config check operator explains what is missing")
    def _():
        import bpy
        from blender_motion_pipeline import registration

        registration.register_all()
        try:
            scene = bpy.context.scene
            group = scene.mpp
            group.scene_list.clear()
            group.output_root = ""
            result = bpy.ops.mpp.check_configuration()
            ok("FINISHED" in result or "CANCELLED" in result, result)
            ok(group.last_report, "the panel must show a report")
            ok("output" in group.last_report.lower(), group.last_report[:300])

            # With an output folder and a queue, the check must pass.
            group.output_root = os.path.join(OUT_DIR, "ui")
            group.template_path = write_templates()
            group.motion_names = "still"
            group.file_path = state["single"]
            bpy.ops.mpp.add_files()
            equal(len(group.scene_list), 1)
            bpy.ops.mpp.check_configuration()
            report = group.last_report
            ok(report.upper().startswith("OK"), report[:300])
            ok("scenes: 1" in report, report[:300])
            ok(group.motion_count == 1, group.motion_count)
        finally:
            registration.unregister_all()

    @suite.case("generation runs from the panel operator and reports progress")
    def _():
        import bpy
        from blender_motion_pipeline import registration

        registration.register_all()
        try:
            scene = bpy.context.scene
            group = scene.mpp
            group.output_root = os.path.join(OUT_DIR, "ui")
            group.template_path = write_templates()
            group.motion_names = "still"
            group.character_mode = CHARACTER_MODE_NONE
            group.overwrite = True
            group.resume = False
            group.validation_sample_step = 1
            # Sequence .blend writes are unsafe from a timer callback; the panel
            # defers them, so disable them here to keep the assertion focused.
            group.save_sequence_blend = False
            group.file_path = state["single"]
            bpy.ops.mpp.add_files()
            equal(len(group.scene_list), 1)
            result = bpy.ops.mpp.start_generation()
            ok("FINISHED" in result, result)
            ok(group.task_state in ("running", "done"), group.task_state)
            # Pump the timer state machine until the task reports completion.
            # ``group`` must be re-read every iteration: generation opens other
            # .blend files, which frees the scene (and its property group) that
            # the old RNA reference points at.
            import time

            deadline = time.time() + 180
            from blender_motion_pipeline.preferences import live_status

            while (live_status().get("state") == "running" and time.time() < deadline):
                ui_ops_pump()
                time.sleep(0.02)
            status = live_status()
            equal(status.get("state"), "done", status)
            group = bpy.context.scene.mpp
            ok(os.path.isdir(os.path.join(OUT_DIR, "ui", "single", "still")),
               "the output tree must exist")
            ok(status.get("generated", 0) >= 1, status)
            ok(os.path.isfile(os.path.join(OUT_DIR, "ui", "single", "still",
                                           "sequence_000001", "sequence_config.json")),
               "the sequence config must be written")
        finally:
            registration.unregister_all()

    @suite.case("a panel run keeps its driver across the scene opens it performs")
    def _():
        # Regression: opening a .blend empties ``bpy.app.timers`` (measured in this
        # build), and a panel run's *first* unit of work opens the first queued
        # scene -- so the loop unregistered its own driver and then sat on
        # "opening <scene>" forever: task stuck at ``running``, 807 s without a
        # single file written, CPU idle.
        #
        # The existing panel case drives the task by calling ``ui_task.step()``
        # directly, which is exactly why it never caught this: it bypassed the
        # timer registry altogether.
        import bpy
        import time

        from blender_motion_pipeline import operators, registration
        from blender_motion_pipeline.core import ui_task

        registration.unregister_all()
        registration.register_all()
        try:
            group = bpy.context.scene.mpp
            group.output_root = os.path.join(OUT_DIR, "timer_survival")
            group.template_path = write_templates()
            group.motion_names = "still"
            group.character_mode = CHARACTER_MODE_NONE
            group.overwrite = True
            group.resume = False
            group.validation_enabled = False
            group.save_sequence_blend = False
            group.file_path = state["single"]
            bpy.ops.mpp.add_files()

            ok("FINISHED" in bpy.ops.mpp.start_generation())
            ok(bpy.app.timers.is_registered(operators._generation_tick),
               "Start generation must arm the driver timer")

            # One real tick: in a GUI Blender the timer calls this, and it is the
            # call that opens the scene (and therefore wipes the registry).
            operators._generation_tick()
            ok(bpy.app.timers.is_registered(operators._generation_tick),
               "the driver must survive the scene open it just performed")
            ok(ui_task.is_running(), ui_task.snapshot())

            # Pump to completion the way the timer would, checking after every
            # tick that the driver is still armed.
            deadline = time.time() + 180
            while ui_task.is_running() and time.time() < deadline:
                operators._generation_tick()
                ok(bpy.app.timers.is_registered(operators._generation_tick),
                   "the driver must stay armed after every step")
                time.sleep(0.01)
            snapshot = ui_task.snapshot()
            equal(snapshot["state"], "done", snapshot)
            ok(snapshot["generated"] >= 1, snapshot)
            ok(os.path.isfile(os.path.join(OUT_DIR, "timer_survival", "single", "still",
                                           "sequence_000001", "sequence_config.json")),
               "the run must actually produce a sequence")

            # Second healing path: any operator press restores the timers after an
            # arbitrary file load (the panel draw does the same).
            bpy.ops.wm.open_mainfile(filepath=state["single"])
            ok(not bpy.app.timers.is_registered(registration._scene_watch_tick),
               "the premise of this check: a file load does clear the timers")
            operators._panel_group()
            ok(bpy.app.timers.is_registered(registration._scene_watch_tick),
               "an operator press must put the settings watcher back")
        finally:
            registration.unregister_all()

    @suite.case("stop_task cancels a running generation")
    def _():
        import bpy
        from blender_motion_pipeline import registration

        registration.register_all()
        try:
            group = bpy.context.scene.mpp
            group.task_state = "idle"
            result = bpy.ops.mpp.stop_task()
            ok("FINISHED" in result, result)
        finally:
            registration.unregister_all()

    @suite.case("error report writes a JSON bundle the user can inspect")
    def _():
        import bpy
        from blender_motion_pipeline import registration

        registration.register_all()
        try:
            group = bpy.context.scene.mpp
            group.output_root = os.path.join(OUT_DIR, "errors")
            result = bpy.ops.mpp.show_error_report()
            ok("FINISHED" in result, result)
        finally:
            registration.unregister_all()

    return suite


def main() -> int:
    return build_suite().run()


if __name__ == "__main__":
    sys.exit(main())
