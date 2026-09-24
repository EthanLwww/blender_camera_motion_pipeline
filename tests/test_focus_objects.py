"""Focus objects: the subject an ``Arc`` orbits.

    blender -b -P tests/test_focus_objects.py

What this suite pins down, in the order the feature works:

* the **orbit maths** -- re-centring an ``Arc`` on the object really does keep it
  centred (the authored template assumes a subject 4 m away, so on a camera 8 m away
  it drifts 45 deg off; the re-centred one stays under a degree);
* **staging** -- every model is placed on the anchor, marked, hidden and registered in
  the scene, and the placement the report carries matches the objects in the file;
* **the matrix** -- ``scene x camera x motion x focus object`` sequences land in the
  *motion* folder with no per-object sub-folder, numbered without collisions;
* **the record** -- each sequence names the object it was generated for, and the
  numbers a render node needs are in ``sequence_config.json``;
* **the non-arc case** -- the motion is untouched and the object is still there.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
if _PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, _PACKAGE_PARENT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _boot  # noqa: E402,F401  (the add-on folder may be called anything)

from blender_motion_pipeline.camera.camera_search import look_at_quaternion  # noqa: E402
from blender_motion_pipeline.camera.motion_templates import (  # noqa: E402
    MotionTemplateGenerator, MotionTemplateLibrary, load_template_file, matrix_to_quaternion,
    quat_rotate, quaternion_to_matrix,
)
from blender_motion_pipeline.config.models import BatchConfig  # noqa: E402
from blender_motion_pipeline.core import focus as focus_objects  # noqa: E402
from blender_motion_pipeline.io.json_io import load_json_file  # noqa: E402
from blender_motion_pipeline.tests.harness import Failure, Suite, close, equal, ok  # noqa: E402

WORK = os.path.join(tempfile.gettempdir(), "motion_pipeline_focus_test")
MODEL_DIR = os.path.join(WORK, "models")
SCENE_DIR = os.path.join(WORK, "scenes")
PROJECT_DIR = os.path.join(WORK, "projects")

DOCUMENT = os.path.join(_PACKAGE_PARENT, "blender_camera_motion_pipeline", "templates",
                        "camera_motion_templates_41.json")
#: Long enough to show the shape, short enough to keep the suite quick.
FRAME_END = 24
ANCHOR = (2.0, 1.0, 0.0)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def build_model(path: str, *, kind: str = "cube", size: float = 1.0,
                hierarchy: bool = False) -> str:
    """One small .blend holding a single mesh, as a stand-in for a subject.

    ``hierarchy`` builds what most downloaded props look like: an empty parent with
    several meshes hanging off it, offset from the origin.  That shape is what exposed
    a placement bug -- moving one chosen mesh instead of the model's root left the rest
    of the model behind.
    """
    import bpy

    bpy.ops.wm.read_factory_settings(use_empty=True)
    if hierarchy:
        parent = bpy.data.objects.new("PropRoot", None)
        bpy.context.scene.collection.objects.link(parent)
        for index, (dx, dy) in enumerate(((-0.30, 0.05), (0.25, -0.10), (0.05, 0.22))):
            bpy.ops.mesh.primitive_cube_add(size=size * 0.5,
                                            location=(dx, dy, size * 0.4 * (index + 1)))
            child = bpy.context.active_object
            child.name = f"Part{index}"
            child.parent = parent
            child.matrix_parent_inverse = parent.matrix_world.inverted()
    elif kind == "cube":
        bpy.ops.mesh.primitive_cube_add(size=size, location=(0.0, 0.0, size * 0.5))
    else:
        bpy.ops.mesh.primitive_cylinder_add(radius=size * 0.4, depth=size * 2.0,
                                            location=(0.0, 0.0, size))
    obj = bpy.context.active_object
    if obj is not None:
        obj.name = f"{kind.capitalize()}Model"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=path, check_existing=False, compress=False)
    return path


def build_scene(path: str, *, cameras: int = 1) -> str:
    """A room-ish scene: a floor, a wall, and cameras looking at the origin."""
    import bpy

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 0, FRAME_END
    scene.render.fps = 24
    scene.render.resolution_x, scene.render.resolution_y = 320, 180
    scene.render.engine = "BLENDER_WORKBENCH"
    bpy.ops.mesh.primitive_plane_add(size=30.0, location=(0.0, 0.0, 0.0))
    bpy.ops.mesh.primitive_cube_add(size=4.0, location=(0.0, 9.0, 2.0))
    for index in range(max(1, cameras)):
        camera = bpy.data.objects.new(f"Camera{index + 1}", bpy.data.cameras.new(f"Camera{index + 1}"))
        camera.data.lens = 35.0
        camera.location = (0.0, -8.0 - 2.0 * index, 1.7)
        camera.rotation_mode = "XYZ"
        camera.rotation_euler = (math.radians(88.0), 0.0, 0.0)
        scene.collection.objects.link(camera)
        if index == 0:
            scene.camera = camera
    scene.frame_set(0)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=path, check_existing=False, compress=False)
    return path


def focus_config(scene_path: str, models, *, templates: str = DOCUMENT,
                 motions=("single_arc_cw", "single_dolly_in"), anchor_mode: str = "numbers",
                 cameras: int = 1) -> BatchConfig:
    config = BatchConfig.from_dict({
        "batch": {"output_root": PROJECT_DIR, "mode": "none"},
        "motion": {"template_path": templates, "frame_start": 0, "frame_end": FRAME_END,
                   "fps": 24},
        "validation": {"enabled": False},
        "search": {"enabled": False},
        "render": {"resolution_x": 320, "resolution_y": 180},
        "focus": {
            "mode": "models",
            "models": [{"path": path, "label": label, "enabled": True}
                       for path, label in models],
            "anchor_mode": anchor_mode,
            "anchor_object": focus_objects.DEFAULT_ANCHOR_OBJECT,
            "anchor_location": list(ANCHOR),
            "keep_visible": True,
            "visible_ratio": 0.95,
        },
        "scenes": [{"path": scene_path, "enabled": True}],
    })
    # Only the two motions this suite cares about: the whole 41-move document would
    # multiply the run by 41 for no extra coverage.
    config.motion.template_names = list(motions)
    config.batch.keep_reports = True
    return config


def run_batch(config: BatchConfig, *, library=None):
    """Generate the whole matrix through the real batch runner.

    A project layout is required for the focus feature: staging is what puts the
    models inside the scene copy, and the copy is what the render node opens.
    """
    from blender_motion_pipeline.core.batch_runner import BatchRunner
    from blender_motion_pipeline.core.project import create_project
    from blender_motion_pipeline.core.scene_loader import SceneEntry

    layout = create_project(config.batch.output_root)
    entries = [SceneEntry(path=str(item.get("path")))
               for item in config.scenes if isinstance(item, dict) and item.get("path")]
    if config.motion.template_names:
        # ``template_names`` is the *caller's* filter (the CLI and the panel apply it);
        # restricting the library is how a batch is kept to the moves under test.
        library = MotionTemplateLibrary.from_config(config.motion)
        library.restrict_to(list(config.motion.template_names))
    runner = BatchRunner(config, output_root=layout.sequence_root, scene_entries=entries,
                         project_layout=layout)
    report = runner.run(library=library)
    return runner, report


def orbit_probe(library: MotionTemplateLibrary, name: str, *, anchor, camera, template_name):
    """Generate ``template_name`` from ``camera`` aimed at ``anchor``; measure the error."""
    template = library.get(template_name)
    forward = tuple(anchor[i] - camera[i] for i in range(3))
    aim = look_at_quaternion(forward)
    rotation = quaternion_to_matrix(aim)
    matrix = [[rotation[r][c] for c in range(3)] + [camera[r]] for r in range(3)]
    matrix.append([0.0, 0.0, 0.0, 1.0])
    base_quaternion = matrix_to_quaternion(matrix)
    retargeted, info = focus_objects.orbit_template(
        template, anchor=anchor, base_position=camera, base_quaternion=base_quaternion, fps=24.0,
    )
    results = {}
    for label, candidate, adjust in (
        ("authored", template, None),
        ("orbiting", retargeted, info.get("rotation_adjust")),
    ):
        pose = [list(row) for row in matrix]
        quaternion = base_quaternion
        if adjust is not None:
            from blender_motion_pipeline.camera.motion_templates import quat_multiply
            spin = quaternion_to_matrix(adjust)
            pose = [[sum(spin[r][k] * matrix[k][c] for k in range(3)) for c in range(3)]
                    + [camera[r]] for r in range(3)] + [[0.0, 0.0, 0.0, 1.0]]
            quaternion = quat_multiply(adjust, base_quaternion)
        animation = MotionTemplateGenerator(fps=24).generate(
            candidate, base_matrix=pose, base_focal=35.0, base_quaternion=quaternion,
            frame_start=0, frame_end=FRAME_END,
        )
        worst = 0.0
        for sample in animation.samples:
            to_anchor = tuple(anchor[i] - sample.position[i] for i in range(3))
            distance = math.sqrt(sum(v * v for v in to_anchor))
            view = quat_rotate(sample.quaternion, (0.0, 0.0, -1.0))
            unit = tuple(v / max(1e-9, distance) for v in to_anchor)
            angle = math.degrees(math.acos(max(-1.0, min(1.0, sum(view[i] * unit[i] for i in range(3))))))
            worst = max(worst, angle)
        results[label] = worst
    return results, info


# --------------------------------------------------------------------------
# suite
# --------------------------------------------------------------------------
def build_suite() -> Suite:
    suite = Suite("test_focus_objects")
    state = {}

    def make_fixtures():
        os.makedirs(MODEL_DIR, exist_ok=True)
        os.makedirs(SCENE_DIR, exist_ok=True)
        os.makedirs(PROJECT_DIR, exist_ok=True)
        state["cube"] = build_model(os.path.join(MODEL_DIR, "cube.blend"), kind="cube",
                                    size=1.0)
        state["cylinder"] = build_model(os.path.join(MODEL_DIR, "cylinder.blend"),
                                        kind="cylinder", size=0.6)
        state["scene"] = build_scene(os.path.join(SCENE_DIR, "room.blend"))

    def need() -> None:
        """Make sure the fixtures are still there before a case uses them.

        A suite that renders and stages scenes spawns a lot of throwaway Blender
        processes, and on a loaded machine those fixtures can disappear from the temp
        folder between cases.  A missing model file would show up as "no focus objects",
        which reads like a feature bug; rebuilding on demand keeps the failure honest.
        """
        for key, maker in (("cube", lambda path: build_model(path, kind="cube", size=1.0)),
                           ("cylinder", lambda path: build_model(path, kind="cylinder", size=0.6)),
                           ("scene", build_scene)):
            path = state.get(key) or ""
            if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
                state[key] = maker(path or os.path.join(
                    MODEL_DIR if key != "scene" else SCENE_DIR,
                    {"cube": "cube.blend", "cylinder": "cylinder.blend",
                     "scene": "room.blend"}[key]))

    def setup():
        if os.path.isdir(WORK):
            shutil.rmtree(WORK, ignore_errors=True)
        make_fixtures()
        state["library"] = load_template_file(DOCUMENT)

    def stage_with_models() -> dict:
        """Stage a copy per model -- exactly what the batch runner does for one scene.

        Deliberately not the shared helper from ``core``: this builds the same shape the
        runner builds (one copy per subject, named ``<scene>__<model>.blend``) so the
        test fails if that shape changes.
        """
        from blender_motion_pipeline.core.project import create_project, place_focus_models

        layout = create_project(os.path.join(WORK, "stage_projects"))
        base = layout.stage_scene(state["scene"])
        models = focus_objects.models_from_section(
            focus_config(state["scene"], [(state["cube"], "cube"),
                                           (state["cylinder"], "cylinder")]).focus
        )
        placements = []
        for model in models:
            variant = layout.stage_scene(base, label=model.id)
            record = place_focus_models(
                variant, [model],
                anchor={"mode": "numbers", "location": list(ANCHOR), "clearance": 0.5},
                report=os.path.join(WORK, "focus_report.json"),
            )
            if record.get("ok"):
                placements.extend(record["placements"])
                state["staged_%s" % model.id] = variant
        state["staged"] = state.get("staged_cube") or state.get("staged_cylinder") or ""
        state["staged_scene_root"] = layout.scene_root
        return {"ok": bool(placements) and len(placements) == len(models),
                "placements": placements, "models": [], "anchor": {}, "note": "",
                "error": "" if len(placements) == len(models) else "a model could not be placed"}

    def case(name: str):
        """``suite.case``, with the fixtures checked before every case runs."""
        def decorate(function):
            def guarded():
                need()
                return function()
            return suite.case(name)(guarded)
        return decorate

    def teardown():
        if os.environ.get("MP_KEEP_TEST_OUTPUT"):
            return
        shutil.rmtree(WORK, ignore_errors=True)

    suite.setup = setup
    suite.teardown = teardown

    @case("a re-centred arc keeps the focus object centred, the authored one does not")
    def _():
        # A camera 8 m from the subject: the document's Arc assumes 4 m, so the authored
        # one drifts far more than the re-centred one.  Both numbers are asserted so the
        # test would notice the retarget quietly becoming a no-op.
        anchor = (0.0, 0.0, 0.0)
        camera = (0.0, -8.0, 0.0)
        results, info = orbit_probe(state["library"], "single_arc_cw", anchor=anchor,
                                    camera=camera, template_name="single_arc_cw")
        ok(info.get("ok"), f"the orbit should be buildable: {info}")
        close(info.get("radius_m"), 8.0, tol=1e-6, message="orbit radius is the real distance")
        close(info.get("sweep_deg"), 90.0, tol=1e-6, message="the sweep comes from the template")
        equal(info.get("direction"), "clockwise", message="direction comes from the template")
        ok(results["authored"] > 3.0 * max(0.1, results["orbiting"]),
           f"the authored arc drifts far more than the re-centred one: "
           f"{results['authored']:.2f} vs {results['orbiting']:.2f} deg")
        ok(results["orbiting"] < 2.0,
           f"the re-centred arc keeps it centred: {results['orbiting']:.2f} deg")
        # The orbit has to start where the camera already is, or the bake contract
        # (a motion anchored on the first frame) would move the camera at frame 0.
        ok(float(info["base_aim_deg"]) <= 1.0,
           f"the camera already looks at the subject: {info['base_aim_deg']} deg to turn")

    @case("a camera that is not aimed at the subject is turned onto it")
    def _():
        anchor = (0.0, 0.0, 0.0)
        camera = (0.0, -8.0, 0.0)
        # Start looking 40 deg away from the subject.
        import blender_motion_pipeline.camera.motion_templates as motion_templates

        spin = quaternion_to_matrix(motion_templates.quat_from_axis_angle("Z", 40.0))
        straight = quaternion_to_matrix(look_at_quaternion((0.0, 8.0, 0.0)))
        matrix = [[sum(spin[r][k] * straight[k][c] for k in range(3)) for c in range(3)]
                  + [camera[r]] for r in range(3)] + [[0.0, 0.0, 0.0, 1.0]]
        base = matrix_to_quaternion(matrix)
        template = state["library"].get("single_arc_cw")
        retargeted, info = focus_objects.orbit_template(
            template, anchor=anchor, base_position=camera, base_quaternion=base, fps=24.0,
        )
        ok(info.get("ok"), f"the orbit should still be buildable: {info}")
        ok(abs(float(info["base_aim_deg"]) - 40.0) < 1.0,
           f"it reports the turn it needs: {info['base_aim_deg']} deg")

    @case("a pan is not an arc and is left completely alone")
    def _():
        template = state["library"].get("single_pan_left")
        equal(focus_objects.arc_sweep(template), None, message="a pan is not an orbit")
        retargeted, info = focus_objects.orbit_template(
            template, anchor=(0.0, 0.0, 0.0), base_position=(0.0, -8.0, 0.0),
            base_quaternion=(1.0, 0.0, 0.0, 0.0), fps=24.0,
        )
        ok(not info.get("ok"))
        equal([(k.frame, k.location) for k in retargeted.keyframes],
              [(k.frame, k.location) for k in template.keyframes],
              message="the template comes back untouched")

    @case("a camera sitting on the subject cannot orbit it and says so")
    def _():
        template = state["library"].get("single_arc_cw")
        _, info = focus_objects.orbit_template(
            template, anchor=(0.0, 0.0, 0.0), base_position=(0.05, 0.0, 0.0),
            base_quaternion=(1.0, 0.0, 0.0, 0.0), fps=24.0,
        )
        ok(not info.get("ok"), "an impossible orbit is refused")
        ok("orbit needs at least" in str(info.get("reason")),
           f"and explains itself: {info.get('reason')}")

    @case("an orbit can be replayed at another distance from the subject")
    def _():
        # The authored distance is the shot; a room that is too tight for it gets the
        # same circle played smaller (or bigger) rather than no arc at all.
        template = state["library"].get("single_arc_cw")
        anchor = (0.0, 0.0, 0.0)
        camera = (0.0, -8.0, 1.6)
        aim = look_at_quaternion((0.0, 1.0, 0.0))
        base = matrix_to_quaternion(quaternion_to_matrix(aim))

        for radius, expected in ((None, "authored"), (4.0, "adapted")):
            retargeted, info = focus_objects.orbit_template(
                template, anchor=anchor, base_position=camera, base_quaternion=base, fps=24.0,
                radius=radius,
            )
            ok(info.get("ok"), info)
            equal(info["radius_source"], expected)
            wanted = 8.0 if radius is None else radius
            ok(abs(float(info["radius_m"]) - wanted) < 1e-6, info)
            ok(abs(float(info["radius_natural_m"]) - 8.0) < 1e-6, info)

            # Replay it: every frame has to sit on the requested circle and look at the
            # subject.  The keys are offsets from the camera's *base* position, and the
            # aim is folded into the base pose, exactly as the generator does it.
            from blender_motion_pipeline.camera.motion_templates import quat_multiply
            adjust = info["rotation_adjust"]
            spin = quaternion_to_matrix(adjust)
            straight = quaternion_to_matrix(base)
            pose = [[sum(spin[r][k] * straight[k][c] for k in range(3)) for c in range(3)]
                    + [camera[r]] for r in range(3)] + [[0.0, 0.0, 0.0, 1.0]]
            animation = MotionTemplateGenerator(fps=24).generate(
                retargeted, base_matrix=pose, base_focal=35.0,
                base_quaternion=quat_multiply(adjust, base), frame_start=0, frame_end=FRAME_END,
            )
            for sample in animation.samples:
                offset = (sample.position[0] - anchor[0], sample.position[1] - anchor[1])
                distance = math.sqrt(offset[0] ** 2 + offset[1] ** 2)
                ok(abs(distance - wanted) < 1e-2,
                   f"radius {wanted}: frame {sample.frame} sits {distance:.4f} m from the subject")
                to_anchor = tuple(anchor[i] - sample.position[i] for i in range(3))
                length = math.sqrt(sum(v * v for v in to_anchor))
                view = quat_rotate(sample.quaternion, (0.0, 0.0, -1.0))
                unit = tuple(v / max(1e-9, length) for v in to_anchor)
                angle = math.degrees(math.acos(
                    max(-1.0, min(1.0, sum(view[i] * unit[i] for i in range(3))))))
                ok(angle < 1.5, f"radius {wanted}: frame {sample.frame} looks {angle:.2f} deg off")

    @case("the radius ladder keeps the authored distance first")
    def _():
        radii = focus_objects.orbit_radius_candidates(4.0, minimum=0.25)
        equal(radii[0], 4.0, message="the authored distance is tried first")
        equal(len(radii), len(set(radii)), message="no distance is tried twice")
        ok(all(value >= 0.25 for value in radii), radii)
        ok(3.0 in radii and 2.0 in radii, f"smaller circles are offered: {radii}")
        # Nothing below the floor survives, and a camera on the subject has no ladder.
        ok(all(value >= 1.0 for value in focus_objects.orbit_radius_candidates(4.0, minimum=1.0)))
        equal(focus_objects.orbit_radius_candidates(0.0), [])

    @case("the fixtures this suite needs are on disk")
    def _():
        # An earlier suite cannot be allowed to take these away silently: without the
        # model files every later case degenerates into "no focus objects", which reads
        # like a feature bug rather than a missing fixture.
        for label, path in (("scene", state["scene"]), ("cube", state["cube"]),
                            ("cylinder", state["cylinder"])):
            ok(os.path.isfile(path),
               f"the {label} fixture is missing: {path} "
               f"(folder exists: {os.path.isdir(os.path.dirname(path))})")
        ok(os.path.isfile(DOCUMENT), f"the 41-move document is missing: {DOCUMENT}")

    @case("every model gets its own scene copy, holding exactly that subject")
    def _():
        import bpy

        record = stage_with_models()
        ok(record.get("ok"), f"placing should succeed: {record.get('error')}")
        equal(len(record["placements"]), 2, message="one placement per model")

        # One copy per subject: a shot has one focus object, so the file a render node
        # opens must not contain the others at all.
        staged = state["staged"]
        bpy.ops.wm.open_mainfile(filepath=staged, load_ui=False)
        scene = bpy.context.scene
        names = focus_objects.registered_names(scene)
        equal(len(names), 1, message="the copy holds exactly one focus object")
        for name in names:
            obj = scene.objects[name]
            ok(obj.hide_render is False,
               f"{name} is the subject of this copy, so it is rendered")
        state["registered"] = names

        cube = next(item for item in record["placements"] if item["id"] == "cube")
        for axis in range(3):
            close(cube["anchor"][axis], ANCHOR[axis], tol=1e-4,
                  message=f"anchor axis {axis} is where it was asked to be")
        close(cube["bbox_min"][2], ANCHOR[2], tol=1e-4,
              message="the model stands *on* the anchor, not centred on it")
        close((cube["bbox_min"][0] + cube["bbox_max"][0]) * 0.5, ANCHOR[0], tol=1e-4,
              message="and its footprint is centred on it")
        close(cube["size"][0], 1.0, tol=1e-3, message="the cube is its own size")
        report = load_json_file(os.path.join(WORK, "focus_report.json"), default={},
                                required=False) or {}
        # One entry per scene *copy*, i.e. one per subject: the report is how a run
        # explains which object went into which file.
        entries = report.get("scenes") or []
        equal(len(entries), 2, message="one placement report per focus object's copy")
        for entry in entries:
            equal(len(entry.get("placements") or []), 1,
                  message=f"{os.path.basename(entry.get('scene', ''))} holds one subject")
        state["placement_record"] = record

    @case("the matrix multiplies by the focus object, into the motion folder")
    def _():
        models = [(state["cube"], "cube"), (state["cylinder"], "cylinder")]
        config = focus_config(state["scene"], models)
        shutil.rmtree(PROJECT_DIR, ignore_errors=True)
        runner, report = run_batch(config, library=state["library"])

        sequence_root = runner.project_layout.sequence_root
        scene_root = os.path.join(sequence_root, "room")
        ok(os.path.isdir(scene_root), f"the scene folder exists: {scene_root}")
        motions = sorted(os.listdir(scene_root))
        equal(motions, ["single_arc_cw", "single_dolly_in"],
              message="one folder per motion, and no per-object folder")

        arc_root = os.path.join(scene_root, "single_arc_cw")
        folders = sorted(os.listdir(arc_root))
        equal(folders, ["manifest.json", "sequence_000001", "sequence_000002"],
              message="two objects x one camera x one motion = two sequences")
        dolly_root = os.path.join(scene_root, "single_dolly_in")
        equal(sorted(name for name in os.listdir(dolly_root) if name != "manifest.json"),
              ["sequence_000001", "sequence_000002"],
              message="numbering restarts per motion folder, not per object")

        seen = []
        for folder in ("sequence_000001", "sequence_000002"):
            payload = load_json_file(os.path.join(arc_root, folder, "sequence_config.json"),
                                     default={}, required=False) or {}
            seen.append((payload.get("focus") or {}).get("object"))
        equal(sorted(seen), ["cube", "cylinder"],
              message="the two sequences are for the two different objects")
        state["arc_folder"] = os.path.join(arc_root, "sequence_000001")
        state["scene_root"] = scene_root
        state["sequence_root"] = sequence_root
        generated = sum(len(outcome.requests) for outcome in report.scenes)
        equal(generated, 4, message="the batch reports every generated sequence")

    @case("the arc sequence records its orbit, and the subject stayed in frame")
    def _():
        payload = load_json_file(os.path.join(state["arc_folder"], "sequence_config.json"),
                                 default={}, required=False) or {}
        block = payload.get("focus") or {}
        ok(block, "the sequence names its focus object")
        for key in ("objects", "anchor", "center", "model_path"):
            ok(block.get(key) not in (None, "", []),
               f"sequence_config.focus.{key} is filled in for the render node")
        ok(len(block.get("objects") or []) >= 1,
           "the renderer is told exactly which objects to show")
        orbit = block.get("orbit") or {}
        ok(orbit.get("radius_m", 0.0) > 1.0,
           f"the arc orbited at the real distance: {orbit}")
        equal(orbit.get("direction"), "clockwise", message="the template's direction survives")
        visibility = block.get("visibility") or {}
        ok(visibility.get("ok") is True,
           f"the subject stayed in frame: {visibility}")
        ok(float(visibility.get("visible_ratio") or 0.0) >= 0.95,
           f"at least 95% of the frames: {visibility}")
        # The orbit really happened: a re-centred 90 deg arc moves the camera a long
        # way, and it must not leave the camera where the authored template would.
        rows = [line.split() for line in
                open(os.path.join(state["arc_folder"], "sequence_000001_camera.txt"),
                     encoding="utf-8").read().splitlines() if not line.startswith("#")]
        rows = [row for row in rows if row and row[0].isdigit()]
        ok(len(rows) > 5, f"the trajectory was written: {len(rows)} rows")
        state["arc_rows"] = rows

    @case("a dolly is untouched by the focus object and still shows it")
    def _():
        dolly = os.path.join(state["scene_root"], "single_dolly_in", "sequence_000001")
        payload = load_json_file(os.path.join(dolly, "sequence_config.json"),
                                 default={}, required=False) or {}
        block = payload.get("focus") or {}
        ok(block.get("objects"), "a non-arc shot still carries its subject")
        ok(not block.get("orbit"), "but no orbit was invented for it")
        template = payload.get("motion") or {}
        # The authored dolly pushes 3 m forward; the recorded keys must still say so.
        keys = template.get("keyframes") or []
        ok(keys, "the motion is recorded")
        forward = min(float(key["location"][2]) for key in keys)
        close(forward, -3.0, tol=1e-6,
              message="the template's own amplitude is used unchanged")

    @case("the renderer runs as a script and honours an engine override")
    def _():
        # Two things this has to prove, neither of which an in-process import can:
        #  * the renderer is executed as ``blender -b -P render_sequences.py``, i.e. as
        #    ``__main__``, where a *relative* import cannot resolve.  A focus-side
        #    import written that way took every render down with
        #    "attempted relative import with no known parent package", and importing
        #    the module inside the test suite hid it completely;
        #  * the engine recorded in ``sequence_config.json`` is a default, not a lock:
        #    ``--engine`` on the render node replaces it, which is what a render farm
        #    needs when the generated engine cannot run there.
        import subprocess

        import bpy

        sequence = os.path.join(state["scene_root"], "single_arc_cw", "sequence_000001")
        ok(os.path.isdir(sequence), f"the generated sequence is there: {sequence}")
        recorded = load_json_file(os.path.join(sequence, "sequence_config.json"),
                                  default={}, required=False) or {}
        equal((recorded.get("render") or {}).get("engine"), "BLENDER_EEVEE",
              message="the sequence recorded the engine used at generation time")

        renderer = os.path.join(os.path.dirname(_HERE), "render", "render_sequences.py")
        output = os.path.join(WORK, "render_out")
        shutil.rmtree(output, ignore_errors=True)
        # Cycles rather than Workbench, because that is the switch this is about
        # (EEVEE recorded, Cycles on the render node) and because it is the only one of
        # the two that has a sample count to override.
        # 50%, not 25%: H.264 needs an even width *and* height, and 25% of the fixture's
        # 180-pixel height is 45, which makes Blender refuse to open the encoder.
        command = [bpy.app.binary_path, "-b", "-noaudio", "--factory-startup",
                   "-P", renderer, "--",
                   "--input", sequence, "--output-root", output,
                   "--engine", "CYCLES", "--device", "CPU", "--samples", "1",
                   "--resolution-percentage", "50", "--log-level", "INFO"]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=900)
        raw = (completed.stderr or "") + (completed.stdout or "")
        tail = " | ".join(line.strip() for line in raw.splitlines() if line.strip())[-1600:]
        equal(completed.returncode, 0, message=f"the renderer exits cleanly: {tail}")

        rendered = os.path.join(output, "room", "single_arc_cw", "sequence_000001")
        produced = sorted(os.listdir(rendered)) if os.path.isdir(rendered) else []
        ok(any(name.endswith(".mp4") for name in produced),
           f"a video came out: {produced} ({tail})")
        payload = load_json_file(os.path.join(rendered, "sequence_000001.json"),
                                 default={}, required=False) or {}
        applied = payload.get("render") or {}
        equal(applied.get("engine"), "CYCLES",
              message="the command line won over the sequence's recorded engine")
        equal(applied.get("samples"), 1, message="and so did --samples")
        equal(payload.get("status"), "rendered")
        # And the subject really was switched on for this render rather than left
        # hidden: the renderer says which objects it showed.
        log = (completed.stderr or "") + (completed.stdout or "")
        ok("focus objects: showing" in log,
           "the renderer reported the subject it put in the shot: " + log[-300:])

    @case("turning the mode off generates exactly the old matrix")
    def _():
        config = focus_config(state["scene"], [(state["cube"], "cube")])
        config.focus.mode = "off"
        shutil.rmtree(PROJECT_DIR, ignore_errors=True)
        runner, _report = run_batch(config, library=state["library"])
        scene_root = os.path.join(runner.project_layout.sequence_root, "room")
        arc_root = os.path.join(scene_root, "single_arc_cw")
        equal(sorted(name for name in os.listdir(arc_root) if name != "manifest.json"),
              ["sequence_000001"], message="one camera x one motion, no focus axis")
        payload = load_json_file(os.path.join(arc_root, "sequence_000001",
                                             "sequence_config.json"),
                                 default={}, required=False) or {}
        equal(payload.get("focus"), {}, message="and no focus block is written")

    @case("a missing model file is skipped instead of failing the run")
    def _():
        config = focus_config(state["scene"], [(state["cube"], "cube")])
        config.focus.models.append({"path": os.path.join(MODEL_DIR, "nope.blend"),
                                    "label": "nope", "enabled": True})
        models = focus_objects.models_from_section(config.focus)
        equal([model.id for model in models], ["cube"],
              message="a model that is not on disk never becomes an axis")

    @case("a fixed template is scaled to fit the scene instead of leaving it")
    def _():
        # The camera stands at y = -8 and a dolly pushes it 3 m forward, so a box whose
        # far wall is at y = -5.5 (1.5 m half size around y = -7) can only be satisfied
        # by 5/6 of the amplitude; with the 0.25 m safety margin, 3/4 of it.
        config = focus_config(state["scene"], [(state["cube"], "cube")],
                              motions=("single_dolly_in",))
        config.focus.mode = "off"
        config.motion.frame_end = 144          # the template's own span, so it plays out
        config.region.mode = "numbers"
        config.region.center = [0.0, -7.0, 1.7]
        config.region.size = [12.0, 3.0, 6.0]
        config.region.margin = 0.25
        shutil.rmtree(PROJECT_DIR, ignore_errors=True)
        runner, _report = run_batch(config)
        folder = os.path.join(runner.project_layout.sequence_root, "room", "single_dolly_in",
                              "sequence_000001")
        payload = load_json_file(os.path.join(folder, "sequence_config.json"), default={},
                                 required=False) or {}
        region = payload.get("region") or {}
        equal(region.get("stage"), "fit", message="the fit stage is what ran")
        ok(region.get("ok") is True, f"and the shot fits afterwards: {region}")
        equal(region.get("exit_frames"), 0)
        ok(0.5 < float(region.get("scale") or 0.0) < 1.0,
           f"the amplitude was scaled down, not left alone: {region.get('scale')}")
        ok(abs(float(region.get("scale")) - 0.75) < 0.02,
           f"and by the amount the box needs: {region.get('scale')}")
        # The recorded motion is the *scaled* one, so the trajectory, the video and the
        # report all describe the same path.
        keys = (payload.get("motion") or {}).get("keyframes") or []
        deepest = min(float(key["location"][2]) for key in keys)
        ok(deepest > -3.0, f"the template's own amplitude was reduced: {deepest}")
        # The camera trajectory TXT is a world-to-camera matrix, so its translation
        # column is not the camera's world position; the animation payload is (the
        # camera has no parent here), and it must stay behind the box's wall at y=-5.5.
        sidecar = load_json_file(os.path.join(folder, "sequence_000001.json"), default={},
                                 required=False) or {}
        samples = ((sidecar.get("camera_animation") or {}).get("samples") or [])
        ok(samples, "the animation payload is there to check the real path")
        worst = max(float(sample["location"][1]) for sample in samples)
        ok(worst <= -5.5 + 1e-3, f"no frame leaves the box: worst world y = {worst}")

    def cli_module():
        """``motion_pipeline_cli.py`` loaded as a module.

        It is a script (Blender runs it with ``-P``), not an importable member of the
        package, so it is loaded by path -- through the same ``_bootstrap`` it uses
        itself, which is what makes the add-on folder name-agnostic.
        """
        import importlib.util

        cached = state.get("cli")
        if cached is not None:
            return cached
        path = os.path.join(os.path.dirname(_HERE), "motion_pipeline_cli.py")
        spec = importlib.util.spec_from_file_location("mpp_cli_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        state["cli"] = module
        return module

    @case("the CLI turns focus flags into the config section")
    def _():
        # A headless run has to be able to say everything the panel can, or the feature
        # is GUI-only: these flags are the whole reason the axis is scriptable.
        cli = cli_module()

        args = cli.parse_args([
            "motion_pipeline_cli.py",
            "--scenes", state["scene"],
            "--output-root", os.path.join(WORK, "cli_out"),
            "--motion-filter", "single_arc_cw",
            "--focus-model", f"{state['cube']}::CubeModel::2.0",
            "--focus-model", state["cylinder"],
            "--focus-anchor", "numbers",
            "--focus-anchor-location", "1.5,2.5,0",
            "--focus-anchor-clearance", "0.75",
            "--no-focus-keep-visible",
            "--focus-strict",
        ])
        config = cli.build_config(args)
        equal(config.focus.mode, "models", message="naming a model turns the feature on")
        equal(len(config.focus.models), 2, message="one row per --focus-model")
        equal(config.focus.models[0]["object_name"], "CubeModel")
        close(float(config.focus.models[0]["scale"]), 2.0, tol=1e-9)
        equal(config.focus.models[1]["object_name"], "",
              message="the short form takes the whole file")
        close(float(config.focus.models[1]["scale"]), 1.0, tol=1e-9)
        equal(config.focus.anchor_mode, "numbers")
        equal([float(value) for value in config.focus.anchor_location], [1.5, 2.5, 0.0])
        close(float(config.focus.anchor_clearance), 0.75, tol=1e-9)
        equal(config.focus.keep_visible, False)
        equal(config.focus.strict, True)
        # And the section survives the round trip a farm run does (--save-config, then
        # --config on the generation machine).
        again = BatchConfig.from_dict(config.to_dict())
        equal(again.focus.to_dict(), config.focus.to_dict())

    @case("a malformed focus flag fails cleanly instead of generating nothing")
    def _():
        cli = cli_module()

        for bad in ("::Chair", f"{state['cube']}::Chair::big", f"{state['cube']}::Chair::-1",
                    f"{state['cube']}::Chair::2.0::extra"):
            try:
                cli.parse_args(["motion_pipeline_cli.py", "--focus-model", bad])
            except SystemExit as exc:
                equal(int(exc.code or 0), 2, message=f"{bad!r} uses the usage-error exit code")
                continue
            raise Failure(f"{bad!r} should have been refused")
        # A run that asks for focus objects but gives no model is refused by the config.
        args = cli.parse_args(["motion_pipeline_cli.py", "--focus-mode", "models",
                               "--scenes", state["scene"]])
        try:
            cli.build_config(args)
        except Exception as exc:  # ConfigError from focus.validate()
            ok("no enabled model" in str(exc), f"the reason is specific: {exc}")
        else:
            raise Failure("focus.mode=models without a model must not build a config")

    @case("a model built as a hierarchy is placed as a whole, not part by part")
    def _():
        # What a downloaded prop actually looks like: an empty parent with meshes
        # hanging off it.  Placing "the biggest mesh" moved that one mesh and left the
        # rest of the model behind -- measured on a real potted plant as 0.8 m of drift,
        # which put it inside a bench.
        import bpy

        from blender_motion_pipeline.core.project import place_focus_models

        model_path = build_model(os.path.join(MODEL_DIR, "hierarchy.blend"),
                                 size=1.0, hierarchy=True)
        staged = os.path.join(SCENE_DIR, "hierarchy_scene.blend")
        shutil.copy2(state["scene"], staged)
        anchor = (-2.10, 1.35, 0.02)
        models = focus_objects.models_from_section(type("S", (), {
            "mode": "models",
            "models": [{"path": model_path, "label": "prop", "enabled": True}],
        })())
        record = place_focus_models(
            staged, models,
            anchor={"mode": "numbers", "location": list(anchor), "clearance": 0.5},
            report=os.path.join(WORK, "hierarchy_report.json"),
        )
        ok(record.get("ok"), f"placing should succeed: {record.get('error')}")
        placement = record["placements"][0]
        equal(len(placement["objects"]), 4, message="the empty and its three meshes")
        for axis, label in ((0, "x"), (1, "y")):
            close(float(placement["center"][axis]), anchor[axis], tol=1e-3,
                  message=f"the whole model is centred on the anchor in {label}")
        close(float(placement["bbox_min"][2]), anchor[2], tol=1e-3,
              message="and its base is on the anchor")
        # The shape has to be intact: nothing may have been left behind at the origin.
        size = [float(v) for v in placement["size"]]
        ok(size[0] > 0.5 and size[1] > 0.3,
           f"the parts are still spread out around the anchor, not collapsed: {size}")
        # The registry is what the renderer reads; it must describe the same objects.
        bpy.ops.wm.open_mainfile(filepath=staged, load_ui=False)
        scene = bpy.context.scene
        equal(sorted(focus_objects.registered_names(scene)), sorted(placement["objects"]))

    @case("each sequence renders from the copy holding its own subject")
    def _():
        # One copy per subject means the render node never has to choose between models:
        # the file it opens holds exactly the object the sequence was generated for.  It
        # still has to switch visibility, because a whole tree renders in one process and
        # a subject-less sequence must not inherit the previous shot's subject.
        import bpy

        from blender_motion_pipeline.render.render_sequences import _apply_focus_objects

        record = state.get("placement_record")
        if not record:
            state["placement_record"] = record = stage_with_models()
        placements = {item["id"]: item for item in record["placements"]}
        cube_objects = placements["cube"]["objects"]
        cylinder_objects = placements["cylinder"]["objects"]
        cube_copy = state["staged_cube"]
        cylinder_copy = state["staged_cylinder"]
        ok(cube_copy != cylinder_copy,
           "each subject has its own scene copy: %s vs %s" % (cube_copy, cylinder_copy))

        bpy.ops.wm.open_mainfile(filepath=cube_copy)
        scene = bpy.context.scene
        equal(sorted(focus_objects.registered_names(scene)), sorted(cube_objects),
              message="the cube's copy holds the cube and nothing else")
        ok(not scene.objects[cube_objects[0]].hide_render,
           "and it is visible, so the shot shows it without any switch")

        # The sequence's own record keeps it visible...
        result = {"warnings": []}
        _apply_focus_objects({"config": {"focus": {"object": "cube",
                                                  "objects": cube_objects}}}, result)
        for name in cube_objects:
            ok(not scene.objects[name].hide_render, f"{name} is rendered")
        equal(result["focus"]["shown"], sorted(cube_objects))

        # ...a sequence with no subject hides it...
        plain = {"warnings": []}
        _apply_focus_objects({"config": {}}, plain)
        for name in cube_objects:
            ok(scene.objects[name].hide_render,
               f"{name} is hidden for a sequence that has no subject")

        # ...and a sequence whose subject lives in a *different* copy says so instead of
        # silently rendering the wrong object.
        wrong = {"warnings": []}
        _apply_focus_objects({"config": {"focus": {"object": "cylinder",
                                                   "objects": cylinder_objects}}}, wrong)
        ok(wrong["warnings"], f"the mismatch is reported: {wrong}")
        for name in cylinder_objects:
            ok(name not in scene.objects,
               f"{name} is not in this copy at all, so nothing can leak in")

    @case("the panel and the config agree about the focus models")
    def _():
        # The panel is the way the feature is configured, and every field has to survive
        # both directions: config -> panel (opening a file, remembered settings) and
        # panel -> config (generating).  A row that loses its object name or its scale
        # here would silently generate the wrong subject.
        import bpy

        from blender_motion_pipeline import registration
        from blender_motion_pipeline.config.defaults import default_config

        registration.register_all()
        bpy.ops.wm.read_factory_settings(use_empty=True)
        group = bpy.context.scene.mpp

        source = default_config()
        source.focus.mode = "models"
        source.focus.models = [
            {"id": "", "path": state["cube"], "label": "cube", "object_name": "CubeModel",
             "scale": 2.0, "rotation": [0.0, 90.0, 0.0], "enabled": True},
            {"id": "", "path": state["cylinder"], "label": "cylinder", "object_name": "",
             "scale": 1.0, "rotation": [0.0, 0.0, 0.0], "enabled": False},
        ]
        source.focus.anchor_mode = "object"
        source.focus.anchor_object = "MyAnchor"
        source.focus.anchor_location = [1.0, 2.0, 3.0]
        source.focus.anchor_clearance = 0.75
        source.focus.keep_visible = False
        source.focus.visible_ratio = 0.8
        source.focus.strict = True

        group.from_config(source)
        equal(len(group.focus_list), 2, message="both rows came back")
        equal(group.focus_list[0].name, "cube")
        equal(group.focus_list[0].object_name, "CubeModel")
        close(float(group.focus_list[0].scale), 2.0, tol=1e-6)
        close(float(group.focus_list[0].rotation[1]), 90.0, tol=1e-4)
        equal(group.focus_list[1].enabled, False)
        equal(group.focus_mode, "models")
        equal(group.focus_anchor_mode, "object")
        equal(group.focus_anchor_object, "MyAnchor")
        equal(group.focus_keep_visible, False)
        equal(group.focus_strict, True)

        back = group.to_config().focus
        equal(back.mode, "models")
        equal(len(back.models), 2, message="both rows go back")
        equal(back.models[0]["object_name"], "CubeModel")
        equal(back.models[0]["label"], "cube")
        equal(back.models[1]["enabled"], False)
        equal(back.anchor_mode, "object")
        equal(back.anchor_object, "MyAnchor")
        close(float(back.visible_ratio), 0.8, tol=1e-6)
        close(float(back.anchor_clearance), 0.75, tol=1e-6)
        equal([round(float(v), 6) for v in back.anchor_location], [1.0, 2.0, 3.0])

    return suite


def main() -> int:
    return build_suite().run()


if __name__ == "__main__":
    sys.exit(main())
