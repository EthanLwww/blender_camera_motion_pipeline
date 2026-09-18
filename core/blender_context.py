"""Bridge between a loaded Blender file and the UI-free camera modules.

Responsibilities:

* detect the cameras that exist in a file and snapshot their *original*
  parameters before anything is keyframed;
* harvest a world-space geometry description for the validators;
* assemble :class:`~blender_motion_pipeline.camera.scene_context.SceneContext`,
  including the ray caster used for clipping/occlusion tests.

Meshes belonging to placed characters are excluded from the geometry snapshot
and from the ray caster, because a camera is not "clipped" by the person it is
filming and a character must not shadow the wall behind it.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..camera.scene_context import (
    BBoxRayCaster,
    BlenderRayCaster,
    CameraSnapshot,
    CharacterBox,
    MeshSnapshot,
    SceneContext,
    mesh_snapshot,
    union_boxes,
)
from ..io.path_utils import normalize_path, to_forward_slashes
from .scene_loader import current_blend_path

#: Object types that are never part of the "geometry" a camera can hit.
_NON_GEOMETRY_TYPES = {
    "CAMERA", "LIGHT", "LIGHT_PROBE", "SPEAKER", "EMPTY", "ARMATURE",
    "FONT", "GPENCIL", "GREASEPENCIL", "VOLUME",
}


def list_camera_objects(scene=None, *, include_hidden: bool = True) -> "list":
    """Camera objects in ``scene`` (defaults to the active scene)."""
    import bpy

    scene = scene or bpy.context.scene
    cameras = []
    for obj in scene.objects:
        if obj.type != "CAMERA":
            continue
        if not include_hidden and (obj.hide_viewport or obj.hide_render):
            continue
        cameras.append(obj)
    return sorted(cameras, key=lambda obj: obj.name)


def _animated_properties(obj) -> "list[str]":
    """Names of the properties that carry animation on ``obj``."""
    from ..utils.animation import action_fcurves, actions_for

    properties: "list[str]" = []
    for action in actions_for(obj):
        for curve in action_fcurves(action):
            path = curve.data_path
            if path.startswith('["') or path.startswith("["):
                entry = f"custom:{path}"
            else:
                entry = path.split(".")[-1]
                if path.startswith("data."):
                    entry = f"data.{entry}"
            if entry not in properties:
                properties.append(entry)
    # Shape keys / constraints can animate a camera without an f-curve here.
    for constraint in getattr(obj, "constraints", None) or []:
        if getattr(constraint, "mute", False):
            continue
        entry = f"constraint:{constraint.type}"
        if entry not in properties:
            properties.append(entry)
    return properties


def camera_snapshot(obj, scene=None) -> CameraSnapshot:
    """Capture everything needed to restore/animate a camera faithfully."""
    import bpy

    from ..utils.animation import action_data_paths

    scene = scene or bpy.context.scene
    data = obj.data
    matrix = [[float(value) for value in row] for row in obj.matrix_world]
    rotation_quaternion = tuple(float(v) for v in obj.matrix_world.to_quaternion())
    animated = _animated_properties(obj)
    animation_data = getattr(obj, "animation_data", None)
    data_animation = getattr(data, "animation_data", None)
    if animation_data is not None:
        action = getattr(animation_data, "action", None)
        if action is not None and any(
            path.endswith("lens") for path in action_data_paths(action)
        ) and "data.lens" not in animated:
            animated.append("data.lens")
    return CameraSnapshot(
        name=obj.name,
        object_name=obj.name,
        matrix_world=matrix,
        location=(float(obj.matrix_world.translation[0]),
                  float(obj.matrix_world.translation[1]),
                  float(obj.matrix_world.translation[2])),
        rotation_mode=str(obj.rotation_mode),
        rotation_euler=tuple(float(v) for v in obj.rotation_euler),
        rotation_quaternion=rotation_quaternion,
        scale=tuple(float(v) for v in obj.scale),
        lens=float(data.lens),
        sensor_width=float(data.sensor_width),
        sensor_height=float(data.sensor_height),
        sensor_fit=str(data.sensor_fit),
        clip_start=float(data.clip_start),
        clip_end=float(data.clip_end),
        dof_enabled=bool(getattr(data.dof, "use_dof", False)),
        focus_distance=float(getattr(data.dof, "focus_distance", 0.0)),
        aperture_fstop=float(getattr(data.dof, "aperture_fstop", 0.0)),
        shift_x=float(getattr(data, "shift_x", 0.0)),
        shift_y=float(getattr(data, "shift_y", 0.0)),
        resolution_x=int(scene.render.resolution_x),
        resolution_y=int(scene.render.resolution_y),
        resolution_percentage=int(scene.render.resolution_percentage),
        fps=float(scene.render.fps) / float(scene.render.fps_base or 1.0),
        had_animation=bool(animated),
        animated_properties=animated,
        data_users=int(data.users),
        action=getattr(animation_data, "action", None),
        data_action=getattr(data_animation, "action", None),
    )


def restore_camera(obj, snapshot: CameraSnapshot) -> None:
    """Put ``obj`` back to its original transform, lens settings and animation.

    The transform is restored by assigning the snapshot's **world** matrix and
    letting Blender derive the local one.  Writing ``snapshot.location`` straight
    into ``obj.location`` looks equivalent and is not: ``location`` is expressed in
    the object's parent space, so for a parented camera it re-interprets a world
    position as a local offset.  On the reference scene (camera parented to an
    animated train empty) that displaced the camera by 25.96 m on *every* restore,
    so each sequence in a batch was anchored further along the train's path than
    the one before it and the same template produced a different shot depending on
    where it sat in the batch.

    The artist's actions are re-attached last, so the animation wins over the
    transform on the next evaluation, exactly as it did before generation.
    """
    from ..utils.animation import assign_action

    obj.rotation_mode = snapshot.rotation_mode
    restored = False
    try:
        from mathutils import Matrix
        import bpy

        bpy.context.view_layer.update()
        obj.matrix_world = Matrix([list(row) for row in snapshot.matrix_world])
        restored = True
    except Exception:
        restored = False
    if not restored:
        # Fallback for an unusable matrix (or a restricted context).
        obj.location = snapshot.location
        if snapshot.rotation_mode == "QUATERNION":
            obj.rotation_quaternion = snapshot.rotation_quaternion
        else:
            obj.rotation_euler = snapshot.rotation_euler
        obj.scale = snapshot.scale
    data = obj.data
    data.lens = snapshot.lens
    data.sensor_width = snapshot.sensor_width
    data.sensor_height = snapshot.sensor_height
    data.sensor_fit = snapshot.sensor_fit
    data.clip_start = snapshot.clip_start
    data.clip_end = snapshot.clip_end
    if hasattr(data, "shift_x"):
        data.shift_x = snapshot.shift_x
        data.shift_y = snapshot.shift_y
    if getattr(snapshot, "action", None) is not None:
        assign_action(obj, snapshot.action)
    if getattr(snapshot, "data_action", None) is not None:
        assign_action(data, snapshot.data_action)


def clear_camera_animation(obj) -> int:
    """Remove animation from ``obj`` **and its data**; returns actions dropped."""
    from ..utils.animation import clear_animation

    removed = clear_animation(obj)
    removed += clear_animation(getattr(obj, "data", None))
    return removed


def mesh_snapshot_safe(obj, *, max_points: int = 0):
    """Evaluated world-space snapshot of one mesh, or ``None`` on any failure."""
    import bpy

    if obj is None or obj.type != "MESH":
        return None
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        return mesh_snapshot(evaluated, matrix=evaluated.matrix_world, max_points=max_points)
    except Exception:
        try:
            return mesh_snapshot(obj, max_points=max_points)
        except Exception:
            return None


def harvest_meshes(
    scene=None,
    *,
    exclude: Iterable[str] = (),
    max_points_per_mesh: int = 0,
    include_hidden: bool = False,
) -> "list[MeshSnapshot]":
    """World-space snapshots of every renderable mesh in ``scene``."""
    import bpy

    scene = scene or bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    excluded = {str(name) for name in exclude}
    snapshots: "list[MeshSnapshot]" = []
    for obj in scene.objects:
        if obj.type != "MESH" or obj.name in excluded:
            continue
        if not include_hidden and (obj.hide_render or not obj.visible_get()):
            # Hidden geometry cannot clip the camera in the rendered image, but
            # it is still listed by the caller if it asks for it explicitly.
            continue
        try:
            snapshot = mesh_snapshot(
                obj, depsgraph=depsgraph, max_points=max_points_per_mesh,
                matrix=obj.evaluated_get(depsgraph).matrix_world,
            )
        except Exception:
            snapshot = mesh_snapshot(obj, max_points=max_points_per_mesh)
        if snapshot is not None:
            snapshots.append(snapshot)
    return snapshots


def world_bounds(meshes: Sequence[MeshSnapshot]) -> "tuple[tuple | None, tuple | None]":
    union = union_boxes(meshes)
    if union is None:
        return (None, None)
    return (union.bbox_min, union.bbox_max)


def build_scene_context(
    *,
    scene=None,
    exclude_objects: Iterable[str] = (),
    characters: Sequence[CharacterBox] = (),
    cameras: Sequence[CameraSnapshot] | None = None,
    blend_path: str = "",
    max_points_per_mesh: int = 0,
    ray_caster: str = "auto",
    logger=None,
) -> SceneContext:
    """Assemble the :class:`SceneContext` the validators operate on."""
    import bpy

    scene = scene or bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    excluded = {str(name) for name in exclude_objects}
    meshes = harvest_meshes(scene, exclude=excluded, max_points_per_mesh=max_points_per_mesh)
    lo, hi = world_bounds(meshes)

    warnings: "list[str]" = []
    if not meshes:
        warnings.append(
            "the scene contains no visible mesh geometry; clipping and occlusion "
            "checks cannot do anything meaningful"
        )

    caster = None
    if ray_caster in ("auto", "bpy"):
        try:
            caster = BlenderRayCaster(scene, depsgraph=depsgraph, exclude=excluded)
        except Exception as exc:
            warnings.append(f"scene.ray_cast is unavailable ({exc}); falling back to AABB tests")
            caster = None
    if caster is None and meshes:
        caster = BBoxRayCaster(meshes)

    camera_snapshots = list(cameras) if cameras is not None else [
        camera_snapshot(obj, scene) for obj in list_camera_objects(scene)
    ]

    context = SceneContext(
        scene=scene,
        depsgraph=depsgraph,
        blend_path=normalize_path(blend_path) if blend_path else current_blend_path(),
        scene_name=scene.name,
        cameras=camera_snapshots,
        meshes=meshes,
        characters=list(characters),
        ray_caster=caster,
        world_bbox_min=lo,
        world_bbox_max=hi,
        warnings=warnings,
    )
    if logger is not None:
        for warning in warnings:
            logger.warning("scene context: %s", warning)
        logger.debug(
            "scene context: %d camera(s), %d mesh(es), %d character(s), caster=%s",
            len(context.cameras), len(context.meshes), len(context.characters),
            getattr(caster, "description", "none"),
        )
    return context


def scene_report(
    scene=None,
    *,
    cameras: Sequence[CameraSnapshot] | None = None,
    meshes: Sequence[MeshSnapshot] | None = None,
) -> dict:
    """Structured summary used by the "Validate scenes" action."""
    import bpy

    scene = scene or bpy.context.scene
    camera_objects = list_camera_objects(scene)
    snapshots = list(cameras) if cameras is not None else [camera_snapshot(o, scene) for o in camera_objects]
    mesh_list = list(meshes) if meshes is not None else harvest_meshes(scene)
    lo, hi = world_bounds(mesh_list)
    total_frames = int(scene.frame_end) - int(scene.frame_start) + 1
    return {
        "scene_name": scene.name,
        "blend_path": to_forward_slashes(current_blend_path()),
        "frame_range": [int(scene.frame_start), int(scene.frame_end)],
        "frame_count": total_frames,
        "fps": float(scene.render.fps) / float(scene.render.fps_base or 1.0),
        "resolution": [int(scene.render.resolution_x), int(scene.render.resolution_y)],
        "resolution_percentage": int(scene.render.resolution_percentage),
        "engine": str(scene.render.engine),
        "camera_count": len(snapshots),
        "cameras": [c.to_dict() for c in snapshots],
        "camera_names": [c.name for c in snapshots],
        "mesh_count": len(mesh_list),
        "mesh_names": [m.name for m in mesh_list],
        "world_bbox": (
            {"min": [float(v) for v in lo], "max": [float(v) for v in hi]} if lo and hi else None
        ),
        "world_diagonal": (math.dist(lo, hi) if lo and hi else None),
        "object_count": len(scene.objects),
        "collection_count": len(bpy.data.collections),
        "has_character_rig": any(obj.type == "ARMATURE" for obj in scene.objects),
        "scene_count": len(bpy.data.scenes),
        "other_scenes": [s.name for s in bpy.data.scenes if s is not scene],
    }


@dataclass
class OriginalCameraState:
    """Bookkeeping so a batch run can restore the artist's file exactly."""

    snapshots: "dict[str, CameraSnapshot]" = field(default_factory=dict)
    cleared_animation: "dict[str, int]" = field(default_factory=dict)

    @classmethod
    def capture(cls, scene=None) -> "OriginalCameraState":
        state = cls()
        for obj in list_camera_objects(scene):
            state.snapshots[obj.name] = camera_snapshot(obj, scene)
        return state

    def restore(self, *, scene=None) -> int:
        """Restore transforms and drop any pipeline-created animation."""
        import bpy

        scene = scene or bpy.context.scene
        restored = 0
        for name, snapshot in self.snapshots.items():
            obj = bpy.data.objects.get(name)
            if obj is None:
                continue
            removed = clear_camera_animation(obj)
            if removed:
                self.cleared_animation[name] = removed
            restore_camera(obj, snapshot)
            restored += 1
        if restored:
            bpy.context.view_layer.update()
        return restored

    def to_dict(self) -> dict:
        return {
            "captured_cameras": sorted(self.snapshots),
            "cleared_animation_curves": dict(self.cleared_animation),
        }


def scene_camera_names(scene=None) -> "list[str]":
    return [obj.name for obj in list_camera_objects(scene)]


def pick_scene(scene_name: str):
    """Return a scene by name, or the active scene when the name is empty."""
    import bpy

    if not scene_name:
        return bpy.context.scene
    scene = bpy.data.scenes.get(scene_name)
    if scene is None:
        raise KeyError(f"scene {scene_name!r} is not present in the loaded file")
    return scene


def ensure_object_mode() -> None:
    """Best-effort mode reset so operators do not fail in edit/pose mode."""
    import bpy

    try:
        if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        pass


def blend_file_label(path: str = "") -> str:
    target = path or current_blend_path()
    if not target:
        return "(unsaved file)"
    return f"{os.path.basename(target)}"
