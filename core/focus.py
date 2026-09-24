"""Focus objects: the subject an ``Arc`` shot orbits.

A *focus object* is a model (a ``.blend`` file, or one object inside it) that the
generator places at the scene's single **anchor point** so that:

* an ``Arc`` motion orbits **it** instead of swinging the camera around nothing --
  the camera keeps the object centred, which is what makes the shot readable;
* every other motion is unchanged; the object is simply there, in frame, doing
  nothing;
* the output matrix gains one axis: ``scene x camera x motion x focus object``.

Design notes that matter for the rest of the package:

* **The object lives in the staged scene copy, not in the sequence.**  A sequence
  ships the camera animation and the renderer replays it onto the scene copy beside
  it, so an object that is not inside that copy cannot be rendered.  The copy is
  therefore *staged with every focus model already placed and hidden*, and both the
  generator and the renderer switch one of them on per sequence with
  :func:`apply_visibility`.  One placement, baked into the ``.blend``, is used by
  both sides -- the arc radius the generator computes is the distance the renderer
  will actually see.
* **Hidden by default.**  A staged copy has every focus object hidden, so a renderer
  that does not know about this feature degrades to "no subject" rather than to a
  pile of overlapping models.  The scene carries the names in
  ``scene[SCENE_REGISTRY_KEY]`` so nothing else has to be passed along.
* **Numbers in the JSON, objects only in the scene.**  Everything a render node
  needs is in ``sequence_config.json`` / the sequence sidecar as plain numbers
  (:meth:`FocusPlacement.to_dict`), the same way the camera region is recorded.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field, replace

from ..io.path_utils import normalize_path, slugify, to_forward_slashes

#: ``focus.mode`` -- "off" keeps the whole feature out of the code paths.
MODE_OFF = "off"
MODE_MODELS = "models"
FOCUS_MODES = (MODE_OFF, MODE_MODELS)

#: ``focus.anchor_mode`` -- how the anchor point is found in a scene.
ANCHOR_AUTO = "auto"
ANCHOR_OBJECT = "object"
ANCHOR_NUMBERS = "numbers"
ANCHOR_MODES = (ANCHOR_AUTO, ANCHOR_OBJECT, ANCHOR_NUMBERS)

#: The empty the panel's *Auto place anchor* button creates when the user has not
#: made one; ``anchor_mode="object"`` looks for exactly this name by default.
DEFAULT_ANCHOR_OBJECT = "MPP_FocusAnchor"
#: Scene custom-property keys.  They travel inside the ``.blend``, so the render
#: node needs no extra file to know which objects are focus objects.
SCENE_ANCHOR_KEY = "mpp_focus_anchor"
SCENE_REGISTRY_KEY = "mpp_focus_objects"
#: Object custom property marking an imported focus object.
OBJECT_MARK_KEY = "mpp_focus_id"

#: A model with no explicit pivot is placed by its bounding-box centre, and this is
#: the smallest radius an arc orbit is allowed to have (a camera sitting on top of
#: its subject cannot orbit it).
MIN_ORBIT_RADIUS = 0.25
#: Largest angle between two keys of a re-centred orbit.  Sampling is linear between
#: keys, so the keys have to be dense enough for the straight line between them to
#: stay on the circle; 3 deg keeps the chord error at 3 mm on an 8 m orbit.
MAX_ORBIT_KEY_STEP_DEG = 3.0
#: A frame counts as "the subject is visible" when this much of the object's box is
#: inside the frustum; the report keeps the raw counts as well.
VISIBLE_RATIO_THRESHOLD = 0.95


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FocusModel:
    """One entry of the panel's model list, resolved against the config."""

    id: str
    path: str
    label: str = ""
    object_name: str = ""
    scale: float = 1.0
    rotation: tuple = (0.0, 0.0, 0.0)
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "path": to_forward_slashes(self.path),
            "label": self.label,
            "object_name": self.object_name,
            "scale": round(float(self.scale), 6),
            "rotation": [round(float(v), 6) for v in self.rotation],
            "enabled": bool(self.enabled),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "FocusModel":
        rotation = raw.get("rotation") or (0.0, 0.0, 0.0)
        return cls(
            id=str(raw.get("id") or ""),
            path=str(raw.get("path") or ""),
            label=str(raw.get("label") or ""),
            object_name=str(raw.get("object_name") or raw.get("object") or ""),
            scale=float(raw.get("scale") or 1.0),
            rotation=tuple(float(v) for v in list(rotation)[:3]) or (0.0, 0.0, 0.0),
            enabled=bool(raw.get("enabled", True)),
        )


