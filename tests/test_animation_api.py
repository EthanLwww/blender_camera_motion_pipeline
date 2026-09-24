"""Regression tests for the version-agnostic Action helpers.

Blender 5.0 replaced the flat ``action.fcurves`` container with layered actions
(``action.layers[].strips[].channelbags[].fcurves``), so any code that walks the
old path breaks on 5.2.  These cases pin the behaviour that the pipeline relies
on, and they are written against *this* Blender's API rather than a guess.

Run with::

    blender -b -P tests/test_animation_api.py
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
if _PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, _PACKAGE_PARENT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _boot  # noqa: E402,F401  (the add-on folder may be called anything)

from blender_motion_pipeline.tests.harness import Suite, equal, ok  # noqa: E402
from blender_motion_pipeline.utils import animation as anim  # noqa: E402


def _scene_with_animated_camera():
    import bpy

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 10
    bpy.ops.object.camera_add(location=(0.0, 0.0, 5.0))
    camera = bpy.context.active_object
    scene.camera = camera
    for frame, x in ((1, 0.0), (5, 1.0), (10, 2.0)):
        scene.frame_set(frame)
        camera.location = (x, 0.0, 5.0)
        camera.keyframe_insert("location", frame=frame)
    camera.data.lens = 35.0
    camera.data.keyframe_insert("lens", frame=1)
    camera.data.lens = 50.0
    camera.data.keyframe_insert("lens", frame=10)
    scene.frame_set(1)
    return scene, camera


def build_suite() -> Suite:
    suite = Suite("test_animation_api")

    @suite.case("action_fcurves finds curves on this Blender's action layout")
    def _():
        _scene, camera = _scene_with_animated_camera()
        action = camera.animation_data.action
        ok(action is not None, "the camera must have an action")
        curves = anim.action_fcurves(action)
        paths = anim.action_data_paths(action)
        # Blender 5.x stores object- and data-level curves in the *same* action
        # datablock, so a camera animated on both location and lens has four
        # curves here.  An older Blender with two separate actions would give
        # three plus one; both shapes are accepted.
        ok(len(curves) >= 3, f"expected the location curves, got {[c.data_path for c in curves]}")
        ok("location" in paths, paths)
        equal(len([c for c in curves if c.data_path == "location"]), 3)

        data_action = camera.data.animation_data.action
        ok(data_action is not None, "the camera data must have an action")
        ok("lens" in anim.action_data_paths(data_action), anim.action_data_paths(data_action))

        # Sanity: on this Blender the legacy attribute really is gone, which is
        # exactly why the helper exists.
        equal(hasattr(action, "fcurves"), False,
              "this Blender version no longer exposes Action.fcurves")

    @suite.case("action_fcurves tolerates None and empty actions")
    def _():
        import bpy

        equal(anim.action_fcurves(None), [])
        empty = bpy.data.actions.new("MP_empty_probe")
        try:
            equal(anim.action_fcurves(empty), [])
            equal(anim.count_fcurves(empty), 0)
            equal(anim.action_data_paths(empty), [])
            equal(list(anim.iter_keyframes(empty)), [])
            equal(anim.set_interpolation(empty, "LINEAR"), 0)
        finally:
            bpy.data.actions.remove(empty)

    @suite.case("set_interpolation retimes every key")
    def _():
        _scene, camera = _scene_with_animated_camera()
        action = camera.animation_data.action
        expected = sum(
            len(curve.keyframe_points) for curve in anim.action_fcurves(action)
        )
        touched = anim.set_interpolation(action, "LINEAR")
        equal(touched, expected, f"every key must be retimed ({expected} expected)")
        ok(touched >= 9, f"3 location curves x 3 keys is the floor, got {touched}")
        for _curve, keyframe in anim.iter_keyframes(action):
            equal(keyframe.interpolation, "LINEAR")
        anim.set_interpolation(action, "CONSTANT")
        for _curve, keyframe in anim.iter_keyframes(action):
            equal(keyframe.interpolation, "CONSTANT")

    @suite.case("assign_action binds an action so it actually animates")
    def _():
        import bpy

        _scene, camera = _scene_with_animated_camera()
        source = camera.animation_data.action
        # A fresh object must pick up the action and evaluate it.
        bpy.ops.object.camera_add(location=(0.0, 0.0, 5.0))
        target = bpy.context.active_object
        equal(anim.assign_action(target, source), True)
        ok(target.animation_data is not None)
        equal(target.animation_data.action, source)
        bpy.context.scene.frame_set(5)
        evaluated = target.evaluated_get(bpy.context.evaluated_depsgraph_get())
        ok(abs(evaluated.matrix_world.translation.x - 1.0) < 1e-4,
           f"the action must drive the object: x={evaluated.matrix_world.translation.x}")

    @suite.case("clear_animation detaches actions and NLA")
    def _():
        _scene, camera = _scene_with_animated_camera()
        ok(camera.animation_data is not None)
        removed = anim.clear_animation(camera)
        ok(removed >= 1, removed)
        ok(camera.animation_data.action is None, "the action must be detached")
        equal(anim.actions_for(camera), [])
        equal(anim.clear_animation(None), 0)

    @suite.case("actions_for collects the direct action and NLA strip actions")
    def _():
        import bpy

        _scene, camera = _scene_with_animated_camera()
        direct = camera.animation_data.action
        equal([a.name for a in anim.actions_for(camera)], [direct.name])

        extra = bpy.data.actions.new("MP_nla_probe")
        try:
            track = camera.animation_data.nla_tracks.new()
            strip = track.strips.new("probe", 1, extra)
            ok(strip is not None)
            names = [a.name for a in anim.actions_for(camera)]
            ok(extra.name in names, names)
            ok(direct.name in names, names)
        finally:
            camera.animation_data.action = None
            for track in list(camera.animation_data.nla_tracks):
                camera.animation_data.nla_tracks.remove(track)
            bpy.data.actions.remove(extra)

    @suite.case("blender_context detects animation on a camera")
    def _():
        import bpy

        from blender_motion_pipeline.core import blender_context as bctx

        _scene, camera = _scene_with_animated_camera()
        snapshot = bctx.camera_snapshot(camera)
        equal(snapshot.had_animation, True)
        ok("location" in snapshot.animated_properties, snapshot.animated_properties)

        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.object.camera_add()
        plain = bpy.context.active_object
        plain_snapshot = bctx.camera_snapshot(plain)
        equal(plain_snapshot.had_animation, False)
        equal(plain_snapshot.animated_properties, [])

    @suite.case("clear_camera_animation clears both the object and its data")
    def _():
        from blender_motion_pipeline.core import blender_context as bctx

        _scene, camera = _scene_with_animated_camera()
        ok(camera.data.animation_data is not None)
        ok(camera.data.animation_data.action is not None)
        bctx.clear_camera_animation(camera)
        ok(camera.animation_data is None or camera.animation_data.action is None)
        ok(camera.data.animation_data is None or camera.data.animation_data.action is None)

    @suite.case("restore_camera resets transform, lens and clip range")
    def _():
        from blender_motion_pipeline.core import blender_context as bctx

        _scene, camera = _scene_with_animated_camera()
        snapshot = bctx.camera_snapshot(camera)
        camera.location = (9.0, 9.0, 9.0)
        camera.rotation_mode = "XYZ"
        camera.rotation_euler = (1.0, 1.0, 1.0)
        camera.data.lens = 5.0
        camera.data.clip_start = 9.0
        camera.data.clip_end = 11.0
        bctx.restore_camera(camera, snapshot)
        for actual, expected in zip(camera.location, snapshot.location):
            ok(abs(actual - expected) < 1e-6, (actual, expected))
        equal(camera.data.lens, snapshot.lens)
        equal(camera.data.clip_start, snapshot.clip_start)
        equal(camera.data.clip_end, snapshot.clip_end)

    @suite.case("a generated sequence carries a usable action on this Blender")
    def _():
        import bpy

        from blender_motion_pipeline.camera.motion_templates import (
            MotionTemplateGenerator, MotionTemplateLibrary,
        )

        scene, camera = _scene_with_animated_camera()
        keyframes = [
            {"frame": 0, "location": [0, 0, 0], "rotation": [0, 0, 0], "focal": 35},
            {"frame": 4, "location": [0, 0, -0.4], "rotation": [0, 0, 0], "focal": 35},
            {"frame": 8, "location": [0, 0, -0.8], "rotation": [0, 0, 0], "focal": 35},
        ]
        library = MotionTemplateLibrary.from_entries([{"id": "push", "keys": keyframes}],
                                                     source="probe")
        generator = MotionTemplateGenerator(frame_start=0)
        animation = generator.generate(
            library.get("push"), base_matrix=camera.matrix_world, base_focal=camera.data.lens
        )
        equal(animation.frame_count, 9)

        # Bake exactly as the generator does and confirm the curves are reachable.
        from blender_motion_pipeline.utils.animation import (
            action_fcurves, clear_animation, set_interpolation,
        )

        clear_animation(camera)
        for sample in animation.samples:
            camera.location = sample.position
            camera.rotation_mode = "QUATERNION"
            camera.rotation_quaternion = sample.quaternion
            camera.keyframe_insert("location", frame=sample.frame)
            camera.keyframe_insert("rotation_quaternion", frame=sample.frame)
        action = camera.animation_data.action
        curves = action_fcurves(action)
        ok(len(curves) >= 6, f"expected location+quaternion curves, got {len(curves)}")
        touched = set_interpolation(action, "LINEAR")
        ok(touched >= 9 * 6, f"expected at least one key per frame per curve, got {touched}")

        # The baked animation must move the camera along its own view axis
        # (this fixture camera looks straight down), by the template's 0.8 m.
        scene.frame_set(0)
        bpy.context.view_layer.update()
        start = camera.evaluated_get(
            bpy.context.evaluated_depsgraph_get()
        ).matrix_world.translation.copy()
        scene.frame_set(8)
        bpy.context.view_layer.update()
        end = camera.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.translation.copy()
        moved = (end - start).length
        ok(abs(moved - 0.8) < 0.05, f"expected an 0.8 m dolly, measured {moved:.4f} m")

    @suite.case("trajectory sampling uses the F-curves and matches the depsgraph")
    def _():
        # ``sample_camera_trajectory`` used to run scene.frame_set() per frame, which
        # re-evaluates the whole scene: on a scattering-heavy scene that is seconds per
        # frame, so a 216-frame sequence spent ~20 minutes building its trajectory
        # before the first frame was rendered.  A plain F-curve camera is now evaluated
        # from its curves; these cases pin both the shortcut and its equivalence.
        import bpy

        from blender_motion_pipeline.render import metadata_exporter as mx

        scene, camera = _scene_with_animated_camera()
        camera.rotation_mode = "QUATERNION"
        # location must drive the pose from the same slot the object uses -- the
        # fixture keys ``lens`` on the camera data, which in Blender 5 lives in the
        # *same* action as another slot (the regression this covers).
        for frame, x in ((1, 0.0), (5, 1.0), (10, 2.0)):
            scene.frame_set(frame)
            camera.location = (x, 0.5, 5.0)
            camera.keyframe_insert("location", frame=frame)

        equal(mx.analytic_camera_reason(camera), "",
              "a keyframed camera with no constraints must take the F-curve path")

        depsgraph = bpy.context.evaluated_depsgraph_get()
        frames = list(range(1, 11))
        poses = mx.analytic_camera_poses(camera, frames)
        ok(poses is not None and len(poses) == len(frames), "poses must be produced")
        ok(mx.verify_camera_poses(camera, scene, depsgraph, frames, poses),
           "the analytic poses must match the dependency graph")

        fast = mx.sample_camera_trajectory(camera, frame_start=1, frame_end=10,
                                          scene=scene, depsgraph=depsgraph)
        original = mx.analytic_camera_poses
        mx.analytic_camera_poses = lambda *a, **k: None
        try:
            slow = mx.sample_camera_trajectory(camera, frame_start=1, frame_end=10,
                                              scene=scene, depsgraph=depsgraph)
        finally:
            mx.analytic_camera_poses = original
        equal(len(fast), len(slow))
        for a, b in zip(fast, slow):
            equal(a.frame, b.frame)
            ok(abs(a.focal_length - b.focal_length) < 1e-6, (a.focal_length, b.focal_length))
            for name in ("r00", "r01", "r02", "tx", "r10", "r11", "r12", "ty",
                         "r20", "r21", "r22", "tz"):
                ok(abs(getattr(a, name) - getattr(b, name)) < 1e-5,
                   f"{name} differs: {getattr(a, name)} vs {getattr(b, name)}")

    @suite.case("a constrained camera reports why it needs the dependency graph")
    def _():
        import bpy

        from blender_motion_pipeline.render import metadata_exporter as mx

        _scene, camera = _scene_with_animated_camera()
        equal(mx.analytic_camera_reason(camera), "")
        constraint = camera.constraints.new(type="LIMIT_LOCATION")
        constraint.use_min_x = True
        ok("constraint" in mx.analytic_camera_reason(camera),
           mx.analytic_camera_reason(camera))
        equal(mx.analytic_camera_poses(camera, [1, 2]), None,
              "the shortcut must refuse a camera it cannot model")

    return suite


def main() -> int:
    return build_suite().run()


if __name__ == "__main__":
    sys.exit(main())
