"""Sidebar panels for the Motion Pipeline add-on.

Layout mirrors the requirement list: scene area, character area, camera
validation area, sequence output area and the action buttons (including the
mandatory "Start generation").
"""

from __future__ import annotations

import os
import time

import bpy
from bpy.types import Panel

from .io.path_utils import to_forward_slashes
from .properties import parse_motion_filter

CATEGORY = "Motion Pipeline"

#: ``panel_state.describe()`` reads a small JSON file; the panel draws on every UI
#: redraw, so the answer is cached for a few seconds instead of hitting the disk
#: dozens of times a second.
_SETTINGS_INFO = {"at": 0.0, "info": {}}
_SETTINGS_INFO_MAX_AGE = 5.0


def _settings_info() -> dict:
    now = time.time()
    if now - _SETTINGS_INFO["at"] > _SETTINGS_INFO_MAX_AGE or not _SETTINGS_INFO["info"]:
        from .config import panel_state

        _SETTINGS_INFO["at"] = now
        _SETTINGS_INFO["info"] = panel_state.describe()
    return _SETTINGS_INFO["info"]


class _MPPPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY


class MPP_PT_scenes(_MPPPanel, Panel):
    """Scene list, add/remove controls and the path/count readout."""

    bl_idname = "MPP_PT_scenes"
    bl_label = "Scenes"
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        group = context.scene.mpp

        # -- add controls ------------------------------------------------
        box = layout.box()
        box.label(text="Add .blend files", icon="FILE_BLEND")
        column = box.column(align=True)
        column.prop(group, "file_path", text="")
        column.operator("mpp.add_files", icon="ADD")
        row = box.row(align=True)
        row.prop(group, "directory", text="")
        row.operator("mpp.add_directory", text="", icon="FILE_FOLDER")
        box.prop(group, "recursive_scan")

        # -- list --------------------------------------------------------
        header = layout.row(align=True)
        header.label(text=f"Queue ({len(group.scene_list)})", icon="ASSET_MANAGER")
        header.prop(group, "missing_only", text="", icon="ERROR", toggle=True)
        if group.scene_list:
            layout.template_list(
                "MPP_UL_scene_list", "",
                group, "scene_list",
                group, "scene_list_index",
                rows=6,
            )
            index = group.scene_list_index
            if 0 <= index < len(group.scene_list):
                item = group.scene_list[index]
                info = layout.box()
                info.label(text=item.label(), icon="FILE_BLEND")
                info.label(text=f"Status: {item.status}" + (f" - {item.note}" if item.note else ""))
                if item.camera_count:
                    info.label(text=f"Cameras: {item.camera_count}")
                if item.detail:
                    info.label(text=item.detail)
        else:
            layout.label(text="No scenes queued yet", icon="INFO")

        row = layout.row(align=True)
        row.operator("mpp.remove_selected", icon="REMOVE")
        row.operator("mpp.clear_list", icon="TRASH")
        row = layout.row(align=True)
        row.prop(group, "scene_list_file", text="")
        row.operator("mpp.save_scene_list", text="", icon="FILE_TICK")
        row.operator("mpp.load_scene_list", text="", icon="FILEBROWSER")

        layout.label(
            text=f"{len(group.scene_list)} scene(s), "
                 f"{sum(1 for i in group.scene_list if i.enabled)} enabled",
            icon="INFO",
        )


class MPP_PT_character(_MPPPanel, Panel):
    """Character handling mode, asset roots and provider status."""

    bl_idname = "MPP_PT_character"
    bl_label = "Character"
    bl_order = 1
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        group = context.scene.mpp

        layout.prop(group, "character_mode")
        column = layout.column(align=True)
        column.prop(group, "character_asset_root", text="Assets")
        column.prop(group, "animation_asset_root", text="Animations")
        layout.prop(group, "character_provider")

        box = layout.box()
        box.label(text="Import status", icon="INFO")
        status = group.character_status or "(not checked yet)"
        box.label(text=status)
        if group.character_mode == "none":
            box.label(text="Character dimension is disabled.", icon="CHECKMARK")
        elif "unavailable" in status or "not_implemented" in status or not status.isidentifier():
            box.label(text="Character sequences will be skipped with a logged reason.", icon="ERROR")
        box.operator("mpp.load_templates", text="Re-check provider", icon="FILE_REFRESH")