@dataclass(frozen=True)
class FocusPlacement:
    """Where one focus model ended up in the staged scene copy.

    ``objects`` are the object names the model contributed; ``bbox_min``/``bbox_max``
    are their world bounds, which is all the arc retarget and the visibility check
    need.  ``anchor`` is the point the model was placed on.
    """

    id: str
    model_path: str = ""
    label: str = ""
    objects: tuple = ()
    bbox_min: tuple = (0.0, 0.0, 0.0)
    bbox_max: tuple = (0.0, 0.0, 0.0)
    anchor: tuple = (0.0, 0.0, 0.0)
    anchor_mode: str = ANCHOR_AUTO
    source: str = ""
    note: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.objects)

    @property
    def center(self) -> tuple:
        return tuple((self.bbox_min[i] + self.bbox_max[i]) * 0.5 for i in range(3))

    @property
    def size(self) -> tuple:
        return tuple(abs(self.bbox_max[i] - self.bbox_min[i]) for i in range(3))

    @property
    def radius(self) -> float:
        return 0.5 * math.sqrt(sum(v * v for v in self.size))

    @property
    def height(self) -> float:
        return float(self.size[2])

    def corners(self) -> "list[tuple]":
        lo, hi = self.bbox_min, self.bbox_max
        return [(lo[0] if i & 1 else hi[0], lo[1] if i & 2 else hi[1], lo[2] if i & 4 else hi[2])
                for i in range(8)]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "model_path": to_forward_slashes(self.model_path),
            "label": self.label,
            "objects": list(self.objects),
            "bbox_min": [round(float(v), 6) for v in self.bbox_min],
            "bbox_max": [round(float(v), 6) for v in self.bbox_max],
            "anchor": [round(float(v), 6) for v in self.anchor],
            "anchor_mode": self.anchor_mode,
            "source": self.source,
            "note": self.note,
            "center": [round(float(v), 6) for v in self.center],
            "size": [round(float(v), 6) for v in self.size],
            "radius_m": round(float(self.radius), 6),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "FocusPlacement | None":
        if not isinstance(raw, dict):
            return None
        objects = raw.get("objects") or []
        if not objects:
            return None

        def vec(name, default=(0.0, 0.0, 0.0)):
            value = raw.get(name) or default
            try:
                return tuple(float(v) for v in list(value)[:3])
            except (TypeError, ValueError):
                return default

        return cls(
            id=str(raw.get("id") or ""),
            model_path=str(raw.get("model_path") or ""),
            label=str(raw.get("label") or ""),
            objects=tuple(str(name) for name in objects),
            bbox_min=vec("bbox_min"),
            bbox_max=vec("bbox_max"),
            anchor=vec("anchor"),
            anchor_mode=str(raw.get("anchor_mode") or ANCHOR_AUTO),
            source=str(raw.get("source") or ""),
            note=str(raw.get("note") or ""),
        )


# --------------------------------------------------------------------------
# config -> records
# --------------------------------------------------------------------------
def enabled(section) -> bool:
    """True when the section asks for focus objects at all."""
    if section is None:
        return False
    return str(getattr(section, "mode", MODE_OFF) or MODE_OFF).strip().lower() == MODE_MODELS


def model_id(path: str, *, index: int = 0, label: str = "") -> str:
    """A stable, filesystem- and JSON-safe id for one model.

    The id never reaches the output *path* (sequences stay in their motion folder);
    it only identifies the model in metadata, filters and the placement registry.
    """
    stem = os.path.splitext(os.path.basename(str(path or "")))[0]
    return slugify(label or stem or f"model{index + 1:02d}", fallback=f"model{index + 1:02d}")


