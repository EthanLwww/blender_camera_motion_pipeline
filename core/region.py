"""Camera movement region: an oriented box (OBB) the camera has to stay inside.

Pure geometry on purpose -- no ``bpy``, no ``mathutils``: the Blender side of the
add-on only has to hand over the object's world matrix, everything here is arithmetic
that the test suite can exercise without opening Blender.

The region is authored in the viewport (a cube the artist moves/rotates/scales, see
``--region`` in the panel) and then **baked into the sequence config as numbers**, so a
render node never has to find the helper object again.

Coordinates: ``center`` and points are world-space metres; ``basis`` is the region's
3x3 rotation, row-major (``[xx, xy, xz, yx, ...]``), mapping region-local axes to world
axes.  A point is converted with ``local = basisᵀ · (point − center)`` and compared
against ``half_size`` minus ``inset`` on every axis.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Region modes, mirroring the panel dropdown.
MODE_OFF = "off"
MODE_AUTO = "auto"
MODE_OBJECT = "object"
MODE_NUMBERS = "numbers"

#: Ignore anything smaller than this fraction of the largest box when auto-detecting,
#: so pebbles and grass cards cannot shrink the region (or a stray object blow it up).
AUTO_SMALL_RATIO = 0.01

#: Never let the auto-detected region be thinner than this (metres), otherwise a flat
#: scene (a ground plane only) would give a zero-height box.
AUTO_MIN_HALF_EXTENT = 0.5

IDENTITY_BASIS = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


def basis_from_euler_deg(rx: float, ry: float, rz: float) -> "tuple[float, ...]":
    """Row-major rotation matrix for an XYZ Euler triple in degrees (world = R·local)."""
    cx, sx = math.cos(math.radians(rx)), math.sin(math.radians(rx))
    cy, sy = math.cos(math.radians(ry)), math.sin(math.radians(ry))
    cz, sz = math.cos(math.radians(rz)), math.sin(math.radians(rz))
    return (
        cy * cz, cz * sx * sy - cx * sz, cx * cz * sy + sx * sz,
        cy * sz, cx * cz + sx * sy * sz, -cz * sx + cx * sy * sz,
        -sy, cy * sx, cx * cy,
    )


def apply_basis(basis, vector):
    """``basis · vector`` (the transpose of the world-to-local direction)."""
    x, y, z = vector
    return (
        basis[0] * x + basis[1] * y + basis[2] * z,
        basis[3] * x + basis[4] * y + basis[5] * z,
        basis[6] * x + basis[7] * y + basis[8] * z,
    )


def to_local(spec: "RegionSpec", point) -> "tuple[float, float, float]":
    """World point -> region-local coordinates."""
    offset = (point[0] - spec.center[0], point[1] - spec.center[1], point[2] - spec.center[2])
    return apply_basis(spec.transposed_basis(), offset)


#: Tolerance for "a frame sits on the box wall" when a fit measures the path it just
#: optimised onto that wall.  The optimum of :func:`fit_translation_scale` puts a frame
#: *exactly* on the wall by construction, so the last bit of the arithmetic (1e-16 m)
#: and the 6-decimal rounding of the scale (5e-7 of the amplitude, i.e. this much per
#: metre the camera travels) read as an overshoot.  A real run then labelled 14 of 246
#: sequences "fit-failed" with a worst excess of 0.000 m.  A micron per metre of travel
#: is far below anything a camera path can be judged by -- a genuine violation is
#: millimetres at the very least -- so nothing is masked by it.
BOUNDARY_SLACK_M = 1e-6


def with_inset(spec: "RegionSpec | None", extra: float) -> "RegionSpec | None":
    """The same box with ``extra`` metres more inset.

    The run's ``region.margin`` is applied through this, so the box a shot is
    *judged* against and the box it is *reported* against are always the same one.
    """
    if spec is None or not extra:
        return spec
    return RegionSpec(center=spec.center, half_size=spec.half_size, basis=spec.basis,
                      inset=float(spec.inset) + float(extra), mode=spec.mode,
                      source=spec.source)


@dataclass(frozen=True)
class RegionSpec:
    """An oriented box plus the inset the camera has to respect."""

    center: "tuple[float, float, float]"
    half_size: "tuple[float, float, float]"
    basis: "tuple[float, ...]" = IDENTITY_BASIS
    inset: float = 0.0
    mode: str = MODE_NUMBERS
    source: str = ""

    # -- derived ---------------------------------------------------------
    def usable_half(self) -> "tuple[float, float, float]":
        """Half size after the inset, never negative."""
        return tuple(max(0.0, float(value) - float(self.inset)) for value in self.half_size)

    def transposed_basis(self) -> "tuple[float, ...]":
        """The inverse rotation (orthonormal, so the transpose is the inverse)."""
        b = self.basis
        return (b[0], b[3], b[6], b[1], b[4], b[7], b[2], b[5], b[8])

    def radius(self) -> float:
        return math.sqrt(sum(float(value) ** 2 for value in self.half_size))

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "source": self.source,
            "center": [round(float(v), 6) for v in self.center],
            "half_size": [round(float(v), 6) for v in self.half_size],
            "basis": [round(float(v), 8) for v in self.basis],
            "inset": round(float(self.inset), 6),
        }

    @classmethod
    def from_dict(cls, payload) -> "RegionSpec | None":
        if not isinstance(payload, dict):
            return None
        try:
            center = tuple(float(v) for v in payload["center"])
            half = tuple(float(v) for v in payload["half_size"])
        except Exception:
            return None
        if len(center) != 3 or len(half) != 3:
            return None
        basis = tuple(float(v) for v in (payload.get("basis") or IDENTITY_BASIS))
        if len(basis) != 9:
            basis = IDENTITY_BASIS
        return cls(center=center, half_size=half, basis=basis,
                   inset=float(payload.get("inset") or 0.0),
                   mode=str(payload.get("mode") or MODE_NUMBERS),
                   source=str(payload.get("source") or ""))

    def describe(self) -> str:
        extent = ", ".join("%.2f" % (2.0 * float(v)) for v in self.half_size)
        return "region %s %.2f x %.2f x %.2f m at (%.2f, %.2f, %.2f), inset %.2f%s" % (
            self.mode, *[float(v) for v in self.half_size], *[float(v) for v in self.center],
            float(self.inset), (" [%s]" % self.source) if self.source else "",
        )


def bounds_from_boxes(boxes, *, small_ratio: float = AUTO_SMALL_RATIO):
    """Union of ``(lo, hi)`` boxes, ignoring the dust.

    *boxes* is an iterable of ``((x, y, z), (x, y, z))`` pairs (world-space AABBs of the
    scene's meshes).  Boxes smaller than ``small_ratio`` of the largest one are dropped,
    which is what keeps grass cards and pebbles from dominating the region.
    Returns ``(lo, hi)`` or ``None`` when nothing usable was passed.

    Note the deliberate asymmetry: a tiny object is ignored, but a huge one (a
    background mountain) still counts -- pass a curated object list if that is wrong for
    the scene.
    """
    entries = []
    for item in boxes or ():
        try:
            lo, hi = item
            size = [max(0.0, float(hi[i]) - float(lo[i])) for i in range(3)]
        except Exception:
            continue
        volume = size[0] * size[1] * size[2]
        entries.append((volume, tuple(float(v) for v in lo), tuple(float(v) for v in hi)))
    if not entries:
        return None
    largest = max(volume for volume, _lo, _hi in entries)
    kept = [row for row in entries if largest <= 0 or row[0] >= largest * float(small_ratio)]
    if not kept:
        kept = entries
    lo = tuple(min(row[1][axis] for row in kept) for axis in range(3))
    hi = tuple(max(row[2][axis] for row in kept) for axis in range(3))
    return lo, hi


def region_from_bounds(lo, hi, *, margin_percent: float = 0.0, inset: float = 0.0,
                       mode: str = MODE_AUTO, source: str = "") -> RegionSpec:
    """Axis-aligned region from world bounds, grown by *margin_percent* then inset."""
    half = []
    for axis in range(3):
        extent = max(0.0, float(hi[axis]) - float(lo[axis]))
        grown = extent * (1.0 + max(0.0, float(margin_percent)) / 100.0)
        half.append(max(AUTO_MIN_HALF_EXTENT, grown / 2.0))
    center = tuple((float(lo[axis]) + float(hi[axis])) / 2.0 for axis in range(3))
    return RegionSpec(center=center, half_size=tuple(half), basis=IDENTITY_BASIS,
                      inset=float(inset), mode=mode, source=source)


def contains(spec: RegionSpec, point, *, tolerance: float = 1e-6) -> bool:
    half = spec.usable_half()
    local = to_local(spec, point)
    return all(abs(local[axis]) <= half[axis] + tolerance for axis in range(3))


def excess_of(spec: RegionSpec, point) -> float:
    """How far outside the region *point* is (0.0 when it is inside)."""
    half = spec.usable_half()
    local = to_local(spec, point)
    return max(0.0, max(abs(local[axis]) - half[axis] for axis in range(3)))


def clearance_of(spec: RegionSpec, point) -> float:
    """Distance from *point* to the closest wall (negative when outside)."""
    half = spec.usable_half()
    local = to_local(spec, point)
    return min(half[axis] - abs(local[axis]) for axis in range(3))


def region_report(spec: "RegionSpec | None", positions, *, frame_start: int = 0,
                  tolerance: float = 0.0) -> dict:
    """Summarise a camera path against the region.

    ``{"available", "frames", "exit_frames", "first_exit_frame", "max_excess_m",
       "min_clearance_m", "position_offset": (frame, (dx, dy, dz))}`` -- the offset of the
    worst frame is included so a message can point at *where* it left the box.

    ``tolerance`` is how far outside counts as still being inside.  It stays 0.0 for
    ordinary measurements (a path either leaves the box or it does not); the template
    fit passes a micron-scale value because *its* optimum is defined to sit on the wall
    (see ``BOUNDARY_SLACK_M``).
    """
    result = {
        "available": False,
        "frames": 0,
        "exit_frames": 0,
        "first_exit_frame": None,
        "max_excess_m": 0.0,
        "min_clearance_m": None,
        "worst_frame": None,
    }
    if spec is None:
        return result
    result["available"] = True
    limit = float(tolerance) if tolerance and tolerance > 0.0 else 0.0
    worst = (0.0, None, None)
    first = None
    for index, point in enumerate(positions or ()):
        frame = int(frame_start) + index
        result["frames"] += 1
        excess = excess_of(spec, point)
        clearance = clearance_of(spec, point)
        if excess > limit:
            result["exit_frames"] += 1
            if first is None:
                first = frame
        if result["min_clearance_m"] is None or clearance < result["min_clearance_m"]:
            result["min_clearance_m"] = clearance
        if excess > worst[0]:
            local = to_local(spec, point)
            half = spec.usable_half()
            offset = tuple(
                math.copysign(abs(local[axis]) - half[axis], local[axis]) if abs(local[axis]) > half[axis]
                else 0.0
                for axis in range(3)
            )
            worst = (excess, frame, offset)
    result["first_exit_frame"] = first
    result["max_excess_m"] = round(worst[0], 6)
    result["worst_frame"] = worst[1]
    result["position_offset"] = list(worst[2]) if worst[2] else [0.0, 0.0, 0.0]
    if result["min_clearance_m"] is not None:
        result["min_clearance_m"] = round(result["min_clearance_m"], 6)
    return result


def feasible(spec: "RegionSpec | None", positions, *, margin: float = 0.0) -> bool:
    """True when every position stays inside the region, optionally with *margin* to spare."""
    if spec is None:
        return True
    if margin:
        spec = RegionSpec(center=spec.center, half_size=spec.half_size, basis=spec.basis,
                          inset=float(spec.inset) + float(margin), mode=spec.mode)
    for point in positions or ():
        if excess_of(spec, point) > 0.0:
            return False
    return True


def fit_translation_scale(spec: "RegionSpec | None", base_position, offsets, *,
                          margin: float = 0.0, minimum: float = 0.05,
                          maximum: float = 1.0) -> dict:
    """Largest uniform shrink of a path's *translation* that keeps it inside the box.

    This is the fixed-template counterpart of the re-draw ladder.  A template is a
    whole shot: its shape, its timing and its angles are the shot, so nothing here
    re-draws or re-orders anything -- the camera's offsets from its first frame are
    multiplied by one factor ``s`` and the shot plays out smaller.  Because
    ``position(s) = base + s * offset`` is affine in ``s``, the feasible set of ``s``
    is an interval per frame and axis, and the answer is the top of their
    intersection: exact, single pass, no search.

    Returns ``{"scale", "ok", "frames", "exit_frames", "max_excess_m",
    "start_clearance_m", "tolerance_m", "reason"}``.  ``scale`` is 1.0 when nothing had
    to change and ``ok`` is False when even ``minimum`` does not fit (the camera's
    *first* frame is outside the box, which no amount of shrinking can fix).
    """
    offsets = [tuple(float(v) for v in item) for item in (offsets or ())]
    base_position = tuple(float(v) for v in base_position)
    result = {"scale": 1.0, "ok": True, "frames": len(offsets), "exit_frames": 0,
              "max_excess_m": 0.0, "start_clearance_m": None, "tolerance_m": 0.0,
              "reason": ""}
    if spec is None or not offsets:
        return result
    spec = with_inset(spec, margin)
    half = spec.usable_half()
    basis = spec.transposed_basis()
    center = spec.center
    relative = tuple(base_position[axis] - center[axis] for axis in range(3))
    base_local = apply_basis(basis, relative)
    limit = float(maximum)
    floor = 0.0
    for axis in range(3):
        if abs(base_local[axis]) > half[axis]:
            result["ok"] = False
            result["scale"] = 0.0
            result["start_clearance_m"] = round(clearance_of(spec, base_position), 6)
            result["reason"] = (
                f"the camera's first frame is outside the box on axis {'xyz'[axis]} by "
                f"{abs(base_local[axis]) - half[axis]:.3f} m; shrinking the motion cannot "
                "move it"
            )
            return result
    for offset in offsets:
        local = apply_basis(basis, offset)
        for axis in range(3):
            delta = local[axis]
            if abs(delta) <= 1e-12:
                continue
            low = (-half[axis] - base_local[axis]) / delta
            high = (half[axis] - base_local[axis]) / delta
            if low > high:
                low, high = high, low
            limit = min(limit, high)
            floor = max(floor, low)
        if limit <= 0.0:
            break
    limit = max(0.0, min(float(maximum), limit))
    if limit + 1e-9 < floor:
        result["ok"] = False
        result["scale"] = 0.0
        result["reason"] = (
            f"no single amplitude keeps the path inside the box: it has to be at least "
            f"{floor:.3f} of the template to fit one axis and at most {limit:.3f} for another"
        )
        return result
    if limit < float(minimum):
        result["ok"] = False
        result["scale"] = 0.0
        result["reason"] = (
            f"the motion would have to shrink to {limit:.3f} of its amplitude to stay "
            f"inside the box, below the {float(minimum):.2f} floor"
        )
        return result
    # Round the scale *down* to six decimals: the recorded scale is what gets applied,
    # and a value above the optimum is a path that leaves the box by construction.
    result["scale"] = math.floor(limit * 1e6 + 1e-9) / 1e6
    # Measure the path this function actually returns -- against the same box, with a
    # tolerance that grows with how far the camera travels, because rounding the scale
    # to six decimals moves a frame by up to 1e-6 of its distance from the first one.
    # The optimum sits on the wall by construction, so without this the fit reported
    # "even at 0.781 of the amplitude 1 frame(s) leave the box (worst excess 0.000 m)".
    applied = float(result["scale"])
    scaled = [tuple(base_position[axis] + applied * offset[axis] for axis in range(3))
              for offset in offsets]
    reach = max((math.sqrt(sum(float(v) ** 2 for v in offset)) for offset in offsets),
                default=0.0)
    tolerance = BOUNDARY_SLACK_M * (1.0 + reach)
    result["tolerance_m"] = tolerance
    report = region_report(spec, scaled, tolerance=tolerance)
    result["exit_frames"] = int(report["exit_frames"])
    result["max_excess_m"] = float(report["max_excess_m"])
    result["ok"] = report["exit_frames"] == 0
    if not result["ok"]:
        result["reason"] = (
            f"even at {applied:.3f} of the amplitude {report['exit_frames']} frame(s) leave "
            f"the box (worst excess {report['max_excess_m']:.3f} m)"
        )
    return result