class MPP_PT_motion(_MPPPanel, Panel):
    """Motion template selection and timing."""

    bl_idname = "MPP_PT_motion"
    bl_label = "Motion templates"
    bl_order = 2
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        group = context.scene.mpp

        # Shown even when empty so the user can see the resolved template set.
        from .config.defaults import discover_template_path

        if not group.template_path:
            discovered = discover_template_path()
            if discovered:
                box = layout.box()
                box.label(text="Default template set:", icon="INFO")
                for line in _wrap(discovered, 46):
                    box.label(text=line)
                box.operator("mpp.load_templates", text="Use the default template set",
                             icon="FILE_TICK")

        layout.prop(group, "template_path", text="")
        row = layout.row(align=True)
        row.prop(group, "motion_names", text="")
        row.operator("mpp.load_templates", text="", icon="FILE_REFRESH")
        layout.label(
            text=f"{group.motion_count} template(s) loaded",
            icon="CHECKMARK" if group.motion_count else "ERROR",
        )
        if group.template_source:
            for line in _wrap(group.template_source, 52):
                layout.label(text=line, icon="FILE")

        column = layout.column(align=True)
        column.prop(group, "frame_start")
        column.prop(group, "fps")
        column.prop(group, "interpolation")
        patterns = parse_motion_filter(group.motion_names)
        if patterns:
            layout.label(text=f"filter: {', '.join(patterns)}", icon="FILTER")


def _wrap(text: str, width: int) -> "list[str]":
    """Split ``text`` into label-sized chunks (Blender labels do not wrap)."""
    return [text[start:start + width] for start in range(0, len(text), width)] or [""]


class MPP_PT_render(_MPPPanel, Panel):
    """Local rendering: pick sequences, set the save folder, render."""

    bl_idname = "MPP_PT_render"
    bl_label = "Local render"
    bl_order = 5

    def draw(self, context):
        layout = self.layout
        group = context.scene.mpp
        from .render import render_runner

        running = render_runner.is_running()

        # -- where the sequences come from ------------------------------
        source = layout.box()
        source.label(text="Sequences", icon="SEQUENCE")
        source.prop(group, "render_sequence_dir", text="")
        column = source.column(align=True)
        column.prop(group, "render_input_root", text="")
        row = column.row(align=True)
        row.prop(group, "render_recursive")
        row.operator("mpp.load_render_sequences", icon="FILE_REFRESH")
        if group.render_sequence_dir:
            source.label(text="Rendering the single sequence folder above", icon="INFO")

        # -- list ------------------------------------------------------
        header = layout.row(align=True)
        header.label(text=f"Sequences ({len(group.render_list)})", icon="ASSET_MANAGER")
        if len(group.render_list):
            layout.template_list(
                "MPP_UL_render_list", "",
                group, "render_list",
                group, "render_list_index",
                rows=5,
            )
        else:
            layout.label(text="No sequences listed yet", icon="INFO")

        # -- where the video goes --------------------------------------
        target = layout.box()
        target.label(text="Save to", icon="FILE_FOLDER")
        target.prop(group, "render_output_root", text="")
        row = target.row(align=True)
        row.prop(group, "render_flat")
        row.prop(group, "render_overwrite")
        row.operator("mpp.open_render_output", text="", icon="FILE_FOLDER")

        # -- quality ---------------------------------------------------
        quality = layout.box()
        quality.label(text="Quality", icon="RENDER_STILL")
        quality.prop(group, "render_engine_choice")
        row = quality.row(align=True)
        row.prop(group, "render_override_resolution", text="")
        sub = row.row(align=True)
        sub.active = group.render_override_resolution
        sub.prop(group, "render_res_x", text="X")
        sub.prop(group, "render_res_y", text="Y")
        row = quality.row(align=True)
        row.prop(group, "render_override_fps", text="")
        sub = row.row(align=True)
        sub.active = group.render_override_fps
        sub.prop(group, "render_fps_choice", text="FPS")
        row = quality.row(align=True)
        row.prop(group, "render_samples_override", text="")
        sub = row.row(align=True)
        sub.active = group.render_samples_override
        sub.prop(group, "render_samples_choice", text="Samples")
        sub.prop(group, "render_device", text="")
        row = quality.row(align=True)
        row.prop(group, "video_format", text="")
        row.prop(group, "render_codec", text="")
        quality.prop(group, "render_crf")
        row = quality.row(align=True)
        row.prop(group, "render_write_png")
        sub = row.row(align=True)
        sub.active = group.render_write_png
        sub.prop(group, "render_keep_png")

        # -- go --------------------------------------------------------
        buttons = layout.column(align=True)
        if running:
            buttons.scale_y = 1.5
            buttons.operator("mpp.stop_render", icon="CANCEL")
        else:
            row = buttons.row(align=True)
            row.scale_y = 1.4
            row.operator("mpp.render_selected_sequences", icon="PLAY")
            row.operator("mpp.render_all_sequences", text="All", icon="PLAY")
        row = layout.row(align=True)
        row.prop(group, "render_dry_run")
        row.operator("mpp.clear_render_list", text="", icon="TRASH")
        layout.label(text=summarise_render(group), icon="INFO")


