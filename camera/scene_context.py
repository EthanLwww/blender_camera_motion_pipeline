"""Blender-side wrappers around the scene a motion template is applied to.

This is the only module in ``camera/`` that imports ``bpy`` at call time; the
validator and search modules talk to the small protocols defined here, which
makes them unit-testable with plain AABB fixtures.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol, Sequence

from .motion_templates import Vec3, vec_add, vec_normalized, vec_scale

# --------------------------------------------------------------------------
# mesh / character snapshots
# --------------------------------------------------------------------------
@dataclass
class MeshSnapshot:
    """World-space geometry of one mesh object, used by the ray caster."""

    name: str
    bbox_min: Vec3
    bbox_max: Vec3
    points: "list[Vec3]" = field(default_factory=list)
    triangle_count: int = 0

    def bbox_corners(self) -> "list[Vec3]":
        lo, hi = self.bbox_min, self.bbox_max
        return [
            (x, y, z)
            for x in (lo[0], hi[0])
            for y in (lo[1], hi[1])
            for z in (lo[2], hi[2])
        ]

    def center(self) -> Vec3:
        return tuple((a + b) * 0.5 for a, b in zip(self.bbox_min, self.bbox_max))  # type: ignore[return-value]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "bbox_min": [round(float(v), 6) for v in self.bbox_min],
            "bbox_max": [round(float(v), 6) for v in self.bbox_max],
            "triangle_count": self.triangle_count,
            "sample_point_count": len(self.points),
        }


@dataclass
class CharacterBox:
    """World-space AABB of a placed character at one frame.

    ``excluded_names`` covers *every* mesh object the character owns (body,
    hair, clothing...).  They must never be treated as scene geometry: not as an
    occluder of the character's own probe points, and not as the "geometry" the
    character is overlapping.

    ``animated_boxes`` carries per-frame boxes for an animation whose root moves
    (or whose limb extents change a lot); the validator unions them when it
    needs a whole-sequence conservative bound.
    """

    name: str
    bbox_min: Vec3
    bbox_max: Vec3
    object_name: str = ""
    animation: str = ""
    animated_boxes: "dict[int, tuple[Vec3, Vec3]]" = field(default_factory=dict)
    excluded_names: "list[str]" = field(default_factory=list)

    def self_object_names(self) -> "set[str]":
        """Object names that belong to the character itself."""
        names = set(self.excluded_names)
        if self.object_name:
            names.add(self.object_name)
        names.discard("")
        return names

    def corners(self) -> "list[Vec3]":
        lo, hi = self.bbox_min, self.bbox_max
        return [
            (x, y, z)
            for x in (lo[0], hi[0])
            for y in (lo[1], hi[1])
            for z in (lo[2], hi[2])
        ]

    def center(self) -> Vec3:
        return tuple((a + b) * 0.5 for a, b in zip(self.bbox_min, self.bbox_max))  # type: ignore[return-value]

    def size(self) -> Vec3:
        return tuple(b - a for a, b in zip(self.bbox_min, self.bbox_max))  # type: ignore[return-value]

    def box_at(self, frame: int) -> "tuple[Vec3, Vec3]":
        return self.animated_boxes.get(int(frame), (self.bbox_min, self.bbox_max))

    def probe_points(self, count: int = 27) -> "list[Vec3]":
        """Deterministic lattice inside the box (cheap, no per-frame evaluation)."""
        lo, hi = self.bbox_min, self.bbox_max
        grid = max(1, round(count ** (1.0 / 3.0)))
        points = []
        for i in range(grid):
            for j in range(grid):
                for k in range(grid):
                    tx = 0.5 if grid == 1 else i / float(grid - 1)
                    ty = 0.5 if grid == 1 else j / float(grid - 1)
                    tz = 0.5 if grid == 1 else k / float(grid - 1)
                    points.append((
                        lo[0] + (hi[0] - lo[0]) * tx,
                        lo[1] + (hi[1] - lo[1]) * ty,
                        lo[2] + (hi[2] - lo[2]) * tz,
                    ))
        return points

    def union_with(self, other: "CharacterBox") -> "CharacterBox":
        return CharacterBox(
            name=self.name,
            bbox_min=tuple(min(a, b) for a, b in zip(self.bbox_min, other.bbox_min)),  # type: ignore[arg-type]
            bbox_max=tuple(max(a, b) for a, b in zip(self.bbox_max, other.bbox_max)),  # type: ignore[arg-type]
            object_name=self.object_name or other.object_name,
            animation=self.animation or other.animation,
            animated_boxes={**self.animated_boxes, **other.animated_boxes},
            excluded_names=sorted(set(self.excluded_names) | set(other.excluded_names)),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "object_name": self.object_name,
            "animation": self.animation,
            "bbox_min": [round(float(v), 6) for v in self.bbox_min],
            "bbox_max": [round(float(v), 6) for v in self.bbox_max],
            "animated_frame_count": len(self.animated_boxes),
            "excluded_objects": sorted(self.self_object_names()),
        }


@dataclass
class CameraSnapshot:
    """The original (pre-animation) state of a scene camera."""

    name: str
    object_name: str
    matrix_world: "list[list[float]]"
    location: Vec3
    rotation_mode: str
    rotation_euler: Vec3
    rotation_quaternion: tuple
    scale: Vec3
    lens: float
    sensor_width: float
    sensor_height: float
    sensor_fit: str
    clip_start: float
    clip_end: float
    dof_enabled: bool = False
    focus_distance: float = 0.0
    aperture_fstop: float = 0.0
    shift_x: float = 0.0
    shift_y: float = 0.0
    resolution_x: int = 1920
    resolution_y: int = 1080
    resolution_percentage: int = 100
    fps: float = 24.0
    had_animation: bool = False
    animated_properties: "list[str]" = field(default_factory=list)
    data_users: int = 1
    #: The artist's actions, held so a restore can put the animation back.
    #: ``clear_animation`` detaches without deleting, so keeping the reference is
    #: enough -- and it stops Blender from collecting the datablock in between.
    #: Not serialised: ``to_dict`` lists its keys explicitly.
    action: "object | None" = None
    data_action: "object | None" = None

    @property
    def effective_resolution(self) -> "tuple[int, int]":
        factor = max(1, int(self.resolution_percentage)) / 100.0
        return (int(round(self.resolution_x * factor)), int(round(self.resolution_y * factor)))

    @property
    def aspect(self) -> float:
        width, height = self.effective_resolution
        return width / float(height) if height else 1.0

    def to_dict(self) -> dict:
        return {
            "camera_name": self.name,
            "camera_object": self.object_name,
            "location": [round(float(v), 6) for v in self.location],
            "rotation_mode": self.rotation_mode,
            "rotation_euler_deg": [round(math.degrees(float(v)), 6) for v in self.rotation_euler],
            "rotation_quaternion": [round(float(v), 8) for v in self.rotation_quaternion],
            "scale": [round(float(v), 6) for v in self.scale],
            "lens_mm": round(float(self.lens), 6),
            "sensor_width_mm": round(float(self.sensor_width), 6),
            "sensor_height_mm": round(float(self.sensor_height), 6),
            "sensor_fit": self.sensor_fit,
            "clip_start": round(float(self.clip_start), 6),
            "clip_end": round(float(self.clip_end), 6),
            "dof_enabled": bool(self.dof_enabled),
            "focus_distance": round(float(self.focus_distance), 6),
            "aperture_fstop": round(float(self.aperture_fstop), 6),
            "shift_x": round(float(self.shift_x), 6),
            "shift_y": round(float(self.shift_y), 6),
            "resolution": list(self.effective_resolution),
            "resolution_percentage": int(self.resolution_percentage),
            "fps": round(float(self.fps), 6),
            "had_animation": bool(self.had_animation),
            "animated_properties": list(self.animated_properties),
            "data_users": int(self.data_users),
        }


# --------------------------------------------------------------------------
# ray casting
# --------------------------------------------------------------------------
class RayCaster(Protocol):
    """Minimal ray query interface used by the validator and the search."""

    def cast(
        self,
        origin: Sequence[float],
        direction: Sequence[float],
        exclude: "Iterable[str] | None" = None,
    ) -> "tuple[bool, float]":
        """Return ``(hit, distance)`` for the closest hit along the ray.

        ``exclude`` names objects to ignore for this query only.  The validator
        uses it to stop a character's own mesh from occluding its own probe
        points, which would otherwise make every placed character report as
        mostly hidden.
        """

    @property
    def description(self) -> str:
        ...


def fibonacci_directions(count: int) -> "list[Vec3]":
    """Near-uniform direction set on the unit sphere (deterministic)."""
    if count <= 0:
        return []
    if count == 1:
        return [(0.0, 0.0, 1.0)]
    golden = math.pi * (3.0 - math.sqrt(5.0))
    directions = []
    for index in range(count):
        z = 1.0 - (2.0 * index + 1.0) / count
        radius = math.sqrt(max(0.0, 1.0 - z * z))
        theta = golden * index
        directions.append((math.cos(theta) * radius, math.sin(theta) * radius, z))
    return directions


def cast_closest(ray_caster: RayCaster, origin: Sequence[float], directions: Iterable[Sequence[float]]):
    """Return ``(hit_count, min_distance)`` across ``directions``."""
    hits = 0
    closest = math.inf
    for direction in directions:
        hit, distance = ray_caster.cast(origin, direction)
        if hit:
            hits += 1
            if distance < closest:
                closest = distance
    return hits, closest


class BlenderRayCaster:
    """``scene.ray_cast`` based caster.

    ``exclude`` lets the caller ignore the character's own meshes (a camera is
    not "clipped" by the person it is filming) and ``depsgraph`` guarantees the
    evaluated geometry, so armature deformation and modifiers are respected.

    Per-query exclusions are supported because the same caster is shared between
    "is the camera clipping geometry" (which must ignore the character) and "is
    the character inside geometry" (which must not).
    """

    def __init__(self, scene, *, depsgraph=None, exclude: Iterable[str] = (), distance: float = 1.0e6):
        self.scene = scene
        self.depsgraph = depsgraph or scene.view_layers[0].depsgraph
        self.exclude = {str(name) for name in exclude}
        self.distance = float(distance)
        self._cache: "dict[tuple, tuple[bool, float]]" = {}
        self._max_depth = 12

    @property
    def description(self) -> str:
        suffix = f", excluding {len(self.exclude)} object(s)" if self.exclude else ""
        return f"bpy.types.Scene.ray_cast via depsgraph{suffix}"

    def cast(self, origin: Sequence[float], direction: Sequence[float], exclude=None):
        unit = vec_normalized(direction)
        if unit == (0.0, 0.0, 0.0):
            return (False, math.inf)
        skip = self.exclude | ({str(name) for name in exclude} if exclude else set())
        cache_key = None
        if not skip:
            cache_key = (
                round(float(origin[0]), 6), round(float(origin[1]), 6), round(float(origin[2]), 6),
                round(unit[0], 6), round(unit[1], 6), round(unit[2], 6),
            )
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached
        result = self._cast_uncached(origin, unit, skip, 0)
        if cache_key is not None:
            self._cache[cache_key] = result
        return result

    def _cast_uncached(self, origin, unit, skip, depth):
        if depth > self._max_depth:
            return (False, math.inf)
        try:
            hit, location, _normal, index, obj, _matrix = self.scene.ray_cast(
                self.depsgraph, origin, unit, distance=self.distance
            )
        except Exception:
            return (False, math.inf)
        if not hit or obj is None:
            return (False, math.inf)
        if obj.name in skip or obj.original.name in skip:
            # Continue past the excluded object so the character does not mask a
            # wall standing directly behind it, and report the distance to the
            # first *non-excluded* hit.
            offset = vec_add(location, vec_scale(unit, 1.0e-3))
            deeper_hit, deeper_distance = self._cast_uncached(offset, unit, skip, depth + 1)
            if deeper_hit:
                travelled = 1.0e-3 + deeper_distance
                return (True, math.dist(origin, vec_add(origin, vec_scale(unit, travelled))))
            return (False, math.inf)
        return (True, math.dist(origin, location))


class BBoxRayCaster:
    """Slab test against AABBs.

    Used by the unit tests and as a fallback when a scene has no usable
    depsgraph geometry.  It is intentionally conservative: an AABB is at least
    as large as the mesh it wraps, so a clearance failure here is a real risk.
    """

    def __init__(self, boxes: Iterable[MeshSnapshot], *, max_distance: float = 1.0e6):
        self.boxes = list(boxes)
        self.max_distance = float(max_distance)
        self._names = {box.name for box in self.boxes}

    @property
    def description(self) -> str:
        return f"AABB slab test over {len(self.boxes)} box(es)"

    def cast(self, origin: Sequence[float], direction: Sequence[float], exclude=None):
        unit = vec_normalized(direction)
        if unit == (0.0, 0.0, 0.0):
            return (False, math.inf)
        skip = {str(name) for name in exclude} if exclude else set()
        best = math.inf
        for box in self.boxes:
            if box.name in skip:
                continue
            distance = _ray_aabb(origin, unit, box.bbox_min, box.bbox_max)
            if distance is not None and distance < best:
                best = distance
        if best < math.inf and best <= self.max_distance:
            return (True, best)
        return (False, math.inf)


def _ray_aabb(origin, direction, lo, hi) -> "float | None":
    """Slab intersection; returns entry distance (0.0 when starting inside)."""
    t_near = 0.0
    t_far = math.inf
    for axis in range(3):
        d = direction[axis]
        o = origin[axis]
        if abs(d) < 1e-12:
            if o < lo[axis] or o > hi[axis]:
                return None
            continue
        t1 = (lo[axis] - o) / d
        t2 = (hi[axis] - o) / d
        if t1 > t2:
            t1, t2 = t2, t1
        t_near = max(t_near, t1)
        t_far = min(t_far, t2)
        if t_near > t_far:
            return None
    return t_near


# --------------------------------------------------------------------------
# project / evaluate helpers (need bpy)
# --------------------------------------------------------------------------
def world_to_ndc(scene, depsgraph, camera_object, point) -> Vec3:
    """Project a world point to camera NDC; ``z`` is distance along view axis."""
    from bpy_extras.object_utils import world_to_camera_view  # type: ignore

    co = world_to_camera_view(scene, depsgraph, camera_object, point)
    return (float(co.x), float(co.y), float(co.z))


def camera_view_frame(camera_data, scene) -> "list[Vec3]":
    """Camera-space corners of the view frame at distance 1 (Blender convention)."""
    try:
        return [tuple(float(c) for c in corner) for corner in camera_data.view_frame(scene=scene)]
    except Exception:
        return []


def mesh_snapshot(obj, *, depsgraph=None, max_points: int = 0, matrix=None) -> "MeshSnapshot | None":
    """Build a world-space snapshot of ``obj`` (evaluated, modifiers applied)."""
    if obj is None or obj.type != "MESH":
        return None
    evaluated = None
    if depsgraph is not None:
        try:
            evaluated = obj.evaluated_get(depsgraph)
        except Exception:
            evaluated = None
    source = evaluated if evaluated is not None and getattr(evaluated, "type", None) == "MESH" else obj
    mesh = getattr(source, "data", None)
    if mesh is None or len(mesh.vertices) == 0:
        return None
    transform = matrix if matrix is not None else source.matrix_world
    lo = [math.inf, math.inf, math.inf]
    hi = [-math.inf, -math.inf, -math.inf]
    points: "list[Vec3]" = []
    stride = 1
    if max_points and len(mesh.vertices) > max_points:
        stride = max(1, len(mesh.vertices) // max_points)
    for index, vertex in enumerate(mesh.vertices):
        world = transform @ vertex.co
        co = (float(world[0]), float(world[1]), float(world[2]))
        for axis in range(3):
            if co[axis] < lo[axis]:
                lo[axis] = co[axis]
            if co[axis] > hi[axis]:
                hi[axis] = co[axis]
        if max_points and index % stride == 0:
            points.append(co)
    return MeshSnapshot(
        name=obj.name,
        bbox_min=tuple(lo),  # type: ignore[arg-type]
        bbox_max=tuple(hi),  # type: ignore[arg-type]
        points=points,
        triangle_count=len(getattr(mesh, "polygons", []) or []),
    )


def union_boxes(boxes: Sequence[MeshSnapshot]) -> "MeshSnapshot | None":
    if not boxes:
        return None
    lo = [min(b.bbox_min[i] for b in boxes) for i in range(3)]
    hi = [max(b.bbox_max[i] for b in boxes) for i in range(3)]
    return MeshSnapshot(name="union", bbox_min=tuple(lo), bbox_max=tuple(hi))  # type: ignore[arg-type]


@dataclass
class SceneContext:
    """Everything the validator/search needs to know about the loaded scene."""

    scene: object
    depsgraph: object
    blend_path: str = ""
    scene_name: str = ""
    cameras: "list[CameraSnapshot]" = field(default_factory=list)
    meshes: "list[MeshSnapshot]" = field(default_factory=list)
    characters: "list[CharacterBox]" = field(default_factory=list)
    ray_caster: "RayCaster | None" = None
    world_bbox_min: Vec3 | None = None
    world_bbox_max: Vec3 | None = None
    warnings: "list[str]" = field(default_factory=list)
    project_fn: "Callable | None" = None

    @property
    def has_characters(self) -> bool:
        return bool(self.characters)

    @property
    def mesh_count(self) -> int:
        return len(self.meshes)

    def camera_by_name(self, name: str) -> CameraSnapshot:
        for camera in self.cameras:
            if camera.name == name or camera.object_name == name:
                return camera
        raise KeyError(f"camera {name!r} is not part of this scene context")

    def project(self, camera_object, point) -> "Vec3 | None":
        """Project to NDC, or return ``None`` when projection is unavailable.

        Returning ``None`` (instead of raising) lets the validator fall back to
        its analytic frustum test, which is what the pure-Python unit tests and
        any non-Blender host rely on.
        """
        if self.project_fn is not None:
            return self.project_fn(camera_object, point)
        try:
            return world_to_ndc(self.scene, self.depsgraph, camera_object, point)
        except Exception:
            return None

    def summary(self) -> dict:
        return {
            "blend_path": self.blend_path,
            "scene_name": self.scene_name,
            "camera_count": len(self.cameras),
            "cameras": [c.name for c in self.cameras],
            "mesh_count": len(self.meshes),
            "character_count": len(self.characters),
            "characters": [c.name for c in self.characters],
            "world_bbox": (
                {
                    "min": [round(float(v), 6) for v in self.world_bbox_min],
                    "max": [round(float(v), 6) for v in self.world_bbox_max],
                }
                if self.world_bbox_min and self.world_bbox_max
                else None
            ),
            "ray_caster": getattr(self.ray_caster, "description", "none"),
            "warnings": list(self.warnings),
        }
