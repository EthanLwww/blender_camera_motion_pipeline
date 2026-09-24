"""Generic parser and animator for camera motion template JSON.

Nothing here is hard-coded to a specific motion name.  A template document is a
flat array of entries shaped like::

    {
      "id": "dolly_in_01_standard",
      "keys": [
        {"frame": 0,  "location": [0, 0, 0],     "rotation": [0, 0, 0],  "focal": 35},
        {"frame": 40, "location": [0, 0, -1.5],  "rotation": [0, 0, 0],  "focal": 35},
        {"frame": 80, "location": [0, 0, -3.0],  "rotation": [0, 0, 0],  "focal": 35}
      ]
    }

The parser also accepts a dictionary root (``{"templates": [...]}``), nested
``motion_templates``/``camera_templates`` keys, and per-key aliases such as
``position``/``pos`` for ``location`` and ``angles``/``rot`` for ``rotation``,
so a future template file can be dropped in without touching code.  Adding a
new *motion type* therefore means adding a JSON entry, never a Python branch.

Coordinate contract -- Blender, with no conversion
--------------------------------------------------
Templates are authored in **Blender coordinates** and the numbers are used
**verbatim**; there is no axis swap, no sign flip and no unit rescaling.

* ``location`` is an offset **in the camera's own frame**, in **metres**, using
  the camera's local Blender axes: ``+X`` right, ``+Y`` up, ``-Z`` forward (so a
  forward push is negative Z, and ``+Z`` is backwards).  It is applied as an
  offset from the camera's starting position, rotated by the camera's starting
  orientation -- i.e. "0.5 m to my right, 0.2 m up, 1.5 m forward" is
  ``[0.5, 0.2, -1.5]`` no matter how the camera is aimed in the world.
* ``rotation`` is ``[rx, ry, rz]`` in **degrees** about the same local axes, i.e.
  the numbers you would type into Blender's own camera rotation fields:
  ``rx`` tilts (pitch), ``ry`` turns left/right about the camera's up axis, and
  ``rz`` rolls the frame about the view axis.  They are composed in
  ``motion.unit_scale.rotation_order`` (default ``XYZ``, Blender's order) and
  applied *inside* the camera's starting orientation.
* ``focal`` is millimetres.

With this contract ``dolly_in`` moves the camera along its own view axis,
``pan_right`` turns it to its right, ``pedestal_up`` raises it, ``truck_right``
strafes it right and ``roll`` spins the frame without turning the aim -- each of
those is asserted in ``tests/test_motion_templates.py``.

Template sets written for Unreal (centimetres, ``X`` forward / ``Y`` right /
``Z`` up, ``[roll, pitch, yaw]``) must be converted once, not at run time::

    python tests/migrate_unreal_templates.py --input <templates.json> --in-place
"""

from __future__ import annotations

import copy
import json
import math
import os
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..config.models import ConfigError, TemplateUnitScale
from ..io.json_io import JsonError, load_json_file
from ..io.path_utils import normalize_path

# --------------------------------------------------------------------------
# tiny vector / quaternion library
# --------------------------------------------------------------------------
Vec3 = tuple
Quat = tuple


def vec_add(a: Sequence[float], b: Sequence[float]) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def vec_sub(a: Sequence[float], b: Sequence[float]) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def vec_scale(a: Sequence[float], factor: float) -> Vec3:
    return (a[0] * factor, a[1] * factor, a[2] * factor)


def vec_length(a: Sequence[float]) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def vec_normalized(a: Sequence[float]) -> Vec3:
    length = vec_length(a)
    if length <= 1e-12:
        return (0.0, 0.0, 0.0)
    return (a[0] / length, a[1] / length, a[2] / length)


def quat_from_axis_angle(axis: str, degrees: float) -> Quat:
    """Quaternion ``(w, x, y, z)`` for a rotation about a principal axis."""
    half = math.radians(degrees) * 0.5
    s = math.sin(half)
    c = math.cos(half)
    if axis == "X":
        return (c, s, 0.0, 0.0)
    if axis == "Y":
        return (c, 0.0, s, 0.0)
    if axis == "Z":
        return (c, 0.0, 0.0, s)
    raise ValueError(f"axis must be X, Y or Z (got {axis!r})")


def quat_normalize(q: Sequence[float]) -> Quat:
    n = math.sqrt(sum(component * component for component in q))
    if n <= 1e-12:
        return (1.0, 0.0, 0.0, 0.0)
    return tuple(component / n for component in q)  # type: ignore[return-value]


def quat_multiply(a: Sequence[float], b: Sequence[float]) -> Quat:
    """``a * b``: apply ``b`` first, then ``a`` (Hamilton product)."""
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def quat_conjugate(q: Sequence[float]) -> Quat:
    return (q[0], -q[1], -q[2], -q[3])


def quat_rotate(q: Sequence[float], v: Sequence[float]) -> Vec3:
    """Rotate vector ``v`` by quaternion ``q``.

    Standard ``v + 2*qw*(qv x v) + 2*(qv x (qv x v))`` form.  Note the positive
    sign on the first cross product: negating it silently mirrors every rotation
    while still returning unit-length vectors, which is very hard to spot
    downstream.
    """
    qw = q[0]
    qx, qy, qz = q[1], q[2], q[3]
    vx, vy, vz = v[0], v[1], v[2]
    # t = 2 * (qv x v)
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)
    # v' = v + qw * t + (qv x t)
    return (
        vx + qw * tx + (qy * tz - qz * ty),
        vy + qw * ty + (qz * tx - qx * tz),
        vz + qw * tz + (qx * ty - qy * tx),
    )


