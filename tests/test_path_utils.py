"""Path/IO helper tests (pure Python)."""

from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from blender_motion_pipeline.io import json_io, manifest, resource_check  # noqa: E402
from blender_motion_pipeline.io import path_utils as pu  # noqa: E402
from blender_motion_pipeline.tests.harness import (  # noqa: E402
    Suite, equal, ok, raises,
)


def build_suite() -> Suite:
    suite = Suite("test_path_utils")

    @suite.case("looks_absolute detects drive, UNC and POSIX roots")
    def _():
        ok(pu.looks_absolute(r"E:\a\b.blend"), "drive path")
        ok(pu.looks_absolute("E:/a/b.blend"), "drive path forward slashes")
        ok(pu.looks_absolute("/mnt/e/a.blend"), "posix path")
        ok(pu.looks_absolute(r"\\server\share\a.blend"), "unc path")
        ok(not pu.looks_absolute("scenes/a.blend"), "relative path")

    @suite.case("to_forward_slashes keeps UNC prefixes")
    def _():
        equal(pu.to_forward_slashes(r"E:\a\b"), "E:/a/b")
        equal(pu.to_forward_slashes(r"\\srv\share\a"), "//srv/share/a")
        equal(pu.to_forward_slashes(""), "")

    @suite.case("safe_filename strips illegal characters and reserved names")
    def _():
        equal(pu.safe_filename('a<b>c:d"e/f\\g|h?i*j'), "a_b_c_d_e_f_g_h_i_j")
        equal(pu.safe_filename("   "), "unnamed")
        equal(pu.safe_filename("CON"), "_CON")
        equal(pu.safe_filename("..."), "unnamed")
        equal(pu.safe_filename("  scene 001  "), "scene_001")

    @suite.case("slugify is ascii and dash separated")
    def _():
        equal(pu.slugify("Dolly In 01"), "dolly-in-01")
        equal(pu.slugify("鍦烘櫙"), "unnamed")
        equal(pu.slugify("a///b"), "a-b")

    @suite.case("apply_path_mappings rewrites the longest matching prefix")
    def _():
        mappings = [
            {"from": r"E:\UE", "to": "/mnt/e/UE"},
            {"from": r"E:\UE\DataGenScenes", "to": "/srv/data"},
        ]
        # The longest matching prefix wins, and the result stays POSIX because
        # the mapping target is POSIX (this is the Linux render-node case).
        equal(
            pu.apply_path_mappings(r"E:\UE\DataGenScenes\x\y.json", mappings),
            "/srv/data/x/y.json",
        )
        equal(
            pu.apply_path_mappings(r"E:\UE\Other\z.blend", mappings),
            "/mnt/e/UE/Other/z.blend",
        )
        equal(pu.apply_path_mappings(r"D:\untouched\a.blend", mappings), r"D:\untouched\a.blend")
        equal(pu.apply_path_mappings("", mappings), "")
        # Windows target from a POSIX source (the reverse direction).
        back = pu.apply_path_mappings("/mnt/e/scenes/a.blend", [{"from": "/mnt/e", "to": r"D:\work"}])
        equal(back, "D:/work/scenes/a.blend")
        # Exact-prefix match with no tail.
        equal(pu.apply_path_mappings(r"E:\UE\DataGenScenes", mappings), "/srv/data")
        # A substring that is not a path prefix must not match.
        equal(pu.apply_path_mappings(r"E:\UExtra\a.blend", mappings), r"E:\UExtra\a.blend")

    @suite.case("parse_path_mappings accepts strings, dicts and pairs")
    def _():
        parsed = pu.parse_path_mappings([r"E:\a=D:\b", {"from": "/x", "to": "/y"}, ("p", "q"), "bad"])
        equal(len(parsed), 3)
        equal(parsed[0], (r"E:\a", r"D:\b"))
        equal(parsed[1], ("/x", "/y"))
        equal(parsed[2], ("p", "q"))

    @suite.case("--path-map round trip: what parse returns, apply must understand")
    def _():
        # Regression: ``parse_path_mappings`` returns tuples, but
        # ``apply_path_mappings`` only read dicts/attributes, so the documented
        # ``--path-map`` flag parsed fine and then rewrote nothing at all.  The two
        # halves were only ever tested apart from each other.
        for pairs_value in (r"E:\scenes=/mnt/e/scenes", {"from": r"E:\scenes", "to": "/mnt/e/scenes"}):
            mappings = pu.parse_path_mappings([pairs_value])
            equal(len(mappings), 1)
            equal(
                pu.apply_path_mappings(r"E:\scenes\room\a.blend", mappings),
                "/mnt/e/scenes/room/a.blend",
            )
            equal(
                pu.apply_path_mappings(r"E:\elsewhere\a.blend", mappings),
                r"E:\elsewhere\a.blend",
            )
        # Several rules: the longest matching prefix still wins.
        mappings = pu.parse_path_mappings([r"E:\s=/a", r"E:\s\deep=/b"])
        equal(pu.apply_path_mappings(r"E:\s\deep\x.blend", mappings), "/b/x.blend")
        equal(pu.apply_path_mappings(r"E:\s\other\x.blend", mappings), "/a/other/x.blend")

    @suite.case("sanitize_relpath cleans every component")
    def _():
        equal(pu.sanitize_relpath("scene/../motion<1>/seq"), "scene/../motion_1/seq")
        equal(pu.sanitize_relpath(""), "")
        equal(pu.sanitize_relpath("./a/./b"), "a/b")
        equal(pu.sanitize_relpath("a/b:c/d"), "a/b_c/d")

    @suite.case("is_subpath and relative_to behave across the tree")
    def _():
        with tempfile.TemporaryDirectory() as tmp:
            child = os.path.join(tmp, "a", "b")
            os.makedirs(child)
            ok(pu.is_subpath(child, tmp), "child inside parent")
            ok(pu.is_subpath(tmp, tmp), "path is its own subpath")
            ok(not pu.is_subpath(tmp, child), "parent is not inside child")
            equal(pu.relative_to(child, tmp), "a/b")

    @suite.case("unique_path and sequence_folder_name")
    def _():
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "x.blend")
            open(target, "w").close()
            suggested = pu.unique_path(target)
            equal(os.path.basename(suggested), "x_001.blend")
        equal(pu.sequence_folder_name(1), "sequence_000001")
        equal(pu.sequence_folder_name(123456), "sequence_123456")

    @suite.case("ensure_dir raises on an empty path and creates parents")
    def _():
        raises(ValueError, lambda: pu.ensure_dir(""))
        with tempfile.TemporaryDirectory() as tmp:
            created = pu.ensure_dir(os.path.join(tmp, "a", "b", "c"))
            ok(os.path.isdir(created), "nested directory created")

    @suite.case("json round trip preserves unicode and survives a BOM")
    def _():
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "data.json")
            payload = {"鍦烘櫙": "鎴块棿001", "n": 3, "nested": {"a": [1, 2, 3]}}
            json_io.dump_json_file(path, payload)
            ok(os.path.isfile(path), "file written")
            equal(json_io.load_json_file(path), payload)
            with open(path, "r", encoding="utf-8-sig") as handle:
                text = handle.read()
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\ufeff" + text)
            equal(json_io.load_json_file(path)["n"], 3)

    @suite.case("malformed json raises JsonError with position info")
    def _():
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"a": 1,,}')
            error = raises(json_io.JsonError, lambda: json_io.load_json_file(path))
            ok("line" in str(error), f"line info present: {error}")
            equal(json_io.load_json_file(os.path.join(tmp, "nope.json"), default={"d": 1}, required=False), {"d": 1})

    @suite.case("manifest writer appends and reports counts")
    def _():
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scene", "motion", "manifest.json")
            writer = manifest.ManifestWriter(path, kind="motion", scope={"scene_name": "s"})
            writer.add_sequence({"sequence_id": "sequence_000001", "status": "ok"})
            writer.add_sequence({"sequence_id": "sequence_000002", "status": "ok"})
            writer.add_failure({"sequence_id": "sequence_000003", "error": "boom"})
            writer.flush()
            stored = json_io.load_json_file(path)
            equal(stored["sequence_count"], 2)
            equal(stored["failure_count"], 1)
            again = manifest.ManifestWriter(path, kind="motion")
            equal(len(again.data["sequences"]), 2)
            again.add_sequence({"sequence_id": "sequence_000001", "status": "updated"})
            equal(len(again.data["sequences"]), 2)
            equal(again.data["sequences"][0]["status"], "updated")

    @suite.case("resource check reports missing files and remaps paths")
    def _():
        with tempfile.TemporaryDirectory() as tmp:
            present = os.path.join(tmp, "tex.png")
            open(present, "w").close()
            resources = [
                ("image", present, "img1"),
                ("image", os.path.join(tmp, "missing.png"), "img2"),
                ("library", os.path.join(tmp, "lib.blend"), "lib"),
            ]
            result = resource_check.check_blend_resources(resources)
            equal(result.checked, 3)
            equal(len(result.missing), 2)
            ok(not result.ok)
            ok("2 of 3" in result.summary(), result.summary())

            other = os.path.join(tmp, "mapped")
            os.makedirs(other)
            mapped = resource_check.check_blend_resources(
                [("image", os.path.join(tmp, "tex.png"), "img1")],
                mappings=[{"from": tmp, "to": other}],
            )
            equal(len(mapped.remapped), 1)
            equal(len(mapped.missing), 1)

    @suite.case("check_path_mappings flags unusable targets")
    def _():
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "target")
            os.makedirs(good)
            report = resource_check.check_path_mappings(
                [(tmp, good), ("/nope", "/also/nope")],
                sample_paths=[os.path.join(tmp, "a.blend")],
            )
            equal(report["ok"], False)
            equal(len(report["unresolved_targets"]), 1)
            equal(report["samples"][0]["changed"], True)

    return suite


if __name__ == "__main__":
    raise SystemExit(build_suite().run())