def summarise_render(group) -> str:
    return group.render_summary_text()


class MPP_PT_quick(_MPPPanel, Panel):
    """Two big buttons for the common workflow."""

    bl_idname = "MPP_PT_quick"
    bl_label = "Quick actions"
    bl_order = -1            # drawn first, above everything else

    def draw(self, context):
        layout = self.layout
        group = context.scene.mpp
        from .core import ui_task
        from .render import render_runner

        # This panel is drawn right after a file load, which is exactly when
        # Blender has emptied the Python timer registry -- so it is the reliable
        # place to notice that a running batch lost its driver.  Two
        # ``is_registered`` calls per redraw is nothing next to a lost run.
        try:
            from . import registration

            registration.ensure_timers()
            if ui_task.is_running() or render_runner.is_running():
                from .operators import rearm_driver_timers

                rearm_driver_timers()
        except Exception:
            pass

        generating = ui_task.is_running()
        rendering = render_runner.is_running()

        row = layout.row(align=True)
        row.scale_y = 2.0
        if generating:
            row.operator("mpp.stop_task", text="Stop generating", icon="CANCEL")
        else:
            row.operator("mpp.quick_generate", icon="PLAY")

        row = layout.row(align=True)
        row.scale_y = 2.0
        if rendering:
            row.operator("mpp.quick_render", text="Stop render", icon="CANCEL")
        else:
            row.operator("mpp.quick_render", icon="RENDER_ANIMATION")

        # -- remembered settings -----------------------------------------
        # Kept out of the .blend on purpose: the settings live on the scene, and a
        # run opens every queued scene, which used to reset them to defaults.
        saved = group.settings_saved_utc or _settings_info().get("saved_utc", "")
        box = layout.box()
        row = box.row(align=True)
        row.label(
            text=("My settings: remembered" if saved else "My settings: not remembered yet"),
            icon="CHECKMARK" if saved else "INFO",
        )
        row.operator("mpp.forget_settings", text="", icon="TRASH")
        if saved:
            box.label(text=f"Saved {saved[:19].replace('T', ' ')} - survives opening other scenes")
        row = box.row(align=True)
        row.operator("mpp.save_settings", icon="FILE_TICK")
        row.operator("mpp.load_settings", icon="LOOP_BACK")

        # A single line telling the user what the two buttons will act on.
        scenes = len(group.scene_list)
        sequences = len(group.render_list)
        detail = layout.column(align=True)
        detail.label(
            text=f"generate: {scenes} scene(s) -> "
                 f"{to_forward_slashes(group.output_root) or '(set output)'}",
            icon="TRIA_RIGHT",
        )
        detail.label(
            text=f"render: {sequences} sequence(s) -> "
                 f"{to_forward_slashes(group.render_output_root) or '(set save folder)'}",
            icon="TRIA_RIGHT",
        )


class MPP_UL_render_list(bpy.types.UIList):
    """Render list rows with state icon and detail."""

    bl_idname = "MPP_UL_render_list"

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag):
        row = layout.row(align=True)
        icon_name = {
            "pending": "DOT",
            "running": "TIME",
            "done": "CHECKMARK",
            "skipped": "FORWARD",
            "failed": "CANCEL",
        }.get(item.state, "DOT")
        column = row.column()
        column.label(text=item.label(), icon=icon_name)
        if item.detail:
            column.label(text=item.detail[:64])
        if item.storage_mode == "animation":
            # ``SEQUENCE`` marks a row that stores the animation instead of a scene
            # copy.  Note the icon enum is not a palette: ``SEQUENCE_COLOR_02``
            # exists as a strip-colour value, not as an icon, and using it here
            # aborted the whole draw with a TypeError.  tests/probe_icons.py checks
            # every literal in the package against the running build.
            row.label(text="", icon="SEQUENCE")
        row.label(text=item.state)


