"""Spatio-temporal compound tests (pure Python).

These pin the three things that would silently ruin a dataset:

* **compatibility** -- two atoms may share a moment only when their channels are
  disjoint, so ``pan_right`` + ``tilt_down`` is legal and ``zoom_in`` + ``zoom_out``
  is not;
* **the time plan** -- segment count capped by the 0.5 s floor, contiguous windows
  that cover the whole video, and a frame range that follows the duration;
* **the maths** -- atoms are *rates*, so a segment's displacement is rate x time,
  simultaneous moves add up, and the flattened template is camera-local like every
  other template.
"""

from __future__ import annotations

import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _boot  # noqa: E402,F401  (the add-on folder may be called anything)

from blender_motion_pipeline.camera import motion_composite as mc  # noqa: E402
from blender_motion_pipeline.camera import motion_templates as mt  # noqa: E402
from blender_motion_pipeline.config.models import (  # noqa: E402
    BatchConfig,
    CompositeSection,
    ConfigError,
)
from blender_motion_pipeline.tests.harness import (  # noqa: E402
    Suite, close, equal, ok, raises, vec_close,
)


def _atoms():
    """The bundled vocabulary."""
    atoms, source = mc.load_atomic_library()
    ok(bool(atoms), f"the bundled atomic document must load ({source})")
    return atoms, source


def _channels_of(segment) -> "list[str]":
    names: "list[str]" = []
    for motion in segment.motions:
        names.extend(motion.channels)
    return names


def _atoms_by_name(atoms) -> dict:
    return {atom.name: atom for atom in atoms}


def _rate(atom) -> float:
    """The largest rate of an atom, whichever channel it drives."""
    values = [abs(v) for v in (*atom.rate_location, *atom.rate_rotation)]
    values.append(abs(atom.rate_focal))
    return max(values)


