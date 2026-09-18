"""Real Blender character adapter: appends ``.blend`` characters + animations.

This is the P2 deliverable from the brief, implemented for Blender-native
character libraries (Rigify rigs, Mixamo rigs converted to ``.blend``, or any
hand-built humanoid).  It is deliberately separated from the orchestration code:
the sequence generator only talks to the :class:`CharacterProvider` interface.

Import strategy: ``bpy.ops.wm.append`` with a full datablock path, falling back
to a whole-file append when the exact path is unknown.  Both are preceded by a
name snapshot so the newly created objects can be identified reliably even when
Blender renames collisions (``Armature.001``).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from ..camera.scene_context import CharacterBox, SceneContext, mesh_snapshot
from ..io.path_utils import normalize_path, to_forward_slashes
from .base_provider import (
    STATUS_AVAILABLE,
    STATUS_DEGRADED,
    STATUS_UNAVAILABLE,
    AnimationDescriptor,
    CharacterDescriptor,
    CharacterPlacement,
    CharacterProvider,
    CharacterValidation,
)
from .library import CharacterLibrary

_SUPPORTED_OBJECT_TYPES = {"MESH", "ARMATURE", "EMPTY"}


@dataclass
class _Snapshot:
    objects: set = field(default_factory=set)
    collections: set = field(default_factory=set)
    actions: set = field(default_factory=set)
    materials: set = field(default_factory=set)


def _take_snapshot() -> _Snapshot:
    import bpy  # local import: this module is inert outside Blender

    return _Snapshot(
        objects={obj.name for obj in bpy.data.objects},
        collections={coll.name for coll in bpy.data.collections},
        actions={action.name for action in bpy.data.actions},
        materials={mat.name for mat in bpy.data.materials},
    )


class BlenderCharacterProvider(CharacterProvider):
    """Append and animate character assets from a :class:`CharacterLibrary`."""

    name = "blender"
    description = (
        "Appends Blender-native character libraries (.blend) and binds armature "
        "actions, then refines the placement against the scene's floor geometry."
    )

    def __init__(self, config=None, *, library: CharacterLibrary | None = None, logger=None):
        super().__init__(config, logger=logger)
        self.library = library or CharacterLibrary([], [])
        self._placements: "dict[str, CharacterPlacement]" = {}
        if not self.library.characters:
            self.messages.append("character library contains no usable characters")
        for warning in self.library.warnings:
            if warning not in self.messages:
                self.messages.append(warning)

    # -- capability ------------------------------------------------------
    def status(self) -> str:
        if not self.library.characters:
            return STATUS_UNAVAILABLE
        if self.library.warnings:
            return STATUS_DEGRADED
        return STATUS_AVAILABLE

    def availability(self) -> dict:
        payload = super().availability()
        payload["manifest_path"] = self.library.manifest_path
        payload["character_count"] = len(self.library.characters)
        payload["animation_count"] = len(self.library.animations)
        return payload

    # -- catalogue -------------------------------------------------------
    def list_characters(self) -> "list[CharacterDescriptor]":
        return list(self.library.characters)

    def list_animations(self) -> "list[AnimationDescriptor]":
        return list(self.library.animations)

    # -- import ----------------------------------------------------------
    def import_character(self, scene_context, character_config) -> CharacterPlacement:
        descriptor = self._descriptor(character_config)
        if descriptor is None:
            return CharacterPlacement(
                descriptor_id=_id_of(character_config),
                status=STATUS_UNAVAILABLE,
                placement_method="none",
                errors=[f"character {_id_of(character_config)!r} is not in the library"],
            )
        if not descriptor.blend_path:
            return CharacterPlacement(
                descriptor_id=descriptor.id,
                status=STATUS_UNAVAILABLE,
                placement_method="none",
                errors=[f"character {descriptor.id!r} has no blend_path in the manifest"],
            )
        if not os.path.isfile(descriptor.blend_path):
            return CharacterPlacement(
                descriptor_id=descriptor.id,
                status=STATUS_UNAVAILABLE,
                placement_method="none",
                errors=[f"character blend file not found: {descriptor.blend_path}"],
            )

        import bpy  # noqa: F401

        before = _take_snapshot()
        import_error = self._append(descriptor)
        after = _take_snapshot()
        new_objects = sorted(after.objects - before.objects)
        new_collections = sorted(after.collections - before.collections)
        new_actions = sorted(after.actions - before.actions)

        placement = CharacterPlacement(
            descriptor_id=descriptor.id,
            status=STATUS_AVAILABLE if new_objects else STATUS_UNAVAILABLE,
            imported_objects=new_objects,
            placement_method="bpy.ops.wm.append",
            details={
                "blend_path": to_forward_slashes(descriptor.blend_path),
                "new_collections": new_collections,
                "new_actions": new_actions,
                "scale": descriptor.scale,
                "offset": list(descriptor.offset),
                "rotation_euler_deg": list(descriptor.rotation_euler_deg),
            },
        )
        if import_error:
            placement.errors.append(import_error)
        if not new_objects:
            placement.status = STATUS_UNAVAILABLE
            placement.errors.append(
                "import reported success but no new objects appeared in the file"
            )
            return placement

        root = self._pick_root(descriptor, new_objects)
        if root is None:
            placement.status = STATUS_DEGRADED
            placement.errors.append(
                "no usable root object found among the imported objects: "
                + ", ".join(new_objects[:10])
            )
            return placement
        placement.object_name = root
        self._placements[descriptor.id] = placement
        self._apply_offsets(descriptor, placement)
        if self.logger is not None:
            self.logger.info(
                "imported character %s as %r (%d object(s))",
                descriptor.id, root, len(new_objects),
            )
        return placement

    def _append(self, descriptor: CharacterDescriptor) -> str:
        """Append the character's objects.  Returns an error string or ``''``."""
        import bpy

        candidates = []
        if descriptor.collection:
            candidates.append(f"{descriptor.collection}")
        if descriptor.object_name:
            candidates.append(f"Object/{descriptor.object_name}")
        # "whole file" append catches everything else (collections, hidden rigs).
        targets = list(candidates) + [""]

        errors = []
        for target in targets:
            try:
                if target:
                    bpy.ops.wm.append(
                        filepath=descriptor.blend_path,
                        directory=f"{descriptor.blend_path}/",
                        filename=target,
                    )
                else:
                    bpy.ops.wm.append(filepath=descriptor.blend_path)
            except Exception as exc:  # Blender raises RuntimeError for most issues
                errors.append(f"{target or '<whole file>'}: {exc}")
                continue
            return ""
        return "append failed for every candidate path -> " + "; ".join(errors)

    def _pick_root(self, descriptor: CharacterDescriptor, new_objects) -> "str | None":
        import bpy

        named = bpy.data.objects.get(descriptor.object_name) if descriptor.object_name else None
        if named is not None and named.name in new_objects:
            return named.name
        objects = [bpy.data.objects[name] for name in new_objects if name in bpy.data.objects]
        if not objects:
            return None
        # Prefer an armature: it carries the animation and usually the whole rig.
        armatures = [obj for obj in objects if obj.type == "ARMATURE"]
        if armatures:
            return sorted(armatures, key=lambda o: -len(o.children))[0].name
        meshes = [obj for obj in objects if obj.type == "MESH"]
        if meshes:
            return sorted(meshes, key=lambda o: -len(o.data.vertices))[0].name
        return sorted(objects, key=lambda o: o.name)[0].name

    def _apply_offsets(self, descriptor: CharacterDescriptor, placement: CharacterPlacement) -> None:
        import bpy
        from mathutils import Euler

        root = bpy.data.objects.get(placement.object_name)
        if root is None:
            return
        if descriptor.rotation_euler_deg and any(descriptor.rotation_euler_deg):
            root.rotation_mode = "XYZ"
            root.rotation_euler = Euler(
                [math.radians(float(v)) for v in descriptor.rotation_euler_deg], "XYZ"
            )
        if descriptor.scale and descriptor.scale != 1.0:
            root.scale = (descriptor.scale, descriptor.scale, descriptor.scale)
        if descriptor.offset and any(descriptor.offset):
            root.location = (
                root.location[0] + descriptor.offset[0],
                root.location[1] + descriptor.offset[1],
                root.location[2] + descriptor.offset[2],
            )
        bpy.context.view_layer.update()

    # -- placement -------------------------------------------------------
    def place_character(self, placement: CharacterPlacement, scene_context) -> CharacterPlacement:
        import bpy

        if not placement.object_name:
            return placement
        root = bpy.data.objects.get(placement.object_name)
        if root is None:
            placement.status = STATUS_UNAVAILABLE
            placement.errors.append(f"root object {placement.object_name!r} disappeared")
            return placement

        bpy.context.view_layer.update()
        bounds = self._bounds(root)
        if bounds is None:
            placement.status = STATUS_DEGRADED
            placement.messages.append("could not compute a bounding box for the character")
            return placement
        lo, hi = bounds

        offset = (0.0, 0.0, 0.0)
        methods = []
        if scene_context.world_bbox_min and scene_context.world_bbox_max:
            scene_center = tuple(
                (scene_context.world_bbox_min[i] + scene_context.world_bbox_max[i]) * 0.5
                for i in range(3)
            )
            # Keep the character near the middle of the scene footprint; the
            # original asset could sit anywhere in its own file.
            current = ((lo[0] + hi[0]) * 0.5, (lo[1] + hi[1]) * 0.5)
            offset = (scene_center[0] - current[0], scene_center[1] - current[1], 0.0)
            if abs(offset[0]) > 1e-6 or abs(offset[1]) > 1e-6:
                root.location = (
                    root.location[0] + offset[0],
                    root.location[1] + offset[1],
                    root.location[2] + offset[2],
                )
                methods.append(f"centred at scene footprint ({offset[0]:+.3f}, {offset[1]:+.3f})")
                bpy.context.view_layer.update()
                bounds = self._bounds(root) or bounds
                lo, hi = bounds

        ground = self._snap_to_floor(root, lo, hi, scene_context)
        if ground is not None:
            methods.append(f"snapped to floor at z={ground:.4f}")
        bpy.context.view_layer.update()

        final = self._bounds(root)
        if final is not None:
            placement.bbox_min, placement.bbox_max = final
        placement.placement_method = "; ".join(methods) or "kept original asset transform"
        placement.details["placement_offset"] = [round(float(v), 6) for v in offset]
        return placement

    def _snap_to_floor(self, root, lo, hi, scene_context) -> "float | None":
        """Drop the character onto the dominant floor plane inside the scene.

        Rays are traced downward on a small grid over the character's footprint;
        the median hit height is used instead of the minimum, so a stray prop
        below the floor does not sink the character.
        """
        import bpy

        caster = scene_context.ray_caster
        if caster is None:
            return None
        heights = []
        samples = 5
        z_start = hi[2] + max(1.0, (hi[2] - lo[2]))
        for ix in range(samples):
            for iy in range(samples):
                tx = ix / float(max(1, samples - 1))
                ty = iy / float(max(1, samples - 1))
                x = lo[0] + (hi[0] - lo[0]) * tx
                y = lo[1] + (hi[1] - lo[1]) * ty
                hit, distance = caster.cast((x, y, z_start), (0.0, 0.0, -1.0))
                if hit:
                    heights.append(z_start - distance)
        if not heights:
            return None
        if len(heights) < 3:
            target = min(heights)
        else:
            heights.sort()
            target = heights[len(heights) // 2]
        delta = target - lo[2]
        if abs(delta) < 1e-4:
            return target
        root.location = (root.location[0], root.location[1], root.location[2] + delta)
        bpy.context.view_layer.update()
        return target

    # -- animation -------------------------------------------------------
    def apply_animation(self, placement: CharacterPlacement, animation_config) -> CharacterPlacement:
        import bpy

        if not placement.object_name:
            return placement
        descriptor = self._animation(animation_config)
        if descriptor is None:
            placement.messages.append(
                f"animation {_id_of(animation_config)!r} is not in the library; "
                "the character keeps its imported rest pose"
            )
            return placement

        root = bpy.data.objects.get(placement.object_name)
        if root is None:
            placement.errors.append(f"root object {placement.object_name!r} disappeared")
            return placement

        targets = [root] + [child for child in _descendants(root) if child.type == "ARMATURE"]
        armature = next((obj for obj in targets if obj.type == "ARMATURE"), None)
        target = armature or root

        if descriptor.action_name and descriptor.blend_path and not _action_exists(descriptor.action_name):
            self._append_action_library(descriptor)
        action = bpy.data.actions.get(descriptor.action_name)
        if action is None:
            placement.messages.append(
                f"action {descriptor.action_name!r} is not present in the file after import"
            )
            return placement
        if not _assign_action(target, action):
            placement.errors.append(
                f"could not bind action {action.name!r} to {target.name!r}"
            )
            return placement

        placement.animation = descriptor.id
        frame_start = int(descriptor.frame_start or action.frame_range[0])
        frame_end = int(descriptor.frame_end or action.frame_range[1])
        if frame_end <= frame_start:
            frame_end = frame_start + 1
        placement.animation_frame_range = (frame_start, frame_end)
        placement.details["action_name"] = action.name
        placement.details["action_frame_range"] = [float(v) for v in action.frame_range]
        placement.details["animated_object"] = target.name
        if self.logger is not None:
            self.logger.info(
                "bound action %s to %s (frames %d..%d)",
                action.name, target.name, frame_start, frame_end,
            )
        return placement

    def _append_action_library(self, descriptor: AnimationDescriptor) -> None:
        import bpy

        if not descriptor.blend_path or not os.path.isfile(descriptor.blend_path):
            return
        try:
            bpy.ops.wm.append(
                filepath=descriptor.blend_path,
                directory=f"{descriptor.blend_path}/",
                filename=f"Action/{descriptor.action_name}",
            )
        except Exception:
            try:
                bpy.ops.wm.append(filepath=descriptor.blend_path)
            except Exception:
                pass

    # -- validation ------------------------------------------------------
    def validate_character_placement(self, placement: CharacterPlacement, scene_context) -> CharacterValidation:
        validation = CharacterValidation()
        if not placement.object_name or placement.bbox_min is None or placement.bbox_max is None:
            validation.valid = False
            validation.messages.append("nothing to validate: the character was not placed")
            return validation

        lo, hi = placement.bbox_min, placement.bbox_max
        size = tuple(hi[i] - lo[i] for i in range(3))
        validation.metrics["bbox_size"] = [round(float(v), 6) for v in size]
        validation.metrics["bbox_volume"] = round(float(size[0] * size[1] * size[2]), 6)
        if max(size) <= 1e-6:
            validation.valid = False
            validation.messages.append("character bounding box is degenerate (zero size)")
            return validation
        if max(size) > 100.0:
            validation.messages.append(
                f"character bounding box is very large (max extent {max(size):.2f}); "
                "check the manifest scale"
            )
            validation.valid = False

        if scene_context.world_bbox_min and scene_context.world_bbox_max:
            scene_lo, scene_hi = scene_context.world_bbox_min, scene_context.world_bbox_max
            margin = max(0.1, 0.02 * max(scene_hi[i] - scene_lo[i] for i in range(3)))
            inside = all(
                lo[i] >= scene_lo[i] - margin and hi[i] <= scene_hi[i] + margin
                for i in range(3)
            )
            validation.inside_scene_bounds = inside
            if not inside:
                validation.messages.append(
                    "character extends outside the scene bounding box; it may leave the frame"
                )
                validation.valid = False

        if scene_context.ray_caster is not None:
            overlap = _box_overlap_ratio(placement, scene_context)
            validation.overlap = overlap > 0.5
            validation.metrics["mesh_overlap_ratio"] = round(float(overlap), 6)
            if validation.overlap:
                validation.messages.append(
                    f"{overlap * 100:.0f}% of the character's probe points are enclosed by scene "
                    "geometry; it is probably inside a wall or prop"
                )
                validation.valid = False

        ground = _ground_offset(scene_context, lo)
        if ground is not None:
            validation.grounded = abs(ground) <= max(0.05, 0.05 * max(1e-6, size[2]))
            validation.metrics["ground_offset"] = round(float(ground), 6)
            if not validation.grounded:
                validation.messages.append(
                    f"character base is {ground:+.3f}m away from the floor under it"
                )
        return validation

    # -- helpers ---------------------------------------------------------
    def _descriptor(self, character_config) -> "CharacterDescriptor | None":
        key = _id_of(character_config)
        if not key:
            return self.library.characters[0] if len(self.library.characters) == 1 else None
        return self.resolve_character(key)

    def _animation(self, animation_config) -> "AnimationDescriptor | None":
        key = _id_of(animation_config)
        if not key:
            return None
        return self.resolve_animation(key)

    def _bounds(self, root):
        """World-space AABB over ``root`` and all descendants."""
        lo = [math.inf] * 3
        hi = [-math.inf] * 3
        found = False
        for obj in [root, *_descendants(root)]:
            if obj.type not in _SUPPORTED_OBJECT_TYPES:
                continue
            snapshot = mesh_snapshot(obj, max_points=1)
            if snapshot is None:
                if obj.type != "EMPTY":
                    continue
                # Include empty children (they often mark the character's base).
                location = obj.matrix_world.translation
                co = (float(location[0]), float(location[1]), float(location[2]))
                for axis in range(3):
                    lo[axis] = min(lo[axis], co[axis])
                    hi[axis] = max(hi[axis], co[axis])
                found = True
                continue
            for axis in range(3):
                lo[axis] = min(lo[axis], snapshot.bbox_min[axis])
                hi[axis] = max(hi[axis], snapshot.bbox_max[axis])
            found = True
        if not found:
            return None
        return (tuple(lo), tuple(hi))


# --------------------------------------------------------------------------
# module level helpers (kept outside the class so they are easy to unit test)
# --------------------------------------------------------------------------
def _descendants(root) -> "list":
    out = []
    stack = list(getattr(root, "children", []) or [])
    seen = set()
    while stack:
        obj = stack.pop()
        if obj.name in seen:
            continue
        seen.add(obj.name)
        out.append(obj)
        stack.extend(getattr(obj, "children", []) or [])
    return out


def _action_exists(name: str) -> bool:
    import bpy

    return bpy.data.actions.get(name) is not None


def _id_of(config) -> str:
    """Extract a character/animation id from whatever the caller passed."""
    if config is None:
        return ""
    if isinstance(config, str):
        return config
    if isinstance(config, dict):
        return str(config.get("id") or config.get("name") or "")
    return str(getattr(config, "id", "") or getattr(config, "name", "") or "")


def _assign_action(target, action) -> bool:
    """Bind ``action`` to ``target``, coping with Blender 4.4+ action slots."""
    from ..utils.animation import assign_action

    return assign_action(target, action)


def _box_overlap_ratio(placement: CharacterPlacement, scene_context: SceneContext, *, probes: int = 27) -> float:
    """Share of character probe points that are enclosed by scene geometry."""
    from ..camera.scene_context import fibonacci_directions

    box = placement.to_character_box()
    if box is None:
        return 0.0
    caster = scene_context.ray_caster
    if caster is None:
        return 0.0

    points = box.probe_points(probes)
    directions = fibonacci_directions(8)
    enclosed = 0
    for point in points:
        surrounded = True
        for direction in directions:
            hit, distance = caster.cast(point, direction)
            if not hit or distance > 0.25:
                surrounded = False
                break
        if surrounded:
            enclosed += 1
    return enclosed / float(len(points)) if points else 0.0


def _ground_offset(scene_context: SceneContext, lo) -> "float | None":
    """Signed distance from the character's base to the floor beneath it."""
    caster = scene_context.ray_caster
    if caster is None:
        return None
    origin = ((lo[0] + lo[0]) * 0.5, (lo[1] + lo[1]) * 0.5, lo[2] + 5.0)
    hit, distance = caster.cast(origin, (0.0, 0.0, -1.0))
    if not hit:
        return None
    floor_z = origin[2] - distance
    return floor_z - lo[2]


def character_box_from_placement(placement: CharacterPlacement) -> "CharacterBox | None":
    return placement.to_character_box()


def describe_library(library: CharacterLibrary) -> str:
    if not library.characters:
        reason = library.warnings[0] if library.warnings else "no characters discovered"
        return f"character library unavailable: {reason}"
    return (
        f"{len(library.characters)} character(s), {len(library.animations)} animation(s) "
        f"from {normalize_path(library.manifest_path)}"
    )