def models_from_section(section, *, mappings=(), logger=None) -> "list[FocusModel]":
    """The section's enabled models, in list order, with unique ids and real paths.

    ``mappings`` are already-parsed ``(from, to)`` pairs (see
    :func:`~..io.path_utils.parse_path_mappings`), so a model list written on the
    authoring machine still resolves after the project moves.
    """
    if section is None:
        return []
    from ..io.path_utils import apply_path_mappings

    models: "list[FocusModel]" = []
    seen: "dict[str, int]" = {}
    for index, raw in enumerate(getattr(section, "models", None) or []):
        if not isinstance(raw, dict):
            continue
        entry = FocusModel.from_dict(raw)
        if not entry.enabled or not entry.path:
            continue
        path = apply_path_mappings(entry.path, mappings) if mappings else entry.path
        path = normalize_path(path, make_absolute=True)
        if not os.path.isfile(path):
            if logger is not None:
                logger.warning("focus model is missing and will be skipped: %s", path)
            continue
        base = entry.id or model_id(path, index=index, label=entry.label)
        count = seen.get(base, 0) + 1
        seen[base] = count
        identifier = base if count == 1 else f"{base}_{count:02d}"
        models.append(replace(entry, id=identifier, path=path,
                              label=entry.label or os.path.basename(path)))
    return models


def anchor_from_section(section) -> dict:
    """The configured anchor request, without touching a scene."""
    if section is None:
        return {"mode": ANCHOR_AUTO, "object": DEFAULT_ANCHOR_OBJECT,
                "location": [0.0, 0.0, 0.0], "clearance": 0.5}
    location = list(getattr(section, "anchor_location", None) or (0.0, 0.0, 0.0))[:3]
    while len(location) < 3:
        location.append(0.0)
    return {
        "mode": str(getattr(section, "anchor_mode", ANCHOR_AUTO) or ANCHOR_AUTO).lower(),
        "object": str(getattr(section, "anchor_object", "") or "") or DEFAULT_ANCHOR_OBJECT,
        "location": [float(v) for v in location],
        "clearance": float(getattr(section, "anchor_clearance", 0.5) or 0.0),
    }


# --------------------------------------------------------------------------
# arcs
# --------------------------------------------------------------------------
def arc_sweep(template) -> "tuple[float, str] | None":
    """``(sweep_degrees, direction)`` when ``template`` is an arc, else ``None``.

    An arc is recognised the way the template document spells it -- ``type`` in the
    template's parameters (the atomic vocabulary's field) or an id/family starting
    with ``arc`` -- and its sweep is the yaw the keys accumulate from the first to
    the last of them.  A template that turns but is not an arc (a pan, a hitchcock)
    returns ``None``: only things that *are* an orbit get re-centred.
    """
    parameters = getattr(template, "parameters", None) or {}
    kind = str(parameters.get("type") or "").strip().lower()
    name = str(getattr(template, "name", "") or "").strip().lower()
    if kind != "arc" and not name.startswith("arc"):
        return None
    keys = list(getattr(template, "keyframes", None) or [])
    if len(keys) < 2:
        return None
    first = float(keys[0].rotation[1])
    last = float(keys[-1].rotation[1])
    sweep = last - first
    if abs(sweep) < 1e-6:
        return None
    direction = "clockwise" if sweep > 0 else "counterclockwise"
    configured = str(parameters.get("direction") or "").strip().lower()
    if configured in ("clockwise", "counterclockwise"):
        direction = configured
    return (sweep, direction)


def orbit_radius_candidates(natural: float, *, minimum: float = MIN_ORBIT_RADIUS,
                            steps=(1.0, 0.75, 1.33, 0.5, 1.75, 0.35, 2.0, 0.25, 0.2)) -> "list[float]":
    """Radii to try for an orbit, closest to the camera's own distance first.

    The natural radius is the camera's distance to the subject -- the shot the author
    framed.  It is tried first and kept whenever the scene allows it; the rest are the
    same circle played bigger or smaller, which is how a room that is too tight for the
    authored distance (a 7 m circle inside a 5 m room) still gets its arc.  Order is
    ``natural`` first, then the smallest change, and values below ``minimum`` are
    dropped: closer than that the camera would be inside the subject.
    """
    base = float(natural)
    if base <= 0.0:
        return []
    seen = []
    for factor in steps:
        value = round(base * float(factor), 6)
        if value < float(minimum):
            continue
        if all(abs(value - other) > 1e-6 for other in seen):
            seen.append(value)
    return seen


