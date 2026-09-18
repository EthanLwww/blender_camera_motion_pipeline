"""Add-on preferences: where the templates live and how the renderer is invoked.

The preferences are deliberately small.  Anything that varies per run belongs in
the panel or in a config JSON; these are the machine-level facts that should be
set once.
"""

from __future__ import annotations

from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences

from .config.defaults import discover_template_path
from .io.path_utils import normalize_path

ADDON_PACKAGE = __package__ or "blender_motion_pipeline"


class MPP_AddonPreferences(AddonPreferences):
    bl_idname = ADDON_PACKAGE

    template_path: StringProperty(
        name="Default motion templates",
        description=(
            "Camera motion template JSON used when the panel does not specify one. "
            "Leave empty to auto-discover the reference document"
        ),
        default="",
        subtype="FILE_PATH",
    )
    auto_discover_templates: BoolProperty(
        name="Auto-discover templates",
        description="Probe the known reference locations on start-up",
        default=True,
    )
    auto_load_templates: BoolProperty(
        name="Load templates on register",
        description="Populate the template count as soon as the add-on loads",
        default=True,
    )
    default_output_root: StringProperty(
        name="Default output folder",
        description="Pre-filled output folder for new scenes",
        default="",
        subtype="DIR_PATH",
    )
    log_level: EnumProperty(
        name="Log level",
        items=(
            ("DEBUG", "Debug", "Very chatty"),
            ("INFO", "Info", "Normal"),
            ("WARNING", "Warning", "Only problems"),
            ("ERROR", "Error", "Only failures"),
        ),
        default="INFO",
    )
    keep_session_after_run: BoolProperty(
        name="Keep the loaded scene after a run",
        description=(
            "Leave the last processed scene loaded when a batch finishes. "
            "Disable to return to the original file"
        ),
        default=True,
    )
    max_sequences_per_run: IntProperty(
        name="Safety limit",
        description="Refuse to queue more than this many sequences in one run (0 = no limit)",
        default=5000,
        min=0,
        max=1000000,
    )
    write_run_log: BoolProperty(
        name="Write a run log",
        description="Append details to <output>/motion_pipeline.log",
        default=True,
    )

    # -- live task status -------------------------------------------------
    # Mirrored here, not only on the scene, because generation opens other
    # ``.blend`` files: scene-level properties are reset when the scene is
    # replaced, which would wipe the progress display mid-run.
    task_state: StringProperty(name="Task state", default="idle")
    progress_text: StringProperty(name="Progress", default="")
    progress_fraction: FloatProperty(name="Progress", default=0.0, min=0.0, max=1.0)
    generated_count: IntProperty(name="Generated", default=0, min=0)
    failed_count: IntProperty(name="Failed", default=0, min=0)
    skipped_count: IntProperty(name="Skipped", default=0, min=0)
    last_report: StringProperty(name="Last report", default="")
    last_output_root: StringProperty(name="Last output", default="")

    # Live *render* status, mirrored for the same reason as the task status.
    render_status: StringProperty(name="Render state", default="idle")
    render_progress: FloatProperty(name="Render progress", default=0.0, min=0.0, max=1.0)
    render_current: StringProperty(name="Rendering", default="")
    render_log_path: StringProperty(name="Render log", default="")
    render_output_root: StringProperty(name="Render output", default="")

    def draw(self, context):
        layout = self.layout
        column = layout.column()
        column.prop(self, "template_path")
        column.prop(self, "auto_discover_templates")
        column.prop(self, "auto_load_templates")
        discovered = discover_template_path()
        if discovered:
            column.label(text=f"Discovered: {discovered[:60]}", icon="CHECKMARK")
        else:
            column.label(text="No template document discovered", icon="ERROR")
        column.separator()
        column.prop(self, "default_output_root")
        column.prop(self, "log_level")
        column.prop(self, "keep_session_after_run")
        column.prop(self, "write_run_log")
        column.prop(self, "max_sequences_per_run")

    def resolved_template_path(self) -> str:
        """The template file the add-on should actually use."""
        if self.template_path:
            return normalize_path(self.template_path)
        if self.auto_discover_templates:
            return discover_template_path()
        return ""

    def status_snapshot(self) -> dict:
        return {
            "state": self.task_state,
            "progress_text": self.progress_text,
            "fraction": float(self.progress_fraction),
            "generated": int(self.generated_count),
            "failed": int(self.failed_count),
            "skipped": int(self.skipped_count),
            "report": self.last_report,
            "output_root": self.last_output_root,
            "render_state": self.render_status,
            "render_fraction": float(self.render_progress),
            "render_current": self.render_current,
            "render_log_path": self.render_log_path,
        }


