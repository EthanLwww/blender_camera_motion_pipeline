"""Resolve the configured camera-movement region inside a live Blender scene.

The maths lives in :mod:`..core.region` (pure Python, unit-tested without Blender); this
module is the thin adapter that turns scene objects into boxes and reads the
``region`` config section.  Three ways to get a box:

``auto``     fit it to the scene's own objects, ignoring scattered debris (a stray leaf
             or a bolt must not stretch the camera's play area across the map);
``object``   use the local bounding box of one named object, including its rotation;
``numbers``  use ``center``/``size``/``rotation`` from the config.

The resolved box is baked into the sequence metadata as plain numbers, so a render node
never needs the helper object or the scene to know where the camera was allowed to go.
"""

from __future__ import annotations

import fnmatch
import math

from ..core.region import (
    AUTO_SMALL_RATIO,
    MODE_AUTO,
    MODE_NUMBERS,
    MODE_OBJECT,
    MODE_OFF,
    RegionSpec,
    basis_from_euler_deg,
    bounds_from_boxes,
    region_from_bounds,
)

#: Object types that never describe the action area.
IGNORE_TYPES = ("CAMERA", "LIGHT", "SPEAKER", "LIGHT_PROBE")

#: Name patterns skipped by ``auto`` (the helper box, cameras, empties, glue objects).
IGNORE_NAME_PATTERNS = (
    "camera*",
    "cam_*",
    "helper*",
    "*_helper",
    "region*",
    "*_region",
    "empty*",
    "_*",
)


def transform_point(matrix, point) -> "tuple[float, float, float]":
    """``matrix @ point`` without importing :mod:`mathutils`."""
    x, y, z = (float(point[0]), float(point[1]), float(point[2]))
    return (
        float(matrix[0][0]) * x + float(matrix[0][1]) * y + float(matrix[0][2]) * z + float(matrix[0][3]),
        float(matrix[1][0]) * x + float(matrix[1][1]) * y + float(matrix[1][2]) * z + float(matrix[1][3]),
        float(matrix[2][0]) * x + float(matrix[2][1]) * y + float(matrix[2][2]) * z + float(matrix[2][3]),
    )


def world_corners(obj) -> "list[tuple[float, float, float]]":
    """The eight world-space corners of an object's local bounding box."""
    matrix = obj.matrix_world
    return [transform_point(matrix, corner) for corner in obj.bound_box]


def oriented_box(obj) -> "tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]":
    """``(center, half_size, basis)`` of an object's own box, rotation included.

    The basis is the object's world axes, normalised, in the row-major layout
    :func:`..core.region.apply_basis` expects, so a rotated helper box stays rotated.
    """
    matrix = obj.matrix_world
    corners = [tuple(float(v) for v in corner) for corner in obj.bound_box]
    lows = [min(corner[axis] for corner in corners) for axis in range(3)]
    highs = [max(corner[axis] for corner in corners) for axis in range(3)]
    center_local = tuple((lows[axis] + highs[axis]) / 2.0 for axis in range(3))
    half_local = tuple((highs[axis] - lows[axis]) / 2.0 for axis in range(3))

    axes, scales = [], []
    for column in range(3):
        axis = tuple(float(matrix[row][column]) for row in range(3))
        length = math.sqrt(sum(value * value for value in axis)) or 1.0
        axes.append(tuple(value / length for value in axis))
        scales.append(length)

    center = transform_point(matrix, center_local)
    half_size = tuple(half_local[axis] * scales[axis] for axis in range(3))
    basis = (
        axes[0][0], axes[1][0], axes[2][0],
        axes[0][1], axes[1][1], axes[2][1],
        axes[0][2], axes[1][2], axes[2][2],
    )
    return center, half_size, basis


def wanted_for_auto(obj, ignore=()) -> bool:
    """True when ``auto`` should take this object into account."""
    if getattr(obj, "type", "") in IGNORE_TYPES:
        return False
    if getattr(obj, "hide_render", False) and getattr(obj, "hide_viewport", False):
        return False
    name = str(getattr(obj, "name", "") or "")
    if name in ignore:
        return False
    return not any(fnmatch.fnmatch(name.lower(), pattern) for pattern in IGNORE_NAME_PATTERNS)


def resolve_helper(scene, name, *, logger=None) -> "object | None":
    """Find the helper object and keep it out of renders."""
    helper = scene.objects.get(name) if name else None
    if helper is None:
        if name and logger is not None:
            logger.warning("region.helper_object %r is not in the scene", name)
        return None
    helper.hide_render = True
    return helper


def region_spec_from_section(section, *, scene=None, logger=None) -> "RegionSpec | None":
    """Build the :class:`RegionSpec` the config asks for (``None`` when disabled).

    ``mode=off`` returns ``None``, which is what keeps the feature byte-for-byte invisible
    for everyone who does not use it.
    """
    mode = str(getattr(section, "mode", MODE_OFF) or MODE_OFF).strip().lower()
    if mode in ("", MODE_OFF, "none", "false"):
        return None
    inset = float(getattr(section, "inset", 0.0) or 0.0)

    if mode == MODE_NUMBERS:
        center = [float(v) for v in (getattr(section, "center", None) or (0.0, 0.0, 0.0))]
        size = [float(v) for v in (getattr(section, "size", None) or (8.0, 8.0, 4.0))]
        rotation = [float(v) for v in (getattr(section, "rotation", None) or (0.0, 0.0, 0.0))]
        return RegionSpec(
            center=tuple(center), half_size=tuple(value / 2.0 for value in size),
            basis=basis_from_euler_deg(*rotation), inset=inset,
            mode=MODE_NUMBERS, source="config",
        )

    if scene is None:
        return None

    ignore = set()
    helper = resolve_helper(scene, getattr(section, "helper_object", ""), logger=logger)
    if helper is not None:
        ignore.add(str(helper.name))

    if mode == MODE_OBJECT:
        target = scene.objects.get(str(getattr(section, "object_name", "") or ""))
        if target is None:
            if logger is not None:
                logger.warning(
                    "region: object %r is not in the scene; the region is ignored",
                    getattr(section, "object_name", ""),
                )
            return None
        center, half_size, basis = oriented_box(target)
        return RegionSpec(center=center, half_size=half_size, basis=basis, inset=inset,
                          mode=MODE_OBJECT, source=str(target.name))

    boxes = [world_corners(obj) for obj in scene.objects if wanted_for_auto(obj, ignore)]
    if not boxes:
        if logger is not None:
            logger.warning("region: auto found no object to fit the region to")
        return None
    fitted = bounds_from_boxes(boxes, small_ratio=AUTO_SMALL_RATIO)
    if fitted is None:
        # Everything looked like debris (a scene of thin or scattered objects can do
        # that).  Fall back to the plain union of what is there rather than switching the
        # feature off behind the user's back.
        lo = tuple(min(corner[axis] for box in boxes for corner in box) for axis in range(3))
        hi = tuple(max(corner[axis] for box in boxes for corner in box) for axis in range(3))
        if logger is not None:
            logger.info(
                "camera region: auto found no dominant object in %d object(s); "
                "using their plain union", len(boxes),
            )
    else:
        lo, hi = fitted
    spec = region_from_bounds(
        lo, hi,
        margin_percent=float(getattr(section, "margin_percent", 0.0) or 0.0),
        inset=inset, mode=MODE_AUTO, source="auto",
    )
    if logger is not None:
        logger.info("camera region (auto, %d object(s)): %s", len(boxes), spec.describe())
    return spec