def orbit_template(template, *, anchor, base_position, base_quaternion, fps,
                   rotation_order="XYZ", up=(0.0, 0.0, 1.0),
                   min_radius=MIN_ORBIT_RADIUS, radius=None):
    """Re-author an arc so it orbits ``anchor`` at the camera's real distance.

    The sweep and the timing come from the template unchanged -- the shot stays the
    shot it was -- but the circle is re-centred on the focus object and the camera is
    aimed at it, which is what keeps the subject in frame for every frame of the arc.

    ``radius`` overrides the circle's size (the camera keeps its bearing and height and
    moves toward or away from the subject).  The caller uses that to adapt the orbit to
    the room: a radius the scene cannot hold is replayed smaller rather than dropped.

    Returns ``(template, info)``.  ``info["ok"]`` is False (and the template comes back
    untouched) when there is nothing to orbit: the camera stands closer to the anchor
    than ``min_radius``, or it already sits on it.  When it *is* ok, ``info`` carries
    ``rotation_adjust`` -- the world-space turn that aims the camera at the subject --
    which the caller folds into the base pose before baking, so the template's own
    "forward" is the direction the camera now looks.
    """
    from ..camera.camera_search import look_at_quaternion
    from ..camera.motion_templates import (
        TemplateKeyframe, quat_angle_between, quat_conjugate, quat_multiply,
        quat_to_euler_xyz,
    )

    sweep_info = arc_sweep(template)
    info = {"ok": False, "reason": "", "radius_m": 0.0, "sweep_deg": 0.0,
            "direction": "", "base_aim_deg": 0.0, "rotation_order": rotation_order,
            "radius_source": "authored", "rotation_adjust": (1.0, 0.0, 0.0, 0.0)}
    if sweep_info is None:
        info["reason"] = "template is not an arc"
        return template, info
    sweep, direction = sweep_info
    if str(rotation_order).upper() != "XYZ":
        info["reason"] = (f"rotation_order {rotation_order!r} is not XYZ; the orbit is "
                          "kept local-yaw only")
    anchor = tuple(float(v) for v in anchor)
    base_position = tuple(float(v) for v in base_position)

    # The horizontal offset from the subject to the camera is the orbit radius; the
    # camera keeps its height, so the orbit is level whatever the subject's height is.
    horizontal = (base_position[0] - anchor[0], base_position[1] - anchor[1], 0.0)
    natural = math.sqrt(horizontal[0] ** 2 + horizontal[1] ** 2)
    if radius is None:
        radius = natural
    else:
        radius = float(radius)
        if natural > 1e-9:
            factor = radius / natural
            horizontal = (horizontal[0] * factor, horizontal[1] * factor, 0.0)
            info["radius_source"] = "adapted"
    # Where the camera starts: the same bearing, the chosen distance.
    start = (anchor[0] + horizontal[0], anchor[1] + horizontal[1], base_position[2])
    info["radius_m"] = round(float(radius), 6)
    info["radius_natural_m"] = round(natural, 6)
    info["sweep_deg"] = round(float(sweep), 6)
    info["direction"] = direction
    if radius < float(min_radius):
        info["reason"] = (f"the camera is {radius:.3f} m from the focus point horizontally; "
                          f"an orbit needs at least {float(min_radius):.2f} m")
        info["ok"] = False
        return template, info

    aim = look_at_quaternion(_sub(anchor, start))
    info["base_aim_deg"] = round(float(quat_angle_between(base_quaternion, aim)), 4)
    info["rotation_adjust"] = tuple(
        float(v) for v in quat_multiply(aim, quat_conjugate(base_quaternion))
    )

    keys = list(template.keyframes)
    first_frame = int(keys[0].frame)
    last_frame = int(keys[-1].frame)
    span = max(1, last_frame - first_frame)
    right, up_axis, forward = _aimed_axes(start, anchor, up)
    # The angle at any frame is the template's *own* yaw at that frame, so the sweep,
    # its timing and any ease in the document are followed rather than re-invented.
    # Extra keys are added between the template's own so the per-frame linear sampler
    # traces the circle instead of a chord: at 3 deg a step, the chord error on an 8 m
    # orbit is 3 mm (measured 68 mm when only the template's 15 deg keys were used).
    step_frames = max(1, int(round(span * MAX_ORBIT_KEY_STEP_DEG / max(1e-6, abs(sweep)))))
    frames = sorted(set(range(first_frame, last_frame + 1, step_frames))
                    | {int(key.frame) for key in keys} | {last_frame})
    new_keys = []
    for frame in frames:
        key = template.interpolate(frame)
        angle = math.radians(float(key.rotation[1]))
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        # Rotate the horizontal offset about the world up axis through the anchor.
        offset_horizontal = (horizontal[0] * cos_a - horizontal[1] * sin_a,
                             horizontal[0] * sin_a + horizontal[1] * cos_a)
        point = (anchor[0] + offset_horizontal[0], anchor[1] + offset_horizontal[1],
                 start[2])
        # The keys are offsets from the camera's *base* position: that is the pose the
        # animation is anchored on, so an adapted radius has to be expressed from there.
        offset_world = _sub(point, base_position)
        local = (_dot(offset_world, right), _dot(offset_world, up_axis),
                 -_dot(offset_world, forward))
        desired = look_at_quaternion(_sub(anchor, point))
        delta = quat_multiply(quat_conjugate(aim), desired)
        rotation = [math.degrees(float(v)) for v in quat_to_euler_xyz(delta)]
        new_keys.append(TemplateKeyframe(
            frame=int(frame),
            location=tuple(round(v, 6) or 0.0 for v in local),
            rotation=tuple(round(v, 6) or 0.0 for v in rotation),
            focal=key.focal,
        ))
    parameters = dict(getattr(template, "parameters", None) or {})
    parameters["focus_orbit"] = {
        "anchor": [round(float(v), 6) for v in anchor],
        "radius_m": round(float(radius), 6),
        "radius_natural_m": round(natural, 6),
        "sweep_deg": round(float(sweep), 6),
        "direction": direction,
        "base_aim_deg": round(float(info["base_aim_deg"]), 4),
        "keys": len(new_keys),
    }
    info["keys"] = len(new_keys)
    info["ok"] = True
    return (replace(template, keyframes=new_keys, parameters=parameters,
                    description=(template.description or "") + " [orbiting the focus object]"),
            info)


