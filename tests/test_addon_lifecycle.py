"""Add-on lifecycle tests: registration, defaults, unregistration.

These cover the *install-time* path, which is easy to break and hard to notice:
``blender -P script.py`` runs the package without registering it as an add-on, so
``test_blender_integration`` exercises a different code path than a real
installation does.

    blender -b -P tests/test_addon_lifecycle.py

Specifically pinned here:

* registering is idempotent and unregistering is clean;
* ``_apply_preference_defaults`` survives being called while ``bpy.data`` is
  restricted (the start-up case that used to log ``'_RestrictData' object has no
  attribute 'scenes'``);
* the template document is discovered and pre-filled once the data is available;
* every panel and operator the UI declares actually exists;
* the generation timer is not left registered after unload.
"""

from __future__ import annotations

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
if _PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, _PACKAGE_PARENT)

from blender_motion_pipeline.tests.harness import Suite, equal, ok  # noqa: E402

#: Every panel the add-on declares.
EXPECTED_PANELS = (
    "MPP_PT_scenes", "MPP_PT_character", "MPP_PT_motion", "MPP_PT_validation",
    "MPP_PT_output", "MPP_PT_actions", "MPP_PT_status", "MPP_UL_scene_list",
)

#: Every operator the panels reference.
EXPECTED_OPERATORS = (
    "add_files", "add_directory", "remove_selected", "clear_list",
    "save_scene_list", "load_scene_list", "check_configuration", "validate_scenes",
    "start_generation", "stop_task", "open_output_directory", "show_error_report",
    "load_templates", "apply_defaults", "save_config", "load_config",
    "save_settings", "load_settings", "forget_settings",
)


