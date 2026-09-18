"""Registration order for every class the add-on adds to Blender.

Kept in one place so ``register()``/``unregister()`` are trivially symmetric and
so a failure names exactly which class broke.
"""

from __future__ import annotations

from .utils.logging_utils import get_logger

LOGGER = get_logger("registration")

#: True while the start-up "finish the defaults once bpy.data exists" timer is
#: pending.  A module-level flag (rather than a mutable) keeps this importable.
_deferred_timer_active = False

#: Identity of the scene the restore pass last ran for, as ``(filepath, scene)``.
_last_scene_token: "tuple[str, str] | None" = None

#: How often the scene watcher checks whether the active scene changed.
SCENE_WATCH_INTERVAL = 1.0


def _classes():
    """Collect the classes in dependency order (lists before panels, ...)."""
    from . import operators, panels, preferences, properties

    ordered = []
    ordered.extend(properties.CLASSES)      # property groups first
    ordered.extend(operators.CLASSES)       # then operators
    ordered.extend(panels.CLASSES)          # then UIList + panels
    ordered.extend(preferences.CLASSES)     # preferences last
    return ordered


def register_all() -> None:
    import bpy

    from .properties import MPP_SceneProperties

    for cls in _classes():
        try:
            bpy.utils.register_class(cls)
        except ValueError as exc:
            # Already registered (e.g. a reload): harmless, but worth a note.
            if "already registered" in str(exc):
                LOGGER.debug("class %s was already registered", cls.__name__)
                continue
            LOGGER.error("failed to register %s: %s", cls.__name__, exc)
            raise

    if not hasattr(bpy.types.Scene, "mpp"):
        bpy.types.Scene.mpp = bpy.props.PointerProperty(type=MPP_SceneProperties)
    LOGGER.info("Motion Pipeline registered (%d classes)", len(_classes()))

    # The "Motion templates" field must show the template set path from the very
    # first draw.  At start-up ``bpy.data`` is still a restricted stub, so:
    #   1. try immediately (works when enabled from Preferences with a file open);
    #   2. hook ``load_post`` so every later open/new/append fills it;
    #   3. arm a one-shot timer for the GUI case where nothing else fires.
    _apply_preference_defaults_quietly()
    _install_load_handler()
    _ensure_deferred_defaults()
    _install_scene_watcher()


def _install_scene_watcher() -> None:
    """Poll for a scene change and put the remembered settings back.

    ``load_post`` alone is not enough: ``bpy.ops.wm.open_mainfile`` drops handlers
    that a *script* registered (Blender re-enables add-ons instead, which may run
    after the hook already fired), and ``read_factory_settings`` clears the whole
    handler list.  A one-second identity check costs nothing and covers every path
    -- opening a file, running a batch that opens files, resetting to factory
    settings -- so the panel never silently falls back to defaults.
    """
    import bpy

    if bpy.app.timers.is_registered(_scene_watch_tick):
        return
    bpy.app.timers.register(_scene_watch_tick, first_interval=SCENE_WATCH_INTERVAL)
    LOGGER.debug("installed the scene watcher")


def _scene_token() -> "tuple[str, str] | None":
    import bpy

    try:
        scene = bpy.context.scene
    except Exception:
        return None
    if scene is None:
        return None
    return (bpy.data.filepath or "", getattr(scene, "name_full", None) or scene.name)


def _scene_watch_tick():
    """Timer callback: restore the remembered settings whenever the scene changes."""
    global _last_scene_token  # noqa: PLW0603 - module-level one-shot state

    token = _scene_token()
    if token is None:
        return SCENE_WATCH_INTERVAL
    if token != _last_scene_token:
        _last_scene_token = token
        ensure_load_handler()
        apply_remembered_settings(quiet=True)
    return SCENE_WATCH_INTERVAL


def _apply_preference_defaults_quietly() -> None:
    try:
        _apply_preference_defaults()
    except Exception as exc:  # never let a convenience default break registering
        LOGGER.debug("preference defaults deferred: %s", exc)


def _install_load_handler() -> None:
    """Re-run the defaults pass for every newly opened file."""
    import bpy

    if _load_handler in bpy.app.handlers.load_post:
        return
    bpy.app.handlers.load_post.append(_load_handler)
    LOGGER.debug("installed the load_post defaults handler")


def ensure_load_handler() -> bool:
    """Make sure the ``load_post`` hook is installed; ``True`` when it was added.

    ``bpy.ops.wm.read_factory_settings`` clears ``bpy.app.handlers``, and a
    ``register_all()``-driven session (tests, scripts, the CLI) has nothing that
    re-registers the add-on afterwards.  The GUI heals itself because Blender
    re-enables installed add-ons; everywhere else this is called on the paths the
    panel already uses, which costs one membership test.
    """
    import bpy

    missing = _load_handler not in bpy.app.handlers.load_post
    _install_load_handler()
    return missing


def _remove_load_handler() -> None:
    import bpy

    while _load_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_handler)


def _load_handler(_dummy=None) -> None:
    """``load_post`` hook: fill the panel once a file is really open."""
    _apply_preference_defaults_quietly()


def _deferred_defaults_tick():
    """One-shot timer that completes ``_apply_preference_defaults``."""
    global _deferred_timer_active
    try:
        import bpy

        # ``bpy.data.scenes`` raises AttributeError while restricted.
        scenes = list(bpy.data.scenes)
    except Exception:
        return 1.0                      # retry once more in a second
    _deferred_timer_active = False
    _apply_preference_defaults_quietly()
    del scenes
    return None