def quat_angle_between(a: Sequence[float], b: Sequence[float]) -> float:
    """Absolute rotation angle in degrees between two orientations."""
    dot = abs(sum(x * y for x, y in zip(quat_normalize(a), quat_normalize(b))))
    dot = max(-1.0, min(1.0, dot))
    return math.degrees(2.0 * math.acos(dot))


def quat_to_euler_xyz(q: Sequence[float]) -> Vec3:
    """Convert to Blender's default ``XYZ`` Euler order (radians)."""
    w, x, y, z = quat_normalize(q)
    sinr_cosp = 2.0 * (w * x + y * z)
    cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
    rx = math.atan2(sinr_cosp, cosr_cosp)
    sinp = 2.0 * (w * y - z * x)
    sinp = max(-1.0, min(1.0, sinp))
    ry = math.asin(sinp)
    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    rz = math.atan2(siny_cosp, cosy_cosp)
    return (rx, ry, rz)


def orthonormal_axes(camera_matrix: Sequence[Sequence[float]]) -> "tuple[Vec3, Vec3, Vec3]":
    """``(x, y, z)`` columns of a world matrix, made orthonormal and right-handed.

    A camera object can carry a scale -- the reference scene has one at **0.542**
    -- and then its world matrix is ``s * R`` rather than ``R``.  Everything in this
    module assumes an orthonormal frame, and feeding it a scaled matrix is not a
    rounding-level mistake:

    * the template offsets are built by multiplying these axes, so every motion came
      out scaled by 0.542 (a 3.4 m push travelled 1.843 m);
    * ``matrix_to_quaternion`` read a 24.7 deg wrong orientation from that matrix,
      which pointed the motion 22 deg away from the camera's own view axis.

    Uniform scale is undone by normalising each column; a non-uniform one is
    repaired with Gram-Schmidt, which keeps the first axis and the plane of the
    first two (the best a shear-free basis can do).
    """
    x = vec_normalized([float(camera_matrix[i][0]) for i in range(3)])
    y_raw = [float(camera_matrix[i][1]) for i in range(3)]
    z_raw = [float(camera_matrix[i][2]) for i in range(3)]
    y = [y_raw[i] - sum(y_raw[j] * x[j] for j in range(3)) * x[i] for i in range(3)]
    if vec_length(y) < 1e-9:
        # Degenerate: pick any axis perpendicular to x.
        y = [0.0, 1.0, 0.0] if abs(x[1]) < 0.9 else [1.0, 0.0, 0.0]
        y = [y[i] - sum(y[j] * x[j] for j in range(3)) * x[i] for i in range(3)]
    y = vec_normalized(y)
    z = (
        x[1] * y[2] - x[2] * y[1],
        x[2] * y[0] - x[0] * y[2],
        x[0] * y[1] - x[1] * y[0],
    )
    # Keep the original handedness: a mirrored matrix must not silently flip.
    if sum(z[i] * z_raw[i] for i in range(3)) < 0.0:
        z = (-z[0], -z[1], -z[2])
        y = (
            z[1] * x[2] - z[2] * x[1],
            z[2] * x[0] - z[0] * x[2],
            z[0] * x[1] - z[1] * x[0],
        )
    return (x, y, z)


def axis_basis(camera_matrix: Sequence[Sequence[float]]) -> "tuple[Vec3, Vec3, Vec3]":
    """Return ``(right, up, forward)`` unit axes of a camera-to-world matrix.

    Blender's camera looks down its local ``-Z``, so the *view direction* is the
    third column negated, not the third column itself.  Getting this backwards
    silently turns every forward dolly into a backward one, so it is asserted
    directly in ``tests/test_motion_templates.py``.

    The axes are **unit length whatever the camera's scale is**
    (:func:`orthonormal_axes`), so a template offset in metres stays metres in the
    world instead of being multiplied by the object's scale.
    """
    right, up, back = orthonormal_axes(camera_matrix)
    return (right, up, vec_scale(back, -1.0))


# --------------------------------------------------------------------------
# keyframes
# --------------------------------------------------------------------------
@dataclass
class TemplateKeyframe:
    frame: int
    location: Vec3 = (0.0, 0.0, 0.0)
    rotation: Vec3 = (0.0, 0.0, 0.0)
    focal: float | None = None

    def to_dict(self) -> dict:
        payload = {
            "frame": self.frame,
            "location": [round(float(v), 6) for v in self.location],
            "rotation": [round(float(v), 6) for v in self.rotation],
        }
        if self.focal is not None:
            payload["focal"] = round(float(self.focal), 6)
        return payload


@dataclass
class InterpolatedKey:
    frame: int
    location: Vec3
    rotation: Vec3
    focal: float


def _as_vec3(value, *, where: str) -> Vec3:
    if value is None:
        return (0.0, 0.0, 0.0)
    if isinstance(value, (int, float)):
        raise ConfigError(f"{where}: expected 3 numbers, got a scalar ({value!r})")
    if isinstance(value, dict):
        # {"x": .., "y": .., "z": ..} or {"pitch": .., "yaw": .., "roll": ..}
        lowered = {str(k).lower(): v for k, v in value.items()}
        if {"x", "y", "z"} <= set(lowered):
            return (float(lowered["x"]), float(lowered["y"]), float(lowered["z"]))
        if {"roll", "pitch", "yaw"} <= set(lowered):
            return (float(lowered["roll"]), float(lowered["pitch"]), float(lowered["yaw"]))
        raise ConfigError(f"{where}: cannot interpret object {sorted(value)} as 3 numbers")
    items = list(value)
    if len(items) == 2:
        items = items + [0.0]
    if len(items) < 3:
        raise ConfigError(f"{where}: expected 3 numbers, got {len(items)}")
    try:
        return (float(items[0]), float(items[1]), float(items[2]))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: non-numeric component ({value!r})") from exc


