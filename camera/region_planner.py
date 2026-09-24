"""Region-aware planning: reject a plan that leaves the box, then re-draw it in stages.

The camera movement region (``core/region.py``) is a hard *feasibility* rule, not a
clamp: an atomic motion is never bent, shortened or slowed down by force.  What happens
instead is a graded re-draw, cheapest change first:

``L1``  re-draw only the segments whose motion leaves the region (everything else, the
       segment boundaries and the duration stay put);
``L2``  re-draw the whole plan (same duration, segments and camera);
``L3``  re-draw preferring the slowest motion of each family (a Dolly at 0.3 m/s instead
       of 1.5 m/s is still a Dolly -- the atom's semantics are untouched);
``L4``  split the offending segments in half and re-draw both halves, because a small
       region is only reachable with short segments;
``L5``  give up and report it, so the caller can fall back to another template/camera.

The path a plan produces is computed analytically from ``flatten_plan`` (no Blender
evaluation, no baking, no files) which is what makes thousands of candidate plans cheap
enough to try.  Every attempt derives its seed from ``seed ⊕ attempt`` so a given
configuration always reproduces the same accepted plan.
"""

from __future__ import annotations

import random
from dataclasses import replace

from ..core.region import RegionSpec, feasible, region_report
from .motion_composite import (
    AtomicMotion,
    MotionPlan,
    choose_motions,
    flatten_plan,
)

#: Attempt limits per stage: (segment redraws, whole-plan redraws, speed-preferring,
#: segment splits).
DEFAULT_LIMITS = (8, 8, 4, 3)

#: Shortest segment the split stage may leave behind (mirrors the compound rule).
MIN_SPLIT_SECONDS = 0.5

#: How much safety room a plan must leave to be accepted (metres).
DEFAULT_MARGIN = 0.25


def _attempt_seed(seed: int, attempt: int) -> int:
    """Deterministic per-attempt seed (never reuse the caller's RNG stream)."""
    return (int(seed) * 1_000_003 + int(attempt) * 104_729 + 17) % (2 ** 31 - 1)


def positions_of_plan(plan: MotionPlan, *, base_position=(0.0, 0.0, 0.0),
                      base_basis=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
                      base_focal: float | None = None) -> "list[tuple[float, float, float]]":
    """World-space camera positions of a plan, analytically.

    ``flatten_plan`` already turns a plan into one camera-local sample per frame (the
    same samples the generator bakes), so the world path is just
    ``base_position + base_basis · local`` -- no scene, no depsgraph, microseconds.
    """
    template = flatten_plan(plan, base_focal=base_focal)
    basis = tuple(float(v) for v in base_basis)
    origin = tuple(float(v) for v in base_position)
    positions = []
    for keyframe in template.keyframes:
        local = tuple(float(v) for v in keyframe.location)
        positions.append((
            origin[0] + basis[0] * local[0] + basis[1] * local[1] + basis[2] * local[2],
            origin[1] + basis[3] * local[0] + basis[4] * local[1] + basis[5] * local[2],
            origin[2] + basis[6] * local[0] + basis[7] * local[1] + basis[8] * local[2],
        ))
    return positions


def plan_report(plan: MotionPlan, region: "RegionSpec | None", *,
                base_position=(0.0, 0.0, 0.0), base_basis=None,
                base_focal: float | None = None) -> dict:
    """Region report for one plan (``available=False`` when there is no region)."""
    if region is None:
        return region_report(None, ())
    kwargs = {"base_position": base_position, "base_focal": base_focal}
    if base_basis is not None:
        kwargs["base_basis"] = base_basis
    positions = positions_of_plan(plan, **kwargs)
    return region_report(region, positions, frame_start=plan.frame_start)


def plan_is_feasible(plan: MotionPlan, region: "RegionSpec | None", *,
                     base_position=(0.0, 0.0, 0.0), base_basis=None,
                     base_focal: float | None = None,
                     margin: float = DEFAULT_MARGIN) -> "tuple[bool, dict]":
    """``(ok, report)`` -- ok is False as soon as one frame leaves the region."""
    if region is None:
        return True, region_report(None, ())
    kwargs = {"base_position": base_position, "base_focal": base_focal}
    if base_basis is not None:
        kwargs["base_basis"] = base_basis
    positions = positions_of_plan(plan, **kwargs)
    report = region_report(region, positions, frame_start=plan.frame_start)
    return feasible(region, positions, margin=margin), report