def _dot(a, b) -> float:
    return float(a[0] * b[0] + a[1] * b[1] + a[2] * b[2])


def _sub(a, b):
    return (float(a[0]) - float(b[0]), float(a[1]) - float(b[1]), float(a[2]) - float(b[2]))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _normalize(v, fallback=(0.0, 0.0, -1.0)):
    length = math.sqrt(sum(float(c) * float(c) for c in v))
    if length <= 1e-12:
        return tuple(fallback)
    return tuple(float(c) / length for c in v)


def _aimed_axes(base_position, anchor, up=(0.0, 0.0, 1.0)):
    """``(right, up, forward)`` of a camera at ``base_position`` aimed at ``anchor``."""
    forward = _normalize(_sub(anchor, base_position))
    hint = tuple(float(v) for v in up)
    if abs(_dot(forward, hint)) > 0.999:
        hint = (0.0, 1.0, 0.0)
    right = _normalize(_cross(forward, hint))
    up_axis = _normalize(_cross(right, forward))
    return right, up_axis, forward


# --------------------------------------------------------------------------
# visibility
# --------------------------------------------------------------------------
def visibility_report(animation, placement, camera, *, threshold=VISIBLE_RATIO_THRESHOLD,
                      step=1) -> dict:
    """How much of ``placement`` stayed inside the camera frustum over ``animation``.

    ``camera`` is a :class:`~..camera.scene_context.CameraSnapshot` (its lens and
    sensor are what the frame is).  A frame counts as *visible* when the object's
    centre projects inside the frustum and at least one of its corners does too, and
    as *fully visible* when all eight corners do; the report keeps both counts so a
    half-out-of-frame subject is visible in the numbers rather than rounded away.
    """
    from ..camera.camera_validator import frustum_contains
    from ..camera.motion_templates import quat_rotate

    corners = placement.corners()
    center = placement.center
    step = max(1, int(step))
    samples = [sample for index, sample in enumerate(animation.samples) if index % step == 0]
    visible = full = 0
    min_distance = float("inf")
    closest_frame = -1
    inside_frames = 0
    for sample in samples:
        position = tuple(float(v) for v in sample.position)
        forward = quat_rotate(sample.quaternion, (0.0, 0.0, -1.0))
        hits = sum(1 for corner in corners
                   if frustum_contains(camera, position, forward, corner))
        centre_in = frustum_contains(camera, position, forward, center)
        if centre_in and hits:
            visible += 1
        if hits == len(corners):
            full += 1
        distance = math.sqrt(sum((position[i] - center[i]) ** 2 for i in range(3)))
        if distance < min_distance:
            min_distance, closest_frame = distance, int(sample.frame)
        if distance < placement.radius:
            inside_frames += 1
    total = len(samples) or 1
    ratio = visible / float(total)
    return {
        "ok": bool(ratio >= float(threshold) and not inside_frames),
        "frames": len(samples),
        "visible_frames": visible,
        "fully_visible_frames": full,
        "visible_ratio": round(ratio, 6),
        "full_ratio": round(full / float(total), 6),
        "threshold": float(threshold),
        "min_distance_m": round(min_distance, 6) if min_distance != float("inf") else 0.0,
        "closest_frame": closest_frame,
        "camera_inside_frames": inside_frames,
        "sample_step": step,
    }