def _ensure_deferred_defaults() -> None:
    global _deferred_timer_active  # noqa: PLW0603 - module-level one-shot flag
    if _deferred_timer_active:
        return
    try:
        import bpy

        if bpy.data.scenes:
            return                      # nothing to defer
        if not bpy.app.timers.is_registered(_deferred_defaults_tick):
            bpy.app.timers.register(_deferred_defaults_tick, first_interval=1.0)
        _deferred_timer_active = True
    except Exception as exc:
        LOGGER.debug("could not schedule the deferred defaults pass: %s", exc)


def is_registered() -> bool:
    import bpy

    return hasattr(bpy.types.Scene, "mpp")


def panel_group(scene=None):
    """The panel property group for a scene, or ``None`` when unavailable."""
    import bpy

    scene = scene or getattr(bpy.context, "scene", None)
    if scene is None:
        return None
    return getattr(scene, "mpp", None)


def unregister_all() -> None:
    import bpy

    from .core import ui_task

    # Stop any running timers first so they cannot touch freed RNA.
    for function in (_timer_function(), _render_timer_function(),
                     _deferred_defaults_tick, _scene_watch_tick):
        try:
            if bpy.app.timers.is_registered(function):
                bpy.app.timers.unregister(function)
        except Exception:
            pass
    _remove_load_handler()
    try:
        from .render import render_runner

        render_runner.cancel("add-on unloaded")
    except Exception:
        pass
    try:
        ui_task.request_cancel("add-on unloaded")
    except Exception:
        pass

    if hasattr(bpy.types.Scene, "mpp"):
        try:
            del bpy.types.Scene.mpp
        except Exception as exc:
            LOGGER.warning("could not remove Scene.mpp: %s", exc)

    for cls in reversed(_classes()):
        try:
            bpy.utils.unregister_class(cls)
        except Exception as exc:
            LOGGER.debug("could not unregister %s: %s", cls.__name__, exc)
    LOGGER.info("Motion Pipeline unregistered")


def _timer_function():
    from .operators import _generation_tick

    return _generation_tick


def _render_timer_function():
    from .operators import _render_tick

    return _render_tick


def _apply_preference_defaults() -> None:
    """Seed the panel from preferences/template discovery on first register.

    Runs at add-on load time, which on startup happens while ``bpy.data`` is
    still a restricted stub (``_RestrictData``), so every access is guarded.
    """
    import bpy

    from .config.defaults import discover_template_path
    from .preferences import default_output_root, get_preferences

    preferences = get_preferences()
    template = ""
    if preferences is not None:
        template = preferences.resolved_template_path()
    if not template:
        template = discover_template_path()

    try:
        scenes = list(bpy.data.scenes)
    except Exception:
        # Startup: the data-block collections are not available yet.  The panel
        # falls back to discovery the first time it is drawn.
        LOGGER.debug("scene defaults deferred: bpy.data.scenes is not readable yet")
        return

    for scene in scenes:
        group = getattr(scene, "mpp", None)
        if group is None:
            continue
        if not group.template_path and template:
            group.template_path = template
        if not group.output_root:
            root = default_output_root() if preferences is not None else ""
            if root:
                group.output_root = root
        if not group.motion_count and template:
            try:
                from .camera.motion_templates import MotionTemplateLibrary

                library = MotionTemplateLibrary.from_config(group.to_config(), logger=LOGGER)
                group.motion_count = len(library)
                group.template_source = library.source or "(embedded)"
            except Exception as exc:
                LOGGER.debug("template preload skipped: %s", exc)

    apply_remembered_settings()


def apply_remembered_settings(*, force: bool = False, quiet: bool = False) -> "list[str]":
    """Put the remembered panel settings back onto the current scene.

    Called after every file load and at registration, because the panel group
    lives on the scene: a batch run opens each queued ``.blend``, so without this
    the configuration the user just typed is replaced by the next scene's defaults
    (measured: 11 of 14 fields lost when another scene was opened).

    A file that already carries a deliberate configuration keeps it -- only a
    pristine group is seeded -- unless ``force`` is set, which is what the panel's
    "Use my settings" button does.

    Returns the section names that were applied (``[]`` when nothing was).
    """
    import bpy

    from .config import panel_state

    # Self-heal the hook first: ``wm.read_factory_settings`` clears
    # ``bpy.app.handlers``, so a restore attempt is also the moment to make sure
    # the next file load still reaches us.
    _install_load_handler()

    saved = panel_state.load()
    if not saved:
        return []
    try:
        scenes = list(bpy.data.scenes)
    except Exception:
        return []
    applied: "list[str]" = []
    for scene in scenes:
        group = getattr(scene, "mpp", None)
        if group is None:
            continue
        if not force and not group.is_pristine():
            if not quiet:
                LOGGER.debug(
                    "scene %r carries its own configuration; not overwriting it "
                    "with the remembered settings", scene.name,
                )
            continue
        sections = group.apply_settings(saved)
        if sections and scene is getattr(bpy.context, "scene", None):
            applied = sections
    if applied:
        LOGGER.info("remembered settings applied (%s)", ", ".join(applied))
    return applied


def reload_addon() -> str:
    """Unregister then register again (used by the panel's reload action)."""
    unregister_all()
    register_all()
    return "reloaded"


__all__ = [
    "apply_remembered_settings",
    "ensure_load_handler",
    "is_registered",
    "panel_group",
    "register_all",
    "reload_addon",
    "unregister_all",
]