def get_preferences(context=None):
    """Return the add-on preferences, or ``None`` when the add-on is not installed.

    ``blender -P script.py`` runs the package without registering it as an
    add-on, in which case ``preferences.addons`` has no entry for it.  Callers
    must handle ``None``; see :func:`status_source`.
    """
    import bpy

    context = context or bpy.context
    preferences = getattr(context, "preferences", None) or bpy.context.preferences
    addons = getattr(preferences, "addons", None)
    if addons is not None:
        entry = addons.get(ADDON_PACKAGE)
        if entry is not None:
            return entry.preferences
    return None


def status_source(context=None):
    """Where the Status panel should read live task state from.

    Preference order:

    1. the add-on preferences, when the add-on is installed -- the only store
       that survives the scene changes generation performs;
    2. the scene's own property group, so the panel still works when the package
       is driven by ``register_all()`` without being installed as an add-on.
       Scene state is reset when a new ``.blend`` is opened, so in that mode the
       panel can only show the status of scenes that have not been replaced --
       which is why ``core.ui_task`` (a module-level singleton) is the real
       source of truth and the operators copy from it on every redraw.
    """
    preferences = get_preferences(context)
    if preferences is not None:
        return preferences
    import bpy

    context = context or bpy.context
    scene = getattr(context, "scene", None)
    return getattr(scene, "mpp", None)


def live_status(context=None) -> dict:
    """Live task status for the panel.

    The in-memory task state (``core.ui_task``) is authoritative for anything
    happening *now*, because it survives the scene changes generation performs;
    the durable store is layered underneath so a finished run keeps showing its
    result.  A scene change resets scene-scoped properties, which is exactly the
    failure mode this ordering avoids.
    """
    from .core import ui_task

    payload: dict = {}
    source = status_source(context)
    if source is not None:
        if hasattr(source, "status_snapshot"):
            payload = source.status_snapshot()
        else:
            payload = {
                "state": getattr(source, "task_state", "idle"),
                "progress_text": getattr(source, "progress_text", ""),
                "fraction": float(getattr(source, "progress_fraction", 0.0)),
                "generated": int(getattr(source, "generated_count", 0)),
                "failed": int(getattr(source, "failed_count", 0)),
                "skipped": int(getattr(source, "skipped_count", 0)),
                "report": getattr(source, "last_report", ""),
                "output_root": getattr(source, "last_output_root", ""),
            }

    snapshot = ui_task.snapshot()
    if snapshot.get("state") != "idle" or snapshot.get("stage"):
        payload["state"] = snapshot["state"]
        payload["progress_text"] = snapshot.get("stage") or payload.get("progress_text", "")
        payload["fraction"] = snapshot.get("fraction", payload.get("fraction", 0.0))
        payload["generated"] = snapshot.get("generated", payload.get("generated", 0))
        payload["failed"] = snapshot.get("failed", payload.get("failed", 0))
        payload["skipped"] = snapshot.get("skipped", payload.get("skipped", 0))
    return payload


def default_output_root(context=None) -> str:
    preferences = get_preferences(context)
    if preferences is None:
        return ""
    return normalize_path(preferences.default_output_root) if preferences.default_output_root else ""


CLASSES = (MPP_AddonPreferences,)
