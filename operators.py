"""Operators behind the sidebar panel.

Long runs never block the UI.  ``mpp.start_generation`` sets up the work and
then hands control to ``bpy.app.timers``: each timer tick generates exactly one
sequence, updates the progress bar and re-arms itself.  Blender therefore stays
responsive and ``mpp.stop_task`` can cancel between sequences.

Two Blender-5 details are handled deliberately here:

* every ``execute`` returns a ``set`` on **all** paths -- returning ``None``
  raises ``Function.result expected a set`` at runtime;
* the file/folder fields are plain strings rather than ``FILE_PATH`` /
  ``DIR_PATH`` subtypes, because RNA validates those before ``execute`` runs and
  would make "this path is wrong" impossible to report from Python.  The file
  pickers are still offered through ``invoke``.
"""

from __future__ import annotations

import os
import time

import bpy
from bpy.types import Operator

from .config.defaults import load_config_file, save_config_file
from .config.models import ConfigError, validate_batch_config
from .core.scene_loader import (
    SceneEntry,
    describe_entries,
    load_scene_list,
    merge_scene_entries,
    save_scene_list,
    scan_directory,
)
from .io.json_io import save_json_file
from .io.path_utils import ensure_dir, normalize_path, to_forward_slashes
from .properties import RENDER_STATES, apply_motion_filter, parse_motion_filter
from .utils.logging_utils import get_logger

LOGGER = get_logger("ui")

#: How often the generation timer runs, in seconds.
TIMER_INTERVAL = 0.05

#: How often the render timer polls its child Blender process.
RENDER_TIMER_INTERVAL = 0.25

#: Valid render-list states, for cheap membership tests.
RENDER_STATES_SET = {state for state, _label, _desc in RENDER_STATES}


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------
def _group(context):
    """The panel property group for the operator's context.

    See :func:`_panel_group` for why this must not be cached across a scene
    change.
    """
    return _panel_group(context)


def _report_to_panel(group, text: str) -> None:
    group.last_report = text


def _log_callback(group):
    """Mirror warning/error log records into the panel's status line."""

    def callback(level: str, message: str) -> None:
        if level in ("WARNING", "ERROR"):
            try:
                group.last_report = f"[{level}] {message}"
            except Exception:
                pass

    return callback


def _entries(group) -> "list[SceneEntry]":
    return [
        SceneEntry(path=item.path, enabled=item.enabled, status=item.status, note=item.note)
        for item in group.scene_list
        if item.path
    ]


def _project_root(group) -> str:
    """The folder the user picked; the dated project folder lives inside it."""
    return group.output_root or group.last_output_root


def _project_target(group) -> str:
    """The folder to open: the project folder of the last run, or its parent.

    ``last_project_folder`` is set when a run starts, but the scene it lives on is
    replaced whenever a queued ``.blend`` is opened -- so the newest
    ``blender_camera_*`` folder under the project root is the fallback.
    """
    known = getattr(group, "last_project_folder", "")
    if known and os.path.isdir(known):
        return known
    root = _project_root(group)
    if not root or not os.path.isdir(root):
        return root
    from .core.project import PROJECT_PREFIX

    candidates = []
    try:
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if name.startswith(PROJECT_PREFIX) and os.path.isdir(path):
                candidates.append((os.path.getmtime(path), path))
    except OSError:
        return root
    if not candidates:
        return root
    candidates.sort()
    return candidates[-1][1]


def remember_settings(group) -> str:
    """Store the panel configuration where the next scene can find it.

    Called when a run starts (and by the panel's Remember button).  A batch opens
    every queued ``.blend``, which replaces the scene this group belongs to, so
    anything not written down first is gone -- see ``config/panel_state.py``.
    """
    from .config import panel_state

    try:
        target = panel_state.save(group.snapshot_settings(), source=bpy.data.filepath or "")
    except Exception:
        LOGGER.debug("could not remember the panel settings", exc_info=True)
        return ""
    if target:
        group.settings_path = target
        group.settings_saved_utc = panel_state.describe().get("saved_utc", "")
    return target


def _add_paths(group, paths, *, source: str = "") -> "tuple[int, list[str]]":
    """Add ``paths`` to the scene list, returning ``(added, problems)``."""
    existing = _entries(group)
    entries, problems = merge_scene_entries(existing, paths, logger=LOGGER)
    added = len(entries) - len(existing)
    group.scene_list.clear()
    previous = {os.path.normcase(item.path): item for item in existing}
    for entry in entries:
        item = group.scene_list.add()
        item.path = entry.path
        item.enabled = True
        item.status = "pending"
        item.note = source
        old = previous.get(os.path.normcase(entry.path))
        if old is not None:
            item.enabled = old.enabled
    if group.scene_list_index < 0 and len(group.scene_list):
        group.scene_list_index = 0
    return added, problems


class _MPPBase:
    """Mixin adding consistent error handling and panel reporting."""

    bl_options = {"REGISTER", "UNDO"}

    def report_outcome(self, group, message: str, *, level: str = "INFO") -> set:
        _report_to_panel(group, message)
        self.report({level}, message)
        return {"FINISHED"}

    def fail(self, group, message: str, *, exception: "Exception | None" = None) -> set:
        """Report a *recoverable* problem.

        Reported as a WARNING rather than an ERROR on purpose: Blender raises the
        error report back into the caller as ``RuntimeError``, which makes an
        expected condition such as "that file is missing" look like a crash and
        aborts the rest of a scripted run.  The message is still written to the
        panel and to the console.
        """
        text = message if exception is None else f"{message}: {exception}"
        _report_to_panel(group, text)
        if exception is not None:
            LOGGER.error("%s", text, exc_info=True)
        else:
            LOGGER.warning("%s", text)
        self.report({"WARNING"}, text)
        return {"CANCELLED"}


