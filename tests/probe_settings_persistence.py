"""Prove the panel configuration survives a run that opens other scenes.

    blender -b -P tests/probe_settings_persistence.py

The panel's settings live on ``scene.mpp``, which is per file, so a batch that
opens every queued ``.blend`` used to replace them with defaults (measured: 11 of
14 configured fields).  This walks the real flow:

1. configure the panel;
2. *remember* it (what pressing Start generation / Render does);
3. open another scene -- the step that used to wipe everything;
4. check a file that carries its own deliberate configuration is left alone.

Uses its own settings file (``MPP_PANEL_SETTINGS``), never the user's.
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

from blender_motion_pipeline import registration  # noqa: E402
from blender_motion_pipeline.config import panel_state  # noqa: E402

SCRATCH = os.path.join(tempfile.gettempdir(), "mpp_settings_probe")
os.environ[panel_state.ENV_OVERRIDE] = os.path.join(SCRATCH, "probe_settings.json")

#: Fields a user configures by hand and would not want to retype.
WATCHED = {
    "output_root": "/tmp/configured_output",
    "motion_names": "fixed_01_standard,truck_right_01_standard",
    "camera_selection": "TREN",
    "frame_start": 12,
    "validation_enabled": False,
    "search_enabled": False,
    "search_max_radius": 7.5,
    "save_sequence_blend": False,
    "overwrite": False,
    "resume": True,
    "render_engine": "BLENDER_EEVEE",
    "render_samples": 11,
    "trajectory_mode": "sampled",
    "trajectory_step": 3,
    "render_output_root": "/tmp/render_out",
    "render_res_x": 640,
}


def values(group) -> dict:
    """Comparable normalised values.

    The panel legitimately reformats what it is given -- paths are made absolute
    with backslashes, the motion filter is re-joined with ", " -- so comparing the
    raw strings would report a difference where there is none.
    """
    from blender_motion_pipeline.io.path_utils import normalize_path
    from blender_motion_pipeline.properties import parse_motion_filter

    raw = {name: getattr(group, name) for name in WATCHED}
    for name in ("output_root", "render_output_root"):
        value = raw.get(name)
        raw[name] = normalize_path(value) if value else ""
    if "motion_names" in raw:
        raw["motion_names"] = list(parse_motion_filter(raw["motion_names"]))
    return raw


def report(label: str, got: dict, want: dict) -> int:
    lost = [name for name in WATCHED if got[name] != want[name]]
    state = "OK  " if not lost else "LOST"
    print(f"  [{state}] {label}: {len(WATCHED) - len(lost)}/{len(WATCHED)} field(s) match")
    for name in lost:
        print(f"           {name:26s} {want[name]!r} -> {got[name]!r}")
    return len(lost)


def main() -> int:
    os.makedirs(SCRATCH, exist_ok=True)
    panel_state.clear()
    configured = os.path.join(SCRATCH, "configured.blend")
    other = os.path.join(SCRATCH, "other_scene.blend")

    # Build both scenes *before* registering: ``read_factory_settings`` clears
    # ``bpy.app.handlers`` (the GUI heals itself because Blender re-enables the
    # add-on, a script does not), and the point of this probe is the restore path,
    # not the handler plumbing.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    bpy.ops.object.camera_add(location=(0.0, 0.0, 2.0))
    bpy.context.scene.camera = bpy.context.active_object
    bpy.ops.wm.save_as_mainfile(filepath=configured, check_existing=False, compress=False)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_plane_add()
    bpy.ops.object.camera_add(location=(1.0, 1.0, 3.0))
    bpy.context.scene.camera = bpy.context.active_object
    bpy.ops.wm.save_as_mainfile(filepath=other, check_existing=False, compress=False)

    registration.register_all()

    # Give the configured scene its own deliberate configuration, which must win
    # over the remembered one.
    bpy.ops.wm.open_mainfile(filepath=configured)
    bpy.context.scene.mpp.render_samples = 7
    bpy.context.scene.mpp.camera_selection = "FLOOR"
    bpy.ops.wm.save_as_mainfile(filepath=configured, check_existing=False, compress=False)

    print("== 1. the user configures the panel ==")
    bpy.ops.wm.open_mainfile(filepath=other)
    group = bpy.context.scene.mpp
    for name, value in WATCHED.items():
        setattr(group, name, value)
    want = values(group)
    report("configured", values(group), want)

    print("\n== 2. pressing Start generation remembers it ==")
    from blender_motion_pipeline.operators import remember_settings

    target = remember_settings(group)
    print(f"  settings file: {target}")
    print(f"  describe: {panel_state.describe()}")

    print("\n== 3. the run opens another scene (this used to wipe everything) ==")
    bpy.ops.wm.open_mainfile(filepath=other)
    # ``open_mainfile`` drops script-registered handlers, which is exactly why the
    # add-on also watches the active scene from a timer; a GUI Blender runs this
    # once a second, this probe calls it once.
    registration._scene_watch_tick()
    lost = report("after opening another scene", values(bpy.context.scene.mpp), want)

    print("\n== 4. a file carrying its own settings keeps them ==")
    bpy.ops.wm.open_mainfile(filepath=configured)
    own_group = bpy.context.scene.mpp
    print(f"  render_samples = {own_group.render_samples} (file's own: 7), "
          f"camera_selection = {own_group.camera_selection!r} (file's own: 'FLOOR')")
    preserved = own_group.render_samples == 7 and own_group.camera_selection == "FLOOR"
    print(f"  [{'OK  ' if preserved else 'LOST'}] the file's own configuration wins")

    print("\n== 5. explicit 'Use my settings' forces the remembered ones ==")
    sections = registration.apply_remembered_settings(force=True, quiet=True)
    forced = report("forced apply", values(bpy.context.scene.mpp), want)
    print(f"  sections applied: {sections}")

    print("\n== 6. a fresh file (never configured) gets them automatically ==")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    registration.ensure_load_handler()
    bpy.ops.wm.open_mainfile(filepath=other)
    bpy.context.scene.mpp.camera_selection = "all"
    registration.apply_remembered_settings(quiet=True)
    fresh = report("fresh file", values(bpy.context.scene.mpp), want)
    lost = max(lost, fresh)

    registration.unregister_all()
    panel_state.clear()
    print()
    ok = lost == 0 and forced == 0 and preserved
    print("verdict:", "OK - the configuration survives a run" if ok
          else "FAILED - settings are still lost")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