class MPP_PT_validation(_MPPPanel, Panel):
    """Camera validation and the spherical auto-search settings."""

    bl_idname = "MPP_PT_validation"
    bl_label = "Camera validation"
    bl_order = 3

    def draw(self, context):
        layout = self.layout
        group = context.scene.mpp

        layout.prop(group, "validation_enabled")
        column = layout.column(align=True)
        column.active = group.validation_enabled
        column.prop(group, "validation_sample_step")
        column.prop(group, "clearance")
        column.prop(group, "obstruction_distance")
        row = column.row(align=True)
        row.prop(group, "max_position_jump")
        row.prop(group, "max_rotation_jump_deg")
        column.prop(group, "check_character_visibility")
        sub = column.column(align=True)
        sub.active = group.check_character_visibility
        sub.prop(group, "min_character_visible_ratio")
        column.prop(group, "check_character_overlap")

        box = layout.box()
        box.prop(group, "search_enabled", text="Auto-adjust camera position")
        column = box.column(align=True)
        column.active = group.search_enabled
        row = column.row(align=True)
        row.prop(group, "search_min_radius")
        row.prop(group, "search_max_radius")
        column.prop(group, "search_candidate_count")
        row = column.row(align=True)
        row.prop(group, "search_azimuth_samples")
        row.prop(group, "search_elevation_samples")
        column.prop(group, "search_shell_only")
        row = column.row(align=True)
        row.prop(group, "search_max_retries")
        row.prop(group, "search_random_seed")
        column.prop(group, "search_allow_rotation")
        sub = column.column(align=True)
        sub.active = group.search_allow_rotation
        sub.prop(group, "search_max_rotation_deg")
        column.prop(group, "search_allow_focal")
        sub = column.column(align=True)
        sub.active = group.search_allow_focal
        sub.prop(group, "search_focal_steps")
        column.prop(group, "search_max_output")


class MPP_PT_output(_MPPPanel, Panel):
    """The project folder a run writes, and how the sequences are written."""

    bl_idname = "MPP_PT_output"
    bl_label = "Sequence output"
    bl_order = 4

    def draw(self, context):
        layout = self.layout
        group = context.scene.mpp

        layout.prop(group, "output_root", text="Project folder")
        # The project tree is created inside the folder above; show exactly what
        # that means, because the folder the user picks is *not* the sequence root.
        box = layout.box()
        box.label(text="Project written on generation", icon="FILE_FOLDER")
        for line in group.project_summary().splitlines():
            box.label(text=line)

        row = layout.row(align=True)
        row.prop(group, "save_validation_report")
        row.prop(group, "overwrite")
        row = layout.row(align=True)
        row.prop(group, "resume")
        row.prop(group, "verbose_logging")
        layout.prop(group, "camera_selection")

        box = layout.box()
        box.label(text="Render defaults (recorded for the renderer)", icon="RENDER_STILL")
        column = box.column(align=True)
        column.prop(group, "render_engine")
        row = column.row(align=True)
        row.prop(group, "render_samples")
        row.prop(group, "render_fps")
        column.prop(group, "video_format")
        row = column.row(align=True)
        row.prop(group, "trajectory_mode")
        row.prop(group, "trajectory_step")

        # The sequence fixes its own output size: a preset list instead of free
        # numbers, defaulting to 720p.  Without this the renderer follows whatever
        # resolution the source scene has, which is how a 2000x2000 scene produced
        # 2000x2000 videos regardless of this panel.
        box.prop(group, "sequence_resolution")
        box.label(text=group.resolution_summary(), icon="INFO")


