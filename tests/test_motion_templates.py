"""Motion template parser / animator tests (pure Python).

The semantic assertions here are the important ones: whatever the axis mapping
does internally, ``dolly_in`` must move the camera along its own view axis,
``pan_right`` must rotate it to its right, ``pedestal_up`` must raise it, and
``truck_right`` must strafe it right.  Those four cover the axis/sign contract
described in the module docstring.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _boot  # noqa: E402,F401  (the add-on folder may be called anything)

from blender_motion_pipeline.camera import motion_templates as mt  # noqa: E402
from blender_motion_pipeline.config import defaults  # noqa: E402
from blender_motion_pipeline.config.models import ConfigError, TemplateUnitScale  # noqa: E402
from blender_motion_pipeline.io.json_io import JsonError  # noqa: E402
from blender_motion_pipeline.tests.harness import (  # noqa: E402
    Suite, close, equal, ok, raises, vec_close,
)


def _reference_path() -> str:
    return defaults.discover_template_path()


def _base_camera(position=(0.0, 0.0, 10.0), columns=None):
    """Camera-to-world matrix for a camera at ``position``.

    ``columns`` is ``(right, up, back)`` in world space and is written straight
    into the matrix columns, which is exactly what ``axis_basis`` reads.  A
    right-handed camera basis requires ``right x up == back``; the default
    (identity) therefore means the camera looks down world ``-Z`` with ``+X``
    right and ``+Y`` up.
    """
    basis = columns or ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    matrix = [[float(basis[col][row]) for col in range(3)] + [0.0] for row in range(3)]
    matrix.append([0.0, 0.0, 0.0, 1.0])
    matrix[0][3] = position[0]
    matrix[1][3] = position[1]
    matrix[2][3] = position[2]
    return matrix


#: A camera looking along world ``-Y`` with world ``+Z`` up, chosen so that
#: "forward", "right" and "up" are all distinct from the world axes and a sign
#: mistake cannot cancel itself out.
#:
#: Handedness check (this is the trap): right x up must equal ``back``, so with
#: up = ``+Z`` and back = ``(0, +1, 0)`` the right axis has to be ``-X``.
#: ``det = +1`` and ``right x up == back`` are asserted in the suite; a
#: determinant of -1 is a reflection, not a rotation, and silently mirrors every
#: derived direction.
_LOOK_NEG_Y_RIGHT = (-1.0, 0.0, 0.0)
_LOOK_NEG_Y_UP = (0.0, 0.0, 1.0)
_LOOK_NEG_Y_BACK = (0.0, 1.0, 0.0)


def _look_neg_y(position=(0.0, 0.0, 10.0)):
    """Camera at ``position`` looking along world ``-Y`` with world ``+Z`` up."""
    return _base_camera(
        position, (_LOOK_NEG_Y_RIGHT, _LOOK_NEG_Y_UP, _LOOK_NEG_Y_BACK)
    )


def build_suite() -> Suite:
    suite = Suite("test_motion_templates")

    # -- parsing ---------------------------------------------------------
    @suite.case("parses the reference document (80 templates, [roll,pitch,yaw] keys)")
    def _():
        path = _reference_path()
        ok(bool(path), "reference template file must be discoverable")
        library = mt.load_template_file(path)
        equal(len(library), 80)
        equal(library.source, os.path.abspath(path))
        template = library.get("dolly_in_01_standard")
        equal(template.frame_min, 0)
        equal(template.frame_max, 80)
        equal(template.duration_frames, 80)
        equal(len(template.keyframes), 3)
        vec_close(template.keyframes[0].location, (0.0, 0.0, 0.0))
        vec_close(template.keyframes[-1].location, (300.0, 0.0, 0.0))
        close(template.keyframes[0].focal, 35.0)
        ok("dolly_in" in template.name)
        ok(all(t.name for t in library), "every template has a name")

    @suite.case("accepts alternate document shapes")
    def _():
        wrapped = mt.parse_template_document(
            {"templates": [{"id": "a", "keys": [{"frame": 0}, {"frame": 10}]}]},
            source="wrapped",
        )
        equal([t.name for t in wrapped], ["a"])
        mapping = mt.parse_template_document(
            {"pan": {"keys": [{"frame": 0}, {"frame": 5}]}}, source="map"
        )
        equal([t.name for t in mapping], ["pan"])
        single = mt.parse_template_document(
            {"id": "solo", "keys": [{"frame": 0}, {"frame": 1}]}, source="single"
        )
        equal([t.name for t in single], ["solo"])
        dictkeys = mt.parse_template_document(
            {"id": "dk", "keys": {"0": {"location": [0, 0, 0]}, "20": {"location": [1, 0, 0]}}},
            source="dictkeys",
        )
        equal(len(dictkeys[0].keyframes), 2)
        equal(dictkeys[0].frame_max, 20)

    @suite.case("accepts key aliases and dict vectors")
    def _():
        templates = mt.parse_template_document([{
            "name": "aliased",
            "parameters": {"note": "x"},
            "keyframes": [
                {"t": 0, "position": {"x": 1, "y": 2, "z": 3}, "angles": {"roll": 1, "pitch": 2, "yaw": 3}, "lens": 50},
                {"time": 12, "pos": [4, 5, 6], "rot": [7, 8, 9], "focal_length": 24},
                [24, [1, 1, 1], [0, 0, 0], 18],
            ],
        }], source="aliases")
        template = templates[0]
        equal(template.name, "aliased")
        equal(len(template.keyframes), 3)
        vec_close(template.keyframes[0].location, (1, 2, 3))
        vec_close(template.keyframes[0].rotation, (1, 2, 3))
        close(template.keyframes[0].focal, 50)
        vec_close(template.keyframes[1].location, (4, 5, 6))
        close(template.keyframes[1].focal, 24)
        equal(template.keyframes[2].frame, 24)
        close(template.keyframes[2].focal, 18)
        equal(template.parameters["note"], "x")

    @suite.case("rejects malformed documents with clear errors")
    def _():
        raises(ConfigError, lambda: mt.parse_template_document({}, source="empty"))
        raises(ConfigError, lambda: mt.parse_template_document([], source="emptylist"))
        raises(ConfigError, lambda: mt.parse_template_document([{"keys": []}], source="noname"))
        raises(ConfigError, lambda: mt.parse_template_document([{"id": "x"}], source="nokeys"))
        raises(ConfigError, lambda: mt.parse_template_document(
            [{"id": "x", "keys": [{"frame": 0}, {"frame": 0}]}], source="dupframes"))
        raises(ConfigError, lambda: mt.parse_template_document(
            [{"id": "x", "keys": [{"frame": -5}, {"frame": 1}]}], source="negframe"))
        raises(ConfigError, lambda: mt.parse_template_document(
            [{"id": "x", "keys": [{"location": [0, 0, 0]}]}], source="noframe"))
        raises(ConfigError, lambda: mt.parse_template_document(
            [{"id": "x", "keys": [{"frame": 0, "location": "abc"}]}], source="badvec"))
        raises(ConfigError, lambda: mt.parse_template_document(
            [{"id": "dup", "keys": [{"frame": 0}]}, {"id": "dup", "keys": [{"frame": 0}]}],
            source="dupid"))
        raises(JsonError, lambda: mt.load_template_file(r"E:\definitely\missing\templates.json"))
        raises(JsonError, lambda: mt.parse_template_text("{not json", source="text"))

    @suite.case("interpolation clamps, blends and wraps yaw the short way")
    def _():
        template = mt.MotionTemplate(name="t", keyframes=[
            mt.TemplateKeyframe(0, (0, 0, 0), (0, 0, 0), 20.0),
            mt.TemplateKeyframe(10, (100, 0, 0), (0, 0, 20.0), 40.0),
        ]).validate()
        before = template.interpolate(-5)
        vec_close(before.location, (0, 0, 0))
        close(before.focal, 20.0)
        middle = template.interpolate(5)
        vec_close(middle.location, (50, 0, 0))
        close(middle.focal, 30.0)
        after = template.interpolate(50)
        vec_close(after.location, (100, 0, 0))
        close(after.focal, 40.0)

        wrapping = mt.MotionTemplate(name="w", keyframes=[
            mt.TemplateKeyframe(0, (0, 0, 0), (0, 0, 350.0), 35.0),
            mt.TemplateKeyframe(10, (0, 0, 0), (0, 0, 10.0), 35.0),
        ]).validate()
        midpoint = wrapping.interpolate(5)
        close(midpoint.rotation[2], 360.0, tol=1e-6, message="should pass through 0/360, not 180")

    @suite.case("focal inheritance when only some keys declare a focal")
    def _():
        template = mt.MotionTemplate(name="f", keyframes=[
            mt.TemplateKeyframe(0, (0, 0, 0), (0, 0, 0), None),
            mt.TemplateKeyframe(10, (0, 0, 0), (0, 0, 0), 60.0),
        ]).validate()
        close(template.interpolate(0).focal, 60.0, message="inherits the nearest declared focal")
        close(template.interpolate(10).focal, 60.0)
        empty = mt.MotionTemplate(name="e", keyframes=[
            mt.TemplateKeyframe(0), mt.TemplateKeyframe(5),
        ]).validate()
        equal(empty.focals(), [])
        equal(empty.effective_focal_range(), (None, None))

    @suite.case("library overrides patch keyframes and parameters")
    def _():
        library = mt.MotionTemplateLibrary.from_entries([
            {"id": "a", "keys": [{"frame": 0, "location": [0, 0, 0], "focal": 35},
                                 {"frame": 40, "location": [100, 0, 0], "focal": 35}]},
            {"id": "b", "keys": [{"frame": 0}, {"frame": 40}]},
        ], source="inline")
        library.apply_overrides({
            "a": {"frame_scale": 2.0, "location_scale": 2.0, "focal": 50, "tag": "custom"},
            "*": {"frame_offset": 1},
        })
        template = library.get("a")
        equal(template.frame_max, 81)
        close(template.keyframes[-1].location[0], 200.0)
        close(template.keyframes[0].focal, 50.0)
        equal(template.parameters["tag"], "custom")
        equal(library.get("b").frame_min, 1)
        raises(ConfigError, lambda: library.apply_overrides({"nope": {}}))
        raises(ConfigError, lambda: library.apply_overrides({"a": {"keys": []}}))

    @suite.case("restrict_to validates names")
    def _():
        library = mt.MotionTemplateLibrary.from_entries(
            [{"id": "a", "keys": [{"frame": 0}]}, {"id": "b", "keys": [{"frame": 0}]}],
            source="inline",
        )
        library.restrict_to(["b"])
        equal(library.names, ["b"])
        raises(ConfigError, lambda: library.restrict_to(["missing"]))

    @suite.case("library from_config falls back to embedded templates with a warning")
    def _():
        class Section:
            template_path = ""
            template_data = []
            template_names = []
            template_overrides = {}
            frame_start = 0
            frame_scale = 1.0
            interpolation = "BEZIER"
        library = mt.MotionTemplateLibrary.from_config(Section())
        ok(len(library) >= 3, f"expected fallback templates, got {len(library)}")
        ok(library.source in ("embedded",) or os.path.isfile(library.source), library.source)

    @suite.case("library from_config raises for an explicitly requested bad file")
    def _():
        class Section:
            template_path = r"E:\nope\missing_templates.json"
            template_data = []
            template_names = []
            template_overrides = {}
            frame_start = 0
            frame_scale = 1.0
            interpolation = "BEZIER"
        raises(JsonError, lambda: mt.MotionTemplateLibrary.from_config(Section()))

    @suite.case("library from_config accepts inline template data")
    def _():
        class Section:
            template_path = ""
            template_data = [{"id": "inline_a", "keys": [{"frame": 0}, {"frame": 10}]}]
            template_names = ["inline_a"]
            template_overrides = {}
            frame_start = 0
            frame_scale = 1.0
            interpolation = "BEZIER"
        library = mt.MotionTemplateLibrary.from_config(Section())
        equal(library.names, ["inline_a"])
        equal(library.source, "config.motion.template_data")

    # -- semantics -------------------------------------------------------
    @suite.case("the test camera fixture is a proper rotation (det +1, right-handed)")
    def _():
        base = _look_neg_y()
        right, up, forward = mt.axis_basis(base)
        vec_close(right, (-1.0, 0.0, 0.0), tol=1e-9, message="fixture right axis")
        vec_close(up, (0.0, 0.0, 1.0), tol=1e-9, message="fixture up axis")
        vec_close(forward, (0.0, -1.0, 0.0), tol=1e-9, message="fixture view axis")
        # right x up == back == -forward for a right-handed camera basis.
        cross = (
            right[1] * up[2] - right[2] * up[1],
            right[2] * up[0] - right[0] * up[2],
            right[0] * up[1] - right[1] * up[0],
        )
        vec_close(cross, mt.vec_scale(forward, -1.0), tol=1e-9,
                  message="fixture basis must be right-handed")
        determinant = (
            base[0][0] * (base[1][1] * base[2][2] - base[1][2] * base[2][1])
            - base[0][1] * (base[1][0] * base[2][2] - base[1][2] * base[2][0])
            + base[0][2] * (base[1][0] * base[2][1] - base[1][1] * base[2][0])
        )
        close(determinant, 1.0, tol=1e-9, message="fixture must not be a reflection")
        # The quaternion conversion must agree with the columns.
        quaternion = mt.matrix_to_quaternion(base)
        vec_close(mt.quat_rotate(quaternion, (0.0, 0.0, -1.0)), forward, tol=1e-9,
                  message="quaternion view direction must match axis_basis")

    @suite.case("dolly_in moves the camera forward along its own view axis")
    def _():
        library = mt.MotionTemplateLibrary.from_entries(
            [{"id": "dolly_in", "keys": [
                {"frame": 0, "location": [0, 0, 0], "rotation": [0, 0, 0]},
                {"frame": 80, "location": [300, 0, 0], "rotation": [0, 0, 0]},
            ]}], source="semantics")
        generator = mt.MotionTemplateGenerator(frame_start=0)
        base = _look_neg_y()
        animation = generator.generate(library.get("dolly_in"), base_matrix=base, base_focal=35.0)
        _right, _up, forward = mt.axis_basis(base)
        vec_close(forward, (0.0, -1.0, 0.0), tol=1e-9,
                  message="the probe camera must look along -Y for this test to mean anything")
        delta = mt.vec_sub(animation.samples[-1].position, animation.samples[0].position)
        vec_close(delta, mt.vec_scale(forward, 3.0), tol=1e-5,
                  message="300 template units at 0.01 m/unit must be 3 m forward")
        ok(delta[1] < 0, f"camera should move toward -Y, got {delta}")

    @suite.case("dolly_out reverses it")
    def _():
        library = mt.MotionTemplateLibrary.from_entries(
            [{"id": "dolly_out", "keys": [
                {"frame": 0}, {"frame": 80, "location": [-300, 0, 0]},
            ]}], source="semantics")
        base = _look_neg_y()
        forward = mt.axis_basis(base)[2]
        animation = mt.MotionTemplateGenerator(frame_start=0).generate(
            library.get("dolly_out"), base_matrix=base, base_focal=35.0)
        delta = mt.vec_sub(animation.samples[-1].position, animation.samples[0].position)
        vec_close(delta, mt.vec_scale(forward, -3.0), tol=1e-5,
                  message="-300 units must move 3 m backwards along the view axis")
        close(delta[1], 3.0, tol=1e-5, message="backwards from -Y is +Y")

    @suite.case("pan_right turns the camera to its right, pan_left to its left")
    def _():
        library = mt.MotionTemplateLibrary.from_entries([
            {"id": "pan_right", "keys": [
                {"frame": 0, "rotation": [0, 0, 0]}, {"frame": 80, "rotation": [0, 0, 30]}]},
            {"id": "pan_left", "keys": [
                {"frame": 0, "rotation": [0, 0, 0]}, {"frame": 80, "rotation": [0, 0, -30]}]},
        ], source="semantics")
        generator = mt.MotionTemplateGenerator(frame_start=0)
        base = _look_neg_y()
        right, _up, forward = mt.axis_basis(base)

        animation = generator.generate(library.get("pan_right"), base_matrix=base, base_focal=35.0)
        start_dir = mt.quat_rotate(animation.samples[0].quaternion, (0, 0, -1))
        end_dir = mt.quat_rotate(animation.samples[-1].quaternion, (0, 0, -1))
        vec_close(start_dir, forward, tol=1e-6, message="start frame must keep the original aim")
        # Turning right means the view direction swings toward the camera's own
        # right axis, i.e. the component along `right` becomes positive.
        lateral = sum(a * b for a, b in zip(end_dir, right))
        ok(lateral > 0.4, f"pan_right should swing toward the right axis ({right}), got {end_dir}")

        animation_left = generator.generate(library.get("pan_left"), base_matrix=base, base_focal=35.0)
        end_left = mt.quat_rotate(animation_left.samples[-1].quaternion, (0, 0, -1))
        lateral_left = sum(a * b for a, b in zip(end_left, right))
        ok(lateral_left < -0.4, f"pan_left should swing toward -right, got {end_left}")
        close(mt.quat_angle_between(animation.samples[0].quaternion, animation.samples[-1].quaternion),
              30.0, tol=1e-3, message="pan magnitude")

    @suite.case("roll rotates about the camera's own view axis, not a world axis")
    def _():
        library = mt.MotionTemplateLibrary.from_entries(
            [{"id": "roll", "keys": [{"frame": 0}, {"frame": 80, "rotation": [20, 0, 0]}]}],
            source="semantics")
        base = _look_neg_y()
        right, up, forward = mt.axis_basis(base)
        animation = mt.MotionTemplateGenerator(frame_start=0).generate(
            library.get("roll"), base_matrix=base, base_focal=35.0)
        start_up = mt.quat_rotate(animation.samples[0].quaternion, (0, 1, 0))
        end_up = mt.quat_rotate(animation.samples[-1].quaternion, (0, 1, 0))
        vec_close(start_up, up, tol=1e-9, message="frame 0 must preserve the camera up axis")
        close(mt.quat_angle_between(animation.samples[0].quaternion, animation.samples[-1].quaternion),
              20.0, tol=1e-3, message="roll magnitude")
        # The aim must not change: a roll spins the frame, it does not turn it.
        end_dir = mt.quat_rotate(animation.samples[-1].quaternion, (0, 0, -1))
        vec_close(end_dir, forward, tol=1e-9, message="roll must not change the view direction")
        # A roll about the view axis swings the up vector toward the right axis.
        tilt = sum(a * b for a, b in zip(end_up, right))
        ok(abs(tilt) > 0.3, f"a 20 deg roll should tilt the up vector, got {end_up}")

    @suite.case("pedestal_up/down move along the camera's own up axis")
    def _():
        library = mt.MotionTemplateLibrary.from_entries([
            {"id": "ped_up", "keys": [{"frame": 0}, {"frame": 80, "location": [0, 0, 120]}]},
            {"id": "ped_down", "keys": [{"frame": 0}, {"frame": 80, "location": [0, 0, -35]}]},
        ], source="semantics")
        generator = mt.MotionTemplateGenerator(frame_start=0)
        base = _look_neg_y()
        _right, up, _forward = mt.axis_basis(base)
        animation = generator.generate(library.get("ped_up"), base_matrix=base, base_focal=35.0)
        delta = mt.vec_sub(animation.samples[-1].position, animation.samples[0].position)
        vec_close(delta, mt.vec_scale(up, 1.2), tol=1e-5, message="120 units = 1.2 m up")

        down = generator.generate(library.get("ped_down"), base_matrix=base, base_focal=35.0)
        down_delta = mt.vec_sub(down.samples[-1].position, down.samples[0].position)
        vec_close(down_delta, mt.vec_scale(up, -0.35), tol=1e-5)

    @suite.case("truck_right strafes along the camera's own right axis")
    def _():
        library = mt.MotionTemplateLibrary.from_entries(
            [{"id": "truck_right", "keys": [
                {"frame": 0}, {"frame": 80, "location": [0, 200, 0]}]}], source="semantics")
        base = _look_neg_y()
        right, _up, _forward = mt.axis_basis(base)
        animation = mt.MotionTemplateGenerator(frame_start=0).generate(
            library.get("truck_right"), base_matrix=base, base_focal=35.0)
        delta = mt.vec_sub(animation.samples[-1].position, animation.samples[0].position)
        vec_close(delta, mt.vec_scale(right, 2.0), tol=1e-5)

    @suite.case("tilt_up pitches the camera up (positive pitch looks up)")
    def _():
        library = mt.MotionTemplateLibrary.from_entries([
            {"id": "tilt_up", "keys": [{"frame": 0}, {"frame": 80, "rotation": [0, 20, 0]}]},
            {"id": "tilt_down", "keys": [{"frame": 0}, {"frame": 80, "rotation": [0, -20, 0]}]},
        ], source="semantics")
        generator = mt.MotionTemplateGenerator(frame_start=0)
        base = _look_neg_y()
        up_animation = generator.generate(library.get("tilt_up"), base_matrix=base, base_focal=35.0)
        end_up = mt.quat_rotate(up_animation.samples[-1].quaternion, (0, 0, -1))
        ok(end_up[2] > 0.2, f"tilt_up should raise the aim (+Z), got {end_up}")
        down_animation = generator.generate(library.get("tilt_down"), base_matrix=base, base_focal=35.0)
        end_down = mt.quat_rotate(down_animation.samples[-1].quaternion, (0, 0, -1))
        ok(end_down[2] < -0.2, f"tilt_down should lower the aim, got {end_down}")

    @suite.case("zoom_in shortens the focal length, zoom_out lengthens it")
    def _():
        library = mt.MotionTemplateLibrary.from_entries([
            {"id": "zoom_in", "keys": [{"frame": 0, "focal": 85}, {"frame": 80, "focal": 24}]},
            {"id": "zoom_out", "keys": [{"frame": 0, "focal": 24}, {"frame": 80, "focal": 85}]},
        ], source="semantics")
        generator = mt.MotionTemplateGenerator(frame_start=0)
        base = _look_neg_y()
        zoom_in = generator.generate(library.get("zoom_in"), base_matrix=base, base_focal=50.0)
        close(zoom_in.samples[0].focal, 85.0)
        close(zoom_in.samples[-1].focal, 24.0)
        close(zoom_in.samples[40].focal, 54.5, tol=0.6, message="linear focal ramp")
        zoom_out = generator.generate(library.get("zoom_out"), base_matrix=base, base_focal=50.0)
        close(zoom_out.samples[0].focal, 24.0)
        close(zoom_out.samples[-1].focal, 85.0)

    @suite.case("a template without focals keeps the camera's original lens")
    def _():
        library = mt.MotionTemplateLibrary.from_entries(
            [{"id": "no_focal", "keys": [{"frame": 0}, {"frame": 30, "location": [10, 0, 0]}]}],
            source="semantics")
        animation = mt.MotionTemplateGenerator(frame_start=0).generate(
            library.get("no_focal"), base_matrix=_look_neg_y(), base_focal=42.0)
        close(animation.samples[0].focal, 42.0)
        close(animation.samples[-1].focal, 42.0)
        ok(any("no focal length" in note for note in animation.notes), animation.notes)

    @suite.case("frame_start and frame_scale shift the timeline")
    def _():
        library = mt.MotionTemplateLibrary.from_entries(
            [{"id": "m", "keys": [{"frame": 0}, {"frame": 80}]}], source="semantics")
        template = library.get("m")
        scaled = mt.MotionTemplateGenerator(frame_start=10, frame_scale=0.5).generate(
            template, base_matrix=_look_neg_y(), base_focal=35.0)
        equal(scaled.frame_start, 10)
        equal(scaled.frame_end, 50)
        equal(scaled.frame_count, 41)
        explicit = mt.MotionTemplateGenerator(frame_start=0).generate(
            template, base_matrix=_look_neg_y(), base_focal=35.0, frame_start=100, frame_end=104)
        equal(explicit.frame_start, 100)
        equal(explicit.frame_end, 104)
        equal(len(explicit.samples), 5)

    @suite.case("unit scale sign/axis overrides change the result predictably")
    def _():
        library = mt.MotionTemplateLibrary.from_entries(
            [{"id": "dolly", "keys": [{"frame": 0}, {"frame": 80, "location": [300, 0, 0]}]}],
            source="semantics")
        base = _look_neg_y()
        forward = mt.axis_basis(base)[2]
        # Flipping location_forward turns the same template into a backward move.
        flipped = TemplateUnitScale(location_forward=1.0)
        animation = mt.MotionTemplateGenerator(unit_scale=flipped, frame_start=0).generate(
            library.get("dolly"), base_matrix=base, base_focal=35.0)
        delta = mt.vec_sub(animation.samples[-1].position, animation.samples[0].position)
        vec_close(delta, mt.vec_scale(forward, -3.0), tol=1e-5,
                  message="location_forward=1 must invert the dolly direction")
        # location_scale=1 switches the unit interpretation from cm to m.
        metric = TemplateUnitScale(location_scale=1.0)
        animation_m = mt.MotionTemplateGenerator(unit_scale=metric, frame_start=0).generate(
            library.get("dolly"), base_matrix=base, base_focal=35.0)
        delta_m = mt.vec_sub(animation_m.samples[-1].position, animation_m.samples[0].position)
        vec_close(delta_m, mt.vec_scale(forward, 300.0), tol=1e-3)

    @suite.case("the original camera orientation is preserved exactly at frame 0")
    def _():
        library = mt.MotionTemplateLibrary.from_entries(
            [{"id": "fixed", "keys": [{"frame": 0}, {"frame": 40}, {"frame": 80}]}],
            source="semantics")
        base = _look_neg_y((1.5, -2.5, 3.25))
        animation = mt.MotionTemplateGenerator(frame_start=0).generate(
            library.get("fixed"), base_matrix=base, base_focal=35.0)
        for sample in animation.samples:
            vec_close(sample.position, (1.5, -2.5, 3.25), tol=1e-9)
            close(mt.quat_angle_between(sample.quaternion, mt.matrix_to_quaternion(base)), 0.0, tol=1e-6)

    @suite.case("animation payload has the documented shape")
    def _():
        library = mt.MotionTemplateLibrary.from_entries(
            [{"id": "doc", "keys": [{"frame": 0, "focal": 35}, {"frame": 80, "focal": 50}]}],
            source="semantics")
        animation = mt.MotionTemplateGenerator(frame_start=0, interpolation="LINEAR").generate(
            library.get("doc"), base_matrix=_look_neg_y(), base_focal=35.0)
        payload = animation.to_dict(include_samples=True)
        for key in ("template_name", "frame_start", "frame_end", "fps", "keyframes", "parameters"):
            ok(key in payload, f"{key!r} missing from the animation payload")
        equal(payload["template_name"], "doc")
        equal(payload["frame_start"], 0)
        equal(payload["frame_end"], 80)
        equal(payload["interpolation"], "LINEAR")
        equal(len(payload["samples"]), 81)
        equal(payload["samples"][0]["frame"], 0)
        ok("rotation_quaternion" in payload["samples"][0])
        equal(payload["source_focal_range"], [35.0, 50.0])
        ok(len(payload["keyframes"]) <= 13, "keyframe summary must stay compact")

    @suite.case("is_static recognises genuinely static templates")
    def _():
        library = mt.MotionTemplateLibrary.from_entries([
            {"id": "s", "keys": [{"frame": 0}, {"frame": 40}, {"frame": 80}]},
            {"id": "m", "keys": [{"frame": 0}, {"frame": 80, "location": [1, 0, 0]}]},
        ], source="semantics")
        ok(library.get("s").is_static())
        ok(not library.get("m").is_static())

    @suite.case("every reference template generates a finite animation")
    def _():
        path = _reference_path()
        if not path:
            return
        library = mt.load_template_file(path)
        generator = mt.MotionTemplateGenerator(frame_start=1)
        base = _look_neg_y()
        checked = 0
        for template in library:
            animation = generator.generate(template, base_matrix=base, base_focal=35.0)
            ok(animation.frame_count > 0, f"{template.name} produced no frames")
            for sample in (animation.samples[0], animation.samples[-1]):
                for value in (*sample.position, *sample.quaternion, sample.focal):
                    ok(value == value and abs(value) != float("inf"),
                       f"{template.name} produced a non-finite value")
                close(sum(v * v for v in sample.quaternion), 1.0, tol=1e-6,
                      message=f"{template.name} quaternion not normalised")
            checked += 1
        equal(checked, len(library))

    @suite.case("quaternion helpers agree with matrix conversion")
    def _():
        for axis, angle in (("X", 30.0), ("Y", -45.0), ("Z", 90.0)):
            quaternion = mt.quat_from_axis_angle(axis, angle)
            matrix = mt.quaternion_to_matrix(quaternion)
            back = mt.matrix_to_quaternion(matrix)
            close(mt.quat_angle_between(quaternion, back), 0.0, tol=1e-6,
                  message=f"round trip about {axis}")
            # A quaternion and its matrix must move a vector to the same place.
            probe = (0.3, -0.7, 0.5)
            rotated_quat = mt.quat_rotate(quaternion, probe)
            rotated_matrix = tuple(
                sum(matrix[row][col] * probe[col] for col in range(3)) for row in range(3)
            )
            vec_close(rotated_quat, rotated_matrix, tol=1e-9,
                      message=f"quat_rotate vs matrix for {axis}{angle}")
        close(mt.quat_angle_between((1, 0, 0, 0), (1, 0, 0, 0)), 0.0)
        euler = mt.quat_to_euler_xyz(mt.quat_from_axis_angle("Z", 90.0))
        close(euler[2], 1.5707963267948966, tol=1e-9)
        # Rotating about +X by +90 must send -Z to +Y (Blender camera up).
        vec_close(mt.quat_rotate(mt.quat_from_axis_angle("X", 90.0), (0.0, 0.0, -1.0)),
                  (0.0, 1.0, 0.0), tol=1e-9)

    @suite.case("a scaled camera matrix does not scale the motion or bend its axis")
    def _():
        # Regression, measured on the user's scene: one camera carries scale 0.542, so
        # its world matrix is ``0.542 * R``.  ``axis_basis`` used the raw columns (not
        # unit) and ``matrix_to_quaternion`` read a **24.7 deg** wrong orientation from
        # it, which made a 3.4 m forward push travel 1.843 m at 22 deg off the camera's
        # own view axis.
        generator = mt.MotionTemplateGenerator()
        template = mt.MotionTemplate.from_dict({
            "id": "push", "keys": [
                {"frame": 0, "location": [0, 0, 0], "rotation": [0, 0, 0], "focal": 35},
                {"frame": 4, "location": [340, 0, 0], "rotation": [0, 0, 0], "focal": 35},
            ],
        })
        rotation = mt.quaternion_to_matrix(mt.quat_from_axis_angle("Z", 37.0))
        unit_matrix = [list(row) + [0.0] for row in rotation] + [[0.0, 0.0, 0.0, 1.0]]

        reference = generator.generate(template, base_matrix=unit_matrix, base_focal=35.0)
        reference_delta = mt.vec_sub(reference.samples[-1].position, reference.samples[0].position)

        for scale in (0.542, 0.5, 2.0):
            scaled = [[value * scale for value in row[:3]] + [row[3]] for row in unit_matrix]
            right, up, forward = mt.axis_basis(scaled)
            for name, axis in (("right", right), ("up", up), ("forward", forward)):
                close(mt.vec_length(axis), 1.0, tol=1e-9, message=f"{name} at scale {scale}")
            # The extracted orientation must be the true rotation, not a bent one.
            close(
                mt.quat_angle_between(
                    mt.matrix_to_quaternion(unit_matrix), mt.matrix_to_quaternion(scaled)
                ),
                0.0, tol=1e-9, message=f"orientation at scale {scale}",
            )
            animation = generator.generate(template, base_matrix=scaled, base_focal=35.0)
            delta = mt.vec_sub(animation.samples[-1].position, animation.samples[0].position)
            close(mt.vec_length(delta), mt.vec_length(reference_delta), tol=1e-9,
                  message=f"push distance at scale {scale}")
            vec_close(delta, reference_delta, tol=1e-9,
                      message=f"push direction at scale {scale}")

    @suite.case("embedded fallback templates are usable")
    def _():
        equal(len(mt.EMBEDDED_TEMPLATES), 5)
        library = mt.MotionTemplateLibrary(mt.EMBEDDED_TEMPLATES, source="embedded")
        for template in library:
            template.validate()
        animation = mt.MotionTemplateGenerator(frame_start=0).generate(
            library.get("dolly_in_01_standard"), base_matrix=_look_neg_y(), base_focal=35.0)
        equal(animation.frame_count, 81)

    @suite.case("template json round trips through to_dict")
    def _():
        library = mt.MotionTemplateLibrary.from_entries(
            [{"id": "rt", "keys": [{"frame": 0, "location": [1, 2, 3], "focal": 20},
                                   {"frame": 10, "rotation": [1, 2, 3]}]}], source="inline")
        payload = library.get("rt").to_dict()
        again = mt.parse_template_document([payload], source="roundtrip")
        equal(again[0].name, "rt")
        vec_close(again[0].keyframes[0].location, (1, 2, 3))
        close(again[0].keyframes[0].focal, 20.0)
        vec_close(again[0].keyframes[1].rotation, (1, 2, 3))

    return suite


if __name__ == "__main__":
    raise SystemExit(build_suite().run())