def _first_present(mapping: dict, names: Iterable[str]):
    for name in names:
        if name in mapping:
            return name, mapping[name]
    return None, None


# --------------------------------------------------------------------------
# template
# --------------------------------------------------------------------------
@dataclass
class MotionTemplate:
    """One motion type.  ``parameters`` carries any extra JSON fields."""

    name: str
    keyframes: "list[TemplateKeyframe]" = field(default_factory=list)
    parameters: dict = field(default_factory=dict)
    description: str = ""
    source: str = ""

    LOCATION_ALIASES = ("location", "loc", "position", "pos", "translation", "offset")
    ROTATION_ALIASES = ("rotation", "rot", "angles", "angle", "orientation", "euler")
    FOCAL_ALIASES = ("focal", "focal_length", "focallength", "lens", "mm")
    FRAME_ALIASES = ("frame", "t", "time", "frame_number")
    KEYS_ALIASES = ("keys", "keyframes", "samples", "frames", "poses")
    PARAM_ALIASES = ("parameters", "params", "options", "defaults")
    NAME_ALIASES = ("id", "name", "template", "template_name", "motion", "motion_name", "type")

    # -- construction ----------------------------------------------------
    @classmethod
    def from_dict(cls, raw: dict, *, source: str = "", index: int = 0) -> "MotionTemplate":
        if not isinstance(raw, dict):
            raise ConfigError(
                f"template #{index} must be an object, got {type(raw).__name__}"
            )
        working = dict(raw)

        name_key, name = _first_present(working, cls.NAME_ALIASES)
        if name_key:
            working.pop(name_key)
        if name is None or not str(name).strip():
            raise ConfigError(f"template #{index} has no id/name field")
        name = str(name).strip()

        keys_key, keys = _first_present(working, cls.KEYS_ALIASES)
        if keys_key:
            working.pop(keys_key)
        if keys is None:
            raise ConfigError(f"template {name!r} has no keys/keyframes array")
        if isinstance(keys, dict):
            # {"0": {...}, "40": {...}} form
            converted = []
            for frame_text, entry in keys.items():
                entry = dict(entry) if isinstance(entry, dict) else {}
                entry.setdefault("frame", frame_text)
                converted.append(entry)
            keys = converted
        if not isinstance(keys, (list, tuple)) or not keys:
            raise ConfigError(f"template {name!r} keys must be a non-empty array")

        params = {}
        params_key, params_value = _first_present(working, cls.PARAM_ALIASES)
        if params_key:
            working.pop(params_key)
            if isinstance(params_value, dict):
                params.update(params_value)
        description = ""
        for key in ("description", "label", "doc", "comment"):
            if key in working:
                description = str(working.pop(key))
                break

        keyframes = []
        for kf_index, entry in enumerate(keys):
            keyframes.append(cls._parse_keyframe(name, kf_index, entry))

        # Anything left over becomes a template parameter so future template
        # fields survive round-tripping and can be read from the config.
        params.update(working)
        return cls(
            name=name,
            keyframes=keyframes,
            parameters=params,
            description=description,
            source=source,
        )

    @classmethod
    def _parse_keyframe(cls, name: str, index: int, entry) -> TemplateKeyframe:
        where = f"template {name!r} key #{index}"
        if isinstance(entry, (list, tuple)):
            if len(entry) < 2:
                raise ConfigError(f"{where}: array form needs at least [frame, location]")
            return TemplateKeyframe(
                frame=int(entry[0]),
                location=_as_vec3(entry[1], where=f"{where}.location"),
                rotation=_as_vec3(entry[2], where=f"{where}.rotation") if len(entry) > 2 else (0.0, 0.0, 0.0),
                focal=float(entry[3]) if len(entry) > 3 and entry[3] is not None else None,
            )
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: must be an object or array, got {type(entry).__name__}")

        frame_key, frame = _first_present(entry, cls.FRAME_ALIASES)
        if frame is None:
            raise ConfigError(f"{where}: missing frame")
        try:
            frame_value = int(round(float(frame)))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{where}: frame {frame!r} is not numeric") from exc

        _, location = _first_present(entry, cls.LOCATION_ALIASES)
        _, rotation = _first_present(entry, cls.ROTATION_ALIASES)
        _, focal = _first_present(entry, cls.FOCAL_ALIASES)
        focal_value = None
        if focal is not None:
            try:
                focal_value = float(focal)
            except (TypeError, ValueError):
                focal_value = None
        return TemplateKeyframe(
            frame=frame_value,
            location=_as_vec3(location, where=f"{where}.location"),
            rotation=_as_vec3(rotation, where=f"{where}.rotation"),
            focal=focal_value,
        )

    # -- validation ------------------------------------------------------
    def validate(self) -> "MotionTemplate":
        if not self.keyframes:
            raise ConfigError(f"template {self.name!r} has no keyframes")
        frames = [kf.frame for kf in self.keyframes]
        if len(set(frames)) != len(frames):
            raise ConfigError(f"template {self.name!r} has duplicate keyframe numbers: {frames}")
        ordered = sorted(self.keyframes, key=lambda kf: kf.frame)
        if ordered[0].frame < 0:
            raise ConfigError(
                f"template {self.name!r} starts at frame {ordered[0].frame}; frame numbers must be >= 0"
            )
        self.keyframes = ordered
        return self

    # -- properties ------------------------------------------------------
    @property
    def frame_min(self) -> int:
        return self.keyframes[0].frame

    @property
    def frame_max(self) -> int:
        return self.keyframes[-1].frame

    @property
    def duration_frames(self) -> int:
        return self.frame_max - self.frame_min

    def focals(self) -> "list[float]":
        return [kf.focal for kf in self.keyframes if kf.focal is not None]

    def to_dict(self) -> dict:
        payload = {
            "id": self.name,
            "keys": [kf.to_dict() for kf in self.keyframes],
        }
        if self.description:
            payload["description"] = self.description
        for key, value in self.parameters.items():
            payload.setdefault(key, value)
        return payload

    def summary(self) -> dict:
        return {
            "name": self.name,
            "keyframe_count": len(self.keyframes),
            "frame_min": self.frame_min,
            "frame_max": self.frame_max,
            "duration_frames": self.duration_frames,
            "focal_min": min(self.focals()) if self.focals() else None,
            "focal_max": max(self.focals()) if self.focals() else None,
            "parameters": sorted(self.parameters),
            "description": self.description,
        }

    # -- interpolation ---------------------------------------------------
    def interpolate(self, frame: float) -> InterpolatedKey:
        """Linearly interpolate the template curve at ``frame``.

        Yaw wraps through the shortest arc (the reference file's ``pan_*`` and
        ``roll_*`` families sweep through +/-60 deg, and blending 350 -> 10 the
        naive way would spin the camera the wrong way round).
        """
        keys = self.keyframes
        if frame <= keys[0].frame:
            first = keys[0]
            return InterpolatedKey(first.frame, first.location, first.rotation,
                                   self._focal_at(first, 0))
        if frame >= keys[-1].frame:
            last = keys[-1]
            return InterpolatedKey(last.frame, last.location, last.rotation,
                                   self._focal_at(last, -1))
        for a, b in zip(keys, keys[1:]):
            if a.frame <= frame <= b.frame:
                span = b.frame - a.frame
                alpha = 0.0 if span == 0 else (frame - a.frame) / float(span)
                location = tuple(
                    x + (y - x) * alpha for x, y in zip(a.location, b.location)
                )
                rotation = tuple(
                    x + _shortest_delta(x, y) * alpha for x, y in zip(a.rotation, b.rotation)
                )
                fa = self._focal_at(a, 0)
                fb = self._focal_at(b, 1)
                return InterpolatedKey(int(round(frame)), location, rotation,
                                       fa + (fb - fa) * alpha)
        last = keys[-1]
        return InterpolatedKey(last.frame, last.location, last.rotation, self._focal_at(last, -1))

    def _focal_at(self, keyframe: TemplateKeyframe, position: int) -> float:
        """Focal for a keyframe, inheriting from the nearest key that has one."""
        if keyframe.focal is not None:
            return float(keyframe.focal)
        keys = self.keyframes
        if position <= 0:
            for candidate in keys:
                if candidate.focal is not None:
                    return float(candidate.focal)
        else:
            for candidate in reversed(keys):
                if candidate.focal is not None:
                    return float(candidate.focal)
        return 35.0

    def effective_focal_range(self) -> "tuple[float | None, float | None]":
        focals = self.focals()
        if not focals:
            return (None, None)
        return (min(focals), max(focals))

    def is_static(self, *, location_epsilon: float = 1e-9, rotation_epsilon: float = 1e-9) -> bool:
        if not self.keyframes:
            return True
        first = self.keyframes[0]
        for key in self.keyframes[1:]:
            if any(abs(a - b) > location_epsilon for a, b in zip(first.location, key.location)):
                return False
            if any(abs(a - b) > rotation_epsilon for a, b in zip(first.rotation, key.rotation)):
                return False
        return True