def build_suite() -> Suite:
    suite = Suite("test_motion_composite")

    # -- vocabulary ------------------------------------------------------
    @suite.case("the bundled atomic document is a normal template document")
    def _():
        atoms, source = _atoms()
        equal(os.path.basename(source), "atomic_motion_templates.json")
        # 16 directional/rate atoms x 3 speeds + static.
        equal(len(atoms), 49)
        names = {atom.name for atom in atoms}
        for stem in ("pan_left", "pan_right", "tilt_up", "tilt_down",
                     "roll_clockwise", "roll_counterclockwise",
                     "truck_left", "truck_right", "dolly_in", "dolly_out",
                     "pedestal_up", "pedestal_down",
                     "arc_clockwise", "arc_counterclockwise",
                     "zoom_in", "zoom_out"):
            for speed in mc.SPEEDS:
                ok(f"{stem}_{speed}" in names, f"{stem}_{speed} is missing")
        equal([atom.name for atom in atoms if atom.is_static], ["static"])
        # The document parses through the ordinary library, so the panel, the CLI
        # and the probes can read it like any other template set.
        library = mt.MotionTemplateLibrary.from_file(source)
        equal(len(library), 49)
        template = library.get("pan_left_medium")
        equal(template.frame_min, 0)
        equal(template.frame_max, 24, "atoms are one-second ramps")

    @suite.case("atoms carry the type/direction/speed/channel metadata")
    def _():
        atoms, _source = _atoms()
        by_name = _atoms_by_name(atoms)
        pan = by_name["pan_left_medium"]
        equal(pan.type, "Pan")
        equal(pan.direction, "left")
        equal(pan.speed, "medium")
        equal(list(pan.channels), ["yaw"])
        close(pan.rate_rotation[1], 18.0, tol=1e-9, message="18 deg/s of yaw")
        vec_close(pan.rate_location, (0.0, 0.0, 0.0), tol=1e-12)

        dolly = by_name["dolly_in_fast"]
        equal(dolly.type, "Dolly In")
        equal(dolly.direction, None)
        close(dolly.rate_location[2], -1.5, tol=1e-9,
              message="dolly_in is a negative Z (forward) rate")

        arc = by_name["arc_clockwise_medium"]
        equal(sorted(arc.channels), ["lateral", "yaw"])
        ok(arc.rate_location[0] > 0 and arc.rate_rotation[1] > 0,
           "arc clockwise tracks right while yawing left")

        zoom = by_name["zoom_in_medium"]
        close(zoom.rate_focal, 10.0, tol=1e-9, message="10 mm/s")
        equal(list(zoom.channels), ["focal"])
        equal(by_name["static"].speed, None)

    @suite.case("faster speeds move further in the same time")
    def _():
        atoms, _source = _atoms()
        by_name = _atoms_by_name(atoms)
        for stem in ("pan_left", "tilt_down", "truck_right", "dolly_in",
                     "pedestal_up", "zoom_in", "arc_clockwise"):
            slow = _rate(by_name[f"{stem}_slow"])
            fast = _rate(by_name[f"{stem}_fast"])
            ok(fast > slow * 1.5, f"{stem}: fast ({fast}) must beat slow ({slow})")

    # -- compatibility ---------------------------------------------------
    @suite.case("two atoms never share a segment when they share a channel")
    def _():
        atoms, source = _atoms()
        rng = random.Random(11)
        plans = 0
        for index in range(60):
            plan = mc.plan_compound(
                atoms, duration_seconds=4.0, fps=24.0, max_simultaneous=5,
                max_segments=6, randomize=True, rng=rng, seed=index, source=source)
            plans += 1
            for segment in plan.segments:
                channels = _channels_of(segment)
                equal(len(channels), len(set(channels)),
                      f"segment {segment.index} of plan {index} reuses a channel: "
                      f"{[m.name for m in segment.motions]}")
                names = {m.name for m in segment.motions}
                for a, b in (("zoom_in_medium", "zoom_out_medium"),
                             ("pedestal_up_medium", "pedestal_down_medium"),
                             ("pan_left_medium", "pan_right_medium"),
                             ("truck_left_medium", "truck_right_medium"),
                             ("dolly_in_medium", "dolly_out_medium"),
                             ("tilt_up_medium", "tilt_down_medium")):
                    ok(not ({a, b} <= names), f"{a} + {b} must not share a segment: {names}")
                # An arc drives lateral + yaw, so neither a truck nor a pan may join it.
                if any(name.startswith("arc_") for name in names):
                    ok(not any(name.startswith(("truck_", "pan_")) for name in names),
                       f"an arc must not be combined with a truck/pan: {names}")
        equal(plans, 60)

    @suite.case("a segment may combine moves on different axes")
    def _():
        atoms, _source = _atoms()
        by_name = _atoms_by_name(atoms)
        # pan (yaw) + tilt (pitch) + truck (lateral) -- the brief's own example.
        combined = mc.choose_motions(
            [by_name["pan_right_medium"], by_name["tilt_down_medium"],
             by_name["truck_left_slow"]], 3, random.Random(3))
        equal(len(combined), 3, [m.name for m in combined])
        # ... while asking for two moves out of one channel cannot exceed it.
        only_one = mc.choose_motions(
            [by_name["pan_right_medium"], by_name["pan_left_slow"]], 2, random.Random(3))
        equal(len(only_one), 1, [m.name for m in only_one])

    @suite.case("static is never drawn at random")
    def _():
        atoms, source = _atoms()
        rng = random.Random(5)
        for _ in range(30):
            plan = mc.plan_compound(atoms, duration_seconds=3.0, fps=24.0,
                                    max_simultaneous=5, max_segments=4,
                                    randomize=True, rng=rng, seed=1, source=source)
            for segment in plan.segments:
                ok(all(not motion.is_static for motion in segment.motions),
                   "a segment must not mix static with moving atoms")

    # -- time plan -------------------------------------------------------
    @suite.case("segments are contiguous, frame-snapped and never shorter than 0.5 s")
    def _():
        for duration, count, fps in ((4.0, 4, 24.0), (3.0, 6, 24.0), (1.0, 8, 24.0),
                                     (2.5, 3, 30.0)):
            windows = mc.segment_boundaries(duration, count, fps)
            expected = mc.max_segments_for(duration, requested=count)
            equal(len(windows), expected,
                  f"{duration}s fits {expected} segment(s), asked for {count}")
            ok(len(windows) <= count, (len(windows), count))
            close(windows[0][0], 0.0, tol=1e-12, message="the plan starts at 0")
            for (start, end), (next_start, _next_end) in zip(windows, windows[1:]):
                close(end, next_start, tol=1e-12, message="windows must touch")
                ok(end - start >= 0.5 - 1e-9,
                   f"{duration}s / {count}: segment {start}-{end} is shorter than 0.5 s")

    @suite.case("the segment count is capped by the 0.5 s minimum")
    def _():
        equal(mc.max_segments_for(4.0, requested=4), 4)
        equal(mc.max_segments_for(4.0, requested=40), 8, "4 s / 0.5 s = 8 segments")
        equal(mc.max_segments_for(1.4, requested=10), 2)
        equal(mc.max_segments_for(0.5, requested=5), 1)
        atoms, source = _atoms()
        plan = mc.plan_compound(atoms, duration_seconds=4.0, fps=24.0,
                                max_simultaneous=3, max_segments=40,
                                randomize=False, rng=random.Random(1),
                                seed=1, source=source)
        equal(len(plan.segments), 8)
        ok(all(segment.duration_seconds >= 0.5 - 1e-9 for segment in plan.segments),
           [s.duration_seconds for s in plan.segments])
        ok(any("only fits" in note for note in plan.notes), plan.notes)

    @suite.case("random off means fixed counts, random on means bounded draws")
    def _():
        atoms, source = _atoms()
        fixed = mc.plan_compound(atoms, duration_seconds=4.0, fps=24.0,
                                 max_simultaneous=3, max_segments=4, randomize=False,
                                 rng=random.Random(2), seed=2, source=source)
        equal(len(fixed.segments), 4, "exactly max_segments windows")
        equal([len(s.motions) for s in fixed.segments], [3, 3, 3, 3],
              "every segment holds exactly max_simultaneous moves")

        rng = random.Random(7)
        seen_segments: "set[int]" = set()
        seen_moves: "set[int]" = set()
        for index in range(40):
            plan = mc.plan_compound(atoms, duration_seconds=4.0, fps=24.0,
                                    max_simultaneous=3, max_segments=4, randomize=True,
                                    rng=rng, seed=index, source=source)
            ok(1 <= len(plan.segments) <= 4, len(plan.segments))
            seen_segments.add(len(plan.segments))
            for segment in plan.segments:
                ok(1 <= len(segment.motions) <= 3, len(segment.motions))
                seen_moves.add(len(segment.motions))
        ok(len(seen_segments) > 1, f"random segment counts must vary: {seen_segments}")
        ok(len(seen_moves) > 1, f"random move counts must vary: {seen_moves}")

    @suite.case("the frame range follows the duration")
    def _():
        atoms, source = _atoms()
        for duration, fps, frames in ((4.0, 24.0, 96), (2.5, 24.0, 60), (3.0, 30.0, 90)):
            plan = mc.plan_compound(atoms, duration_seconds=duration, fps=fps,
                                    max_simultaneous=2, max_segments=3,
                                    randomize=False, rng=random.Random(0), seed=0,
                                    source=source)
            equal(plan.frame_start, 0)
            equal(plan.frame_count, frames)
            equal(plan.frame_end, frames - 1)
            close(plan.duration_seconds, duration, tol=1e-9)
            equal(plan.segments[-1].end_frame, frames - 1,
                  "the last segment ends on the last frame")

    @suite.case("a plan is reproducible from its seed")
    def _():
        atoms, source = _atoms()
        first = mc.plan_compound(atoms, duration_seconds=5.0, fps=24.0,
                                 max_simultaneous=3, max_segments=5, randomize=True,
                                 rng=random.Random(mc.consecutive_seed(99, 3)),
                                 seed=mc.consecutive_seed(99, 3), source=source)
        second = mc.plan_compound(atoms, duration_seconds=5.0, fps=24.0,
                                  max_simultaneous=3, max_segments=5, randomize=True,
                                  rng=random.Random(mc.consecutive_seed(99, 3)),
                                  seed=mc.consecutive_seed(99, 3), source=source)
        equal(first.to_dict()["segments"], second.to_dict()["segments"])
        other = mc.plan_compound(atoms, duration_seconds=5.0, fps=24.0,
                                 max_simultaneous=3, max_segments=5, randomize=True,
                                 rng=random.Random(mc.consecutive_seed(99, 4)),
                                 seed=mc.consecutive_seed(99, 4), source=source)
        ok(other.to_dict()["segments"] != first.to_dict()["segments"],
           "a different sequence number must give a different plan")

    @suite.case("durations are fixed or drawn from the configured range")
    def _():
        close(mc.plan_duration(mode="fixed", duration=4.0), 4.0, tol=1e-9)
        rng = random.Random(3)
        drawn = [mc.plan_duration(mode="random", minimum=2.0, maximum=6.0, rng=rng)
                 for _ in range(50)]
        ok(all(2.0 <= value <= 6.0 for value in drawn), (min(drawn), max(drawn)))
        ok(max(drawn) - min(drawn) > 0.5, "random durations must vary")
        # A reversed range is tolerated (swapped), and nothing goes below the floor.
        close(mc.plan_duration(mode="random", minimum=6.0, maximum=2.0, rng=random.Random(1)),
              4.0, tol=2.0, message="a reversed range must still produce a duration")
        equal(mc.plan_duration(mode="fixed", duration=0.1), mc.MIN_SEGMENT_SECONDS)
        raises(ConfigError, lambda: mc.plan_duration(mode="sometimes"))

    # -- maths -----------------------------------------------------------
    @suite.case("a single atom is rate x time, in the camera's own frame")
    def _():
        atoms, source = _atoms()
        by_name = _atoms_by_name(atoms)
        for name, seconds in (("pan_left_medium", 3.0), ("truck_right_slow", 2.0),
                              ("pedestal_up_fast", 1.5), ("dolly_in_medium", 4.0)):
            atom = by_name[name]
            plan = mc.plan_single(atom, duration_seconds=seconds, fps=24.0, source=source)
            template = mc.flatten_plan(plan, base_focal=35.0)
            first, last = template.keyframes[0], template.keyframes[-1]
            for axis in range(3):
                close(last.location[axis], atom.rate_location[axis] * seconds, tol=1e-6,
                      message=f"{name} location axis {axis}")
                close(last.rotation[axis], atom.rate_rotation[axis] * seconds, tol=1e-6,
                      message=f"{name} rotation axis {axis}")
            equal(len(template.keyframes), plan.frame_count)
            equal(template.name, name)

    @suite.case("simultaneous moves add up in the flattened template")
    def _():
        atoms, source = _atoms()
        by_name = _atoms_by_name(atoms)
        pan = by_name["pan_right_medium"]        # yaw
        tilt = by_name["tilt_down_slow"]         # pitch
        truck = by_name["truck_left_slow"]       # lateral
        plan = mc.MotionPlan(
            duration_seconds=2.0, fps=24.0, frame_start=0, frame_end=47,
            segments=(mc.PlanSegment(index=0, start_time=0.0, end_time=2.0,
                                     start_frame=0, end_frame=47,
                                     motions=(pan, tilt, truck)),),
            compound=True, seed=0, source=source)
        template = mc.flatten_plan(plan, base_focal=35.0)
        last = template.keyframes[-1]
        close(last.rotation[1], pan.rate_rotation[1] * 2.0, tol=1e-6, message="yaw")
        close(last.rotation[0], tilt.rate_rotation[0] * 2.0, tol=1e-6, message="pitch")
        close(last.location[0], truck.rate_location[0] * 2.0, tol=1e-6, message="lateral")
        close(last.rotation[2], 0.0, tol=1e-9, message="no roll was asked for")

    @suite.case("a motion that has ended keeps its pose while the next segment runs")
    def _():
        atoms, source = _atoms()
        by_name = _atoms_by_name(atoms)
        pan = by_name["pan_left_medium"]
        tilt = by_name["tilt_up_medium"]
        plan = mc.MotionPlan(
            duration_seconds=2.0, fps=24.0, frame_start=0, frame_end=47,
            segments=(
                mc.PlanSegment(0, 0.0, 1.0, 0, 23, (pan,)),
                mc.PlanSegment(1, 1.0, 2.0, 24, 47, (tilt,)),
            ),
            compound=True, seed=0, source=source)
        template = mc.flatten_plan(plan, base_focal=35.0)
        by_frame = {key.frame: key for key in template.keyframes}
        close(by_frame[24].rotation[1], pan.rate_rotation[1], tol=1e-6,
              message="the pan stops at its end value")
        close(by_frame[47].rotation[1], pan.rate_rotation[1], tol=1e-6,
              message="... and holds it to the end")
        close(by_frame[23].rotation[0], 0.0, tol=1e-9, message="the tilt starts at 0")
        close(by_frame[47].rotation[0], tilt.rate_rotation[0],
              tol=1e-6, message="the tilt ramps over its own second")

    @suite.case("zoom plans pin the focal, other plans keep the camera's own lens")
    def _():
        atoms, source = _atoms()
        by_name = _atoms_by_name(atoms)
        zoom = mc.plan_single(by_name["zoom_in_medium"], duration_seconds=2.0,
                              fps=24.0, source=source)
        template = mc.flatten_plan(zoom, base_focal=35.0)
        # Frame 0 is already one frame into the move (the sample is taken at the end
        # of the exposure), and the last frame lands exactly on rate x duration.
        close(template.keyframes[0].focal, 35.0 + 10.0 / 24.0, tol=1e-6)
        close(template.keyframes[-1].focal, 55.0, tol=1e-6,
              message="35 mm + 2 s x 10 mm/s")
        pan = mc.plan_single(by_name["pan_left_medium"], duration_seconds=2.0,
                             fps=24.0, source=source)
        plain = mc.flatten_plan(pan, base_focal=35.0)
        equal([key.focal for key in plain.keyframes], [None] * len(plain.keyframes),
              "a non-zoom plan must not pin the lens")
        equal(plain.focals(), [], "so the generator keeps the camera's focal")

    @suite.case("every atom produces a valid flattened template")
    def _():
        atoms, source = _atoms()
        for atom in atoms:
            plan = mc.plan_single(atom, duration_seconds=1.0, fps=24.0, source=source)
            template = mc.flatten_plan(plan, base_focal=35.0)
            equal(len(template.keyframes), 24)
            for key in template.keyframes:
                for value in (*key.location, *key.rotation):
                    ok(abs(value) < 1e4, f"{atom.name} produced an absurd value: {key}")

    # -- report ----------------------------------------------------------
    @suite.case("the shot report is exactly the requested shape")
    def _():
        atoms, source = _atoms()
        rng = random.Random(13)
        plan = mc.plan_compound(atoms, duration_seconds=5.2, fps=24.0,
                                max_simultaneous=3, max_segments=4, randomize=True,
                                rng=rng, seed=13, source=source)
        report = plan.report()
        equal(len(report), len(plan.segments))
        close(report[0]["start_time"], 0.0, tol=1e-9, message="the report starts at 0")
        close(report[-1]["end_time"], round(plan.frame_count / 24.0, 6), tol=1e-6,
              message="the report covers the whole video")
        for index, entry in enumerate(report):
            equal(sorted(entry), ["basic_movement", "end_time", "start_time"])
            ok(entry["end_time"] > entry["start_time"], entry)
            ok(len(entry["basic_movement"]) >= 1, entry)
            for movement in entry["basic_movement"]:
                equal(sorted(movement), ["direction", "speed", "type"])
                ok(isinstance(movement["type"], str) and movement["type"], movement)
                ok(movement["speed"] in (*mc.SPEEDS, None), movement)
                ok(movement["direction"] is None or isinstance(movement["direction"], str),
                   movement)
            if index:
                close(entry["start_time"], report[index - 1]["end_time"], tol=1e-9,
                      message="segments must be contiguous in the report")
        # JSON round trip, because this is what lands on disk.
        text = json.dumps(report)
        ok(json.loads(text) == report, "the report must be JSON-serialisable")
        # Field order is part of the contract: a consumer keys on
        # start_time -> end_time -> basic_movement and type -> direction -> speed.
        for entry in report:
            equal(list(entry), list(mc.REPORT_ENTRY_KEYS))
            for movement in entry["basic_movement"]:
                equal(list(movement), list(mc.REPORT_MOVEMENT_KEYS))
        raw = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False)
        ok(raw.index('"start_time"') < raw.index('"end_time"') < raw.index('"basic_movement"'),
           raw[:200])
        ok(raw.index('"type"') < raw.index('"direction"') < raw.index('"speed"'), raw[:200])
        # ... and a report that came back through JSON (where a sorted writer may have
        # shuffled it) is rebuilt in that order before it is written again.
        shuffled = json.loads(json.dumps(report, sort_keys=True))
        equal([list(entry) for entry in mc.ordered_report(shuffled)],
              [list(entry) for entry in report])
        equal(json.dumps(mc.ordered_report(shuffled), sort_keys=False),
              json.dumps(report, sort_keys=False))
        # A still segment reports Static.
        still = mc.MotionPlan(duration_seconds=1.0, fps=24.0, frame_start=0, frame_end=23,
                              segments=(mc.PlanSegment(0, 0.0, 1.0, 0, 23, ()),),
                              compound=True, seed=0, source=source)
        equal(still.report()[0]["basic_movement"],
              [{"type": "Static", "direction": None, "speed": None}])

    @suite.case("the plan payload describes the layout for the sidecar")
    def _():
        atoms, source = _atoms()
        plan = mc.plan_compound(atoms, duration_seconds=4.0, fps=24.0,
                                max_simultaneous=2, max_segments=2, randomize=False,
                                rng=random.Random(4), seed=4, source=source)
        payload = plan.to_dict()
        for key in ("compound", "duration_seconds", "fps", "frame_start", "frame_end",
                    "frame_count", "segment_count", "max_simultaneous", "segments",
                    "shot_report", "template_source"):
            ok(key in payload, f"{key} missing from the plan payload")
        equal(payload["compound"], True)
        equal(payload["segment_count"], 2)
        equal(payload["max_simultaneous"], 2)
        equal(len(payload["segments"]), 2)
        equal(payload["segments"][0]["start_frame"], 0)
        summary = mc.summarize_plan(plan)
        equal(summary["segments"], 2)
        ok(plan.atom_names, "the plan must name the atoms it uses")

    @suite.case("describe_plan reads like a shot list")
    def _():
        atoms, source = _atoms()
        plan = mc.plan_compound(atoms, duration_seconds=4.0, fps=24.0,
                                max_simultaneous=3, max_segments=3, randomize=False,
                                rng=random.Random(6), seed=6, source=source)
        text = mc.describe_plan(plan)
        ok("3 segment(s)" in text, text)
        ok("4.00 s" in text, text)
        single = mc.plan_single(atoms[0], duration_seconds=2.0, fps=24.0, source=source)
        ok("for 2.00 s" in mc.describe_plan(single), mc.describe_plan(single))

    @suite.case("the contract probe verifies a plan against the poses it produced")
    def _():
        # The generation -> sidecar -> probe round trip, without Blender: generate an
        # animation from a flattened plan and check the probe agrees with it, then
        # perturb one sample and check the probe notices.
        from blender_motion_pipeline.tests import probe_template_contract as contract

        atoms, source = _atoms()
        plan = mc.plan_compound(atoms, duration_seconds=2.0, fps=24.0,
                                max_simultaneous=3, max_segments=2, randomize=False,
                                rng=random.Random(3), seed=3, source=source)
        template = mc.flatten_plan(plan, base_focal=35.0)
        base = [[1.0, 0.0, 0.0, 0.5], [0.0, 0.0, -1.0, 1.5], [0.0, 1.0, 0.0, 0.9],
                [0.0, 0.0, 0.0, 1.0]]
        animation = mt.MotionTemplateGenerator(frame_start=0).generate(
            template, base_matrix=base, base_focal=35.0,
            frame_start=plan.frame_start, frame_end=plan.frame_end)
        samples = [sample.to_dict() for sample in animation.samples]
        config = {
            "sequence": {"motion_name": "combo"},
            "frames": {"frame_start": plan.frame_start, "frame_end": plan.frame_end,
                       "frame_count": plan.frame_count},
            "motion_plan": plan.to_dict(),
        }
        equal(contract.check_plan(samples, config), [],
              "a plan's own poses must satisfy the probe")
        drifted = [dict(sample) for sample in samples]
        drifted[-1]["location"] = [drifted[-1]["location"][0] + 0.02,
                                   drifted[-1]["location"][1],
                                   drifted[-1]["location"][2]]
        detected = contract.check_plan(drifted, config)
        ok(any("away from the plan" in problem for problem in detected), detected)
        turned = [dict(sample) for sample in samples]
        turned[-1]["rotation_quaternion"] = list(turned[0]["rotation_quaternion"])
        detected = contract.check_plan(turned, config)
        ok(any("aim" in problem for problem in detected), detected)

    # -- configuration ---------------------------------------------------
    @suite.case("the composite section validates and round trips")
    def _():
        section = CompositeSection.from_dict(
            {"enabled": True, "max_simultaneous": 4, "max_segments": 6, "random": False,
             "output_mode": "only_compound", "duration_mode": "random",
             "duration_min": 3.0, "duration_max": 8.0, "seed": 5}, [])
        equal(section.enabled, True)
        equal(section.max_simultaneous, 4)
        equal(section.max_segments, 6)
        equal(section.random, False)
        equal(section.want_compound(), True)
        equal(section.want_base(), False, "only_compound must drop the single-move shots")
        equal(section.effective_duration_range(), (3.0, 8.0))
        equal(CompositeSection.from_dict({"enabled": True, "duration": 7.5}, [])
              .effective_duration_range(), (7.5, 7.5))
        equal(CompositeSection().want_compound(), False, "off by default")
        equal(CompositeSection().max_simultaneous, 3)
        equal(CompositeSection().random, True)
        equal(CompositeSection().sequences_per_camera, 1, "one compound per camera by default")
        equal(CompositeSection.from_dict({"sequences_per_camera": 6}, [])
              .sequences_per_camera, 6)
        raises(ConfigError, lambda: CompositeSection.from_dict(
            {"sequences_per_camera": 0}, []))
        raises(ConfigError, lambda: CompositeSection.from_dict(
            {"sequences_per_camera": 501}, []))

        # The limits of the brief.
        raises(ConfigError, lambda: CompositeSection.from_dict({"max_simultaneous": 0}, []))
        raises(ConfigError, lambda: CompositeSection.from_dict({"max_simultaneous": 6}, []))
        raises(ConfigError, lambda: CompositeSection.from_dict({"max_segments": 0}, []))
        raises(ConfigError, lambda: CompositeSection.from_dict({"duration": 0.2}, []))
        raises(ConfigError, lambda: CompositeSection.from_dict({"duration_min": 0.1}, []))
        raises(ConfigError, lambda: CompositeSection.from_dict(
            {"duration_mode": "random", "duration_min": 5.0, "duration_max": 1.0}, []))
        mode_warnings: "list[str]" = []
        fallback = CompositeSection.from_dict({"output_mode": "nonsense"}, mode_warnings)
        equal(fallback.output_mode, "with_base")
        ok(any("nonsense" in w for w in mode_warnings), mode_warnings)

        config = BatchConfig.from_dict({"composite": {"enabled": True, "max_segments": 5}})
        equal(config.composite.max_segments, 5)
        again = BatchConfig.from_dict(config.to_dict())
        equal(again.composite.to_dict(), config.composite.to_dict())

    @suite.case("the config description mentions the compound settings")
    def _():
        from blender_motion_pipeline.config.models import describe_config

        config = BatchConfig()
        ok("composite      : off" in describe_config(config), describe_config(config))
        config.composite.enabled = True
        config.composite.max_simultaneous = 5
        config.composite.max_segments = 6
        config.composite.random = False
        config.composite.duration_mode = "random"
        config.composite.duration_min = 3.0
        config.composite.duration_max = 7.0
        text = describe_config(config)
        ok("max_simultaneous=5" in text and "max_segments=6" in text, text)
        ok("fixed" in text and "3-7s" in text, text)

    return suite


if __name__ == "__main__":
    raise SystemExit(build_suite().run())