def offending_segments(plan: MotionPlan, region: "RegionSpec | None", *,
                       base_position=(0.0, 0.0, 0.0), base_basis=None,
                       base_focal: float | None = None,
                       margin: float = DEFAULT_MARGIN) -> "list[int]":
    """Indices of the segments whose own frames leave the region.

    Only segments that actually move the camera can show up here (a Pan or a Zoom does
    not change the position), which is exactly the rule the region follows.
    """
    if region is None:
        return []
    whole_ok, _report = plan_is_feasible(
        plan, region, base_position=base_position, base_basis=base_basis,
        base_focal=base_focal, margin=margin)
    if whole_ok:
        return []
    kwargs = {"base_position": base_position, "base_focal": base_focal}
    if base_basis is not None:
        kwargs["base_basis"] = base_basis
    positions = positions_of_plan(plan, **kwargs)
    bad: "list[int]" = []
    for segment in plan.segments:
        # ``positions`` holds one sample per frame from the plan's first frame, and a
        # segment's own start/end frames are relative to that first frame (the same
        # convention ``plan_compound`` and ``flatten_plan`` use).
        chunk = positions[max(0, int(segment.start_frame)):
                          max(1, int(segment.end_frame) + 1)]
        if not chunk:
            continue
        if not feasible(region, chunk, margin=margin):
            bad.append(int(segment.index))
    return bad


def changes_position(atom: AtomicMotion) -> bool:
    """True when the atom translates the camera, i.e. the region applies to it.

    Rotation- and focal-only atoms can never leave a box, so they are deliberately
    absent from the re-draw pool: their atomic definition is never touched.
    """
    rate = getattr(atom, "rate_location", None) or ()
    return any(abs(float(value)) > 1e-12 for value in rate)


def _motion_size(atom: AtomicMotion) -> float:
    """How far an atom pushes the camera: metres plus a scaled rotation allowance.

    Measured from the atom's own rates, because :meth:`AtomicMotion.summary` carries no
    numbers.  Used only to *prefer* the smaller atom of a family at stage L3 -- never to
    scale or clamp one.
    """
    total = 0.0
    rates = list(getattr(atom, "rate_location", None) or ())
    rates += list(getattr(atom, "rate_rotation", None) or ())
    for value in rates:
        try:
            total += abs(float(value))
        except (TypeError, ValueError):
            continue
    try:
        total += abs(float(getattr(atom, "rate_focal", 0.0) or 0.0)) / 100.0
    except (TypeError, ValueError):
        pass
    return total


def _smaller_pool(movable, slower) -> "list[AtomicMotion]":
    """Candidate atoms for the L3/L4 stages: *movable* plus smaller family variants.

    *slower* is an optional wider pool of atoms (normally the whole atomic library).  The
    pool keeps every variant of a family -- the ``type``, so another speed *and* the
    opposite direction are available, which is what lets a plan bring the camera back
    towards the middle of the box instead of drifting out of it.  Variants are ordered
    smallest first, so drawing from the front of the pool means "prefer the slow one".
    """
    if not slower:
        return []
    families = {atom.type for atom in movable}
    pool, seen = [], set()
    for atom in list(slower) + list(movable):
        if atom.type in families and changes_position(atom):
            if atom.name not in seen:
                seen.add(atom.name)
                pool.append(atom)
    # A hold (an atom with no channels) is the smallest move of all and is always
    # compatible, so it is what makes a really tight box reachable -- and it is a real
    # atomic motion being *chosen*, never an atom being bent.
    for atom in slower:
        if not getattr(atom, "channels", ()) and atom.name not in seen:
            seen.add(atom.name)
            pool.append(atom)
    pool.sort(key=_motion_size)
    return pool