# --------------------------------------------------------------------------
# scene list
# --------------------------------------------------------------------------
class MPP_OT_add_files(_MPPBase, Operator):
    """Add .blend files to the scene list"""

    bl_idname = "mpp.add_files"
    bl_label = "Add scene file"

    filepath: bpy.props.StringProperty(name="Scene file", default="")
    files: bpy.props.CollectionProperty(
        name="Files",
        type=bpy.types.OperatorFileListElement,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    directory: bpy.props.StringProperty(name="Directory", subtype="DIR_PATH", default="")
    filter_glob: bpy.props.StringProperty(default="*.blend", options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        group = _group(context)
        candidates = []
        if self.files:
            base = self.directory or os.path.dirname(self.filepath or "")
            candidates.extend(os.path.join(base, item.name) for item in self.files)
        if self.filepath:
            candidates.append(self.filepath)
        # Fall back to the panel's own "Scene file" field.
        if not candidates and group.file_path:
            candidates.append(group.file_path)
        candidates = [normalize_path(c) for c in candidates if c]
        if not candidates:
            # A plain execute() (no picker) should use the panel field, which may
            # legitimately be empty; guide the user rather than fail silently.
            return self.fail(
                group,
                "No .blend file selected. Use the file picker or fill the Scene file field.",
            )
        try:
            added, problems = _add_paths(group, candidates)
        except Exception as exc:
            return self.fail(group, "Could not add the scene", exception=exc)
        for problem in problems:
            LOGGER.warning("%s", problem)
        if added == 0:
            return self.fail(group, problems[0] if problems else "Nothing was added")
        return self.report_outcome(
            group,
            f"Added {added} scene(s). {describe_entries(_entries(group))}"
            + (f" | {problems[0]}" if problems else ""),
        )


class MPP_OT_add_directory(_MPPBase, Operator):
    """Scan a folder for .blend files and add them"""

    bl_idname = "mpp.add_directory"
    bl_label = "Add folder"

    directory: bpy.props.StringProperty(name="Directory", subtype="DIR_PATH", default="")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        group = _group(context)
        target = self.directory or group.directory
        if not target:
            return self.fail(group, "No folder selected. Use the picker or fill the Folder field.")
        try:
            found = scan_directory(target, recursive=group.recursive_scan)
        except NotADirectoryError as exc:
            return self.fail(group, str(exc))
        except Exception as exc:
            return self.fail(group, "Could not scan the folder", exception=exc)
        if not found:
            return self.fail(group, f"No .blend files found in {to_forward_slashes(target)}")
        try:
            added, problems = _add_paths(group, found, source="folder scan")
        except Exception as exc:
            return self.fail(group, "Could not add the folder contents", exception=exc)
        return self.report_outcome(
            group,
            f"Found {len(found)}, added {added} scene(s) from {to_forward_slashes(target)}"
            + (f" | {len(problems)} skipped" if problems else ""),
        )


class MPP_OT_remove_selected(_MPPBase, Operator):
    """Remove the selected scene from the list"""

    bl_idname = "mpp.remove_selected"
    bl_label = "Remove selected"

    def execute(self, context):
        group = _group(context)
        index = group.scene_list_index
        if index < 0 or index >= len(group.scene_list):
            return self.fail(group, "Select a scene in the list first")
        removed = group.scene_list[index].path
        group.scene_list.remove(index)
        group.scene_list_index = min(index, len(group.scene_list) - 1)
        return self.report_outcome(group, f"Removed {to_forward_slashes(removed)}")


class MPP_OT_clear_list(_MPPBase, Operator):
    """Clear the whole scene list"""

    bl_idname = "mpp.clear_list"
    bl_label = "Clear list"

    def execute(self, context):
        group = _group(context)
        count = len(group.scene_list)
        group.scene_list.clear()
        group.scene_list_index = -1
        return self.report_outcome(group, f"Cleared {count} scene(s)")


class MPP_OT_save_scene_list(_MPPBase, Operator):
    """Save the scene list to a JSON file"""

    bl_idname = "mpp.save_scene_list"
    bl_label = "Save list"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.json", options={"HIDDEN"})

    def invoke(self, context, event):
        if not self.filepath:
            group = _group(context)
            self.filepath = group.scene_list_file or os.path.join(
                group.output_root or os.path.expanduser("~"), "scene_list.json"
            )
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        group = _group(context)
        path = self.filepath or group.scene_list_file
        if not path:
            path = os.path.join(group.output_root or os.path.expanduser("~"), "scene_list.json")
        try:
            target = save_scene_list(path, _entries(group))
        except Exception as exc:
            return self.fail(group, "Could not save the scene list", exception=exc)
        group.scene_list_file = target
        return self.report_outcome(group, f"Scene list saved to {to_forward_slashes(target)}")


class MPP_OT_load_scene_list(_MPPBase, Operator):
    """Load a scene list from a JSON file"""

    bl_idname = "mpp.load_scene_list"
    bl_label = "Load list"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.json", options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        group = _group(context)
        path = self.filepath or group.scene_list_file
        if not path:
            return self.fail(group, "No scene list file selected")
        try:
            entries, warnings = load_scene_list(path)
        except Exception as exc:
            return self.fail(group, "Could not load the scene list", exception=exc)
        if not entries:
            return self.fail(group, warnings[0] if warnings else "The scene list is empty")
        group.scene_list.clear()
        for entry in entries:
            item = group.scene_list.add()
            item.path = entry.path
            item.enabled = entry.enabled
            item.status = "pending"
            item.note = ""
        group.scene_list_index = 0
        for warning in warnings:
            LOGGER.warning("%s", warning)
        return self.report_outcome(
            group,
            f"Loaded {len(entries)} scene(s)"
            + (f"; {len(warnings)} warning(s): {warnings[0]}" if warnings else ""),
        )


class MPP_OT_save_config(_MPPBase, Operator):
    """Write the current settings to a batch configuration JSON"""

    bl_idname = "mpp.save_config"
    bl_label = "Export configuration"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.json", options={"HIDDEN"})

    def invoke(self, context, event):
        if not self.filepath:
            self.filepath = os.path.join(
                _group(context).output_root or os.path.expanduser("~"),
                "motion_pipeline_config.json",
            )
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        group = _group(context)
        path = self.filepath or group.scene_list_file
        if not path:
            return self.fail(group, "No destination selected")
        try:
            config = group.to_config()
            config.scenes = group.enabled_scene_paths()
            target = save_config_file(path, config)
        except Exception as exc:
            return self.fail(group, "Could not write the configuration", exception=exc)
        return self.report_outcome(group, f"Configuration written to {to_forward_slashes(target)}")


class MPP_OT_load_config(_MPPBase, Operator):
    """Load a batch configuration JSON into the panel"""

    bl_idname = "mpp.load_config"
    bl_label = "Import configuration"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.json", options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        group = _group(context)
        if not self.filepath:
            return self.fail(group, "No configuration file selected")
        try:
            config = load_config_file(self.filepath)
            group.from_config(config)
            if config.scenes:
                paths = [
                    item.get("path") if isinstance(item, dict) else item
                    for item in config.scenes
                ]
                _add_paths(group, [p for p in paths if p], source="config")
        except (ConfigError, ValueError) as exc:
            return self.fail(group, f"Invalid configuration: {exc}")
        except Exception as exc:
            return self.fail(group, "Could not load the configuration", exception=exc)
        warnings = getattr(config, "warnings", []) or []
        return self.report_outcome(
            group,
            f"Configuration loaded from {to_forward_slashes(self.filepath)}"
            + (f" ({len(warnings)} warning(s))" if warnings else ""),
        )


class MPP_OT_apply_defaults(_MPPBase, Operator):
    """Reset the panel to the shipped defaults"""

    bl_idname = "mpp.apply_defaults"
    bl_label = "Reset to defaults"

    def execute(self, context):
        group = _group(context)
        from .config.defaults import default_config

        try:
            group.from_config(default_config())
        except Exception as exc:
            return self.fail(group, "Could not reset the settings", exception=exc)
        return self.report_outcome(group, "Settings reset to the package defaults")


# --------------------------------------------------------------------------
# remembered settings
# --------------------------------------------------------------------------
class MPP_OT_save_settings(_MPPBase, Operator):
    """Remember the current panel settings for every future scene"""

    bl_idname = "mpp.save_settings"
    bl_label = "Remember these settings"

    def execute(self, context):
        group = _group(context)
        from .config import panel_state

        target = panel_state.save(group.snapshot_settings(), source=bpy.data.filepath or "")
        if not target:
            return self.fail(group, "Could not write the settings file")
        group.settings_saved_utc = panel_state.describe().get("saved_utc", "")
        group.settings_path = target
        return self.report_outcome(group, f"Settings remembered in {to_forward_slashes(target)}")


class MPP_OT_load_settings(_MPPBase, Operator):
    """Put the remembered settings back into this scene"""

    bl_idname = "mpp.load_settings"
    bl_label = "Use my settings"

    def execute(self, context):
        group = _group(context)
        from . import registration
        from .config import panel_state

        saved = panel_state.load()
        if not saved:
            return self.fail(group, "No settings have been remembered yet")
        try:
            sections = registration.apply_remembered_settings(force=True, quiet=True)
        except Exception as exc:
            return self.fail(group, "Could not apply the remembered settings", exception=exc)
        if not sections:
            return self.fail(group, "The settings file could not be applied to this scene")
        return self.report_outcome(group, f"Remembered settings applied ({', '.join(sections)})")


class MPP_OT_forget_settings(_MPPBase, Operator):
    """Forget the remembered settings"""

    bl_idname = "mpp.forget_settings"
    bl_label = "Forget"

    def execute(self, context):
        group = _group(context)
        from .config import panel_state

        removed = panel_state.clear()
        group.settings_saved_utc = ""
        group.settings_path = panel_state.state_path()
        return self.report_outcome(
            group,
            "Remembered settings deleted" if removed else "There was nothing to forget",
        )


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------
class MPP_OT_load_templates(_MPPBase, Operator):
    """Load (or reload) the camera motion templates"""

    bl_idname = "mpp.load_templates"
    bl_label = "Load motion templates"

    def execute(self, context):
        group = _group(context)
        from .camera.motion_templates import MotionTemplateLibrary

        try:
            config = group.to_config()
            library = MotionTemplateLibrary.from_config(config, logger=LOGGER)
            count = apply_motion_filter(library, parse_motion_filter(group.motion_names))
        except (ConfigError, ValueError) as exc:
            group.motion_count = 0
            group.template_source = ""
            return self.fail(group, f"Motion templates: {exc}")
        except Exception as exc:
            group.motion_count = 0
            group.template_source = ""
            return self.fail(group, "Could not load the motion templates", exception=exc)

        group.motion_count = count
        group.template_source = library.source or "(embedded)"
        message = f"Loaded {count} motion template(s) from {group.template_source}"
        for warning in library.warnings:
            LOGGER.warning("motion templates: %s", warning)
            message += f" | {warning}"
        print(f"[motion pipeline] templates: {', '.join(library.names[:8])}"
              + (" ..." if count > 8 else ""))
        return self.report_outcome(group, message)


# --------------------------------------------------------------------------
# checking / validation
# --------------------------------------------------------------------------
class MPP_OT_check_configuration(_MPPBase, Operator):
    """Check the configuration and the scene list without changing anything"""

    bl_idname = "mpp.check_configuration"
    bl_label = "Check configuration"

    def execute(self, context):
        group = _group(context)
        from .camera.motion_templates import MotionTemplateLibrary

        lines: "list[str]" = []
        problems: "list[str]" = []
        try:
            config = group.to_config()
        except (ConfigError, ValueError) as exc:
            return self.fail(group, f"Configuration is invalid: {exc}")

        problems.extend(validate_batch_config(config, require_output=True))
        entries = _entries(group)
        if not entries:
            problems.append("no scenes are queued")

        try:
            library = MotionTemplateLibrary.from_config(config, logger=LOGGER)
            apply_motion_filter(library, parse_motion_filter(group.motion_names))
            group.motion_count = len(library)
            group.template_source = library.source or "(embedded)"
            lines.append(f"motion templates: {len(library)} from {group.template_source}")
            lines.extend(f"  warning: {w}" for w in library.warnings)
        except Exception as exc:
            problems.append(f"motion templates: {exc}")
            group.motion_count = 0

        for entry in entries:
            if not entry.exists:
                problems.append(f"missing scene file: {entry.path}")
        missing = sum(1 for entry in entries if not entry.exists)
        lines.append(f"scenes: {len(entries)} queued, {missing} missing")
        lines.append(f"project folder: {to_forward_slashes(group.project_folder()) or '(unset)'}")
        lines.append("  sequence/ + scene/ + video/ + render toolkit are created in it")
        lines.append(
            f"validation: {'on' if config.validation.enabled else 'off'}, "
            f"step {config.validation.sample_step}, clearance {config.validation.clearance}"
        )
        lines.append(
            f"search: {'on' if config.search.enabled else 'off'}, "
            f"{config.search.candidate_count} candidate(s) in "
            f"[{config.search.min_radius}, {config.search.max_radius}]"
        )

        if problems:
            text = "PROBLEMS: " + " | ".join(problems[:6])
            for problem in problems:
                LOGGER.warning("configuration: %s", problem)
            _report_to_panel(group, text)
            self.report({"WARNING"}, text)
            return {"CANCELLED"}

        text = "OK. " + " | ".join(lines)
        _report_to_panel(group, text)
        self.report(
            {"INFO"},
            f"Configuration OK: {len(entries)} scene(s), {group.motion_count} template(s)",
        )
        return {"FINISHED"}


class MPP_OT_validate_scenes(_MPPBase, Operator):
    """Open each queued scene and report cameras, geometry and problems"""

    bl_idname = "mpp.validate_scenes"
    bl_label = "Validate scenes"

    def execute(self, context):
        group = _group(context)
        entries = _entries(group)
        if not entries:
            return self.fail(group, "Queue at least one scene first")

        from .core import blender_context as bctx
        from .core.scene_loader import current_blend_path, load_blend_file, open_scene_for_generation

        original_path = current_blend_path()
        reports = []
        problems = []
        summary_lines = []
        for entry in entries:
            item = group.find_scene(entry.path)
            if not entry.exists:
                if item:
                    item.status = "missing"
                    item.note = "the .blend file does not exist"
                problems.append(f"{os.path.basename(entry.path)}: file does not exist")
                continue
            load = open_scene_for_generation(entry)
            if not load.ok:
                if item:
                    item.status = "load_failed"
                    item.note = load.error
                problems.append(f"{os.path.basename(entry.path)}: {load.error}")
                continue
            try:
                report = bctx.scene_report()
                scene_context = bctx.build_scene_context(logger=LOGGER)
                report["ray_caster"] = getattr(scene_context.ray_caster, "description", "none")
                report["scene_warnings"] = list(scene_context.warnings)
                report["source_file"] = to_forward_slashes(entry.path)
                reports.append(report)
                if item:
                    item.camera_count = report["camera_count"]
                    item.detail = (
                        f"{report['camera_count']} camera(s), {report['mesh_count']} mesh(es), "
                        f"frames {report['frame_range'][0]}..{report['frame_range'][1]}"
                    )
                    if report["camera_count"] == 0:
                        item.status = "no_camera"
                        item.note = "no camera in this file"
                        problems.append(f"{os.path.basename(entry.path)}: no camera")
                    else:
                        item.status = "ok"
                        item.note = ""
                summary_lines.append(
                    f"{os.path.basename(entry.path)}: {report['camera_count']} cam, "
                    f"{report['mesh_count']} mesh"
                )
            except Exception as exc:
                if item:
                    item.status = "load_failed"
                    item.note = str(exc)
                problems.append(f"{os.path.basename(entry.path)}: {exc}")

        # Restore the file the user had open.
        if original_path and os.path.isfile(original_path):
            try:
                load_blend_file(original_path)
            except Exception as exc:
                LOGGER.warning("could not restore %s after validation: %s", original_path, exc)

        payload = {
            "checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "scene_count": len(entries),
            "reports": reports,
            "problems": problems,
        }
        if group.output_root:
            try:
                target = save_json_file(
                    os.path.join(ensure_dir(group.output_root), "scene_validation.json"), payload
                )
                payload["written_to"] = to_forward_slashes(target)
            except Exception as exc:
                LOGGER.warning("could not write scene_validation.json: %s", exc)

        if problems:
            text = (
                f"{len(reports)} scene(s) checked, {len(problems)} problem(s): "
                + " | ".join(problems[:4])
            )
            _report_to_panel(group, text)
            self.report({"WARNING"}, text)
            return {"CANCELLED"}
        text = f"All {len(reports)} scene(s) OK. " + " | ".join(summary_lines[:4])
        _report_to_panel(group, text)
        self.report({"INFO"}, text)
        return {"FINISHED"}


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------
def _panel_group(context=None):
    """Current ``scene.mpp``, re-read every time.

    This must **never** be cached across a scene change.  ``open_mainfile()``
    frees the previous scene (and its property groups) while Python keeps a
    reference to the old RNA struct; writing to it afterwards segfaults Blender
    with ``EXCEPTION_ACCESS_VIOLATION`` inside ``RNA_property_float_set``.  The
    generation step below opens scenes, so the group has to be re-fetched after
    every step.
    """
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    if scene is None:
        raise RuntimeError("no active scene")
    group = getattr(scene, "mpp", None)
    if group is None:
        raise RuntimeError("the Motion Pipeline panel is not registered")
    # Cheap self-heal: a factory-settings reset drops ``load_post`` handlers, and a
    # file load drops the timers; without them a newly opened file would neither
    # get the remembered settings nor keep a running batch alive.
    try:
        from . import registration

        registration.ensure_load_handler()
        registration.ensure_timers()
        rearm_driver_timers()
    except Exception:
        pass
    return group


def rearm_driver_timers() -> "list[str]":
    """Re-register the timers a file load may have cleared.

    ``bpy.ops.wm.open_mainfile`` **empties Blender's Python timer registry** (the
    same file-read path that drops script-registered ``load_post`` handlers;
    ``save_as_mainfile`` does not do it).  A panel run is driven by
    ``_generation_tick`` and its *first* unit of work opens the first queued scene
    -- so the loop used to delete its own driver and then sit on
    ``"opening <scene>"`` forever, with the task still reporting ``running``
    (observed: 807 s of no progress, an empty output folder, and
    ``bpy.app.timers.is_registered(_generation_tick) == False``).

    Every timer callback calls this **after** its unit of work, which is the only
    moment the damage is visible: the callback is still on the stack, so it can
    put its own registration back.  Returns the names it had to re-arm, so the
    caller can avoid double-arming by returning ``None`` instead of an interval.
    """
    armed: "list[str]" = []
    try:
        from .core import ui_task

        if ui_task.is_running() and not bpy.app.timers.is_registered(_generation_tick):
            bpy.app.timers.register(_generation_tick, first_interval=TIMER_INTERVAL)
            armed.append("generation")
    except Exception:
        LOGGER.debug("could not re-arm the generation timer", exc_info=True)
    try:
        from .render import render_runner

        if render_runner.is_running() and not bpy.app.timers.is_registered(_render_tick):
            bpy.app.timers.register(_render_tick, first_interval=RENDER_TIMER_INTERVAL)
            armed.append("render")
    except Exception:
        LOGGER.debug("could not re-arm the render timer", exc_info=True)
    try:
        from . import registration

        if registration.ensure_timers():
            armed.append("settings watcher")
    except Exception:
        LOGGER.debug("could not re-arm the settings watcher", exc_info=True)
    if armed:
        LOGGER.info("re-armed timer(s) cleared by a file load: %s", ", ".join(armed))
    return armed


def _generation_tick():
    """Timer callback: generate one unit of work, then re-arm.

    Never performs file-level operations that re-enter Blender's main loop
    (``save_as_mainfile``); those are queued and flushed by
    :func:`_flush_deferred_blends` once the timer stops.

    Its step opens ``.blend`` files, and a file load wipes Blender's timer
    registry -- including this callback's own entry.  The re-arm pass at the
    bottom is what keeps the loop alive; see :func:`rearm_driver_timers`.
    """
    from .core import ui_task

    try:
        state = ui_task.state().state
    except Exception:
        return None

    if state == "cancelling":
        ui_task.finish(state="cancelled")
        _finish_panel()
        rearm_driver_timers()
        return None

    try:
        done = ui_task.step()
    except Exception as exc:
        LOGGER.error("generation step failed: %s", exc, exc_info=True)
        ui_task.abort(f"{type(exc).__name__}: {exc}")
        _finish_panel()
        rearm_driver_timers()
        return None

    snapshot = ui_task.snapshot()
    try:
        _sync_panel(None, snapshot)
    except Exception:
        # The scene may have been replaced; the task itself is still fine.
        LOGGER.debug("could not refresh the panel after a step", exc_info=True)
    if done:
        state = "done" if snapshot["failed"] == 0 else "failed"
        ui_task.finish(state=state)
        _finish_panel()
        return None
    if "generation" in rearm_driver_timers():
        # Already re-registered by hand; returning an interval as well would
        # leave two entries calling this callback.
        return None
    return TIMER_INTERVAL


def _finish_panel() -> None:
    """Push the final status into the panel and put the user's settings back."""
    from .core import ui_task

    try:
        # The run opened every queued scene, so the scene on screen now is not the
        # one the user configured.  Put their settings back before they look.
        from . import registration

        registration.apply_remembered_settings(quiet=True)
    except Exception:
        LOGGER.debug("could not restore the remembered settings after the run", exc_info=True)
    try:
        _sync_panel()
    except Exception:
        LOGGER.debug("could not refresh the panel at the end of a run", exc_info=True)
    del ui_task


def _sync_panel(group=None, snapshot: "dict | None" = None) -> None:
    """Copy the task snapshot into the live status fields.

    Status is written to **both** the scene group and the add-on preferences.
    The preferences copy is the authoritative one for the Status panel because
    generation opens other ``.blend`` files, and scene-level properties are
    reset to their defaults whenever the scene is replaced.
    """
    from .core import ui_task
    from .preferences import get_preferences

    group = group if group is not None else _panel_group()
    snapshot = snapshot or ui_task.snapshot()
    state = {
        "idle": "idle",
        "preparing": "preparing",
        "running": "running",
        "cancelling": "cancelling",
        "done": "done",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(snapshot["state"], snapshot["state"])
    report = ""
    if not ui_task.is_running():
        report = (
            f"{snapshot['generated']} generated, {snapshot['failed']} failed, "
            f"{snapshot['skipped']} skipped in {snapshot['elapsed_seconds']:.1f}s"
        )

    targets = [group]
    preferences = get_preferences()
    if preferences is not None:
        targets.append(preferences)

    for target in targets:
        try:
            target.progress_fraction = snapshot["fraction"]
            target.progress_text = snapshot["stage"]
            target.generated_count = snapshot["generated"]
            target.failed_count = snapshot["failed"]
            target.skipped_count = snapshot["skipped"]
            if hasattr(target, "task_state"):
                target.task_state = state
            if report:
                target.last_report = report
        except Exception:
            # A stale RNA reference must never abort a generation step.
            LOGGER.debug("could not update status on %r", type(target).__name__, exc_info=True)


class MPP_OT_start_generation(_MPPBase, Operator):
    """Generate the camera motion sequences (runs in the background)"""

    bl_idname = "mpp.start_generation"
    bl_label = "Start generation"

    def execute(self, context):
        group = _group(context)
        from .core import ui_task

        if ui_task.is_running():
            # A second press while running is a progress refresh; the timer owns
            # the actual stepping.
            _sync_panel(group)
            return {"FINISHED"}

        entries = [entry for entry in _entries(group) if entry.enabled]
        if not entries:
            return self.fail(group, "Queue at least one enabled scene first")

        try:
            config = group.to_config()
        except (ConfigError, ValueError) as exc:
            return self.fail(group, f"Configuration is invalid: {exc}")

        problems = validate_batch_config(config, require_output=True)
        if problems:
            return self.fail(group, "Cannot start: " + " | ".join(problems[:4]))

        # Snapshot the configuration now, while it is known-good: the run opens
        # every queued .blend, so the scene this group lives on is about to be
        # replaced and its settings would be lost with it.
        remember_settings(group)

        try:
            setup = ui_task.begin(
                config,
                entries,
                camera_selection=group.camera_selection,
                log_callback=_log_callback(group),
            )
        except Exception as exc:
            group.task_state = "failed"
            return self.fail(group, "Could not start generation", exception=exc)

        group.task_state = "running"
        group.progress_fraction = 0.0
        group.progress_text = "starting"
        group.generated_count = 0
        group.failed_count = 0
        group.skipped_count = 0
        group.character_status = (setup.get("provider") or {}).get("status", "")
        group.last_output_root = config.batch.output_root
        group.last_project_folder = setup.get("project_folder", "")
        group.last_report = (
            f"Generating from {setup['scene_count']} scene(s) with "
            f"{setup['template_count']} template(s); {setup['variants']}."
        )
        bpy.app.timers.register(_generation_tick, first_interval=TIMER_INTERVAL)
        self.report({"INFO"}, "Generation started; watch the Status panel")
        return {"FINISHED"}


class MPP_OT_stop_task(_MPPBase, Operator):
    """Stop the running generation task"""

    bl_idname = "mpp.stop_task"
    bl_label = "Stop task"

    def execute(self, context):
        group = _group(context)
        from .core import ui_task

        if not ui_task.is_running():
            group.task_state = "idle"
            group.progress_text = ""
            return self.report_outcome(group, "Nothing is running")
        ui_task.request_cancel("stopped from the panel")
        group.task_state = "cancelling"
        group.progress_text = "cancelling"
        return self.report_outcome(
            group, "Cancellation requested; the current sequence will finish first"
        )


class MPP_OT_open_output_directory(_MPPBase, Operator):
    """Open the project folder in the system file browser"""

    bl_idname = "mpp.open_output_directory"
    bl_label = "Open project folder"

    def execute(self, context):
        group = _group(context)
        target = _project_target(group)
        if not target:
            return self.fail(group, "No project folder is configured")
        target = normalize_path(target)
        if not os.path.isdir(target):
            try:
                ensure_dir(target)
            except OSError as exc:
                return self.fail(group, f"Project folder is not writable: {exc}")
        try:
            bpy.ops.wm.path_open(filepath=target)
        except Exception as exc:
            return self.fail(group, f"Could not open {to_forward_slashes(target)}", exception=exc)
        return self.report_outcome(group, f"Opened {to_forward_slashes(target)}")


class MPP_OT_show_error_report(_MPPBase, Operator):
    """Write a JSON error report and print a summary"""

    bl_idname = "mpp.show_error_report"
    bl_label = "View error report"

    def execute(self, context):
        group = _group(context)
        from .core import ui_task
        from .core.sequence_manager import SequenceManager

        snapshot = ui_task.snapshot()
        output_root = group.output_root or group.last_output_root
        failures = ui_task.failures()
        payload = {
            "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "task": snapshot,
            "panel_report": group.last_report,
            "output_root": to_forward_slashes(output_root) if output_root else "",
            "counts": {
                "generated": group.generated_count,
                "failed": group.failed_count,
                "skipped": group.skipped_count,
            },
            "failures": failures,
            "output_summary": SequenceManager(output_root).summary() if output_root else {},
        }
        target = ""
        if output_root:
            try:
                ensure_dir(output_root)
                target = save_json_file(os.path.join(output_root, "ui_error_report.json"), payload)
            except Exception as exc:
                LOGGER.warning("could not write the error report: %s", exc)

        if failures:
            for failure in failures[:5]:
                LOGGER.error("%s: %s", failure.get("sequence_id") or "(scene)", failure.get("error", ""))
            text = (
                f"{len(failures)} failure(s); report at "
                f"{to_forward_slashes(target) if target else '(not written)'}"
            )
            _report_to_panel(group, text)
            self.report({"WARNING"}, text)
            return {"FINISHED"}
        text = f"No failures recorded. Report: {to_forward_slashes(target) if target else '(not written)'}"
        return self.report_outcome(group, text)


# --------------------------------------------------------------------------
# local rendering
# --------------------------------------------------------------------------
def _render_status_targets(group=None):
    """Where render status is mirrored (scene group + preferences)."""
    from .preferences import get_preferences

    targets = []
    try:
        targets.append(group if group is not None else _panel_group())
    except Exception:
        pass
    preferences = get_preferences()
    if preferences is not None:
        targets.append(preferences)
    return targets


def _sync_render_panel(group=None, snapshot: "dict | None" = None) -> None:
    """Copy the render snapshot into the panel's status fields."""
    from .render import render_runner

    snapshot = snapshot or render_runner.snapshot()
    for target in _render_status_targets(group):
        try:
            if hasattr(target, "render_status"):
                target.render_status = snapshot["state"]
            if hasattr(target, "render_progress"):
                target.render_progress = snapshot["fraction"]
            if hasattr(target, "render_current"):
                target.render_current = snapshot.get("current", "")
            if hasattr(target, "render_log_path"):
                target.render_log_path = snapshot.get("log_path", "")
            if hasattr(target, "last_report") and not render_runner.is_running():
                target.last_report = (
                    f"render: {snapshot['done']} rendered, {snapshot['failed']} failed, "
                    f"{snapshot['skipped']} skipped of {snapshot['total']}"
                )
        except Exception:
            LOGGER.debug("could not update render status", exc_info=True)


def _render_tick():
    """Timer callback: advance the render by one unit, then re-arm.

    Rendering happens in child processes, but the parent still has to poll -- and
    a file load in the parent wipes the timer registry just the same, so the
    re-arm pass runs here too.
    """
    from .render import render_runner

    try:
        done = render_runner.step()
    except Exception as exc:
        LOGGER.error("render step failed: %s", exc, exc_info=True)
        render_runner.cancel(f"internal error: {exc}")
        _refresh_render_list()
        _sync_render_panel()
        rearm_driver_timers()
        return None
    snapshot = render_runner.snapshot()
    _sync_render_panel(None, snapshot)
    # Reflect per-sequence state in the list so the user sees rows turn green.
    _refresh_render_list(states_only=True)
    if done:
        _sync_render_panel()
        _refresh_render_list()
        return None
    if "render" in rearm_driver_timers():
        return None
    return RENDER_TIMER_INTERVAL


def _refresh_render_list(*, states_only: bool = False) -> None:
    """Mirror the runner's job states onto the panel rows.

    ``states_only`` updates rows in place: it must never touch the *length* of
    ``render_list``.  Rebuilding the list mid-run would drop every sequence the
    user had not submitted yet (an earlier version did exactly that, so
    "Render all" found only the row it had just rendered).
    """
    from .render import render_runner

    try:
        group = _panel_group()
    except Exception:
        return
    snapshot = render_runner.snapshot()
    try:
        group.render_progress = snapshot["fraction"]
        group.render_current = snapshot.get("current", "")
        group.render_status = snapshot["state"]
    except Exception:
        return

    if states_only:
        # Match on the sequence folder, not on the index: the runner's job list
        # is only the subset that was submitted.
        by_dir = {
            os.path.normcase(job.sequence_dir): job
            for job in render_runner.runner().jobs
        }
        for item in group.render_list:
            job = by_dir.get(os.path.normcase(item.sequence_dir))
            if job is None:
                continue
            try:
                item.state = job.state if job.state in RENDER_STATES_SET else "pending"
                item.detail = job.error or job.video or (
                    f"{job.elapsed:.1f}s" if job.state == "done" else ""
                )
            except Exception:
                continue
        return

    # Full rebuild: this is the path used when a new list is loaded.
    jobs = render_runner.runner().jobs
    group.render_list.clear()
    for job in jobs:
        item = group.render_list.add()
        item.sequence_dir = job.sequence_dir
        item.sequence_id = job.sequence_id
        item.scene_name = job.scene_name
        item.motion_name = job.motion_name
        item.blend = job.blend
        item.storage_mode = job.storage_mode
        item.state = job.state if job.state in RENDER_STATES_SET else "pending"
        item.detail = job.error or job.video or ""
    if group.render_list_index < 0 and len(group.render_list):
        group.render_list_index = 0


class MPP_OT_load_render_sequences(_MPPBase, Operator):
    """Scan a folder for generated sequences and list them"""

    bl_idname = "mpp.load_render_sequences"
    bl_label = "Load sequences"

    def execute(self, context):
        group = _group(context)
        from .render import render_runner

        if render_runner.is_running():
            return self.fail(group, "A render is already running")

        root = group.render_input()
        if not root:
            return self.fail(
                group, "Pick a Sequence root folder (or one Sequence folder) first"
            )
        if not os.path.isdir(root):
            return self.fail(group, f"Not a folder: {to_forward_slashes(root)}")

        try:
            found = render_runner.discover_sequences(root, recursive=group.render_recursive)
        except Exception as exc:
            return self.fail(group, "Could not scan the sequence folder", exception=exc)

        group.render_list.clear()
        for entry in found:
            item = group.render_list.add()
            item.sequence_dir = entry["sequence_dir"]
            item.sequence_id = entry["sequence_id"]
            item.scene_name = entry["scene_name"]
            item.motion_name = entry["motion_name"]
            item.blend = entry["blend"]
            item.storage_mode = entry.get("storage_mode", "blend")
            item.state = "skipped" if entry.get("has_video") else "pending"
            problems = entry.get("problems") or []
            if entry.get("has_video"):
                detail = "video already exists"
            elif entry.get("has_partial_video"):
                # An interrupted render leaves an unplayable video; it must not look
                # like finished work, and the renderer will replace it.
                detail = "unfinished video from an interrupted render; will be re-rendered"
            elif item.storage_mode == "animation":
                detail = "animation only (renders from the source scene)"
            else:
                detail = ""
            item.detail = problems[0] if problems else detail
        group.render_list_index = 0 if len(group.render_list) else -1
        if not group.render_output_root:
            # Pre-fill the save folder so Render is one click away -- and use the
            # project's own video/ folder when the sequence root is a project tree.
            from .core.project import SEQUENCE_DIRNAME

            parent = os.path.dirname(root.rstrip("\\/")) or root
            if os.path.basename(root.rstrip("\\/")).lower() == SEQUENCE_DIRNAME:
                group.render_output_root = os.path.join(parent, "video")
            else:
                group.render_output_root = os.path.join(parent, "render_output")
        if not found:
            return self.fail(group, f"No sequences found under {to_forward_slashes(root)}")
        ready = sum(1 for item in group.render_list if item.state == "pending")
        return self.report_outcome(
            group,
            f"Found {len(found)} sequence(s): {ready} ready to render, "
            f"{len(found) - ready} already have a video",
        )


class MPP_OT_render_selected_sequences(_MPPBase, Operator):
    """Render the sequences ticked in the list (or the selected one)"""

    bl_idname = "mpp.render_selected_sequences"
    bl_label = "Render selected"

    def execute(self, context):
        group = _group(context)
        from .render import render_runner

        if render_runner.is_running():
            return self.fail(group, "A render is already running; press Stop first")
        if not len(group.render_list):
            return self.fail(group, "Press 'Load sequences' first")

        index = group.render_list_index
        if index < 0 or index >= len(group.render_list):
            return self.fail(group, "Select a sequence in the list first")
        chosen = [group.render_list[index]]
        return self._start(group, chosen)

    def _start(self, group, items) -> set:
        from .render import render_runner

        output = group.render_output_root
        if not output:
            return self.fail(group, "Set the 'Save to' folder first")
        # No settings snapshot here: rendering spawns child processes and leaves
        # this scene alone, so nothing needs restoring afterwards.
        jobs = []
        for item in items:
            if not item.renderable():
                LOGGER.warning(
                    "skipping %s: neither a .blend nor an animation payload", item.label()
                )
                continue
            jobs.append(render_runner.RenderJob(
                sequence_dir=item.sequence_dir,
                sequence_id=item.sequence_id or os.path.basename(item.sequence_dir),
                scene_name=item.scene_name,
                motion_name=item.motion_name,
                blend=item.blend,
                storage_mode=item.storage_mode,
            ))
        if not jobs:
            return self.fail(
                group,
                "None of the selected sequences is renderable: each needs either its "
                "sequence .blend or a camera_animation payload plus its source scene",
            )
        try:
            render_runner.start(
                jobs,
                output_root=output,
                options=group.render_options(),
            )
        except Exception as exc:
            return self.fail(group, "Could not start the render", exception=exc)

        _refresh_render_list()
        _sync_render_panel(group)
        bpy.app.timers.register(_render_tick, first_interval=RENDER_TIMER_INTERVAL)
        mode = "check" if group.render_dry_run else "render"
        self.report({"INFO"}, f"Started {mode} of {len(jobs)} sequence(s)")
        return {"FINISHED"}


class MPP_OT_render_all_sequences(_MPPBase, Operator):
    """Render every pending sequence in the list (one click)"""

    bl_idname = "mpp.render_all_sequences"
    bl_label = "Render all"

    def execute(self, context):
        group = _group(context)
        from .render import render_runner

        if render_runner.is_running():
            return self.fail(group, "A render is already running; press Stop first")
        if not len(group.render_list):
            return self.fail(group, "Press 'Load sequences' first")
        pending = [item for item in group.render_list if item.state != "skipped"]
        if not pending:
            return self.fail(
                group, "Every listed sequence already has a video (tick Overwrite to redo them)"
            )
        return MPP_OT_render_selected_sequences._start(self, group, pending)


class MPP_OT_stop_render(_MPPBase, Operator):
    """Stop the running local render"""

    bl_idname = "mpp.stop_render"
    bl_label = "Stop render"

    def execute(self, context):
        group = _group(context)
        from .render import render_runner

        if not render_runner.is_running():
            _sync_render_panel(group)
            return self.report_outcome(group, "No render is running")
        render_runner.cancel("stopped from the panel")
        _sync_render_panel(group)
        return self.report_outcome(
            group, "Stopping: the current Blender render process is being terminated"
        )


class MPP_OT_clear_render_list(_MPPBase, Operator):
    """Clear the render list"""

    bl_idname = "mpp.clear_render_list"
    bl_label = "Clear"

    def execute(self, context):
        group = _group(context)
        count = len(group.render_list)
        group.render_list.clear()
        group.render_list_index = -1
        return self.report_outcome(group, f"Cleared {count} sequence row(s)")


class MPP_OT_open_render_output(_MPPBase, Operator):
    """Open the render save folder"""

    bl_idname = "mpp.open_render_output"
    bl_label = "Open save folder"

    def execute(self, context):
        group = _group(context)
        target = group.render_output_root
        if not target:
            return self.fail(group, "No render save folder is set")
        target = normalize_path(target)
        if not os.path.isdir(target):
            try:
                ensure_dir(target)
            except OSError as exc:
                return self.fail(group, f"Save folder is not writable: {exc}")
        try:
            bpy.ops.wm.path_open(filepath=target)
        except Exception as exc:
            return self.fail(group, f"Could not open {to_forward_slashes(target)}", exception=exc)
        return self.report_outcome(group, f"Opened {to_forward_slashes(target)}")


# --------------------------------------------------------------------------
# quick actions
# --------------------------------------------------------------------------
class MPP_OT_quick_generate(_MPPBase, Operator):
    """Quick action: start sequence generation with the current settings"""

    bl_idname = "mpp.quick_generate"
    bl_label = "Generate sequences"
    bl_description = "Start generating camera motion sequences from the queued scenes"

    def execute(self, context):
        # Delegate so the two buttons can never drift from the full actions.
        return bpy.ops.mpp.start_generation()


class MPP_OT_quick_render(_MPPBase, Operator):
    """Quick action: render the generated sequences to local video"""

    bl_idname = "mpp.quick_render"
    bl_label = "Render video"
    bl_description = (
        "Render the listed sequences to local video. Loads the list first when "
        "the sequence root is set and the list is still empty"
    )

    def execute(self, context):
        group = _group(context)
        from .render import render_runner

        if render_runner.is_running():
            return bpy.ops.mpp.stop_render()
        if not len(group.render_list):
            root = group.render_input()
            if not root:
                return self.fail(
                    group,
                    "Set 'Sequence root' (the generated sequence folder) in the "
                    "Local render panel, or press 'Load sequences' there",
                )
            result = bpy.ops.mpp.load_render_sequences()
            if "CANCELLED" in result:
                return result
        return MPP_OT_render_all_sequences.execute(self, context)


CLASSES = (
    MPP_OT_add_files,
    MPP_OT_add_directory,
    MPP_OT_remove_selected,
    MPP_OT_clear_list,
    MPP_OT_save_scene_list,
    MPP_OT_load_scene_list,
    MPP_OT_save_config,
    MPP_OT_load_config,
    MPP_OT_apply_defaults,
    MPP_OT_save_settings,
    MPP_OT_load_settings,
    MPP_OT_forget_settings,
    MPP_OT_load_templates,
    MPP_OT_check_configuration,
    MPP_OT_validate_scenes,
    MPP_OT_start_generation,
    MPP_OT_stop_task,
    MPP_OT_open_output_directory,
    MPP_OT_show_error_report,
    MPP_OT_load_render_sequences,
    MPP_OT_render_selected_sequences,
    MPP_OT_render_all_sequences,
    MPP_OT_stop_render,
    MPP_OT_clear_render_list,
    MPP_OT_open_render_output,
    MPP_OT_quick_generate,
    MPP_OT_quick_render,
)
