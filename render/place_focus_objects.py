"""Place every focus model at the scene's anchor, inside a staged scene copy.

    blender -b -noaudio <scene copy> -P render/place_focus_objects.py -- \
        --scene <scene copy> --job <focus_job.json> --report <focus_report.json>

Run as a throwaway Blender process next to ``pack_textures.py`` -- a generation run has
the artist's scene open, so the staged copy is prepared out of process and saved back.

Why the objects are baked into the **copy** rather than imported per sequence: a
sequence ships the camera animation and the renderer replays it onto the scene copy
beside it, so an object that is not inside that copy cannot be in the picture.  One
placement, saved once, is then what both the generator (which measures the orbit
radius from it) and the render node (which shows it) use -- the arc the generator
validated is the arc the renderer films.

Every placed object is left **visible** and its name recorded in the scene
(``mpp_focus_objects``).  One copy carries one subject -- the staging step makes a copy
per focus model -- so "visible" is what the shot wants; a renderer that predates this
feature therefore still films the right thing.  The generator and the renderer switch
the registered objects per sequence, which is what keeps a copy with more than one
object (or a sequence with no subject at all) honest.

The job file (written by ``core/project.place_focus_models``):

.. code-block:: json

    {"scene": "...", "anchor": {"mode": "auto", "object": "", "location": [0,0,0],
                                "clearance": 0.5},
     "models": [{"id": "chair", "path": "chair.blend", "object_name": "",
                 "scale": 1.0, "rotation": [0, 0, 0], "label": "chair.blend"}]}
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = os.path.dirname(HERE)
for candidate in (os.path.dirname(PACKAGE), PACKAGE):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)


def _bootstrap() -> None:
    """Import the package by path, whatever the folder is called."""
    import importlib.util

    path = os.path.join(PACKAGE, "_bootstrap.py")
    if not os.path.isfile(path):
        return
    spec = importlib.util.spec_from_file_location("_mpp_bootstrap", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.bootstrap(path)


_bootstrap()


def _place_one(scene, model, anchor, index: int) -> dict:
    """Append one model, drop it on the anchor, mark and hide it."""
    import bpy

    from blender_motion_pipeline.core import focus as focus_objects

    record = {"id": model.get("id") or f"model{index + 1:02d}", "path": model.get("path") or "",
              "ok": False, "objects": [], "note": "", "error": ""}
    path = str(model.get("path") or "")
    if not path or not os.path.isfile(path):
        record["error"] = f"model file not found: {path}"
        return record
    identifier = record["id"]
    before = {obj.name for obj in bpy.data.objects}
    target = str(model.get("object_name") or "")
    try:
        # ``bpy.data.libraries.load`` rather than ``bpy.ops.wm.append``: the operator
        # needs a datablock path and a file-select context, and a whole-file append
        # through it fails in a background run ("'': no indication").  The library API
        # loads named objects (or all of them) straight into the file, and the objects
        # it brings in are linked into the scene below -- collections in the model file
        # are deliberately flattened, because a focus object is one placed model.
        with bpy.data.libraries.load(path, link=False) as (source, destination):
            if target:
                available = [name for name in source.objects]
                if target not in available:
                    record["error"] = (
                        f"{target!r} is not in {os.path.basename(path)}; it holds: "
                        + ", ".join(available[:8])
                    )
                    return record
                destination.objects = [target]
            else:
                destination.objects = list(source.objects)
    except Exception as exc:  # noqa: BLE001 - a model that will not load is reported
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record
    new_objects = [obj for obj in bpy.data.objects if obj.name not in before]
    if not new_objects:
        record["error"] = record["error"] or "the model contributed no object to the scene"
        return record
    for obj in new_objects:
        try:
            scene.collection.objects.link(obj)
        except RuntimeError:
            pass  # already linked (a name the scene had before)

    # Everything the append brought in belongs to the model.  What gets *placed* is the
    # model's **roots** -- the objects that have no parent inside the model -- because
    # moving a root carries its children with it.  Picking one "main" object instead
    # (the biggest mesh, say) moves only that object: a potted plant whose meshes hang
    # off an empty parent ended up 0.8 m from the anchor, inside a bench.
    roots = [obj for obj in new_objects
             if getattr(obj, "parent", None) is None or obj.parent not in new_objects]
    if not roots:
        roots = list(new_objects)
    note = ""
    if target:
        named = next((obj for obj in new_objects if obj.name == target), None)
        if named is not None:
            # A named object deep in a hierarchy is placed through its root, or the rest
            # of the model would stay behind.
            root_of_named = named
            while getattr(root_of_named, "parent", None) in new_objects:
                root_of_named = root_of_named.parent
            if root_of_named is not named:
                note = (f"placed through {root_of_named.name!r}, the root of the "
                        f"hierarchy {target!r} belongs to")
            roots = [root_of_named]

    scale = float(model.get("scale") or 1.0)
    rotation = list(model.get("rotation") or [0.0, 0.0, 0.0])[:3]
    for obj in new_objects:
        try:
            obj[focus_objects.OBJECT_MARK_KEY] = identifier
        except Exception:  # noqa: BLE001 - not every datablock takes custom properties
            pass
    for root in roots:
        root.rotation_mode = "XYZ"
        root.rotation_euler = [float(v) for v in rotation]
        root.scale = (scale, scale, scale)
    bpy.context.view_layer.update()
    lo, hi = _bounds(new_objects)
    # Stand the model *on* the anchor: its footprint centres on the point and its base
    # sits at the anchor's height, which is what "the object is generated at this spot"
    # means for a scene full of furniture.  Every root moves by the same delta, so the
    # model travels rigidly.
    offset = (
        float(anchor[0]) - (lo[0] + hi[0]) * 0.5,
        float(anchor[1]) - (lo[1] + hi[1]) * 0.5,
        float(anchor[2]) - lo[2],
    )
    for root in roots:
        root.location = (root.location[0] + offset[0], root.location[1] + offset[1],
                         root.location[2] + offset[2])
    bpy.context.view_layer.update()

    # The copy exists for this one subject, so it stays visible: an unaware renderer
    # then shows the right object instead of an empty room.  (The generator and the
    # renderer still call ``focus.apply_visibility`` per sequence, which is what keeps
    # a copy with more than one object honest.)
    for obj in new_objects:
        try:
            obj.hide_render = False
            obj.hide_viewport = False
        except AttributeError:  # pragma: no cover
            pass
    record["objects"] = sorted(obj.name for obj in new_objects)
    record["roots"] = [root.name for root in roots]
    record["note"] = note
    record["ok"] = True
    return record


def _bounds(objects):
    from mathutils import Vector

    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for obj in objects:
        for corner in obj.bound_box:
            point = obj.matrix_world @ Vector((corner[0], corner[1], corner[2]))
            for axis in range(3):
                lo[axis] = min(lo[axis], point[axis])
                hi[axis] = max(hi[axis], point[axis])
    return lo, hi


def run(args) -> dict:
    import bpy

    from blender_motion_pipeline.core import focus as focus_objects
    from blender_motion_pipeline.io.json_io import load_json_file, save_json_file

    job = load_json_file(args.job, default={}, required=False) or {}
    scene = bpy.context.scene
    models = list(job.get("models") or [])
    anchor_request = dict(job.get("anchor") or {})
    report = {"schema_version": 1, "scene": os.path.abspath(args.scene), "ok": False,
              "anchor": {}, "models": [], "placements": [], "note": "", "error": ""}

    class _Section:
        """The anchor settings, in the shape ``resolve_anchor`` reads."""

        anchor_mode = str(anchor_request.get("mode") or "auto")
        anchor_object = str(anchor_request.get("object") or "")
        anchor_location = list(anchor_request.get("location") or [0.0, 0.0, 0.0])
        anchor_clearance = float(anchor_request.get("clearance") or 0.0)

    existing = focus_objects.registered_names(scene)
    if existing:
        # Re-staging an already-prepared copy: drop what a previous run placed so the
        # file does not accumulate a second copy of every model.
        for name in existing:
            obj = bpy.data.objects.get(name)
            if obj is not None:
                bpy.data.objects.remove(obj, do_unlink=True)
        scene.pop(focus_objects.SCENE_REGISTRY_KEY, None)

    resolved = focus_objects.resolve_anchor(_Section(), scene)
    anchor = tuple(float(v) for v in resolved.get("location") or (0.0, 0.0, 0.0))
    report["anchor"] = {
        "location": [round(v, 6) for v in anchor],
        "mode": resolved.get("source") or _Section.anchor_mode,
        "requested": _Section.anchor_mode,
        "note": resolved.get("note") or "",
        "clearance_m": resolved.get("clearance_m"),
    }
    scene[focus_objects.SCENE_ANCHOR_KEY] = json.dumps(report["anchor"])

    placements = []
    for index, raw in enumerate(models):
        model = dict(raw or {})
        model.setdefault("id", f"model{index + 1:02d}")
        placed = _place_one(scene, model, anchor, index)
        report["models"].append(placed)
        if not placed.get("ok"):
            continue
        from blender_motion_pipeline.core.focus import FocusModel

        entry = FocusModel.from_dict({**model, "enabled": True})
        placement = focus_objects.measure_placement(
            scene, entry, anchor=anchor, anchor_mode=str(resolved.get("source") or "auto")
        )
        placements.append(placement.to_dict())

    # Stored as JSON text: a Blender custom property does not round-trip a nested
    # Python structure (it comes back as IDPropertyGroup/Array), and this record is
    # read by both the generator and the renderer.
    scene[focus_objects.SCENE_REGISTRY_KEY] = json.dumps(placements)
    report["placements"] = placements
    report["ok"] = bool(placements)
    if not placements:
        report["error"] = report["error"] or "no focus model could be placed"

    # Save the copy back in place: this IS the file the sequences will be generated
    # from and the file the render node will open.  ``save_version = 0`` keeps Blender
    # from leaving a ``.blend1`` beside it -- ``scene/`` holds only .blend files.
    try:
        bpy.context.preferences.filepaths.save_version = 0
    except Exception:  # noqa: BLE001 - a preference that is not always writable
        pass
    bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(args.scene), check_existing=False,
                                compress=True)
    if args.report:
        save_json_file(args.report, report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scene", required=True, help="the staged scene copy to edit")
    parser.add_argument("--job", required=True, help="JSON job description")
    parser.add_argument("--report", default="", help="where to write the JSON report")
    args = parser.parse_args(argv if argv is not None else sys.argv[sys.argv.index("--") + 1:]
                             if "--" in sys.argv else None)
    try:
        report = run(args)
    except Exception as exc:  # noqa: BLE001 - the parent process reports it
        from blender_motion_pipeline.io.json_io import save_json_file

        report = {"schema_version": 1, "ok": False, "placements": [], "models": [],
                  "anchor": {}, "note": "", "error": f"{type(exc).__name__}: {exc}"}
        if args.report:
            save_json_file(args.report, report)
        print(f"place_focus_objects failed: {report['error']}", file=sys.stderr)
        return 1
    print(f"place_focus_objects: {len(report.get('placements') or [])} placement(s) at "
          f"{report.get('anchor', {}).get('location')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
