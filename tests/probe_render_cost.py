"""Report the render settings that dominate EEVEE cost for a sequence file.

    blender -b -P tests/probe_render_cost.py -- "<sequence.blend>"

Prints the settings that make EEVEE slow (raytracing, motion blur, soft shadows,
volumetrics, TAA samples) plus the object/light/material counts, so a slow render
can be attributed to the scene rather than guessed at.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
for path in (_PACKAGE_PARENT, _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import bpy  # noqa: E402

from blender_motion_pipeline.core.scene_loader import SceneEntry, open_scene_for_generation  # noqa: E402


def report(label, owner, names):
    for name in names:
        if hasattr(owner, name):
            value = getattr(owner, name)
            try:
                value = round(float(value), 4) if isinstance(value, float) else value
            except Exception:
                pass
            print(f"  {label}.{name:34s} = {value}")


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        print("usage: blender -b -P tests/probe_render_cost.py -- <sequence.blend>")
        return 2
    load = open_scene_for_generation(SceneEntry(path=os.path.abspath(argv[0])))
    if not load.ok:
        print("open failed:", load.error)
        return 1
    scene = bpy.context.scene
    print(f"scene {scene.name!r}  frames {scene.frame_start}..{scene.frame_end}  engine={scene.render.engine}")
    print("\nrender:")
    report("render", scene.render, [
        "resolution_x", "resolution_y", "resolution_percentage", "film_transparent",
        "use_motion_blur", "use_simplify", "simplify_subdivision_render",
        "use_multiview", "engine", "image_settings.file_format", "fps",
    ])
    print("\ncycles:")
    report("cycles", getattr(scene, "cycles", None) or scene, ["samples", "use_denoising", "max_bounces"])
    eevee = getattr(scene, "eevee", None)
    if eevee is None:
        print("\n(no scene.eevee in this build)")
    else:
        print("\neevee:")
        report("eevee", eevee, [
            "taa_render_samples", "taa_samples", "use_raytracing", "use_shadows",
            "use_volumetric_lights", "use_volumetric_shadows", "use_bloom",
            "use_gtao", "use_soft_shadows", "shadow_ray_count", "shadow_step_count",
            "use_shadow_jitter_viewport", "use_ssr", "use_ssr_refraction",
            "volumetric_start", "volumetric_end", "volumetric_tile_size",
            "use_high_quality_normals", "use_overscan", "use_taa_reprojection",
            "use_fast_gi", "fast_gi_method", "use_bokeh_jittered", "bokeh_overblur",
            "use_raytracing_options", "ray_tracing_method", "use_shadow_raytracing",
        ])
        for group in ("ray_tracing_options", "fast_gi_options"):
            nested = getattr(eevee, group, None)
            if nested is not None:
                names = [p.identifier for p in nested.bl_rna.properties if p.identifier != "rna_type"]
                report(f"eevee.{group}", nested, names[:12])

    counts = {"objects": 0, "meshes": 0, "lights": 0, "materials": 0, "images": 0,
              "particles": 0, "modifiers": 0, "instances": 0}
    triangles = 0
    lights = []
    for obj in scene.objects:
        counts["objects"] += 1
        if obj.type == "MESH":
            counts["meshes"] += 1
            counts["modifiers"] += len(obj.modifiers)
            if obj.instance_type != "NONE":
                counts["instances"] += 1
        elif obj.type == "LIGHT":
            counts["lights"] += 1
            lights.append((obj.name, obj.data.type, round(obj.data.energy, 1), obj.data.use_shadow))
    counts["materials"] = len(bpy.data.materials)
    counts["images"] = len(bpy.data.images)
    for mesh in bpy.data.meshes:
        triangles += len(mesh.polygons)
    print(f"\nscene contents: {counts}  polygons={triangles:,}")
    print(f"lights ({len(lights)}):")
    for entry in lights[:20]:
        print(f"  {entry}")
    missing = [image.name for image in bpy.data.images if image.source == "FILE" and not image.has_data]
    print(f"images with no data ({len(missing)}): {missing[:5]}")
    volumes = [o.name for o in scene.objects if o.type == "VOLUME"]
    print(f"volume objects: {len(volumes)} {volumes[:5]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