# --------------------------------------------------------------------------
# library
# --------------------------------------------------------------------------
def parse_template_document(payload, *, source: str = "") -> "list[MotionTemplate]":
    """Normalise any accepted template document into a list of templates."""
    entries = _extract_template_entries(payload, source=source)
    templates: "list[MotionTemplate]" = []
    seen = set()
    for index, raw in enumerate(entries):
        template = MotionTemplate.from_dict(raw, source=source, index=index).validate()
        if template.name in seen:
            raise ConfigError(f"duplicate template id {template.name!r} in {source or 'document'}")
        seen.add(template.name)
        templates.append(template)
    if not templates:
        raise ConfigError(f"no motion templates found in {source or 'document'}")
    return templates


def _extract_template_entries(payload, *, source: str) -> "list[dict]":
    if isinstance(payload, list):
        return list(payload)
    if isinstance(payload, dict):
        for key in ("templates", "motion_templates", "camera_templates", "camera_motion_templates", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return list(value)
            if isinstance(value, dict):
                # {name: {...}} form
                merged = []
                for name, entry in value.items():
                    entry = dict(entry) if isinstance(entry, dict) else {"keys": entry}
                    entry.setdefault("id", name)
                    merged.append(entry)
                return merged
        # a single template or {name: {keys: [...]}}
        if any(k in payload for k in MotionTemplate.KEYS_ALIASES):
            return [payload]
        looks_like_map = all(
            isinstance(v, dict) and any(k in v for k in MotionTemplate.KEYS_ALIASES)
            for v in payload.values()
        )
        if payload and looks_like_map:
            merged = []
            for name, entry in payload.items():
                entry = dict(entry)
                entry.setdefault("id", name)
                merged.append(entry)
            return merged
    raise ConfigError(
        f"{source or 'template document'}: unrecognised structure "
        f"({type(payload).__name__}); expected an array of templates"
    )


class MotionTemplateLibrary:
    """Loaded, validated template set with override support."""

    def __init__(self, templates: "list[MotionTemplate] | None" = None, *, source: str = ""):
        self.source = source
        self.warnings: "list[str]" = []
        self._templates: "dict[str, MotionTemplate]" = {}
        for template in templates or []:
            self._templates[template.name] = template

    # -- loading ---------------------------------------------------------
    @classmethod
    def from_file(cls, path: str) -> "MotionTemplateLibrary":
        target = normalize_path(path)
        if not os.path.isfile(target):
            raise JsonError(f"motion template file not found: {path}")
        try:
            payload = load_json_file(target)
        except JsonError as exc:
            raise JsonError(f"cannot parse motion template file: {exc}") from exc
        templates = parse_template_document(payload, source=target)
        return cls(templates, source=target)

    @classmethod
    def from_entries(cls, entries, *, source: str = "inline") -> "MotionTemplateLibrary":
        return cls(parse_template_document(entries, source=source), source=source)

    @classmethod
    def from_config(cls, motion_section, *, logger=None) -> "MotionTemplateLibrary":
        """Build from a ``MotionSection``.

        Resolution order: explicit ``template_path`` -> inline
        ``template_data`` -> discovered bundled/reference file.  A malformed
        explicit file is a hard error (the user asked for it); a malformed
        discovered file only produces a warning so the pipeline can still run on
        embedded defaults.

        A whole ``BatchConfig`` is accepted too and unwrapped, because passing
        the container instead of its ``motion`` section is an easy mistake that
        would otherwise surface as a confusing ``AttributeError``.
        """
        motion_section = _as_motion_section(motion_section)
        library = cls()
        path = (motion_section.template_path or "").strip()
        if path:
            library = cls.from_file(path)
        elif motion_section.template_data:
            library = cls.from_entries(motion_section.template_data, source="config.motion.template_data")
        else:
            from ..config.defaults import load_discovered_templates

            entries, discovered = load_discovered_templates()
            if entries:
                try:
                    library = cls.from_entries(entries, source=discovered)
                except ConfigError as exc:
                    library = cls()
                    library.warnings.append(
                        f"discovered template file {discovered} is unusable ({exc}); "
                        "falling back to the embedded minimal template set"
                    )
                    library = cls(EMBEDDED_TEMPLATES, source="embedded")
            else:
                library = cls(EMBEDDED_TEMPLATES, source="embedded")
                if discovered:
                    library.warnings.append(
                        f"template file {discovered} exists but could not be read; "
                        "using the embedded minimal template set"
                    )
                else:
                    library.warnings.append(
                        "no motion template file found; using the embedded minimal "
                        "template set (set motion.template_path to override)"
                    )

        library.apply_overrides(getattr(motion_section, "template_overrides", None) or {})
        if motion_section.template_names:
            library.restrict_to(motion_section.template_names)
        if logger is not None:
            for warning in library.warnings:
                logger.warning("motion templates: %s", warning)
        return library

    # -- access ----------------------------------------------------------
    def __len__(self) -> int:
        return len(self._templates)

    def __iter__(self):
        return iter(self._templates.values())

    def __contains__(self, name: str) -> bool:
        return name in self._templates

    @property
    def names(self) -> "list[str]":
        return sorted(self._templates)

    def get(self, name: str) -> MotionTemplate:
        if name not in self._templates:
            raise KeyError(
                f"unknown motion template {name!r}; available: {', '.join(self.names)}"
            )
        return self._templates[name]

    def add(self, template: MotionTemplate) -> None:
        self._templates[template.validate().name] = template

    def restrict_to(self, names: Sequence[str]) -> None:
        """Keep only the named templates; a name may be a glob.

        An exact name always wins, and anything else is tried as a glob (``dolly_*``),
        which is what the ``--motion-filter`` flag and the panel's *Motion filter* have
        always advertised.  An entry that matches nothing is still a hard error, so a
        typo cannot silently shrink a run to nothing.
        """
        import fnmatch

        wanted = [str(n).strip() for n in names if str(n).strip()]
        if not wanted:
            return
        chosen: "list[str]" = []
        missing: "list[str]" = []
        for entry in wanted:
            if entry in self._templates:
                if entry not in chosen:
                    chosen.append(entry)
                continue
            matched = [name for name in self._templates
                       if fnmatch.fnmatch(name, entry) and name not in chosen]
            if matched:
                chosen.extend(matched)
            else:
                missing.append(entry)
        if missing:
            raise ConfigError(
                f"requested motion template(s) not found: {', '.join(missing)}; "
                f"available: {', '.join(self.names)}"
            )
        self._templates = {name: self._templates[name] for name in chosen}

    def apply_overrides(self, overrides: dict) -> None:
        """Apply ``{template_name: {parameters...}}`` or ``{"*": {...}}``."""
        if not overrides:
            return
        unknown = [key for key in overrides if key != "*" and key not in self._templates]
        if unknown:
            raise ConfigError(
                f"motion.template_overrides references unknown template(s): {', '.join(sorted(unknown))}"
            )
        for name, patch in overrides.items():
            if not isinstance(patch, dict):
                raise ConfigError(f"motion.template_overrides[{name!r}] must be an object")
            targets = list(self._templates.values()) if name == "*" else [self._templates[name]]
            for template in targets:
                self._apply_patch(template, patch, scope=name)

    def _apply_patch(self, template: MotionTemplate, patch: dict, *, scope: str) -> None:
        consumed = set()
        if "frame_scale" in patch:
            scale = float(patch["frame_scale"]); consumed.add("frame_scale")
            if scale <= 0:
                raise ConfigError(f"template_overrides[{scope!r}].frame_scale must be > 0")
            for key in template.keyframes:
                key.frame = int(round(key.frame * scale))
        if "frame_offset" in patch:
            offset = int(round(float(patch["frame_offset"]))); consumed.add("frame_offset")
            for key in template.keyframes:
                key.frame = max(0, key.frame + offset)
        if "location_scale" in patch:
            scale = float(patch["location_scale"]); consumed.add("location_scale")
            for key in template.keyframes:
                key.location = vec_scale(key.location, scale)
        if "focal_scale" in patch:
            scale = float(patch["focal_scale"]); consumed.add("focal_scale")
            for key in template.keyframes:
                if key.focal is not None:
                    key.focal = float(key.focal) * scale
        if "focal" in patch:
            focal = float(patch["focal"]); consumed.add("focal")
            for key in template.keyframes:
                key.focal = focal
        if "keys" in patch:
            keys = patch["keys"]; consumed.add("keys")
            if not isinstance(keys, list) or not keys:
                raise ConfigError(f"template_overrides[{scope!r}].keys must be a non-empty array")
            template.keyframes = [
                MotionTemplate._parse_keyframe(template.name, index, entry)
                for index, entry in enumerate(keys)
            ]
        for key, value in patch.items():
            if key in consumed:
                continue
            template.parameters[key] = copy.deepcopy(value)
        template.validate()

    def to_dict(self) -> dict:
        return {"source": self.source, "templates": [t.to_dict() for t in self]}

    def manifest(self) -> dict:
        return {
            "source": self.source,
            "count": len(self),
            "names": self.names,
            "warnings": list(self.warnings),
            "templates": [t.summary() for t in self],
        }


#: Minimal fallback used only when no template document can be found/read, so
#: the pipeline can still be exercised end-to-end.  It deliberately mirrors the
#: three families present in the reference file rather than inventing new ones.
def _embedded_template(tid: str, keys: Sequence[tuple]) -> MotionTemplate:
    return MotionTemplate(
        name=tid,
        keyframes=[
            TemplateKeyframe(frame=f, location=tuple(loc), rotation=tuple(rot), focal=focal)
            for f, loc, rot, focal in keys
        ],
        source="embedded",
        description="Embedded fallback template (no external template file was found).",
    ).validate()


EMBEDDED_TEMPLATES: "list[MotionTemplate]" = [
    _embedded_template("fixed_01_standard", [
        (0, (0, 0, 0), (0, 0, 0), 35.0),
        (40, (0, 0, 0), (0, 0, 0), 35.0),
        (80, (0, 0, 0), (0, 0, 0), 35.0),
    ]),
    _embedded_template("dolly_in_01_standard", [
        (0, (0, 0, 0), (0, 0, 0), 35.0),
        (40, (0, 0, -1.5), (0, 0, 0), 35.0),
        (80, (0, 0, -3.0), (0, 0, 0), 35.0),
    ]),
    _embedded_template("dolly_out_01_standard", [
        (0, (0, 0, 0), (0, 0, 0), 35.0),
        (40, (0, 0, 1.5), (0, 0, 0), 35.0),
        (80, (0, 0, 3.0), (0, 0, 0), 35.0),
    ]),
    _embedded_template("pan_right_01_standard", [
        (0, (0, 0, 0), (0, 0, 0), 35.0),
        (40, (0, 0, 0), (0, -15.0, 0), 35.0),
        (80, (0, 0, 0), (0, -30.0, 0), 35.0),
    ]),
    _embedded_template("pedestal_up_01_standard", [
        (0, (0, 0, 0), (0, 0, 0), 35.0),
        (40, (0, 0.6, 0), (0, 0, 0), 35.0),
        (80, (0, 1.2, 0), (0, 0, 0), 35.0),
    ]),
]


# --------------------------------------------------------------------------
# animation generation
# --------------------------------------------------------------------------
def _shortest_delta(a: float, b: float) -> float:
    return (b - a + 180.0) % 360.0 - 180.0


@dataclass
class CameraSample:
    """One animated camera pose in world space for a single frame."""

    frame: int
    position: Vec3
    quaternion: Quat
    focal: float
    template_offset: Vec3 = (0.0, 0.0, 0.0)
    template_rotation: Vec3 = (0.0, 0.0, 0.0)

    def to_dict(self, *, ndigits: int = 6) -> dict:
        return {
            "frame": self.frame,
            "location": [round(float(v), ndigits) for v in self.position],
            "rotation_quaternion": [round(float(v), ndigits) for v in self.quaternion],
            "focal_length": round(float(self.focal), ndigits),
        }


@dataclass
class MotionAnimation:
    """Result of converting a template into a concrete camera animation."""

    template_name: str
    frame_start: int
    frame_end: int
    fps: float
    interpolation: str
    samples: "list[CameraSample]"
    template_parameters: dict = field(default_factory=dict)
    unit_scale: dict = field(default_factory=dict)
    notes: "list[str]" = field(default_factory=list)
    source_unit_focal_range: "tuple[float | None, float | None]" = (None, None)

    @property
    def frame_count(self) -> int:
        return len(self.samples)

    def to_dict(self, *, include_samples: bool = True, ndigits: int = 6) -> dict:
        payload = {
            "template_name": self.template_name,
            "frame_start": self.frame_start,
            "frame_end": self.frame_end,
            "fps": self.fps,
            "interpolation": self.interpolation,
            "frame_count": self.frame_count,
            "keyframes": _keyframe_summary(self.samples),
            "parameters": dict(self.template_parameters),
            "unit_scale": dict(self.unit_scale),
            "notes": list(self.notes),
            "source_focal_range": list(self.source_unit_focal_range),
        }
        if include_samples:
            payload["samples"] = [s.to_dict(ndigits=ndigits) for s in self.samples]
        return payload


def _keyframe_summary(samples: Sequence[CameraSample], *, limit: int = 12) -> "list[dict]":
    """Subsample the animation into a compact keyframe list for metadata."""
    if not samples:
        return []
    if len(samples) <= limit:
        return [s.to_dict() for s in samples]
    step = max(1, len(samples) // limit)
    chosen = list(samples[::step])[:limit]
    if chosen[-1] is not samples[-1]:
        # Replace the last entry rather than appending, so the total never
        # exceeds ``limit`` (callers use this as a fixed-size summary field).
        chosen[-1] = samples[-1]
    return [s.to_dict() for s in chosen]


class MotionTemplateGenerator:
    """Turn a :class:`MotionTemplate` into :class:`CameraSample` poses.

    The generator is pure math: it only needs the camera's base 4x4 world
    matrix and its base focal length, so it can be unit-tested outside Blender
    and reused by the camera-search module without touching the scene.
    """

    def __init__(
        self,
        *,
        unit_scale: TemplateUnitScale | None = None,
        frame_start: int | None = None,
        frame_scale: float = 1.0,
        fps: float | None = None,
        interpolation: str = "BEZIER",
    ):
        self.unit_scale = unit_scale or TemplateUnitScale()
        self.frame_offset = int(frame_start) if frame_start is not None else None
        self.frame_scale = float(frame_scale)
        self.fps = float(fps if fps is not None else self.unit_scale.fps)
        self.interpolation = str(interpolation or "BEZIER").upper()
        if self.frame_scale <= 0:
            raise ConfigError("frame_scale must be > 0")

    # -- helpers ---------------------------------------------------------
    def template_frame_to_scene_frame(self, template_frame: float) -> float:
        """Map a template frame number into the sequence timeline.

        ``frame_start`` pins the template's first key to a specific scene frame;
        otherwise the template's own frame numbering is used as-is (the
        reference file starts at 0).
        """
        base_offset = self.frame_offset if self.frame_offset is not None else 0
        return base_offset + (float(template_frame) * self.frame_scale)

    def local_offset(self, template_location: Sequence[float]) -> Vec3:
        """Template location -> offset in the camera's own (Blender) frame.

        The numbers are Blender coordinates already: ``+X`` right, ``+Y`` up,
        ``+Z`` backwards (so ``-Z`` is forward), in metres.  Nothing is scaled or
        swapped -- that is the whole point of the Blender-native contract -- so this
        only normalises the sequence into a 3-tuple.  :meth:`generate` applies it as
        ``right*x + up*y + back*z`` with the camera's own local axes, which is the
        same thing as pasting the numbers into a camera's local transform.
        """
        values = [float(v) for v in template_location[:3]]
        while len(values) < 3:
            values.append(0.0)
        return (values[0], values[1], values[2])

    def rotation_delta(self, template_rotation: Sequence[float]) -> Quat:
        """``[rx, ry, rz]`` (degrees, camera-local) -> rotation of the camera.

        The three components are rotations about the camera's own ``X`` (tilt),
        ``Y`` (turn) and ``Z`` (roll) axes, composed in
        ``unit_scale.rotation_order`` (``XYZ`` applies ``rx`` first, matching
        Blender's own Euler order).  The result is returned as a quaternion in the
        camera's local frame, so callers compose it *inside* the base orientation
        (``q_base * q_local``) -- no world-axis yaw, no sign flips.
        """
        values = [float(v) for v in template_rotation[:3]]
        while len(values) < 3:
            values.append(0.0)
        angles = {"X": values[0], "Y": values[1], "Z": values[2]}
        order = self.unit_scale.rotation_order
        composed: Quat = (1.0, 0.0, 0.0, 0.0)
        for axis in order:
            composed = quat_multiply(quat_from_axis_angle(axis, angles[axis]), composed)
        return quat_normalize(composed)

    # -- main ------------------------------------------------------------
    def generate(
        self,
        template: MotionTemplate,
        *,
        base_matrix: Sequence[Sequence[float]],
        base_focal: float,
        frame_start: int | None = None,
        frame_end: int | None = None,
        base_quaternion: Quat | None = None,
    ) -> MotionAnimation:
        """Sample ``template`` for every integer frame of the requested range."""
        template.validate()
        scene_start = (
            int(frame_start) if frame_start is not None
            else int(round(self.template_frame_to_scene_frame(template.frame_min)))
        )
        scene_end = (
            int(frame_end) if frame_end is not None
            else int(round(self.template_frame_to_scene_frame(template.frame_max)))
        )
        if scene_end < scene_start:
            scene_end = scene_start

        base_position = (
            float(base_matrix[0][3]),
            float(base_matrix[1][3]),
            float(base_matrix[2][3]),
        )
        right, up, forward = axis_basis(base_matrix)
        # ``axis_basis`` returns the *view* direction as its third vector, which is
        # the camera's local ``-Z``; a Blender-coordinate template indexes ``+Z``
        # (backwards), so the third component multiplies the back axis.
        back = vec_scale(forward, -1.0)
        if base_quaternion is None:
            base_quaternion = matrix_to_quaternion(base_matrix)
        base_quaternion = quat_normalize(base_quaternion)

        notes: "list[str]" = []
        if not template.focals():
            notes.append(
                "template declares no focal length; the camera's original focal length is kept"
            )

        samples: "list[CameraSample]" = []
        for scene_frame in range(scene_start, scene_end + 1):
            if self.frame_scale == 1.0:
                template_frame = scene_frame - (self.frame_offset or 0)
            else:
                template_frame = (scene_frame - (self.frame_offset or 0)) / self.frame_scale
            key = template.interpolate(template_frame)
            local = self.local_offset(key.location)
            # ``local`` holds the template's Blender numbers unchanged: +X right,
            # +Y up, +Z backwards, so multiplying the camera's own axes applies the
            # offset exactly as authored (a forward push is a negative Z).
            world_offset = vec_add(
                vec_add(vec_scale(right, local[0]), vec_scale(up, local[1])),
                vec_scale(back, local[2]),
            )
            # The rotation is local, so it composes inside the base orientation.
            quaternion = quat_multiply(base_quaternion, self.rotation_delta(key.rotation))
            focal = float(key.focal) if template.focals() else float(base_focal)
            samples.append(
                CameraSample(
                    frame=scene_frame,
                    position=vec_add(base_position, world_offset),
                    quaternion=quat_normalize(quaternion),
                    focal=focal,
                    template_offset=tuple(float(v) for v in key.location),
                    template_rotation=tuple(float(v) for v in key.rotation),
                )
            )

        return MotionAnimation(
            template_name=template.name,
            frame_start=scene_start,
            frame_end=scene_end,
            fps=self.fps,
            interpolation=self.interpolation,
            samples=samples,
            template_parameters=dict(template.parameters),
            unit_scale=self.unit_scale.to_dict(),
            notes=notes,
            source_unit_focal_range=template.effective_focal_range(),
        )


def matrix_to_quaternion(matrix: Sequence[Sequence[float]]) -> Quat:
    """Robust 3x3 rotation -> quaternion for a 4x4 (or 3x3) row-major matrix.

    The rotation is taken from an orthonormalised copy of the matrix: a camera with
    a scale (the reference scene has one at 0.542, i.e. ``0.542 * R``) made the
    trace-based formula below return an orientation **24.7 deg** off the truth,
    which pointed generated camera moves away from the camera's own view axis.
    """
    x, y, z = orthonormal_axes(matrix)
    m00, m10, m20 = x
    m01, m11, m21 = y
    m02, m12, m22 = z
    trace = m00 + m11 + m22
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        return quat_normalize(((s * 0.25), (m21 - m12) / s, (m02 - m20) / s, (m10 - m01) / s))
    if m00 > m11 and m00 > m22:
        s = math.sqrt(1.0 + m00 - m11 - m22) * 2.0
        return quat_normalize(((m21 - m12) / s, s * 0.25, (m01 + m10) / s, (m02 + m20) / s))
    if m11 > m22:
        s = math.sqrt(1.0 + m11 - m00 - m22) * 2.0
        return quat_normalize(((m02 - m20) / s, (m01 + m10) / s, s * 0.25, (m12 + m21) / s))
    s = math.sqrt(1.0 + m22 - m00 - m11) * 2.0
    return quat_normalize(((m10 - m01) / s, (m02 + m20) / s, (m12 + m21) / s, s * 0.25))


def quaternion_to_matrix(q: Sequence[float]) -> "list[list[float]]":
    """Quaternion -> 3x3 rotation matrix (row major)."""
    w, x, y, z = quat_normalize(q)
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def load_template_file(path: str) -> MotionTemplateLibrary:
    """Convenience wrapper used by the CLI and the tests."""
    return MotionTemplateLibrary.from_file(path)


def parse_template_text(text: str, *, source: str = "<inline>") -> "list[MotionTemplate]":
    """Parse a raw JSON string (used by tests and ``--templates-json``)."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JsonError(f"Malformed template JSON in {source}: {exc}") from exc
    return parse_template_document(payload, source=source)


def _as_motion_section(candidate):
    """Accept a ``MotionSection``, or unwrap a container that has one.

    Also fails with a useful message when neither is supplied, instead of
    raising ``AttributeError: 'X' object has no attribute 'template_path'``.
    """
    if candidate is None:
        raise ConfigError("no motion template configuration was supplied")
    if hasattr(candidate, "template_path") and hasattr(candidate, "template_data"):
        return candidate
    for attribute in ("motion", "motion_section"):
        nested = getattr(candidate, attribute, None)
        if nested is not None and hasattr(nested, "template_path"):
            return nested
    if isinstance(candidate, dict):
        nested = candidate.get("motion")
        if isinstance(nested, dict):
            return MotionSection.from_dict(dict(nested), [])
    raise ConfigError(
        "motion templates need a MotionSection; got "
        f"{type(candidate).__name__} (pass config.motion, not config)"
    )
