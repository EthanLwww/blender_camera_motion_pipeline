"""Camera validation, search and export tests (pure Python, no bpy needed).

The scene is modelled with :class:`BBoxRayCaster`, which does exact slab
intersection against axis-aligned boxes, so "the camera is inside a wall" and
"a wall blocks the character" are reproducible without Blender.
"""

from __future__ import annotations

import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from blender_motion_pipeline.camera import camera_export as ce  # noqa: E402
from blender_motion_pipeline.camera import camera_search as cs  # noqa: E402
from blender_motion_pipeline.camera import camera_validator as cv  # noqa: E402
from blender_motion_pipeline.camera import motion_templates as mt  # noqa: E402
from blender_motion_pipeline.camera.scene_context import (  # noqa: E402
    BBoxRayCaster,
    CameraSnapshot,
    CharacterBox,
    MeshSnapshot,
    SceneContext,
    fibonacci_directions,
)
from blender_motion_pipeline.config.models import SearchSection, ValidationSection  # noqa: E402
from blender_motion_pipeline.tests.harness import (  # noqa: E402
    Suite, close, equal, ok, vec_close,
)

# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
ROOM_SIZE = 10.0
ROOM_HEIGHT = 4.0


def _wall_boxes():
    """A closed 10x10x4 room built from six thin slabs (closed box, solid walls)."""
    t = 0.2
    half = ROOM_SIZE / 2.0
    return [
        # floor
        MeshSnapshot("floor", (-half, -half, -t), (half, half, 0.0)),
        # ceiling
        MeshSnapshot("ceiling", (-half, -half, ROOM_HEIGHT), (half, half, ROOM_HEIGHT + t)),
        # four walls
        MeshSnapshot("wall_x_min", (-half - t, -half, 0.0), (-half, half, ROOM_HEIGHT)),
        MeshSnapshot("wall_x_max", (half, -half, 0.0), (half + t, half, ROOM_HEIGHT)),
        MeshSnapshot("wall_y_min", (-half, -half - t, 0.0), (half, -half, ROOM_HEIGHT)),
        MeshSnapshot("wall_y_max", (-half, half, 0.0), (half, half + t, ROOM_HEIGHT)),
    ]


def _blocker():
    """A tall thin wall across the room, offset so it does not sit on the origin.

    Deliberately *not* at ``x == 0``: the character fixture stands at the origin,
    and a wall centred there would bury the character inside geometry instead of
    merely standing between it and the far side of the room.
    """
    return MeshSnapshot("blocker", (1.4, -5.0, 0.0), (1.6, 5.0, ROOM_HEIGHT))


def _camera_snapshot(name="Camera", *, location=(0.0, 0.0, 1.6), lens=35.0,
                     clip_start=0.1, clip_end=100.0, rotation_quaternion=(1.0, 0.0, 0.0, 0.0)) -> CameraSnapshot:
    return CameraSnapshot(
        name=name,
        object_name=name,
        matrix_world=[[1, 0, 0, location[0]], [0, 1, 0, location[1]], [0, 0, 1, location[2]], [0, 0, 0, 1]],
        location=location,
        rotation_mode="QUATERNION",
        rotation_euler=(0.0, 0.0, 0.0),
        rotation_quaternion=rotation_quaternion,
        scale=(1.0, 1.0, 1.0),
        lens=lens,
        sensor_width=36.0,
        sensor_height=24.0,
        sensor_fit="AUTO",
        clip_start=clip_start,
        clip_end=clip_end,
        resolution_x=1920,
        resolution_y=1080,
        fps=24.0,
    )


def _context(boxes, *, characters=(), project=None) -> SceneContext:
    lo = [min(b.bbox_min[i] for b in boxes) for i in range(3)]
    hi = [max(b.bbox_max[i] for b in boxes) for i in range(3)]
    return SceneContext(
        scene=None,
        depsgraph=None,
        scene_name="fixture",
        cameras=[_camera_snapshot()],
        meshes=list(boxes),
        characters=list(characters),
        ray_caster=BBoxRayCaster(boxes),
        world_bbox_min=tuple(lo),  # type: ignore[arg-type]
        world_bbox_max=tuple(hi),  # type: ignore[arg-type]
        project_fn=project,
    )


def _character_center() -> CharacterBox:
    return CharacterBox(
        name="ch", bbox_min=(-0.3, -0.3, 0.0), bbox_max=(0.3, 0.3, 1.75),
        object_name="ch", animation="idle",
    )


def _point_camera(position, direction):
    """Camera sample looking along ``direction`` from ``position``."""
    from blender_motion_pipeline.camera.camera_search import look_at_quaternion

    quaternion = look_at_quaternion(direction)
    return mt.CameraSample(frame=0, position=tuple(position), quaternion=quaternion, focal=35.0)


def _animation(samples, *, frame_start=0, frame_end=None, focal=35.0):
    samples = list(samples)
    return mt.MotionAnimation(
        template_name="fixture",
        frame_start=frame_start,
        frame_end=frame_end if frame_end is not None else (samples[-1].frame if samples else frame_start),
        fps=24.0,
        interpolation="BEZIER",
        samples=samples,
    )


