"""Measure where the bytes in a generated sequence ``.blend`` come from.

    blender -b -P tests/probe_blend_size.py -- "<file.blend>" [more files ...]

A sequence file is a **complete copy of the scene** plus the generated camera
action, so its size is dominated by geometry, materials and packed textures --
not by the animation.  For every file this prints:

* the size on disk, and what it holds (objects, meshes, polygons, images, actions);
* how many bytes of *packed textures* it carries;
* the size the same datablocks take saved with compression on and off;
* the size of a **camera-only** copy (everything else deleted), which is the
  lower bound for "store only the animation, re-link the scene at render time".

Nothing is written outside the system temp folder, and the copies are removed
again before the probe returns.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
for path in (_PACKAGE_PARENT, _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import bpy  # noqa: E402


def mb(value: float) -> float:
    return round(float(value) / (1024 * 1024), 1)


def scene_stats(label: str) -> dict:
    objects = list(bpy.data.objects)
    meshes = list(bpy.data.meshes)
    polygons = sum(len(mesh.polygons) for mesh in meshes)
    vertices = sum(len(mesh.vertices) for mesh in meshes)
    packed = 0
    packed_count = 0
    for image in bpy.data.images:
        handle = getattr(image, "packed_file", None)
        if handle is not None:
            packed += int(getattr(handle, "size", 0) or 0)
            packed_count += 1
    return {
        "label": label,
        "objects": len(objects),
        "meshes": len(meshes),
        "polygons": polygons,
        "vertices": vertices,
        "materials": len(bpy.data.materials),
        "images": len(bpy.data.images),
        "packed_images": packed_count,
        "packed_mb": mb(packed),
        "actions": len(bpy.data.actions),
        "autopack": bool(getattr(bpy.data, "use_autopack", False)),
    }


def save_copy(target: str, *, compress: bool) -> int:
    # ``use_autopack`` is a per-file setting and this scene has it on; leaving it
    # on makes every save try to pack each external file and abort on the one
    # whose source path is gone.  Same workaround the generator uses.
    before = bool(getattr(bpy.data, "use_autopack", False))
    try:
        if before:
            bpy.data.use_autopack = False
        bpy.ops.wm.save_as_mainfile(
            filepath=target, check_existing=False, copy=True, compress=compress
        )
    finally:
        if before:
            bpy.data.use_autopack = True
    return os.path.getsize(target)


def camera_only(target: str, *, compress: bool) -> "tuple[int, int]":
    """Delete everything but the camera (and its data), then save."""
    camera = bpy.context.scene.camera
    keep = {camera.name} if camera is not None else set()
    if camera is not None and camera.data is not None:
        keep.add(camera.data.name)
    removed = 0
    for obj in list(bpy.data.objects):
        if obj.name in keep:
            continue
        bpy.data.objects.remove(obj, do_unlink=True)
        removed += 1
    for _ in range(4):
        try:
            bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
        except Exception:
            break
    size = save_copy(target, compress=compress)
    return removed, size


def animation_only(target: str, *, compress: bool) -> "tuple[int, int]":
    """Drop the world as well, so the packed textures go too.

    What is left is the camera plus its *action* -- but also every other datablock
    that survives a purge (fake-user actions, node groups, texts).  Returns
    ``(size, actions_left)``.
    """
    bpy.context.scene.world = None
    for _ in range(4):
        try:
            bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
        except Exception:
            break
    return save_copy(target, compress=compress), len(bpy.data.actions)


def camera_action_only(target: str, *, compress: bool) -> "tuple[int, int]":
    """Keep only the action that animates the camera: the true payload size."""
    camera = bpy.context.scene.camera
    wanted = set()
    for owner in (camera, getattr(camera, "data", None)):
        animation_data = getattr(owner, "animation_data", None) if owner is not None else None
        action = getattr(animation_data, "action", None) if animation_data is not None else None
        if action is not None:
            wanted.add(action.name)
    for action in list(bpy.data.actions):
        if action.name not in wanted:
            bpy.data.actions.remove(action)
    for _ in range(4):
        try:
            bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
        except Exception:
            break
    leftovers = {
        "objects": len(bpy.data.objects),
        "meshes": len(bpy.data.meshes),
        "materials": len(bpy.data.materials),
        "images": len(bpy.data.images),
        "packed_mb": mb(sum(
            int(getattr(image.packed_file, "size", 0) or 0)
            for image in bpy.data.images
            if getattr(image, "packed_file", None) is not None
        )),
        "node_groups": len(bpy.data.node_groups),
        "texts": len(bpy.data.texts),
        "actions": len(bpy.data.actions),
        "collections": len(bpy.data.collections),
        "worlds": len(bpy.data.worlds),
    }
    return save_copy(target, compress=compress), leftovers


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        print("usage: blender -b -P tests/probe_blend_size.py -- <file.blend> [...]")
        return 2

    scratch = os.path.join(tempfile.gettempdir(), "mpp_blend_size")
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch, exist_ok=True)
    rows = []
    try:
        for index, path in enumerate(argv, start=1):
            path = os.path.abspath(path)
            if not os.path.isfile(path):
                print(f"missing: {path}")
                continue
            on_disk = os.path.getsize(path)
            bpy.ops.wm.open_mainfile(filepath=path)
            stats = scene_stats(os.path.basename(path))
            stats["on_disk_mb"] = mb(on_disk)
            stats["uncompressed_mb"] = mb(save_copy(os.path.join(scratch, f"u{index}.blend"), compress=False))
            stats["compressed_mb"] = mb(save_copy(os.path.join(scratch, f"c{index}.blend"), compress=True))
            removed, plain = camera_only(os.path.join(scratch, f"cam_u{index}.blend"), compress=False)
            _removed, packed = camera_only(os.path.join(scratch, f"cam_c{index}.blend"), compress=True)
            stats["objects_removed"] = removed
            stats["camera_only_mb"] = mb(plain)
            stats["camera_only_compressed_mb"] = mb(packed)
            if os.environ.get("MP_SIZE_PROBE_ANIMATION_ONLY"):
                anim_size, actions_left = animation_only(
                    os.path.join(scratch, f"anim{index}.blend"), compress=True
                )
                stats["animation_only_kb"] = round(anim_size / 1024, 1)
                stats["actions_left"] = actions_left
                pure_size, pure_left = camera_action_only(
                    os.path.join(scratch, f"pure{index}.blend"), compress=True
                )
                stats["camera_action_only_kb"] = round(pure_size / 1024, 1)
                stats["actions_kept"] = pure_left
            rows.append(stats)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    for stats in rows:
        print()
        print(f"== {stats['label']}")
        print(f"   on disk                     : {stats['on_disk_mb']} MB")
        print(f"   objects/meshes/polygons     : {stats['objects']} / {stats['meshes']} / {stats['polygons']:,}")
        print(f"   vertices                    : {stats['vertices']:,}")
        print(f"   materials / images          : {stats['materials']} / {stats['images']}"
              f" (packed: {stats['packed_images']}, {stats['packed_mb']} MB)")
        print(f"   actions                     : {stats['actions']}   use_autopack={stats['autopack']}")
        print(f"   re-saved uncompressed       : {stats['uncompressed_mb']} MB")
        print(f"   re-saved compressed         : {stats['compressed_mb']} MB")
        print(f"   camera-only, {stats['objects_removed']} object(s) deleted:")
        print(f"       uncompressed            : {stats['camera_only_mb']} MB")
        print(f"       compressed              : {stats['camera_only_compressed_mb']} MB")
        if "animation_only_kb" in stats:
            print(f"   camera + everything that survives a purge: {stats['animation_only_kb']} KB "
                  f"({stats['actions_left']} action(s) left)")
            print(f"   camera + only its own action            : {stats['camera_action_only_kb']} KB")
            print(f"       what is left in the file            : {stats['actions_kept']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