# --------------------------------------------------------------------------
# Blender-side helpers
# --------------------------------------------------------------------------
def _non_geometry_types() -> tuple:
    return ("CAMERA", "LIGHT", "EMPTY", "SPEAKER", "ARMATURE")


def mesh_objects(scene, *, visible_only=True, exclude=()):
    """Mesh objects of a scene, optionally only those that would render."""
    ignore = {str(name) for name in exclude}
    found = []
    for obj in scene.objects:
        if obj.type != "MESH" or obj.name in ignore:
            continue
        if visible_only and (obj.hide_render or not obj.visible_get()):
            continue
        found.append(obj)
    return found


def scene_bounds(scene, *, exclude=()) -> "tuple[tuple, tuple] | None":
    """World-space AABB of the scene's visible mesh geometry, or ``None``."""
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    found = False
    for obj in mesh_objects(scene, exclude=exclude):
        for corner in obj.bound_box:
            point = obj.matrix_world @ _vec(corner)
            for axis in range(3):
                lo[axis] = min(lo[axis], point[axis])
                hi[axis] = max(hi[axis], point[axis])
            found = True
    if not found:
        return None
    return tuple(lo), tuple(hi)


def _vec(values):
    from mathutils import Vector

    return Vector((float(values[0]), float(values[1]), float(values[2])))


def is_clear(scene, point, *, clearance=0.5, exclude=(), samples=26) -> "tuple[bool, float]":
    """Whether ``point`` has ``clearance`` metres of free space around it."""
    from mathutils import Vector

    depsgraph = getattr(scene, "evaluated_get", None)
    graph = None
    try:
        import bpy

        graph = bpy.context.evaluated_depsgraph_get()
        depsgraph = True
    except Exception:  # pragma: no cover - only without a context
        depsgraph = None
    origin = Vector((float(point[0]), float(point[1]), float(point[2])))
    ignore = {str(name) for name in exclude}
    minimum = float("inf")
    directions = _sphere_directions(samples)
    for direction in directions:
        hit, location, _normal, _index, obj, _matrix = scene.ray_cast(
            graph, origin, direction
        ) if graph is not None else (False, None, None, -1, None, None)
        if not hit:
            minimum = min(minimum, clearance)
            continue
        if obj is not None and getattr(obj, "name", "") in ignore:
            continue
        distance = (Vector(location) - origin).length
        minimum = min(minimum, distance)
    return (minimum >= clearance, minimum if minimum != float("inf") else clearance)


def _sphere_directions(count: int) -> "list":
    """A deterministic, roughly even set of unit directions (fibonacci sphere)."""
    from mathutils import Vector

    count = max(6, int(count))
    golden = math.pi * (3.0 - math.sqrt(5.0))
    directions = []
    for index in range(count):
        z = 1.0 - (2.0 * index + 1.0) / count
        radius = math.sqrt(max(0.0, 1.0 - z * z))
        theta = golden * index
        directions.append(Vector((math.cos(theta) * radius, math.sin(theta) * radius, z)))
    return directions


