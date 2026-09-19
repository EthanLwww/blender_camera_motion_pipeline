"""Ad-hoc probe: does sequence generation change the scene's render engine?

The reported symptom is "Cycles starts rendering after I start sequence
generation".  This drives the real panel operators against a scene whose engine
is CYCLES and prints the engine before/after every stage.

    blender -b -P tests/probe_engine_switch.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
for path in (_PACKAGE_PARENT, _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _boot  # noqa: E402,F401  (the add-on folder may be called anything)

import bpy  # noqa: E402

from blender_motion_pipeline import registration  # noqa: E402
from blender_motion_pipeline.core import ui_task  # noqa: E402

WORK = os.path.join(tempfile.gettempdir(), "mp_engine_probe")
TEMPLATES = r"E:\UE\DataGenScenes\Plugins\MetaHumanScenePipeline\Templates\camera_motion_templates.json"


def build(engine: str) -> str:
    blend = os.path.join(WORK, "scene_" + engine.lower() + ".blend")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = engine
    scene.frame_start, scene.frame_end = 0, 8
    scene.render.resolution_x, scene.render.resolution_y = 160, 90
    bpy.ops.mesh.primitive_plane_add(size=20.0)
    bpy.ops.object.camera_add(location=(0.0, 0.0, 1.6),
                              rotation=(1.5707963, 0.0, -1.5707963))
    scene.camera = bpy.context.active_object
    bpy.ops.wm.save_as_mainfile(filepath=blend, check_existing=False, compress=False)
    return blend


def pump(limit: float = 180.0) -> dict:
    deadline = time.time() + limit
    while ui_task.is_running() and time.time() < deadline:
        ui_task.step()
        time.sleep(0.05)
    return ui_task.snapshot()


def run(engine: str) -> None:
    print("=" * 70)
    print(f"scene engine = {engine}")
    print("=" * 70)
    blend = build(engine)
    print(f"  engine in saved file            : {bpy.context.scene.render.engine}")

    registration.register_all()
    group = bpy.context.scene.mpp
    group.scene_list.clear()
    group.output_root = os.path.join(WORK, "out_" + engine.lower())
    group.template_path = TEMPLATES
    group.motion_names = "fixed_01_standard"
    group.character_mode = "none"
    group.overwrite = True
    group.resume = False
    group.validation_sample_step = 100
    group.file_path = blend
    bpy.ops.mpp.add_files()
    print(f"  after add_files                 : {bpy.context.scene.render.engine}")

    bpy.ops.mpp.check_configuration()
    print(f"  after check_configuration       : {bpy.context.scene.render.engine}")
    bpy.ops.mpp.load_templates()
    print(f"  after load_templates            : {bpy.context.scene.render.engine}")

    print(f"  panel render_engine_choice      : {group.render_engine_choice}")
    print(f"  panel render_engine (recorded)  : {group.render_engine}")
    print(f"  panel render_options() engine   : {group.render_options()['engine']}")

    bpy.ops.mpp.start_generation()
    print(f"  engine right after start        : {bpy.context.scene.render.engine}")
    snapshot = pump()
    print(f"  generation                      : "
          f"{snapshot['generated']} ok / {snapshot['failed']} failed")
    print(f"  engine AFTER generation         : {bpy.context.scene.render.engine}")
    print(f"  file Blender now has open       : {bpy.data.filepath}")

    sequence = os.path.join(ui_task.snapshot().get("project_folder", group.output_root),
                            "sequence", "scene_" + engine.lower(),
                            "fixed_01_standard", "sequence_000001")
    config = os.path.join(sequence, "sequence_config.json")
    if os.path.isfile(config):
        with open(config, encoding="utf-8") as handle:
            payload = json.load(handle)
        render = payload.get("render") or {}
        print(f"  sequence_config.render.engine   : {render.get('engine')}")
        print(f"  sequence_config resolution/fps  : {render.get('resolution_x')}x"
              f"{render.get('resolution_y')} @{render.get('fps')}")
        print(f"  sequence_config.render.engine   : (from the scene at generation time)")
    else:
        print("  sequence_config.json missing")
    registration.unregister_all()
    print()


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    if not os.path.isfile(TEMPLATES):
        print(f"template document missing: {TEMPLATES}")
        return 1
    for engine in ("BLENDER_EEVEE", "CYCLES"):
        run(engine)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