def _prefer_slow(atoms, rng: random.Random) -> "list[AtomicMotion]":
    """Atoms ordered so that the smaller ones come first (same atoms, new order)."""
    pool = list(atoms)
    rng.shuffle(pool)
    pool.sort(key=_motion_size)
    return pool


def _redraw_segments(plan: MotionPlan, movable, indices, rng: random.Random,
                     max_simultaneous: int) -> MotionPlan:
    """Replace the motion sets of *indices*, keeping every boundary untouched."""
    segments = []
    for segment in plan.segments:
        if segment.index not in indices:
            segments.append(segment)
            continue
        wanted = max(1, min(int(max_simultaneous), len(segment.motions) or 1))
        motions = choose_motions(movable, wanted, rng)
        if not motions:                       # nothing compatible: keep the old set
            segments.append(segment)
            continue
        segments.append(replace(segment, motions=tuple(motions)))
    return replace(plan, segments=tuple(segments))


def _split_segments(plan: MotionPlan, indices, movable, rng: random.Random,
                    max_simultaneous: int) -> MotionPlan:
    """Split each offending segment in half and re-draw both halves.

    Displacement is a rate integrated over a segment's duration, so no choice of motions
    can fit a small region inside a segment that is simply too long.  Splitting changes
    the plan's time structure only -- never an atomic definition -- and keeps every piece
    on the plan's own frame grid.  Untouched segments keep their boundaries and indices.
    """
    pieces = []
    split_any = False
    for segment in plan.segments:
        if segment.index not in indices:
            pieces.append(segment)
            continue
        start, end = float(segment.start_time), float(segment.end_time)
        middle = (start + end) / 2.0
        if (end - start) < 2.0 * MIN_SPLIT_SECONDS:
            pieces.append(segment)               # too short to split any further
            continue
        split_any = True
        wanted = max(1, min(int(max_simultaneous), len(segment.motions) or 1))
        for lo, hi in ((start, middle), (middle, end)):
            motions = choose_motions(movable, wanted, rng) or tuple(segment.motions)
            start_frame = int(round(lo * float(plan.fps)))
            end_frame = max(start_frame, int(round(hi * float(plan.fps))) - 1)
            pieces.append(replace(segment, start_time=lo, end_time=hi,
                                  start_frame=start_frame, end_frame=end_frame,
                                  motions=tuple(motions)))
    if not split_any:
        return plan
    return replace(plan, segments=tuple(replace(piece, index=index)
                                        for index, piece in enumerate(pieces)))


