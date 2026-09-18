"""Isolate why save_as_mainfile fails on a scene with a missing external asset.

    blender -b -P tests/probe_save_flags.py -- "<blend path>" <scratch dir>

Tries the flag combinations the plugin could use and reports which one saves
successfully when an image's file is not on disk.
"""
from __future__ import annotations

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
for path in (_PACKAGE_PARENT, _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import bpy  # noqa: E402

from blender_motion_pipeline.core.scene_loader import SceneEntry, open_scene_for_generation  # noqa: E402


def attempt(label: str, target: str, **flags) -> bool:
    if os.path.isfile(target):
        os.remove(target)
    try:
        bpy.ops.wm.save_as_mainfile(filepath=target, check_existing=False, **flags)
    except Exception as exc:
        print(f"  {label:52s} FAILED: {type(exc).__name__}: {exc}")
        return False
    ok = os.path.isfile(target)
    size = os.path.getsize(target) if ok else 0
    print(f"  {label:52s} ok={ok} size={size / 1024 / 1024:.1f} MB")
    return ok


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    blend = os.path.abspath(argv[0])
    scratch = os.path.abspath(argv[1]) if len(argv) > 1 else os.path.join(
        tempfile.gettempdir(), "mp_save_flags"
    )
    os.makedirs(scratch, exist_ok=True)

    load = open_scene_for_generation(SceneEntry(path=blend))
    if not load.ok:
        print("open failed:", load.error)
        return 1

    missing = [image for image in bpy.data.images
               if image.filepath and not image.packed_file
               and not os.path.exists(bpy.path.abspath(image.filepath))]
    print(f"images with a missing file on disk: {len(missing)}")
    for image in missing[:5]:
        print(f"  {image.name}: {bpy.path.abspath(image.filepath)}")

    print("\n--- save attempts ---")
    results = {}
    results["copy=True"] = attempt(
        "copy=True", os.path.join(scratch, "copy.blend"), copy=True)
    results["copy=True, relative_remap=True"] = attempt(
        "copy=True, relative_remap=True", os.path.join(scratch, "copy_remap.blend"),
        copy=True, relative_remap=True)
    results["copy=True, relative_remap=False"] = attempt(
        "copy=True, relative_remap=False", os.path.join(scratch, "copy_noremap.blend"),
        copy=True, relative_remap=False)
    results["compress=False"] = attempt(
        "compress=False", os.path.join(scratch, "plain.blend"), compress=False)
    results["compress=False, relative_remap=False"] = attempt(
        "compress=False, relative_remap=False",
        os.path.join(scratch, "plain_noremap.blend"), compress=False, relative_remap=False)
    # Is the failure caused by *packing*, i.e. is there a way to opt out?
    for flag in ("use_save_preview_images", "use_save_as_copy"):
        if flag in bpy.types.RenderSettings.bl_rna.properties or True:
            results[flag] = attempt(
                f"compress=False, {flag}=False",
                os.path.join(scratch, f"flag_{flag}.blend"),
                compress=False, **{flag: False})

    print("\n--- summary ---")
    for label, ok in results.items():
        print(f"  {'OK    ' if ok else 'FAILED'} {label}")
    winners = [label for label, ok in results.items() if ok]
    print(f"\nworking combination(s): {winners or 'NONE'}")
    return 0 if winners else 1


if __name__ == "__main__":
    sys.exit(main())