def auto_anchor(scene, *, clearance=0.5, exclude=(), logger=None) -> dict:
    """Find a sensible place for the anchor: the middle of the open part of a scene.

    The search starts at the centre of the scene's bounding box, drops to the floor
    under it, and then walks outwards over a small spiral until it finds a spot with
    ``clearance`` metres of free space -- so a scene whose centre is inside a wall, a
    table or a train still gets a usable anchor.  Anything that cannot be measured
    falls back to the bounding-box centre instead of failing.
    """
    bounds = scene_bounds(scene, exclude=exclude)
    note = ""
    if bounds is None:
        return {"ok": False, "location": [0.0, 0.0, 0.0], "source": "fallback",
                "note": "the scene has no visible mesh to measure"}
    lo, hi = bounds
    center = [(lo[i] + hi[i]) * 0.5 for i in range(3)]
    size = [hi[i] - lo[i] for i in range(3)]
    floor = float(lo[2])
    radius = max(0.5, 0.5 * math.sqrt(size[0] ** 2 + size[1] ** 2))
    candidates = [(center[0], center[1], floor)]
    rings = 4
    per_ring = 8
    for ring in range(1, rings + 1):
        distance = radius * 0.15 * ring
        for index in range(per_ring):
            angle = 2.0 * math.pi * index / per_ring
            candidates.append((center[0] + distance * math.cos(angle),
                               center[1] + distance * math.sin(angle), floor))
    best = None
    for x, y, z in candidates:
        ok, measured = is_clear(scene, (x, y, z), clearance=clearance, exclude=exclude)
        if ok:
            best = ((x, y, z), measured)
            break
        if best is None or measured > best[1]:
            best = ((x, y, z), measured)
    location, measured = best if best else ((center[0], center[1], floor), 0.0)
    if measured < clearance:
        note = (f"the most open spot found has {measured:.2f} m of clearance, less than the "
                f"{clearance:.2f} m asked for")
    if logger is not None and note:
        logger.warning("focus anchor: %s", note)
    return {"ok": measured >= clearance, "location": [float(v) for v in location],
            "source": "auto", "note": note,
            "clearance_m": round(float(measured), 6)}


def resolve_anchor(section, scene, *, exclude=(), logger=None) -> dict:
    """The world point the focus objects are placed on, honouring the panel's mode."""
    request = anchor_from_section(section)
    mode = request["mode"]
    if mode == ANCHOR_OBJECT:
        name = request["object"]
        obj = scene.objects.get(name) if hasattr(scene.objects, "get") else None
        if obj is not None:
            location = obj.matrix_world.translation
            return {"ok": True, "location": [float(location[i]) for i in range(3)],
                    "source": "object", "object": name, "note": ""}
        note = (f"anchor object {name!r} is not in the scene; falling back to the "
                "automatic position")
        if logger is not None:
            logger.warning("focus anchor: %s", note)
        auto = auto_anchor(scene, clearance=request["clearance"], exclude=exclude,
                           logger=logger)
        auto.update({"object": name, "note": f"{note} ({auto.get('note') or 'ok'})"})
        return auto
    if mode == ANCHOR_NUMBERS:
        return {"ok": True, "location": list(request["location"]), "source": "numbers",
                "note": ""}
    auto = auto_anchor(scene, clearance=request["clearance"], exclude=exclude, logger=logger)
    auto["object"] = request["object"]
    return auto