def draw_feasible_plan(
    build_plan,
    atoms,
    *,
    region: "RegionSpec | None" = None,
    base_position=(0.0, 0.0, 0.0),
    base_basis=None,
    base_focal: float | None = None,
    seed: int = 0,
    max_simultaneous: int = 3,
    margin: float = DEFAULT_MARGIN,
    limits=DEFAULT_LIMITS,
    downgrade: bool = True,
    slower=None,
    logger=None,
) -> "tuple[MotionPlan, dict]":
    """Draw a plan that stays inside *region*, changing as little as possible.

    *build_plan* is called as ``build_plan(seed=…, atoms=…)`` and must return a
    :class:`MotionPlan` (normally a lambda around ``plan_compound``).  *slower* is an
    optional wider atom pool (normally the whole atomic library) that stage L3 may draw
    smaller same-family variants from; without it L3 only re-orders *atoms*.  Returns the
    plan plus a record::

        {"ok": bool, "stage": "L0|L1|L2|L3|L4|L5", "attempts": int,
         "segment_redraws": int, "plan_redraws": int, "speed_preferred": int,
         "split_rounds": int, "report": {...}, "seed": int}
    """
    limit_segment, limit_plan, limit_slow, limit_split = (list(limits) + [0, 0, 0, 0])[:4]
    movable = [atom for atom in atoms if changes_position(atom)]
    record = {
        "ok": False, "stage": "L0", "attempts": 0, "segment_redraws": 0,
        "plan_redraws": 0, "speed_preferred": 0, "split_rounds": 0, "report": {},
        "seed": int(seed),
    }

    plan = build_plan(seed=int(seed), atoms=atoms)
    record["attempts"] += 1
    ok, report = plan_is_feasible(plan, region, base_position=base_position,
                                  base_basis=base_basis, base_focal=base_focal,
                                  margin=margin)
    if ok or region is None:
        record.update({"ok": True, "stage": "L0", "report": report})
        return plan, record

    # -- L1: only the offending segments ---------------------------------
    for attempt in range(1, limit_segment + 1):
        bad = offending_segments(plan, region, base_position=base_position,
                                 base_basis=base_basis, base_focal=base_focal,
                                 margin=margin)
        if not bad:
            break
        rng = random.Random(_attempt_seed(seed, attempt))
        candidate = _redraw_segments(plan, movable, set(bad), rng, max_simultaneous)
        record["attempts"] += 1
        record["segment_redraws"] += 1
        ok, report = plan_is_feasible(candidate, region, base_position=base_position,
                                      base_basis=base_basis, base_focal=base_focal,
                                      margin=margin)
        plan = candidate
        if ok:
            record.update({"ok": True, "stage": "L1", "report": report})
            return plan, record

    # -- L2: a whole new plan --------------------------------------------
    for attempt in range(1, limit_plan + 1):
        candidate = build_plan(seed=_attempt_seed(seed, 1000 + attempt), atoms=atoms)
        record["attempts"] += 1
        record["plan_redraws"] += 1
        ok, report = plan_is_feasible(candidate, region, base_position=base_position,
                                      base_basis=base_basis, base_focal=base_focal,
                                      margin=margin)
        plan = candidate
        if ok:
            record.update({"ok": True, "stage": "L2", "report": report})
            return plan, record

    # -- L3: prefer the smallest motion of each family -------------------
    if downgrade and movable:
        smaller = _smaller_pool(movable, slower)
        for attempt in range(1, limit_slow + 1):
            rng = random.Random(_attempt_seed(seed, 2000 + attempt))
            ordered = _prefer_slow(smaller or movable, rng)
            candidate = build_plan(seed=_attempt_seed(seed, 3000 + attempt), atoms=ordered)
            record["attempts"] += 1
            record["speed_preferred"] += 1
            ok, report = plan_is_feasible(candidate, region, base_position=base_position,
                                          base_basis=base_basis, base_focal=base_focal,
                                          margin=margin)
            plan = candidate
            if ok:
                record.update({"ok": True, "stage": "L3", "report": report})
                return plan, record

    # -- L4: split the offending segments (structure change, last resort) --
    if downgrade and movable and limit_split:
        pool = _smaller_pool(movable, slower) or movable
        for attempt in range(1, limit_split + 1):
            bad = offending_segments(plan, region, base_position=base_position,
                                     base_basis=base_basis, base_focal=base_focal,
                                     margin=margin)
            if not bad:
                break
            rng = random.Random(_attempt_seed(seed, 4000 + attempt))
            candidate = _split_segments(plan, set(bad), pool, rng, max_simultaneous)
            record["attempts"] += 1
            record["split_rounds"] += 1
            ok, report = plan_is_feasible(candidate, region, base_position=base_position,
                                          base_basis=base_basis, base_focal=base_focal,
                                          margin=margin)
            plan = candidate
            if ok:
                record.update({"ok": True, "stage": "L4", "report": report})
                return plan, record

    # -- L5: report honestly --------------------------------------------
    ok, report = plan_is_feasible(plan, region, base_position=base_position,
                                  base_basis=base_basis, base_focal=base_focal,
                                  margin=margin)
    record.update({"ok": False, "stage": "L5", "report": report})
    if logger is not None:
        logger.warning(
            "camera movement region: no feasible plan after %d attempt(s) "
            "(%d segment redraw(s), %d plan redraw(s), %d slow-preference retry(ies)); "
            "worst frame %s leaves the box by %.2f m",
            record["attempts"], record["segment_redraws"], record["plan_redraws"],
            record["speed_preferred"], report.get("worst_frame"),
            float(report.get("max_excess_m") or 0.0),
        )
    return plan, record