class MPP_PT_actions(_MPPPanel, Panel):
    """The action buttons, including the mandatory Start generation."""

    bl_idname = "MPP_PT_actions"
    bl_label = "Actions"
    bl_order = 5

    def draw(self, context):
        layout = self.layout
        group = context.scene.mpp

        column = layout.column(align=True)
        column.scale_y = 1.15
        row = column.row(align=True)
        row.operator("mpp.check_configuration", icon="CHECKMARK")
        row.operator("mpp.validate_scenes", icon="VIEWZOOM")

        row = layout.row(align=True)
        row.scale_y = 1.6
        if group.task_state in ("preparing", "running", "cancelling"):
            row.operator("mpp.stop_task", icon="CANCEL")
        else:
            row.operator("mpp.start_generation", icon="PLAY")

        column = layout.column(align=True)
        column.operator("mpp.open_output_directory", icon="FILE_FOLDER")
        column.operator("mpp.show_error_report", icon="ERROR")

        row = layout.row(align=True)
        row.operator("mpp.save_config", text="Export config", icon="EXPORT")
        row.operator("mpp.load_config", text="Import config", icon="IMPORT")
        layout.operator("mpp.apply_defaults", icon="LOOP_BACK")


class MPP_PT_status(_MPPPanel, Panel):
    """Live task progress and the last message.

    Reads from :func:`preferences.status_source` rather than the scene directly:
    generation opens other ``.blend`` files and scene-scoped properties are reset
    when that happens, so the add-on preferences are the durable place for
    progress.
    """

    bl_idname = "MPP_PT_status"
    bl_label = "Status"
    bl_order = 6

    def draw(self, context):
        from .preferences import live_status

        layout = self.layout
        group = context.scene.mpp
        status = live_status(context) or {}
        task_state = status.get("state", group.task_state)
        progress_text = status.get("progress_text") or group.progress_text or "idle"
        fraction = float(status.get("fraction", group.progress_fraction))
        report = status.get("report") or group.last_report
        output_root = status.get("output_root") or group.last_output_root

        box = layout.box()
        row = box.row()
        row.label(text=f"Task: {task_state}", icon="TIME")
        if task_state in ("preparing", "running", "cancelling"):
            box.progress(factor=fraction, text=progress_text or "working")
        else:
            box.label(text=progress_text or "idle")

        row = box.row(align=True)
        row.label(text=f"Generated: {status.get('generated', group.generated_count)}",
                  icon="CHECKMARK")
        row.label(text=f"Failed: {status.get('failed', group.failed_count)}", icon="CANCEL")
        row.label(text=f"Skipped: {status.get('skipped', group.skipped_count)}", icon="FORWARD")

        if report:
            report_box = box.box()
            report_box.label(text="Last report", icon="TEXT")
            # Wrap manually: Blender labels do not wrap.
            width = 52
            for start in range(0, min(len(report), width * 6), width):
                report_box.label(text=report[start:start + width])

        if output_root:
            row = box.row(align=True)
            row.label(text=os.path.basename(output_root) or output_root, icon="FILE_FOLDER")


class MPP_UL_scene_list(bpy.types.UIList):
    """Scene list rows with enable toggle, name and status."""

    bl_idname = "MPP_UL_scene_list"

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag):
        group = context.scene.mpp
        if group.missing_only and os.path.isfile(item.path or ""):
            return
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        icon_name = {
            "ok": "CHECKMARK",
            "missing": "ERROR",
            "no_camera": "CAMERA_DATA",
            "load_failed": "CANCEL",
            "generated": "CHECKMARK",
            "failed": "CANCEL",
        }.get(item.status, "DOT")
        column = row.column()
        column.label(text=item.label(), icon=icon_name)
        if item.detail:
            column.label(text=item.detail)
        elif item.note:
            column.label(text=item.note)
        if item.camera_count:
            row.label(text=f"{item.camera_count} cam")

    def filter_items(self, context, data, propname):
        group = context.scene.mpp
        items = getattr(data, propname)
        flags = [self.bitflag_filter_item] * len(items)
        if group.missing_only:
            for index, item in enumerate(items):
                if os.path.isfile(item.path or ""):
                    flags[index] &= ~self.bitflag_filter_item
        order = []
        return flags, order


CLASSES = (
    MPP_UL_scene_list,
    MPP_UL_render_list,
    MPP_PT_quick,
    MPP_PT_scenes,
    MPP_PT_character,
    MPP_PT_motion,
    MPP_PT_validation,
    MPP_PT_output,
    MPP_PT_render,
    MPP_PT_actions,
    MPP_PT_status,
)
