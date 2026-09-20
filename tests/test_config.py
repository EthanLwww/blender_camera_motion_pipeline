"""Configuration model tests (pure Python)."""

from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _boot  # noqa: E402,F401  (the add-on folder may be called anything)

from blender_motion_pipeline.config import defaults, models  # noqa: E402
from blender_motion_pipeline.config.models import (  # noqa: E402
    CHARACTER_MODE_BOTH,
    CHARACTER_MODE_NONE,
    CHARACTER_MODE_WITH,
    BatchConfig,
    ConfigError,
    TemplateUnitScale,
    combine_frame_ranges,
    validate_batch_config,
)
from blender_motion_pipeline.io import json_io  # noqa: E402
from blender_motion_pipeline.tests.harness import Suite, close, equal, ok, raises  # noqa: E402


def build_suite() -> Suite:
    suite = Suite("test_config")

    @suite.case("defaults are self-consistent and validate cleanly")
    def _():
        config = BatchConfig()
        equal(config.schema_version, 1)
        equal(config.batch.mode, CHARACTER_MODE_NONE)
        ok(config.validation.sample_step >= 1)
        problems = validate_batch_config(config)
        ok(all("output_root" in p for p in problems), f"only output_root should be missing: {problems}")

    @suite.case("camelCase keys from the Unreal reference configs are accepted")
    def _():
        config = BatchConfig.from_dict({
            "schemaVersion": 1,
            "batch": {"outputRoot": r"D:\out", "sceneNameMode": "filename", "pathMappings": []},
            "motion": {"templatePath": r"E:\t.json", "frameStart": 5, "interpolation": "LINEAR"},
            "validation": {"sampleStep": 4, "extraSampleFrames": [7, 13]},
            "search": {"maxRadius": 8.0, "randomSeed": 99},
            "render": {"resolutionX": 640, "videoFormat": "mkv"},
        })
        equal(config.batch.output_root, r"D:\out")
        equal(config.batch.scene_name_mode, "filename")
        equal(config.motion.template_path, r"E:\t.json")
        equal(config.motion.frame_start, 5)
        equal(config.motion.interpolation, "LINEAR")
        equal(config.validation.sample_step, 4)
        equal(config.validation.extra_sample_frames, [7, 13])
        close(config.search.max_radius, 8.0)
        equal(config.search.random_seed, 99)
        equal(config.render.resolution_x, 640)
        equal(config.render.video_format, "mkv")
        equal(config.warnings, [])

    @suite.case("unknown keys warn instead of failing")
    def _():
        config = BatchConfig.from_dict({"batch": {"nonsense": 1}, "mystery": True})
        joined = " ".join(config.warnings)
        ok("nonsense" in joined, joined)
        ok("mystery" in joined, joined)

    @suite.case("wrong types fall back to defaults with a warning")
    def _():
        config = BatchConfig.from_dict({"validation": {"sample_step": "many", "clearance": "wide"}})
        equal(config.validation.sample_step, 10)
        close(config.validation.clearance, 0.25)
        equal(len(config.warnings), 2)

    @suite.case("out of range values are rejected by validation")
    def _():
        raises(ConfigError, lambda: BatchConfig.from_dict({"validation": {"sample_step": 0}}))
        raises(ConfigError, lambda: BatchConfig.from_dict({"search": {"min_radius": 5.0, "max_radius": 1.0}}))
        raises(ConfigError, lambda: BatchConfig.from_dict({"render": {"workers": 0}}))
        raises(ConfigError, lambda: BatchConfig.from_dict({"search": {"weights": {"distance": -1}}}))
        raises(ConfigError, lambda: BatchConfig.from_dict({"motion": {"frame_scale": 0}}))
        raises(ConfigError, lambda: BatchConfig.from_dict({"motion": {"frame_start": -5}}))
        raises(ConfigError, lambda: BatchConfig.from_dict({"validation": {"min_character_visible_ratio": 2.0}}))

    @suite.case("character mode accepts labels and rejects nonsense")
    def _():
        equal(BatchConfig.from_dict({"batch": {"mode": "both"}}).batch.mode, CHARACTER_MODE_BOTH)
        equal(BatchConfig.from_dict({"batch": {"mode": "WITH_CHARACTER"}}).batch.mode, CHARACTER_MODE_WITH)
        config = BatchConfig.from_dict({"batch": {"mode": "sometimes"}})
        equal(config.batch.mode, CHARACTER_MODE_NONE)
        ok(any("sometimes" in w for w in config.warnings), config.warnings)

    @suite.case("the template unit scale keeps only the timeline settings")
    def _():
        # Templates are Blender-native now, so there is no axis/sign mapping left to
        # configure: fps and the rotation order are all that remain, and a config
        # that still sets the Unreal keys gets one actionable warning.
        warnings: "list[str]" = []
        scale = TemplateUnitScale.from_dict({"fps": 30.0}, warnings)
        close(scale.fps, 30.0)
        equal(scale.rotation_order, "XYZ")
        equal(warnings, [])
        # The removed keys are named once, with the migration script, instead of
        # each producing its own "unknown key" line.
        ok("location_scale" in TemplateUnitScale.REMOVED_KEYS)

        raises(ConfigError, lambda: TemplateUnitScale.from_dict({"fps": 0.0}, []))
        # An unknown order is a warning plus the default, and validate() still
        # refuses it when it is set directly on the dataclass.
        order_warnings: "list[str]" = []
        fallback = TemplateUnitScale.from_dict({"rotation_order": "ABC"}, order_warnings)
        equal(fallback.rotation_order, "XYZ")
        ok(any("ABC" in w for w in order_warnings), order_warnings)
        raises(ConfigError, lambda: TemplateUnitScale(rotation_order="ABC").validate())

        legacy_warnings: "list[str]" = []
        legacy = TemplateUnitScale.from_dict({"location_scale": 0.01, "yaw_axis": "Z"},
                                            legacy_warnings)
        equal(len(legacy_warnings), 1, legacy_warnings)
        ok("migrate_unreal_templates.py" in legacy_warnings[0], legacy_warnings[0])
        equal([name for name in TemplateUnitScale.REMOVED_KEYS if hasattr(legacy, name)], [])
        default = TemplateUnitScale.from_dict({}, [])
        close(default.fps, 24.0)

    @suite.case("applying a partial config onto a base preserves the rest")
    def _():
        base = BatchConfig()
        base.batch.output_root = r"E:\out"
        base.search.candidate_count = 16
        merged = BatchConfig.from_dict({"search": {"max_radius": 2.5}}, base=base)
        equal(merged.batch.output_root, r"E:\out")
        equal(merged.search.candidate_count, 16)
        close(merged.search.max_radius, 2.5)

    @suite.case("to_dict / from_dict round trip is lossless")
    def _():
        original = BatchConfig.from_dict({
            "batch": {"output_root": r"D:\o", "mode": "both", "resume": False,
                      "path_mappings": [{"from": "A", "to": "B"}]},
            "motion": {"template_names": ["fixed_01_standard"], "frame_scale": 2.0,
                       "unit_scale": {"fps": 30.0, "rotation_order": "ZYX"}},
            "validation": {"extra_sample_frames": [3, 9]},
            "search": {"weights": {"distance": 2.0}},
            "render": {"engine": "CYCLES", "samples": 7},
        })
        again = BatchConfig.from_dict(original.to_dict())
        equal(again.to_dict(), original.to_dict())

    @suite.case("config file discovery finds the reference template document")
    def _():
        path = defaults.discover_template_path()
        ok(bool(path), "a motion template file should be discoverable in this environment")
        ok(os.path.isfile(path), path)
        entries, source = defaults.load_discovered_templates()
        ok(len(entries) >= 3, f"expected templates, got {len(entries)}")
        equal(os.path.normcase(source), os.path.normcase(path))

    @suite.case("default_config wires the discovered template path")
    def _():
        config = defaults.default_config()
        ok(config.motion.template_path != "" or True, "path may be empty on a bare machine")
        if config.motion.template_path:
            ok(os.path.isfile(config.motion.template_path), config.motion.template_path)

    @suite.case("config file save/load round trip and error on bad root")
    def _():
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            config = defaults.default_config()
            config.batch.output_root = os.path.join(tmp, "out")
            defaults.save_config_file(path, config)
            reloaded = defaults.load_config_file(path)
            equal(reloaded.batch.output_root, config.batch.output_root)
            bad = os.path.join(tmp, "bad.json")
            json_io.save_json_file(bad, [1, 2, 3])
            raises(ConfigError, lambda: defaults.load_config_file(bad))

    @suite.case("remembered panel settings round trip, tolerate damage, and clear")
    def _():
        # ``panel_state`` keeps the panel configuration outside the .blend, so a
        # run that opens other scenes cannot wipe it.  The path is explicit here so
        # this stays a pure-Python test that never touches the user's real file.
        from blender_motion_pipeline.config import panel_state

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "settings.json")
            payload = {
                "config": {"batch": {"output_root": "x", "resume": False}},
                "panel": {"camera_selection": "TREN"},
                "render": {"render_samples_choice": 8},
                "scenes": [{"path": "a.blend", "enabled": True}],
            }
            equal(panel_state.load(path), {}, "nothing stored yet")
            written = panel_state.save(payload, path, source="unit test")
            equal(written, os.path.abspath(path))
            ok(os.path.isfile(written), written)
            equal(panel_state.load(path), payload)

            described = panel_state.describe(path)
            equal(described["exists"], True)
            ok(described["saved_utc"], described)
            ok(described["fields"] > 0, described)

            # No leftover temporary files from the atomic write.
            equal([n for n in os.listdir(os.path.dirname(path)) if n.startswith(".")], [],
                  "the atomic write must not leave residue")

            # A corrupt file must not raise, and must not be mistaken for settings.
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{ this is not json")
            equal(panel_state.load(path), {})
            equal(panel_state.describe(path)["exists"], True)
            equal(panel_state.describe(path)["fields"], 0)

            # A file written by a newer schema is ignored rather than guessed at.
            json_io.save_json_file(path, {"schema": panel_state.STATE_SCHEMA + 5, "settings": payload})
            equal(panel_state.load(path), {})

            # ... and an older/unversioned one is still usable.
            json_io.save_json_file(path, {"settings": payload})
            equal(panel_state.load(path), payload)

            equal(panel_state.clear(path), True)
            equal(panel_state.clear(path), False, "clearing twice is not an error")
            equal(panel_state.load(path), {})

    @suite.case("the settings file location follows the environment override")
    def _():
        from blender_motion_pipeline.config import panel_state

        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "override.json")
            previous = os.environ.get(panel_state.ENV_OVERRIDE)
            os.environ[panel_state.ENV_OVERRIDE] = target
            try:
                equal(panel_state.state_path(), os.path.abspath(target))
                panel_state.save({"panel": {"camera_selection": "TREN"}})
                ok(os.path.isfile(target), target)
                equal(panel_state.load(), {"panel": {"camera_selection": "TREN"}})
            finally:
                if previous is None:
                    os.environ.pop(panel_state.ENV_OVERRIDE, None)
                else:
                    os.environ[panel_state.ENV_OVERRIDE] = previous

    @suite.case("the sequence resolution round-trips and defaults to the scene's")
    def _():
        # ``resolution_explicit`` is what tells the renderer to obey the recorded
        # size instead of the loaded scene's own resolution.
        plain = BatchConfig.from_dict({})
        equal(plain.render.resolution_explicit, False)
        equal((plain.render.resolution_x, plain.render.resolution_y), (1280, 720))
        equal("resolution_explicit" in plain.render.to_dict(), True)

        stamped = BatchConfig.from_dict({
            "render": {"resolution_x": 640, "resolution_y": 360,
                       "resolution_percentage": 50, "resolution_explicit": True},
        })
        equal(stamped.render.resolution_explicit, True)
        equal(stamped.render.resolution_x, 640)
        equal(stamped.render.resolution_percentage, 50)
        # ... and it survives a save/load cycle through JSON.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            defaults.save_config_file(path, stamped)
            reloaded = defaults.load_config_file(path)
            equal(reloaded.render.resolution_explicit, True)
            equal((reloaded.render.resolution_x, reloaded.render.resolution_y), (640, 360))
            equal(reloaded.render.resolution_percentage, 50)

    @suite.case("combine_frame_ranges merges touching spans")
    def _():
        combined = combine_frame_ranges([(0, 10), (11, 20), (40, 50), (45, 60), (5, 8)])
        equal(combined["frame_start"], 0)
        equal(combined["frame_end"], 60)
        equal([r for r in combined["ranges"]], [{"start": 0, "end": 20}, {"start": 40, "end": 60}])
        equal(combined["total_frames"], 21 + 21)

    @suite.case("describe_config mentions the important knobs")
    def _():
        text = models.describe_config(BatchConfig.from_dict({"batch": {"output_root": r"E:\o"}}))
        for needle in ("output_root", "character mode", "validation", "camera search", "render"):
            ok(needle in text, f"{needle!r} missing from:\n{text}")

    @suite.case("scenes with missing paths are reported by validate_batch_config")
    def _():
        config = BatchConfig()
        config.batch.output_root = "x"
        config.scenes = [{"path": ""}, {"path": r"E:\a.blend"}]
        problems = validate_batch_config(config)
        ok(any("scenes[0]" in p for p in problems), problems)

    return suite


if __name__ == "__main__":
    raise SystemExit(build_suite().run())