def _straight_animation(start, end, frames=9, direction=(0.0, 1.0, 0.0), focal=35.0):
    """A linear move between two points, aimed along ``direction``."""
    from blender_motion_pipeline.camera.camera_search import look_at_quaternion

    quaternion = look_at_quaternion(direction)
    samples = []
    for index in range(frames):
        alpha = index / float(max(1, frames - 1))
        position = tuple(a + (b - a) * alpha for a, b in zip(start, end))
        samples.append(mt.CameraSample(frame=index, position=position, quaternion=quaternion, focal=focal))
    return _animation(samples, frame_start=0, frame_end=frames - 1)


# --------------------------------------------------------------------------
# suite
# --------------------------------------------------------------------------
def build_suite() -> Suite:
    suite = Suite("test_camera_validation")

    @suite.case("sampled_frames always includes both endpoints and extras")
    def _():
        frames = cv.sampled_frames(0, 80, 10, [7, 33])
        equal(frames[0], 0)
        equal(frames[-1], 80)
        for expected in (10, 20, 30, 40, 50, 60, 70, 7, 33):
            ok(expected in frames, f"{expected} missing from {frames}")
        equal(cv.sampled_frames(5, 5, 10), [5])
        equal(len(cv.sampled_frames(0, 9, 3)), 4)  # 0, 3, 6, 9
        equal(cv.sampled_frames(0, 10, 100, [999]), [0, 10])

    @suite.case("a camera in open space passes and reports clearance")
    def _():
        boxes = _wall_boxes()
        context = _context(boxes)
        # sample_step=1 so the jump check is evaluated frame-to-frame, which is
        # what its limits are expressed in.
        config = ValidationSection(sample_step=1)
        validator = cv.CameraValidator(context, config)
        camera = context.cameras[0]
        animation = _straight_animation((0.0, -3.0, 1.6), (0.0, 3.0, 1.6))
        report = validator.validate(camera, animation, base_matrix=camera.matrix_world, base_focal=35.0)
        ok(report.passed, f"expected a pass, got {report.failures}: {report.messages}")
        ok(report.metrics.get("min_clearance", 0) > 0.5, report.metrics)
        equal(len(report.frames), 9)
        # The camera starts 3 m from where the artist's camera stands.
        close(report.offset_from_base, 3.0, tol=1e-9)
        close(report.metrics["max_position_jump"], 0.75, tol=1e-9)

    @suite.case("a coarse sample step scales the jump limit by sqrt(gap)")
    def _():
        context = _context(_wall_boxes())
        camera = context.cameras[0]
        # 8 m over 20 frames = 0.4 m/frame: comfortably inside the 2 m/frame
        # limit, while the raw whole-span displacement (8 m) is 4x the per-frame
        # limit -- so only a correctly gap-scaled limit lets this pass.
        animation = _straight_animation((0.0, -4.0, 1.6), (0.0, 4.0, 1.6), frames=21)
        equal(animation.frame_end, 20, "the fixture must span 20 frame intervals")
        validator = cv.CameraValidator(context, ValidationSection(sample_step=10))
        report = validator.validate(camera, animation)
        equal(len(report.frames), 3, "step 10 over 21 frames samples frames 0, 10 and 20")
        last = report.frames[-1]
        equal(last.metrics_span, 10)
        close(last.position_delta, 4.0, tol=1e-9, message="raw whole-span displacement")
        close(last.position_jump, 0.4, tol=1e-9, message="per-frame rate stays readable")
        close(cv._jump_scale(10, "sqrt"), math.sqrt(10.0), tol=1e-9)
        ok(report.passed, f"expected a pass with gap scaling, got {report.failures}")

        strict = cv.CameraValidator(context, ValidationSection(sample_step=10, jump_gap_scale="none"))
        strict_report = strict.validate(camera, animation)
        ok(not strict_report.passed,
           "with jump_gap_scale='none' the raw 4 m displacement must trip the 2 m limit")
        ok(cv.REASON_POSITION_JUMP in strict_report.failures, strict_report.failures)

        per_frame = cv.CameraValidator(context, ValidationSection(sample_step=1))
        ok(per_frame.validate(camera, animation).passed, "0.4 m/frame is well inside the limit")

        per_frame_strict = cv.CameraValidator(
            context, ValidationSection(sample_step=1, max_position_jump=0.2)
        )
        ok(cv.REASON_POSITION_JUMP in per_frame_strict.validate(camera, animation).failures,
           "a genuinely too-fast move must still fail frame-by-frame")

    @suite.case("a camera buried in a wall fails with the clipping reason")
    def _():
        boxes = _wall_boxes() + [_blocker()]
        context = _context(boxes)
        validator = cv.CameraValidator(context, ValidationSection())
        camera = context.cameras[0]
        # Travel straight through the blocker slab at x = 1.5.
        animation = _straight_animation((1.5, -2.0, 1.6), (1.5, 2.0, 1.6))
        report = validator.validate(camera, animation, base_matrix=camera.matrix_world, base_focal=35.0)
        ok(not report.passed, "a camera inside geometry must fail")
        ok(cv.REASON_INSIDE_GEOMETRY in report.failures or cv.REASON_CLIPPING in report.failures,
           report.failures)
        inside_frames = [f.frame for f in report.frames if f.inside_geometry]
        ok(inside_frames, "at least one frame must be flagged as inside geometry")

    @suite.case("a camera that ends up against a wall fails on clearance")
    def _():
        boxes = _wall_boxes()
        context = _context(boxes)
        config = ValidationSection(clearance=0.5)
        validator = cv.CameraValidator(context, config)
        camera = context.cameras[0]
        # Finish 5 cm from the x_max wall.
        animation = _straight_animation((0.0, 0.0, 1.6), (4.95, 0.0, 1.6))
        report = validator.validate(camera, animation, base_matrix=camera.matrix_world, base_focal=35.0)
        ok(not report.passed, "moving up against a wall must fail the clearance check")
        ok(cv.REASON_CLIPPING in report.failures, report.failures)
        ok(report.metrics["min_clearance"] < config.clearance, report.metrics)

    @suite.case("a wall in front of the lens is reported as an obstruction")
    def _():
        boxes = _wall_boxes() + [_blocker()]
        context = _context(boxes)
        config = ValidationSection(clearance=0.5, sample_step=100)
        validator = cv.CameraValidator(context, config)
        camera = context.cameras[0]
        # Sit 1 m in front of the blocker (at x = 1.5) facing it.
        sample = _point_camera((0.5, 0.0, 1.6), (1.0, 0.0, 0.0))
        animation = _animation([sample])
        report = validator.validate(camera, animation)
        ok(not report.passed, "a wall 1 m ahead must fail")
        ok(cv.REASON_OBSTRUCTION in report.failures, report.failures)
        close(report.frames[0].obstruction_distance, 0.9, tol=0.05)

    @suite.case("abnormal position and rotation jumps are detected")
    def _():
        boxes = _wall_boxes()
        context = _context(boxes)
        config = ValidationSection(sample_step=1, max_position_jump=1.0, max_rotation_jump_deg=20.0)
        validator = cv.CameraValidator(context, config)
        camera = context.cameras[0]
        samples = [
            _point_camera((0.0, -3.0, 1.6), (0.0, 1.0, 0.0)),
            _point_camera((0.0, -2.0, 1.6), (0.0, 1.0, 0.0)),
            # 3 m jump on the next frame
            _point_camera((0.0, 1.0, 1.6), (0.0, 1.0, 0.0)),
        ]
        for index, sample in enumerate(samples):
            sample.frame = index
        report = validator.validate(camera, _animation(samples))
        ok(not report.passed, "a 3 m single-frame jump must fail")
        ok(cv.REASON_POSITION_JUMP in report.failures, report.failures)

        # Now a pure rotation jump.
        rotated = [
            _point_camera((0.0, -3.0, 1.6), (0.0, 1.0, 0.0)),
            _point_camera((0.0, -3.0, 1.6), (0.9, 0.1, 0.0)),
        ]
        rotated[1].frame = 1
        report_rot = validator.validate(camera, _animation(rotated))
        ok(cv.REASON_ROTATION_JUMP in report_rot.failures, report_rot.failures)

    @suite.case("non-finite camera values are rejected before any geometry test")
    def _():
        boxes = _wall_boxes()
        context = _context(boxes)
        validator = cv.CameraValidator(context, ValidationSection())
        camera = context.cameras[0]
        bad = _point_camera((0.0, 0.0, 1.6), (0.0, 1.0, 0.0))
        bad.position = (float("nan"), 0.0, 1.6)
        report = validator.validate(camera, _animation([bad]))
        ok(not report.passed, "NaN position must fail")
        ok(cv.REASON_ILLEGAL_VALUE in report.failures, report.failures)
        ok(report.frames[0].illegal_values, "the reason must name the offending field")

        degenerate = _point_camera((0.0, 0.0, 1.6), (0.0, 1.0, 0.0))
        degenerate.quaternion = (0.0, 0.0, 0.0, 0.0)
        report2 = validator.validate(camera, _animation([degenerate]))
        ok(cv.REASON_ILLEGAL_VALUE in report2.failures, report2.failures)

    @suite.case("an empty animation is reported rather than silently passing")
    def _():
        context = _context(_wall_boxes())
        validator = cv.CameraValidator(context, ValidationSection())
        report = validator.validate(context.cameras[0], _animation([]))
        ok(not report.passed)
        ok(any("no frames" in message for message in report.messages), report.messages)

    @suite.case("clip range problems are reported")
    def _():
        context = _context(_wall_boxes())
        config = ValidationSection(max_clip_end=50.0, min_clip_start=0.05)
        validator = cv.CameraValidator(context, config)
        camera = _camera_snapshot(clip_start=0.001, clip_end=500.0)
        report = validator.validate(camera, _straight_animation((0.0, 0.0, 1.6), (0.0, 1.0, 1.6)))
        ok(cv.REASON_CLIP_RANGE in report.failures, report.failures)

    @suite.case("validate_camera_static catches bad lens and clip values")
    def _():
        config = ValidationSection()
        equal(cv.validate_camera_static(_camera_snapshot(), config), [])
        problems = cv.validate_camera_static(_camera_snapshot(lens=0.0, clip_start=0.0, clip_end=0.0), config)
        ok(len(problems) >= 3, problems)
        ok(any("focal" in p for p in problems), problems)
        ok(any("clip_end" in p for p in problems), problems)

    @suite.case("character visibility is measured and occlusion detected")
    def _():
        boxes = _wall_boxes() + [_blocker()]
        character = _character_center()
        context = _context(boxes, characters=[character])
        config = ValidationSection(
            sample_step=100, check_character_visibility=True,
            min_character_visible_ratio=0.05, character_probe_points=27,
        )
        validator = cv.CameraValidator(context, config)
        camera = context.cameras[0]

        # Camera on the same side of the blocker as the character, aimed at it.
        # A 1.75 m character 4 m away with a 35 mm lens does not fill the frame
        # vertically, so a perfect 1.0 ratio is not expected -- only most of it.
        good = _point_camera((-4.0, 0.0, 0.9), (1.0, 0.0, 0.0))
        report = validator.validate(camera, _animation([good]), character=character)
        ratio = report.frames[0].character_visible_ratio
        ok(ratio is not None, "visibility must be measured")
        ok(ratio > 0.5, f"expected most of the character visible, got {ratio}")
        ok(report.frames[0].character_on_screen, "the character must be on screen")
        ok(not report.frames[0].character_overlap, "a free-standing character must not overlap geometry")

        # Camera on the far side of the blocker, aimed back at the character:
        # the wall is between them, so nothing is visible.
        bad = _point_camera((3.0, 0.0, 0.9), (-1.0, 0.0, 0.0))
        report_bad = validator.validate(camera, _animation([bad]), character=character)
        ok(not report_bad.passed, "a character behind a wall must fail")
        ok(cv.REASON_CHARACTER_INVISIBLE in report_bad.failures, report_bad.failures)

        # Camera aimed away from the character entirely.
        away = _point_camera((-4.0, 0.0, 0.9), (-1.0, 0.0, 0.0))
        report_away = validator.validate(camera, _animation([away]), character=character)
        ok(not report_away.passed, "a character out of frame must fail")
        ok(cv.REASON_CHARACTER_UNFRAMED in report_away.failures, report_away.failures)

    @suite.case("character checks are skipped (and recorded) when there is no character")
    def _():
        context = _context(_wall_boxes())
        validator = cv.CameraValidator(context, ValidationSection())
        report = validator.validate(context.cameras[0], _straight_animation((0, 0, 1.6), (0, 1, 1.6)))
        ok(any("no character" in item for item in report.skipped_checks), report.skipped_checks)
        ok(report.passed)

    @suite.case("frustum_contains agrees with a simple forward-facing case")
    def _():
        camera = _camera_snapshot()
        position = (0.0, 0.0, 0.0)
        direction = (0.0, 1.0, 0.0)
        ok(cv.frustum_contains(camera, position, direction, (0.0, 5.0, 0.0)), "straight ahead")
        ok(not cv.frustum_contains(camera, position, direction, (0.0, -5.0, 0.0)), "behind")
        ok(not cv.frustum_contains(camera, position, direction, (50.0, 1.0, 0.0)), "far off axis")
        tan_x, tan_y = cv.camera_tangents(camera)
        close(tan_x, math.tan(math.radians(2 * math.degrees(math.atan(18.0 / 35.0)) / 2)), tol=1e-6)
        ok(tan_y < tan_x, "landscape 16:9 must have a narrower vertical half-angle")

    @suite.case("score rewards closeness and passes are perfect")
    def _():
        context = _context(_wall_boxes())
        validator = cv.CameraValidator(context, ValidationSection())
        camera = context.cameras[0]
        report = validator.validate(camera, _straight_animation((0, 0, 1.6), (0, 1, 1.6)),
                                    base_matrix=camera.matrix_world, base_focal=35.0)
        close(report.score, 1.0, tol=1e-9, message="a clean, unmoved, on-spec camera scores 1.0")

        far_report = validator.validate(
            _camera_snapshot(), _straight_animation((8.0, 0, 1.6), (8.0, 1, 1.6)),
            base_matrix=camera.matrix_world, base_focal=35.0,
        )
        ok(far_report.score < report.score, "a distant camera must score lower")

    @suite.case("candidate generation is deterministic, bounded and varied")
    def _():
        first = cs.generate_candidate_offsets(
            min_radius=0.5, max_radius=2.0, candidate_count=40,
            azimuth_samples=8, elevation_samples=5, shell_only=False, seed=7,
        )
        second = cs.generate_candidate_offsets(
            min_radius=0.5, max_radius=2.0, candidate_count=40,
            azimuth_samples=8, elevation_samples=5, shell_only=False, seed=7,
        )
        equal(len(first), 40)
        equal([entry["offset"] for entry in first], [entry["offset"] for entry in second])
        for entry in first:
            radius = entry["radius"]
            ok(0.5 - 1e-9 <= radius <= 2.0 + 1e-9, f"radius {radius} outside the requested shell")
            length = math.dist(entry["offset"], (0.0, 0.0, 0.0))
            close(length, radius, tol=1e-9, message="offset length must equal its radius")

        # A request larger than the regular grid must pull in the other
        # generators (fibonacci / radial / random) rather than repeat the grid.
        mixed = cs.generate_candidate_offsets(
            min_radius=0.5, max_radius=2.0, candidate_count=100,
            azimuth_samples=8, elevation_samples=5, shell_only=False, seed=7,
        )
        equal(len(mixed), 100)
        sources = {entry["source"] for entry in mixed}
        ok("azimuth" in sources, sources)
        ok(len(sources) >= 2, f"expected several candidate sources, got {sources}")
        # With enough room the radial shell generator must contribute too.
        wide = cs.generate_candidate_offsets(
            min_radius=0.5, max_radius=2.0, candidate_count=200,
            azimuth_samples=8, elevation_samples=5, shell_only=False, seed=7,
        )
        ok("radial" in {entry["source"] for entry in wide},
           {entry["source"] for entry in wide})

        shell = cs.generate_candidate_offsets(
            min_radius=1.0, max_radius=3.0, candidate_count=10,
            azimuth_samples=4, elevation_samples=2, shell_only=True, seed=1,
        )
        ok(all(abs(entry["radius"] - 3.0) < 1e-9 for entry in shell),
           "shell_only must pin every candidate to max_radius")
        equal(cs.generate_candidate_offsets(
            min_radius=0, max_radius=1, candidate_count=0, azimuth_samples=1,
            elevation_samples=1, shell_only=False, seed=0), [])

    @suite.case("search finds a position that clears an obstruction")
    def _():
        boxes = _wall_boxes() + [_blocker()]
        context = _context(boxes)
        config = ValidationSection(sample_step=100, clearance=0.4)
        search_config = SearchSection(
            enabled=True, min_radius=1.0, max_radius=4.0, candidate_count=48,
            azimuth_samples=12, elevation_samples=5, max_retries=1, random_seed=3,
            allow_rotation_adjust=False, allow_focal_adjust=False, max_output_candidates=1,
        )
        validator = cv.CameraValidator(context, config)
        camera = context.cameras[0]

        # Original camera looks straight at the blocker from 1 m away.
        def make_animation(candidate):
            sample = _point_camera(candidate.position, (1.0, 0.0, 0.0))
            return _animation([sample])

        search = cs.CameraSearch(context, search_config, validator)
        result = search.search(
            camera, make_animation,
            base_position=(-1.0, 0.0, 1.6),
            base_quaternion=cs.look_at_quaternion((1.0, 0.0, 0.0)),
            base_focal=35.0,
        )
        ok(result.passed, f"search should find a valid position: {result.messages}")
        ok(result.accepted, "an accepted candidate must be recorded")
        winner = result.accepted[0]
        ok(winner.radius >= search_config.min_radius - 1e-9, winner.describe())
        ok(result.best is not None and result.best.report.passed, "the best evaluation must pass")
        ok(result.attempts >= 1)
        # Deterministic for a fixed seed.
        repeat = cs.CameraSearch(context, search_config, validator).search(
            camera, make_animation,
            base_position=(-1.0, 0.0, 1.6),
            base_quaternion=cs.look_at_quaternion((1.0, 0.0, 0.0)),
            base_focal=35.0,
        )
        equal([c.to_dict() for c in repeat.accepted], [c.to_dict() for c in result.accepted])

    @suite.case("search reports failure honestly when every candidate is bad")
    def _():
        # A tiny sealed box: no camera position can be valid.
        boxes = [
            MeshSnapshot("shell", (-0.3, -0.3, -0.3), (0.3, 0.3, 0.3)),
        ]
        context = _context(boxes)
        validator = cv.CameraValidator(context, ValidationSection(sample_step=100, clearance=0.5))
        search_config = SearchSection(
            enabled=True, min_radius=0.05, max_radius=0.1, candidate_count=6,
            azimuth_samples=3, elevation_samples=2, max_retries=0, random_seed=1,
            allow_rotation_adjust=False, allow_focal_adjust=False,
        )
        camera = context.cameras[0]

        # Camera sits inside the sealed shell, so every candidate is inside too.
        def make_animation(candidate):
            return _animation([_point_camera(candidate.position, (0.0, 1.0, 0.0))])

        result = cs.CameraSearch(context, search_config, validator).search(
            camera, make_animation,
            base_position=(0.0, 0.0, 0.0),
            base_quaternion=(1.0, 0.0, 0.0, 0.0),
            base_focal=35.0,
        )
        ok(not result.passed, "no candidate can pass inside a sealed shell")
        equal(result.accepted, [])
        ok(result.best is not None, "the best failing attempt must still be reported")
        ok(any("failed" in message for message in result.messages), result.messages)

    @suite.case("disabled search returns immediately without evaluating")
    def _():
        context = _context(_wall_boxes())
        validator = cv.CameraValidator(context, ValidationSection())
        result = cs.CameraSearch(context, SearchSection(enabled=False), validator).search(
            context.cameras[0], lambda c: _animation([]),
            base_position=(0.0, 0.0, 1.6), base_quaternion=(1.0, 0.0, 0.0, 0.0), base_focal=35.0,
        )
        equal(result.attempts, 0)
        ok(any("disabled" in message for message in result.messages), result.messages)

    @suite.case("look_at_quaternion actually aims at the target")
    def _():
        for direction in ((1, 0, 0), (0, 1, 0), (0, 0, -1), (0.3, -0.5, 0.8), (-1, -1, 0)):
            quaternion = cs.look_at_quaternion(direction)
            aim = mt.quat_rotate(quaternion, (0.0, 0.0, -1.0))
            vec_close(aim, mt.vec_normalized(direction), tol=1e-6, message=f"aim {direction}")

    @suite.case("aim_rotation respects its angle budget")
    def _():
        base_position = (0.0, 0.0, 1.6)
        base_quaternion = cs.look_at_quaternion((0.0, 1.0, 0.0))
        # Target directly behind: would need a 180 degree turn.
        limited = cs.aim_rotation(base_position, base_quaternion, (0.0, -5.0, 1.6), max_deg=30.0)
        ok(limited is not None)
        turn = mt.quat_angle_between(mt.quat_multiply(limited, cs._quat_inverse(base_quaternion)), (1.0, 0.0, 0.0, 0.0))
        ok(turn <= 30.0 + 1e-6, f"correction must stay within budget, got {turn}")
        aimed = cs.aim_rotation(base_position, base_quaternion, (0.0, 5.0, 1.6), max_deg=30.0)
        ok(aimed is None, "no correction is needed when already aimed at the target")

    @suite.case("sphere_sample_points stays inside the requested shell")
    def _():
        center = (1.0, 2.0, 3.0)
        points = cs.sphere_sample_points(center=center, min_radius=0.5, max_radius=2.0, count=200, seed=11)
        equal(len(points), 200)
        for point in points:
            radius = math.dist(point, center)
            ok(0.5 - 1e-9 <= radius <= 2.0 + 1e-9, f"radius {radius} outside [0.5, 2.0]")
        equal(cs.sphere_sample_points(center=center, min_radius=0, max_radius=1, count=0), [])

    @suite.case("build_candidates expands rotation and focal variants")
    def _():
        plain = cs.build_candidates(
            base_position=(0.0, 0.0, 1.6), base_quaternion=(1.0, 0.0, 0.0, 0.0),
            section=SearchSection(candidate_count=4, azimuth_samples=2, elevation_samples=2,
                                  max_radius=1.0, allow_rotation_adjust=False, allow_focal_adjust=False),
        )
        equal(len(plain), 4)
        rich = cs.build_candidates(
            base_position=(0.0, 0.0, 1.6), base_quaternion=(1.0, 0.0, 0.0, 0.0),
            section=SearchSection(candidate_count=4, azimuth_samples=2, elevation_samples=2,
                                  max_radius=1.0, allow_rotation_adjust=True, allow_focal_adjust=True),
            character=_character_center(),
        )
        ok(len(rich) > len(plain), "rotation/focal variants must multiply the candidate count")
        ok(any(abs(c.focal_scale - 1.0) > 1e-9 for c in rich), "focal variants must be present")
        ok(any(c.rotation_adjust_deg > 1.0 for c in rich), "rotation variants must be present")

    @suite.case("apply_candidate bakes rotation and focal into the animation")
    def _():
        animation = _straight_animation((0, 0, 1.6), (0, 1, 1.6), frames=3, focal=40.0)
        candidate = cs.SearchCandidate(
            index=0, offset=(0.0, 0.0, 0.0), position=(0.0, 0.0, 1.6),
            rotation_adjust=mt.quat_from_axis_angle("Z", 15.0), focal_scale=1.5,
        )
        baked = cs.apply_candidate(animation, candidate)
        equal(len(baked.samples), len(animation.samples))
        close(baked.samples[0].focal, 60.0)
        close(mt.quat_angle_between(baked.samples[0].quaternion, animation.samples[0].quaternion),
              15.0, tol=1e-6)
        # The original must not be mutated.
        close(animation.samples[0].focal, 40.0)

    @suite.case("world_to_camera_row produces a valid OpenCV extrinsic")
    def _():
        # Camera at (0, 0, -5) with identity rotation looks toward +Z.
        matrix = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, -5], [0, 0, 0, 1]]
        row = ce.world_to_camera_row(matrix)
        vec_close(row[0:3], (1.0, 0.0, 0.0), tol=1e-9, message="OpenCV +X row")
        vec_close(row[4:7], (0.0, -1.0, 0.0), tol=1e-9, message="OpenCV +Y (down) row")
        vec_close(row[8:11], (0.0, 0.0, 1.0), tol=1e-9, message="OpenCV +Z row")
        # The defining invariant: the camera's own world position maps to the
        # camera-space origin.
        camera_position = (0.0, 0.0, -5.0)
        for axis in range(3):
            value = sum(row[axis * 4 + k] * camera_position[k] for k in range(3)) + row[axis * 4 + 3]
            close(value, 0.0, tol=1e-9, message=f"camera-space axis {axis} of the camera origin")
        # Row 2 is the camera's `back` column (Blender local +Z), matching the
        # reference implementation's `R^T` convention: a point IN FRONT of the
        # camera gets positive z, a point behind it gets negative z.
        front = (0.0, 0.0, 0.0)
        depth = sum(row[8 + k] * front[k] for k in range(3)) + row[11]
        close(depth, 5.0, tol=1e-9, message="5 units ahead of the camera")
        behind = (0.0, 0.0, -10.0)
        depth_behind = sum(row[8 + k] * behind[k] for k in range(3)) + row[11]
        close(depth_behind, -5.0, tol=1e-9, message="5 units behind the camera")

    @suite.case("world_to_camera_row matches a hand-computed lookup table")
    def _():
        import math as _math

        # 90 degrees about world Z, camera at (2, 3, 1).
        angle = _math.radians(90.0)
        cosine, sine = _math.cos(angle), _math.sin(angle)
        centre = (2.0, 3.0, 1.0)
        matrix = [
            [cosine, -sine, 0.0, centre[0]],
            [sine, cosine, 0.0, centre[1]],
            [0.0, 0.0, 1.0, centre[2]],
            [0.0, 0.0, 0.0, 1.0],
        ]
        row = ce.world_to_camera_row(matrix)
        # Blender columns after rotation: right = (c, s, 0), up = (-s, c, 0), back = (0, 0, 1).
        vec_close(row[0:3], (cosine, sine, 0.0), tol=1e-9, message="right row")
        vec_close(row[4:7], (sine, -cosine, 0.0), tol=1e-9, message="down row (negated up)")
        vec_close(row[8:11], (0.0, 0.0, 1.0), tol=1e-9, message="back row")
        # Translation terms must satisfy -R^T c for each row axis.
        close(row[3], -(row[0] * centre[0] + row[1] * centre[1] + row[2] * centre[2]), tol=1e-9)
        close(row[7], -(row[4] * centre[0] + row[5] * centre[1] + row[6] * centre[2]), tol=1e-9)
        close(row[11], -(row[8] * centre[0] + row[9] * centre[1] + row[10] * centre[2]), tol=1e-9)
        for axis in range(3):
            value = sum(row[axis * 4 + k] * centre[k] for k in range(3)) + row[axis * 4 + 3]
            close(value, 0.0, tol=1e-9, message=f"camera-space axis {axis} of the camera origin")
        # The camera's up axis is world -X here, so a point 5 units along world
        # -X is 5 units towards the camera's UP direction: OpenCV y = -5.
        target = (centre[0] - 5.0, centre[1], centre[2])
        camera_space = [
            sum(row[axis * 4 + k] * target[k] for k in range(3)) + row[axis * 4 + 3]
            for axis in range(3)
        ]
        vec_close(camera_space, (0.0, -5.0, 0.0), tol=1e-9, message="5 units above the camera")
        # ... and a point 5 units along the camera's own BACK axis (+Z) is 5
        # units in front, i.e. camera-space z = +5 with x = y = 0 (the reference
        # `R^T` convention puts in-front points at positive z).
        behind = (centre[0], centre[1], centre[2] + 5.0)
        camera_space_back = [
            sum(row[axis * 4 + k] * behind[k] for k in range(3)) + row[axis * 4 + 3]
            for axis in range(3)
        ]
        vec_close(camera_space_back, (0.0, 0.0, 5.0), tol=1e-9,
                  message="5 units in front of the camera")

    @suite.case("trajectory rows honour mode, step and endpoints")
    def _():
        animation = _straight_animation((0, 0, 1.6), (0, 1, 1.6), frames=11)
        all_rows = ce.build_trajectory_rows(animation.samples, mode="all_frames")
        equal(len(all_rows), 11)
        sampled = ce.build_trajectory_rows(animation.samples, mode="sampled", step=5)
        equal([row.frame for row in sampled], [0, 5, 10])
        forced = ce.build_trajectory_rows(animation.samples, mode="sampled", step=5, always_include=[3])
        equal([row.frame for row in forced], [0, 3, 5, 10])
        equal(ce.build_trajectory_rows([], mode="all_frames"), [])

    @suite.case("trajectory txt matches the reference header and row shape")
    def _():
        import tempfile

        animation = _straight_animation((0, 0, 1.6), (0, 1, 1.6), frames=3)
        rows = ce.build_trajectory_rows(animation.samples)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "seq", "traj.txt")
            ce.write_trajectory_txt(path, rows, extra_header=["sequence_id=sequence_000001"])
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        ok(lines[0].startswith("# sequence_id="), lines[0])
        equal(lines[1], "frame focal_length d1 d2 d3 d4 d5 "
                        "r00 r01 r02 tx r10 r11 r12 ty r20 r21 r22 tz")
        equal(len(lines), 2 + 3)
        fields = lines[2].split()
        equal(len(fields), 1 + 1 + 5 + 12, "frame + focal + 5 reserved + 12 matrix values")
        equal(int(fields[0]), 0)
        close(float(fields[1]), 35.0)
        equal(fields[2:7], ["0"] * 5)

    @suite.case("the txt rotation block is orthonormal and z-forward")
    def _():
        animation = _straight_animation((0, 0, 1.6), (0, 1, 1.6), frames=2)
        rows = ce.build_trajectory_rows(animation.samples)
        row = rows[0]
        entries = [
            (row.r00, row.r01, row.r02),
            (row.r10, row.r11, row.r12),
            (row.r20, row.r21, row.r22),
        ]
        for index, axis in enumerate(entries):
            close(math.dist(axis, (0.0, 0.0, 0.0)), 1.0, tol=1e-9, message=f"row {index} unit length")
        for i in range(3):
            for j in range(i + 1, 3):
                dot = sum(entries[i][k] * entries[j][k] for k in range(3))
                close(dot, 0.0, tol=1e-9, message=f"rows {i},{j} orthogonal")

    @suite.case("sensor crop factor reflects Blender's AUTO fit rule")
    def _():
        landscape = _camera_snapshot()
        close(ce.sensor_crop_factor(landscape), 1.0, tol=1e-9)
        portrait = _camera_snapshot()
        portrait.resolution_x, portrait.resolution_y = 1080, 1920
        close(ce.sensor_crop_factor(portrait), 1080 / 1920.0, tol=1e-9)
        forced = _camera_snapshot()
        forced.sensor_fit = "HORIZONTAL"
        forced.resolution_x, forced.resolution_y = 1080, 1920
        close(ce.sensor_crop_factor(forced), 1.0, tol=1e-9)

    @suite.case("camera_intrinsics reports consistent fov and resolution")
    def _():
        intrinsics = ce.camera_intrinsics(_camera_snapshot(lens=50.0))
        equal(intrinsics["image_width"], 1920)
        equal(intrinsics["image_height"], 1080)
        close(intrinsics["focal_length_mm"], 50.0)
        expected_h = 2.0 * math.degrees(math.atan((36.0 * 0.5) / 50.0))
        close(intrinsics["horizontal_fov_deg"], expected_h, tol=1e-6)
        ok(intrinsics["vertical_fov_deg"] < intrinsics["horizontal_fov_deg"])

    @suite.case("sequence metadata carries the reference keys plus the extensions")
    def _():
        import tempfile

        animation = _straight_animation((0, 0, 1.6), (0, 1, 1.6), frames=5)
        rows = ce.build_trajectory_rows(animation.samples)
        camera = _camera_snapshot()
        metadata = ce.SequenceMetadata(
            sequence_id="sequence_000001",
            scene_name="room001",
            motion_name="dolly_in_01_standard",
            camera_name="Camera",
            source_blend=r"E:\scenes\room001.blend",
            frame_start=0,
            frame_end=4,
            fps=24.0,
            video_path=r"E:\out\room001\dolly_in_01_standard\sequence_000001\sequence_000001.mp4",
            camera=camera,
            camera_trajectory=rows,
            has_character=True,
            character_name="ch41",
            character_animation="Cross_Punch",
        )
        payload = metadata.to_dict()
        for key in ("level_name", "sequence_name", "video_id", "video_path",
                    "frame_count", "camera_trajectory", "text_prompt"):
            ok(key in payload, f"reference key {key!r} missing")
        equal(payload["level_name"], "room001")
        equal(payload["video_id"], "sequence_000001")
        equal(payload["frame_count"], 5)
        equal(payload["has_character"], True)
        equal(payload["random_seed"], 0)
        equal(payload["generator_version"], ce.GENERATOR_VERSION)
        ok(payload["video_path"].startswith("E:/"), payload["video_path"])
        entry = payload["camera_trajectory"][0]
        equal(len(entry["matrix"]), 4)
        equal(entry["matrix"][3], [0.0, 0.0, 0.0, 1.0])
        ok("fov" in entry and "focal_length" in entry)
        equal(payload["trajectory_export"]["coordinate_system"], "opencv_world_to_camera")

    @suite.case("validation report to_dict is JSON-serialisable")
    def _():
        import json

        context = _context(_wall_boxes() + [_blocker()], characters=[_character_center()])
        config = ValidationSection(sample_step=2)
        report = cv.CameraValidator(context, config).validate(
            context.cameras[0],
            _straight_animation((-1.0, 0.0, 1.6), (1.0, 0.0, 1.6), frames=5),
            character=_character_center(),
        )
        payload = report.to_dict(config=config, include_frames=True)
        text = json.dumps(payload)
        ok(len(text) > 100)
        ok("reason_counts" in payload and "frames" in payload)
        ok("score" in payload)
        ok(isinstance(report.summary_line(), str))
        parsed = json.loads(text)
        equal(parsed["passed"], report.passed)

    @suite.case("build_character_union composes several characters")
    def _():
        first = CharacterBox("a", (0, 0, 0), (1, 1, 1))
        second = CharacterBox("b", (-1, 0, 0), (0, 2, 3))
        union = cv.build_character_union([first, second])
        ok(union is not None)
        vec_close(union.bbox_min, (-1, 0, 0))
        vec_close(union.bbox_max, (1, 2, 3))
        equal(cv.build_character_union([]), None)

    @suite.case("fibonacci_directions are unit length and distinct")
    def _():
        directions = fibonacci_directions(26)
        equal(len(directions), 26)
        for direction in directions:
            close(math.dist(direction, (0.0, 0.0, 0.0)), 1.0, tol=1e-9)
        equal(len({tuple(round(v, 6) for v in d) for d in directions}), 26)
        equal(fibonacci_directions(0), [])
        equal(fibonacci_directions(1), [(0.0, 0.0, 1.0)])

    @suite.case("CharacterBox probe lattice and per-frame boxes behave")
    def _():
        box = _character_center()
        points = box.probe_points(27)
        equal(len(points), 27)
        for point in points:
            for axis in range(3):
                ok(box.bbox_min[axis] - 1e-9 <= point[axis] <= box.bbox_max[axis] + 1e-9,
                   f"probe {point} outside the box")
        vec_close(box.center(), (0.0, 0.0, 0.875), tol=1e-9)
        vec_close(box.size(), (0.6, 0.6, 1.75), tol=1e-9)
        equal(len(box.corners()), 8)
        box.animated_boxes[3] = ((0, 0, 0), (1, 1, 2))
        equal(box.box_at(3), ((0, 0, 0), (1, 1, 2)))
        equal(box.box_at(4), (box.bbox_min, box.bbox_max))

    @suite.case("BBoxRayCaster reports hits, misses and inside-origin cases")
    def _():
        caster = BBoxRayCaster([MeshSnapshot("blk", (-1, -1, -1), (1, 1, 1))])
        hit, distance = caster.cast((0, 0, 5), (0, 0, -1))
        ok(hit)
        close(distance, 4.0, tol=1e-9)
        hit, _distance = caster.cast((0, 0, 5), (0, 0, 1))
        ok(not hit)
        hit, distance = caster.cast((0, 0, 0), (0, 0, 1))
        ok(hit, "an origin inside the box must register a hit")
        close(distance, 0.0, tol=1e-9)

    return suite


if __name__ == "__main__":
    raise SystemExit(build_suite().run())
