"""Headless smoke test: does ``blender -b`` write MP4 via FFmpeg here?

Creates a tiny animated scene in memory, renders it to MP4 and reports what was
written.  Run with::

    blender -b -P tests/smoke_render.py

The pipeline avoids a temporary proxy image so the ``Cannot write to Render
Result`` warning can be observed directly.
"""
from __future__ import annotations

import os
import sys
import tempfile

import bpy

OUT = os.path.join(tempfile.gettempdir(), "mp_smoke_render")
os.makedirs(OUT, exist_ok=True)


def build_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = 5
    scene.render.fps = 24
    scene.render.resolution_x = 160
    scene.render.resolution_y = 90
    scene.render.resolution_percentage = 100
    scene.render.engine = "BLENDER_WORKBENCH"

    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 0.0))
    cube = bpy.context.active_object
    bpy.ops.object.camera_add(location=(0.0, -5.0, 1.0), rotation=(1.4, 0.0, 0.0))
    camera = bpy.context.active_object
    scene.camera = camera

    # Animate the cube so the video is not a single repeated frame.
    for frame, y in ((1, -1.0), (3, 0.0), (5, 1.0)):
        scene.frame_set(frame)
        cube.location = (0.0, y, 0.0)
        cube.keyframe_insert("location", frame=frame)
    scene.frame_set(1)


def try_render(label: str, configure) -> dict:
    import time

    build_scene()
    scene = bpy.context.scene
    target = os.path.join(OUT, f"{label}.mp4")
    if os.path.exists(target):
        os.remove(target)
    configure(scene, target)
    started = time.time()
    error = ""
    try:
        bpy.ops.render.render(animation=True)
    except Exception as exc:  # pragma: no cover
        error = f"{type(exc).__name__}: {exc}"
    candidates = sorted(
        os.path.join(OUT, name) for name in os.listdir(OUT)
        if name.startswith(label) and not name.endswith(".py")
    )
    return {
        "label": label,
        "error": error,
        "seconds": round(time.time() - started, 2),
        "target_exists": os.path.isfile(target),
        "target_size": os.path.getsize(target) if os.path.isfile(target) else 0,
        "filepath_used": scene.render.filepath,
        "image_format": scene.render.image_settings.file_format,
        "candidates": [(os.path.basename(p), os.path.getsize(p)) for p in candidates],
    }


def set_media(settings, media_type: str) -> None:
    """Blender 5.2 gates FFMPEG behind image_settings.media_type."""
    if hasattr(settings, "media_type"):
        settings.media_type = media_type


def plain_ffmpeg(scene, target: str) -> None:
    """No proxy image: observe whether Render Result blocks the writer."""
    set_media(scene.render.image_settings, "VIDEO")
    scene.render.image_settings.file_format = "FFMPEG"
    scene.render.ffmpeg.format = "MPEG4"
    scene.render.ffmpeg.codec = "H264"
    scene.render.ffmpeg.constant_rate_factor = "HIGH"
    scene.render.ffmpeg.audio_codec = "NONE"
    scene.render.filepath = target


def proxy_ffmpeg(scene, target: str) -> None:
    """Install a proxy Render Result image first."""
    image = bpy.data.images.get("Render Result")
    if image is not None and (image.size[0] == 0 or image.size[1] == 0):
        try:
            image.scale(32, 32)
        except Exception as exc:
            print("image.scale failed:", exc)
    plain_ffmpeg(scene, target)


def png_frames(scene, target: str) -> None:
    set_media(scene.render.image_settings, "IMAGE")
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = os.path.join(OUT, "pngs", "frame_")


def main() -> int:
    print("=" * 60)
    print("blender", bpy.app.version_string, "background:", bpy.app.background)
    print("output dir:", OUT)
    print("=" * 60)
    results = [
        try_render("plain", plain_ffmpeg),
        try_render("proxy", proxy_ffmpeg),
        try_render("pngs", png_frames),
    ]
    for result in results:
        print("-" * 60)
        for key, value in result.items():
            print(f"  {key:16s}: {value}")
    ok = results[0]["target_exists"] and results[0]["target_size"] > 0
    print("=" * 60)
    print("MP4 via plain FFMPEG output:", "OK" if ok else "FAILED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