def _plain(value):
    """A Blender ID-property tree as plain Python (dicts, lists, numbers, strings).

    A scene custom property does not come back as the dict that was stored: Blender
    turns it into ``IDPropertyGroup``/``IDPropertyArray``, which are neither dicts nor
    lists.  Everything that reads a stored record goes through here so the rest of the
    module only ever sees plain values.  (The registry is stored as a JSON string for
    exactly this reason; this keeps older files readable too.)
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _plain(to_dict())
        except Exception:  # noqa: BLE001 - fall through to the generic paths
            pass
    keys = getattr(value, "keys", None)
    if callable(keys):
        try:
            return {key: _plain(value[key]) for key in keys()}
        except Exception:  # noqa: BLE001
            pass
    try:
        return [_plain(item) for item in value]
    except TypeError:
        return value


def _stored(scene, key) -> list:
    """Read a list record out of a scene, accepting JSON text or an ID-property tree."""
    raw = scene.get(key) if hasattr(scene, "get") else None
    if raw is None:
        return []
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    plain = _plain(raw)
    return plain if isinstance(plain, list) else []


def registered_placements(scene) -> "list[FocusPlacement]":
    """The focus placements the staged copy carries (empty when there are none)."""
    placements = []
    for item in _stored(scene, SCENE_REGISTRY_KEY):
        placement = FocusPlacement.from_dict(item)
        if placement is not None:
            placements.append(placement)
    return placements


def registered_names(scene) -> "list[str]":
    names: "list[str]" = []
    for placement in registered_placements(scene):
        names.extend(placement.objects)
    return names


def apply_visibility(scene, active=()) -> dict:
    """Show exactly the objects in ``active``, hide every other focus object.

    Called with the sequence's own objects by the generator and by the renderer, so
    the picture on the render node is the picture that was validated.  Objects that
    the registry mentions but the file no longer has are reported, never raised.
    """
    wanted = {str(name) for name in active}
    report = {"shown": [], "hidden": [], "missing": []}
    for name in registered_names(scene):
        obj = scene.objects.get(name) if hasattr(scene.objects, "get") else None
        if obj is None:
            report["missing"].append(name)
            continue
        visible = name in wanted
        try:
            obj.hide_render = not visible
            obj.hide_viewport = not visible
        except AttributeError:  # pragma: no cover - non-viewport objects
            pass
        report["shown" if visible else "hidden"].append(name)
    for name in sorted(wanted - set(registered_names(scene))):
        report["missing"].append(name)
    return report


def measure_placement(scene, model: FocusModel, *, anchor, anchor_mode=ANCHOR_AUTO) -> FocusPlacement:
    """Measure what ``model`` contributed to the scene (its objects and world box)."""
    names = []
    for obj in scene.objects:
        try:
            marker = obj.get(OBJECT_MARK_KEY)
        except AttributeError:
            marker = None
        if marker and str(marker) == model.id:
            names.append(obj.name)
    if not names and model.object_name:
        candidate = scene.objects.get(model.object_name)
        if candidate is not None:
            names.append(candidate.name)
    names = sorted(set(names))
    if not names:
        return FocusPlacement(id=model.id, model_path=model.path, label=model.label,
                              anchor=tuple(anchor), anchor_mode=anchor_mode,
                              note="the model contributed no object to the scene")
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for name in names:
        obj = scene.objects[name]
        for corner in obj.bound_box:
            point = obj.matrix_world @ _vec(corner)
            for axis in range(3):
                lo[axis] = min(lo[axis], point[axis])
                hi[axis] = max(hi[axis], point[axis])
    return FocusPlacement(
        id=model.id,
        model_path=model.path,
        label=model.label,
        objects=tuple(names),
        bbox_min=tuple(lo),
        bbox_max=tuple(hi),
        anchor=tuple(float(v) for v in anchor),
        anchor_mode=anchor_mode,
    )


def placement_for(scene, model_id_value: str) -> "FocusPlacement | None":
    """The registered placement of one model id, if the scene carries it."""
    for placement in registered_placements(scene):
        if placement.id == model_id_value:
            return placement
    return None


def focus_box(placement: FocusPlacement):
    """A :class:`CharacterBox`-shaped box for the placement, for reuse by validators."""
    from ..camera.scene_context import CharacterBox

    return CharacterBox(
        name=placement.id,
        bbox_min=tuple(float(v) for v in placement.bbox_min),
        bbox_max=tuple(float(v) for v in placement.bbox_max),
        excluded_names=list(placement.objects),
    )


def summary_line(placements, models) -> str:
    """One line for the panel: what the feature will do."""
    if not models:
        return "No focus models configured"
    return (f"{len(models)} focus model(s) x {len(placements)} placement(s); "
            "an Arc shot orbits the object, every other motion is unchanged")