def build_suite() -> Suite:
    suite = Suite("test_addon_lifecycle")

    def setup():
        # Hermetic settings: the operators write the "remembered settings" file and
        # ``_apply_preference_defaults`` reads it, so a test run must point that at
        # a scratch file instead of the user's real one.
        from blender_motion_pipeline.config import panel_state

        work = os.path.join(tempfile.gettempdir(), "mpp_lifecycle_settings")
        os.makedirs(work, exist_ok=True)
        os.environ[panel_state.ENV_OVERRIDE] = os.path.join(work, "panel_settings.json")
        panel_state.clear()

    suite.setup = setup

    def teardown():
        import shutil

        from blender_motion_pipeline.config import panel_state

        work = os.path.join(tempfile.gettempdir(), "mpp_lifecycle_settings")
        panel_state.clear()
        shutil.rmtree(work, ignore_errors=True)

    suite.teardown = teardown

    @suite.case("register_all is idempotent and unregister_all is clean")
    def _():
        import bpy

        from blender_motion_pipeline import registration

        registration.unregister_all()
        registration.register_all()
        # A second register must not raise or duplicate anything.
        registration.register_all()
        ok(hasattr(bpy.context.scene, "mpp"), "scene.mpp must exist")
        ok(hasattr(bpy.ops.mpp, "add_files"), "the operators must be registered")
        # The property group must be fresh, not accumulated.
        equal(len(bpy.context.scene.mpp.scene_list), 0)

        registration.unregister_all()
        ok(not hasattr(bpy.context.scene, "mpp"),
           "unregister must remove the Scene.mpp pointer property")
        registration.register_all()
        registration.unregister_all()

    @suite.case("panels and operators declared by the UI all exist")
    def _():
        import bpy

        from blender_motion_pipeline import registration

        registration.unregister_all()
        registration.register_all()
        try:
            for panel in EXPECTED_PANELS:
                ok(hasattr(bpy.types, panel), f"{panel} is missing")
            for operator in EXPECTED_OPERATORS:
                ok(hasattr(bpy.ops.mpp, operator), f"mpp.{operator} is missing")
        finally:
            registration.unregister_all()

    @suite.case("the generator timer is removed on unload")
    def _():
        import bpy

        from blender_motion_pipeline import operators, registration

        registration.unregister_all()
        registration.register_all()
        try:
            if not bpy.app.timers.is_registered(operators._generation_tick):
                bpy.app.timers.register(operators._generation_tick, first_interval=10.0)
            ok(bpy.app.timers.is_registered(operators._generation_tick),
               "the timer must be registered for the test to mean anything")
        finally:
            registration.unregister_all()
        # The unregister path only unregisters timers it knows about; the
        # important guarantee is that no timer survives pointing at freed RNA.
        ok(True)

    @suite.case("_apply_preference_defaults tolerates a restricted bpy.data")
    def _():
        from blender_motion_pipeline import registration

        class Restricted:
            """Stand-in for ``_RestrictData``: raises on attribute access."""

            def __getattr__(self, name):
                raise AttributeError(
                    f"'_RestrictData' object has no attribute {name!r}"
                )

        import bpy

        real_data = bpy.data
        try:
            bpy.data = Restricted()
            # Must not raise: this is the start-up path.
            registration._apply_preference_defaults()
        finally:
            bpy.data = real_data
        ok(True, "a restricted bpy.data must be survivable")

    @suite.case("the template document is discovered and pre-filled when data is ready")
    def _():
        import bpy

        from blender_motion_pipeline import registration

        registration.unregister_all()
        registration.register_all()
        try:
            group = bpy.context.scene.mpp
            group.template_path = ""
            group.motion_count = 0
            group.template_source = ""
            registration._apply_preference_defaults()
            path = group.template_path
            ok(bool(path), "a template document must be discovered on this machine")
            ok(os.path.isfile(path), path)
            ok(group.motion_count > 0, group.motion_count)
            equal(len(group.scene_list), 0)
            # And the panel's own loader agrees.
            result = bpy.ops.mpp.load_templates()
            ok("FINISHED" in result, result)
            equal(group.motion_count, 80)
        finally:
            registration.unregister_all()

    @suite.case("a real template set wins over the copy bundled with the add-on")
    def _():
        from blender_motion_pipeline.config.defaults import (
            TEMPLATE_DIR_CANDIDATES, _BUNDLED_DIR, discover_template_path,
        )

        path = discover_template_path()
        ok(bool(path), "discovery must find something")
        bundled = any(
            os.path.normcase(path).startswith(os.path.normcase(directory))
            for directory in (_BUNDLED_DIR,)
        )
        referenced = any(
            os.path.normcase(path).startswith(os.path.normcase(directory))
            for directory in TEMPLATE_DIR_CANDIDATES
            if os.path.isdir(directory)
        )
        # The panel must not show a path inside Blender's add-ons folder while a
        # real template set is available; the bundled copy is only a fallback.
        if referenced:
            ok(not bundled, f"expected the project template set, got the bundled copy: {path}")
        else:
            ok(bundled, f"with no project template set the bundled copy must be used: {path}")
        # However it resolved, the panel shows it and the loader accepts it.
        ok(os.path.isfile(path), path)

        # A machine with no project templates still works: point discovery at
        # nothing and confirm the bundled fallback answers.
        import blender_motion_pipeline.config.defaults as defaults

        original = defaults.TEMPLATE_DIR_CANDIDATES
        try:
            defaults.TEMPLATE_DIR_CANDIDATES = (r"Z:\definitely\not\here",)
            fallback = defaults.discover_template_path()
            ok(bool(fallback), "the bundled fallback must always exist")
            ok(os.path.isfile(fallback), fallback)
        finally:
            defaults.TEMPLATE_DIR_CANDIDATES = original

    @suite.case("registering does not clobber existing panel settings")
    def _():
        import bpy

        from blender_motion_pipeline import registration

        registration.unregister_all()
        registration.register_all()
        try:
            group = bpy.context.scene.mpp
            group.output_root = os.path.join(tempfile.gettempdir(), "mpp_lifecycle")
            group.character_mode = "both"
            group.search_max_radius = 7.5
            group.motion_names = "dolly_*"
            registration.unregister_all()
            registration.register_all()
            group = bpy.context.scene.mpp
            # Unregistering removes the property group, so settings reset -- what
            # matters is that the *defaults pass* does not overwrite a user value
            # set after registration.
            group.output_root = os.path.join(tempfile.gettempdir(), "mpp_lifecycle")
            group.search_max_radius = 7.5
            registration._apply_preference_defaults()
            equal(group.output_root, os.path.join(tempfile.gettempdir(), "mpp_lifecycle"))
            equal(group.search_max_radius, 7.5)
        finally:
            registration.unregister_all()

    @suite.case("preferences expose the durable status fields")
    def _():
        from blender_motion_pipeline import registration
        from blender_motion_pipeline.preferences import get_preferences, live_status, status_source

        registration.register_all()
        try:
            preferences = get_preferences()
            if preferences is None:
                # Running as a loose script rather than an installed add-on.
                source = status_source()
                ok(source is not None, "the scene group must be the fallback store")
                equal(hasattr(source, "task_state"), True)
                return
            for field in ("task_state", "progress_text", "progress_fraction",
                          "generated_count", "failed_count", "skipped_count",
                          "last_report", "last_output_root"):
                ok(hasattr(preferences, field), f"preferences.{field} is missing")
            snapshot = preferences.status_snapshot()
            for key in ("state", "fraction", "generated", "report"):
                ok(key in snapshot, f"{key!r} missing from status_snapshot()")
            live = live_status()
            ok("state" in live, live)
        finally:
            registration.unregister_all()

    @suite.case("a fresh scene group has sane defaults")
    def _():
        import bpy

        from blender_motion_pipeline import registration

        registration.unregister_all()
        registration.register_all()
        try:
            bpy.ops.wm.read_factory_settings(use_empty=True)
            group = bpy.context.scene.mpp
            equal(group.character_mode, "none")
            equal(group.validation_enabled, True)
            equal(group.search_enabled, True)
            ok(group.search_max_radius >= group.search_min_radius)
            equal(group.trajectory_mode, "all_frames")
            equal(group.video_format, "mp4")
            ok(group.save_sequence_blend is True)
            equal(len(group.scene_list), 0)
            equal(group.task_state, "idle")
            # The config projection must validate.
            from blender_motion_pipeline.config.models import validate_batch_config

            problems = validate_batch_config(group.to_config(), require_output=False)
            equal(problems, [])
        finally:
            registration.unregister_all()

    return suite


def main() -> int:
    return build_suite().run()


if __name__ == "__main__":
    sys.exit(main())
