"""Project-folder tests (pure Python, no bpy).

A run writes one self-contained *project folder* rather than a bare sequence tree,
because that folder is what gets zipped to a render node::

    <project root>/blender_camera_<date>/
        sequence/   the sequence tree          (--input-root)
        scene/      a copy of every source .blend
        video/      render output              (--output-root)
        render_sequences.py, pack_textures.py, <package>/, README, launchers

These cases pin the layout, the copy semantics (idempotent, de-duplicated), the
recorded relative scene path and the fact that scripts copied to the project root
can still find the package.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import _boot  # noqa: E402,F401  (the add-on folder may be called anything)

PACKAGE_ROOT = os.path.dirname(HERE)

from blender_motion_pipeline.core import project as project_mod  # noqa: E402
from blender_motion_pipeline.io import json_io  # noqa: E402
from blender_motion_pipeline.tests.harness import Suite, equal, ok, raises  # noqa: E402


def _write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def build_suite() -> Suite:
    suite = Suite("test_project_layout")
    scratch = os.path.join(tempfile.gettempdir(), "mpp_project_tests")
    work = os.path.join(scratch, f"run_{int(time.time() * 1000) % 100000}")
    os.makedirs(work, exist_ok=True)

    @suite.case("the project folder is dated and lives inside the chosen folder")
    def _():
        equal(project_mod.project_folder_name(0.0), "blender_camera_19700101")
        stamp = project_mod.project_folder_name()
        ok(stamp.startswith("blender_camera_"), stamp)
        equal(len(stamp), len("blender_camera_") + 8, stamp)
        ok(stamp[16:].isdigit(), stamp)

    @suite.case("create() builds sequence/scene/video and ships the render toolkit")
    def _():
        layout = project_mod.ProjectLayout.create(
            os.path.join(work, "created"), package_root=PACKAGE_ROOT
        )
        for directory in (layout.sequence_root, layout.scene_root, layout.video_root):
            ok(os.path.isdir(directory), directory)
            ok(os.path.normcase(directory).startswith(os.path.normcase(layout.root) + os.sep),
               directory)
        equal(os.path.basename(layout.sequence_root), "sequence")
        equal(os.path.basename(layout.scene_root), "scene")
        equal(os.path.basename(layout.video_root), "video")

        # The renderer and the package it imports must be inside the project, or
        # the folder cannot render anywhere else.
        for name in ("render_sequences.py", "pack_textures.py"):
            ok(os.path.isfile(os.path.join(layout.root, name)), name)
        copied_package = os.path.join(layout.root, os.path.basename(PACKAGE_ROOT))
        ok(os.path.isfile(os.path.join(copied_package, "__init__.py")), copied_package)
        ok(os.path.isfile(os.path.join(copied_package, "_bootstrap.py")), copied_package)
        ok(os.path.isfile(os.path.join(copied_package, "config", "models.py")),
           "the package copy must be complete")
        ok(not os.path.isdir(os.path.join(copied_package, "__pycache__")),
           "byte-code caches must not be shipped")
        ok(layout.toolkit["package_files"] > 20, layout.toolkit)
        equal(layout.toolkit["missing"], [])

    @suite.case("create() refuses to guess a project folder")
    def _():
        raises(project_mod.ProjectError,
               lambda: project_mod.ProjectLayout.create("", package_root=PACKAGE_ROOT))
        raises(project_mod.ProjectError,
               lambda: project_mod.ProjectLayout.create("   ", package_root=PACKAGE_ROOT))

    @suite.case("re-running on the same day reuses the folder (so --resume works)")
    def _():
        root = os.path.join(work, "reuse")
        first = project_mod.ProjectLayout.create(root, package_root=PACKAGE_ROOT)
        marker = _write(os.path.join(first.sequence_root, "keep.txt"), "keep me")
        second = project_mod.ProjectLayout.create(root, package_root=PACKAGE_ROOT)
        equal(second.root, first.root)
        ok(os.path.isfile(marker), "an existing project folder must not be wiped")

    @suite.case("auto-detection ships the package itself, never a parent or a subpackage")
    def _():
        # Regression, twice over: ``core/project.py`` lives in a subpackage, so a
        # naive walk-up for ``__init__.py`` shipped ``core/`` as "the package" (no
        # renderer, no _bootstrap.py, and the renderer then died with
        # ``No module named 'blender_motion_pipeline.core'``); going to the other
        # extreme copied the whole workspace, including the package's own ``.git``
        # (hundreds of files, permission errors, a half-finished copy).
        layout = project_mod.create_project(os.path.join(work, "autodetect"))
        package_name = os.path.basename(PACKAGE_ROOT)
        equal(layout.toolkit["package_dir"], os.path.join(layout.root, package_name))
        equal(layout.toolkit["missing"], [])
        ok(os.path.isfile(os.path.join(layout.root, package_name, "core", "project.py")),
           "the copied package must be complete")
        ok(os.path.isfile(os.path.join(layout.root, "render_sequences.py")),
           "the renderer must be copied to the project root")
        ok(not os.path.isdir(os.path.join(layout.root, package_name, ".git")),
           "version-control metadata must not be shipped")
        folders = sorted(
            name for name in os.listdir(layout.root)
            if os.path.isdir(os.path.join(layout.root, name))
        )
        equal(folders, sorted([package_name, "scene", "sequence", "video"]),
              "nothing from outside the package may be copied into the project")

    @suite.case("scene copies are idempotent and keep the original's timestamps")
    def _():
        layout = project_mod.ProjectLayout.create(os.path.join(work, "scenes"),
                                                 package_root=PACKAGE_ROOT)
        source = _write(os.path.join(work, "src", "room.blend"), "scene-bytes")
        os.utime(source, (1_600_000_000, 1_600_000_000))

        copy = layout.stage_scene(source)
        equal(os.path.dirname(copy), layout.scene_root)
        equal(os.path.basename(copy), "room.blend")
        with open(copy, encoding="utf-8") as handle:
            equal(handle.read(), "scene-bytes")
        equal(int(os.path.getmtime(copy)), 1_600_000_000, "copy2 must preserve the mtime")

        first_mtime = os.path.getmtime(copy)
        equal(layout.stage_scene(source), copy, "a repeat call must return the same copy")
        equal(os.path.getmtime(copy), first_mtime, "an unchanged scene must not be re-copied")
        equal(len(os.listdir(layout.scene_root)), 1)

        # Changed source -> re-copied.
        _write(source, "scene-bytes-v2")
        os.utime(source, (1_600_000_100, 1_600_000_100))
        equal(layout.stage_scene(source), copy)
        with open(copy, encoding="utf-8") as handle:
            equal(handle.read(), "scene-bytes-v2")

    @suite.case("two scenes with the same file name do not overwrite each other")
    def _():
        layout = project_mod.ProjectLayout.create(os.path.join(work, "clash"),
                                                 package_root=PACKAGE_ROOT)
        first = _write(os.path.join(work, "a", "room.blend"), "from-a")
        second = _write(os.path.join(work, "b", "room.blend"), "from-b")
        copy_a = layout.stage_scene(first)
        copy_b = layout.stage_scene(second)
        ok(copy_a != copy_b, (copy_a, copy_b))
        equal(sorted(os.listdir(layout.scene_root)), ["room.blend", "room_2.blend"])
        with open(copy_a, encoding="utf-8") as handle:
            equal(handle.read(), "from-a")
        with open(copy_b, encoding="utf-8") as handle:
            equal(handle.read(), "from-b")
        equal(layout.copy_for(second), copy_b)

    @suite.case("relative_scene only answers for paths inside the project")
    def _():
        layout = project_mod.ProjectLayout.create(os.path.join(work, "relative"),
                                                 package_root=PACKAGE_ROOT)
        inside = os.path.join(layout.scene_root, "room.blend")
        equal(layout.relative_scene(inside), "scene/room.blend")
        equal(layout.relative_scene(os.path.join(layout.sequence_root, "a", "b.json")),
              "sequence/a/b.json")
        equal(layout.relative_scene(r"Z:\elsewhere\room.blend"), "")
        equal(layout.relative_scene(""), "")

    @suite.case("copy_file refreshes the toolkit but not the scenes")
    def _():
        source = _write(os.path.join(work, "refresh", "src.py"), "one")
        target = os.path.join(work, "refresh", "copy.py")
        ok(project_mod.copy_file(source, target), "the first copy writes")
        ok(not project_mod.copy_file(source, target), "an identical copy is skipped")
        # A scene edited in place has a new mtime, so it is copied again even when
        # the size did not change (copy2 preserves the nanosecond timestamp).
        _write(source, "two")
        ok(project_mod.copy_file(source, target), "a changed file is copied again")
        with open(target, encoding="utf-8") as handle:
            equal(handle.read(), "two")
        # Toolkit copies are always refreshed, so the shipped renderer can never be
        # older than the add-on that wrote the project.
        ok(not project_mod.copy_file(source, target), "nothing changed, nothing to do")
        ok(project_mod.copy_file(source, target, refresh=True), "refresh always rewrites")
        raises(project_mod.ProjectError,
               lambda: project_mod.copy_file(os.path.join(work, "nope.py"), target))

    @suite.case("the README and project.json say how to render the folder")
    def _():
        layout = project_mod.ProjectLayout.create(os.path.join(work, "docs"),
                                                 package_root=PACKAGE_ROOT)
        layout.stage_scene(_write(os.path.join(work, "docsrc", "room001.blend"), "x"))
        readme = layout.write_readme(scenes=["scene/room001.blend"], blender_version="5.2.2 LTS")
        manifest = layout.write_manifest(metadata={"generated": 7}, blender_version="5.2.2 LTS")

        with open(readme, encoding="utf-8") as handle:
            text = handle.read()
        for needle in ("sequence/", "scene/", "video/", "render_sequences.py",
                       "pack_textures.py", "--input-root", "--output-root",
                       "--path-map", "scene/room001.blend", "5.2.2 LTS", "render_project.bat"):
            ok(needle in text, f"the README must mention {needle!r}")

        payload = json_io.load_json_file(manifest)
        equal(payload["generated"], 7, "metadata must survive into project.json")
        equal(payload["blender_version"], "5.2.2 LTS")
        equal(payload["schema_version"], 1)
        equal(payload["sequence_root"], project_mod.to_forward_slashes(layout.sequence_root))
        equal(payload["toolkit_missing"], [])
        equal(payload["scenes"][0]["relative"], "scene/room001.blend")
        equal(sorted(payload["toolkit_scripts"]), ["pack_textures.py", "render_sequences.py"])
        ok("--input-root" in payload["render_command"], payload["render_command"])

    @suite.case("the launchers point at this folder and forward extra arguments")
    def _():
        layout = project_mod.ProjectLayout.create(os.path.join(work, "launch"),
                                                 package_root=PACKAGE_ROOT)
        layout.write_launchers()
        bat = os.path.join(layout.root, "render_project.bat")
        sh = os.path.join(layout.root, "render_project.sh")
        ok(os.path.isfile(bat) and os.path.isfile(sh), (bat, sh))
        with open(bat, encoding="utf-8") as handle:
            bat_text = handle.read()
        with open(sh, encoding="utf-8") as handle:
            sh_text = handle.read()
        for text in (bat_text, sh_text):
            ok("--background" in text, text[:200])
            ok("--factory-startup" in text, text[:200])
            ok("sequence" in text and "video" in text, text[:200])
            ok("BLENDER" in text, "the launcher must let the render node name its Blender")
        ok("%*" in bat_text, "the Windows launcher must forward extra arguments")
        ok('"$@"' in sh_text, "the POSIX launcher must forward extra arguments")

    @suite.case("scripts copied to the project root still find the package")
    def _():
        # This is what makes the folder portable: ``render_sequences.py`` sits at the
        # project root and the package in a *subfolder*, which the walk-up search in
        # _bootstrap used to miss.
        from blender_motion_pipeline import _bootstrap

        layout = project_mod.ProjectLayout.create(os.path.join(work, "bootstrap"),
                                                 package_root=PACKAGE_ROOT)
        script = os.path.join(layout.root, "render_sequences.py")
        nested = os.path.join(layout.root, os.path.basename(PACKAGE_ROOT), "_bootstrap.py")
        equal(_bootstrap.locate(script), nested)

        # ... and the shapes that already worked must keep working.
        equal(_bootstrap.locate(os.path.join(PACKAGE_ROOT, "render", "x.py")),
              os.path.join(PACKAGE_ROOT, "_bootstrap.py"))
        equal(_bootstrap.locate(os.path.join(PACKAGE_ROOT, "tests", "x.py")),
              os.path.join(PACKAGE_ROOT, "_bootstrap.py"))
        beside = os.path.join(work, "beside", "script.py")
        os.makedirs(os.path.dirname(beside), exist_ok=True)
        _write(beside, "# a farm-side copy\n")
        import shutil

        shutil.copytree(PACKAGE_ROOT, os.path.join(os.path.dirname(beside),
                                                   os.path.basename(PACKAGE_ROOT)),
                        ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
        equal(_bootstrap.locate(beside),
              os.path.join(os.path.dirname(beside), os.path.basename(PACKAGE_ROOT),
                           "_bootstrap.py"))

    @suite.case("a parent folder with __init__.py does not shadow the package")
    def _():
        # Measured in the wild: this machine's Blender add-ons folder
        # (``%APPDATA%/.../scripts/addons/``) contains an ``__init__.py``, so a rule
        # that picked the *outermost* folder with one registered the historical name
        # against ``addons/`` -- and running the installed CLI died with
        # ``No module named 'blender_motion_pipeline.config'``.
        import shutil
        import subprocess

        from blender_motion_pipeline import _bootstrap

        fake_addons = os.path.join(work, "shadow", "addons")
        os.makedirs(fake_addons, exist_ok=True)
        _write(os.path.join(fake_addons, "__init__.py"), "")
        package = os.path.join(fake_addons, os.path.basename(PACKAGE_ROOT))
        shutil.copytree(
            PACKAGE_ROOT, package, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", ".git", "*.pyc"),
        )
        bootstrap = os.path.join(package, "_bootstrap.py")
        equal(_bootstrap.package_root(bootstrap), package,
              "the nearest folder with __init__.py + _bootstrap.py is the package")
        equal(_bootstrap.locate(os.path.join(package, "render", "x.py")), bootstrap)
        equal(_bootstrap.locate(os.path.join(package, "tests", "x.py")), bootstrap)
        # ``addons/__init__.py`` must not be mistaken for the package root.
        equal(_bootstrap.package_root(os.path.join(fake_addons, "x.py")), "")

        # A fresh interpreter, exactly like the CLI: the alias must resolve to the
        # package, not to the shadowing parent.
        script = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('_b', r'{bootstrap}')\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            f"print(module.bootstrap(r'{bootstrap}'))\n"
            "from blender_motion_pipeline.config import models\n"
            "from blender_motion_pipeline.render import pack_textures\n"
            "print(models.__file__)\n"
            f"print(pack_textures.__file__)\n"
        )
        completed = subprocess.run([sys.executable, "-c", script],
                                   capture_output=True, text=True)
        equal(completed.returncode, 0, (completed.stderr or "")[-600:])
        printed = completed.stdout.strip().splitlines()
        equal(printed[0], os.path.basename(PACKAGE_ROOT))
        equal(os.path.normcase(printed[1]), os.path.normcase(
            os.path.join(package, "config", "models.py")))
        equal(os.path.normcase(printed[2]), os.path.normcase(
            os.path.join(package, "render", "pack_textures.py")))

    @suite.case("pack_textures takes its own arguments after --")
    def _():
        # Blender eats everything before ``--``, so a standalone tool that parses
        # ``sys.argv[1:]`` sees Blender's own flags and dies with "unrecognized
        # arguments: -b -P ...".  The renderer had this right; the packer did not.
        from blender_motion_pipeline.render import pack_textures

        parsed = pack_textures.parse_args([
            r"D:\SteamLibrary\...\blender.exe", "-b", "-P", r"X:\proj\pack_textures.py",
            "--", "--scene-root", r"X:\proj\scene", "--dry-run",
        ])
        equal(parsed.scene_root, [r"X:\proj\scene"])
        equal(parsed.dry_run, True)
        equal(parsed.compress, True)
        equal(parsed.recursive, True)

        # Under a plain interpreter the whole argv is ours.
        plain = pack_textures.parse_args(["pack_textures.py", "--list", "--no-compress"])
        equal(plain.list, True)
        equal(plain.compress, False)

    @suite.case("the shipped pack_textures helper scans a project's scene folder")
    def _():
        from blender_motion_pipeline.render import pack_textures

        layout = project_mod.ProjectLayout.create(os.path.join(work, "pack"),
                                                 package_root=PACKAGE_ROOT)
        layout.stage_scene(_write(os.path.join(work, "packsrc", "a.blend"), "a"))
        layout.stage_scene(_write(os.path.join(work, "packsrc", "b.blend"), "b"))
        _write(os.path.join(layout.scene_root, "notes.txt"), "not a scene")
        _write(os.path.join(layout.scene_root, "nested", "c.blend"), "c")

        found = [os.path.basename(path) for path in
                 pack_textures.collect_scenes(layout.scene_root)]
        equal(sorted(found), ["a.blend", "b.blend", "c.blend"])
        top = [os.path.basename(path) for path in
               pack_textures.collect_scenes(layout.scene_root, recursive=False)]
        equal(sorted(top), ["a.blend", "b.blend"])
        equal(pack_textures.collect_scenes(os.path.join(layout.scene_root, "a.blend")),
              [os.path.join(layout.scene_root, "a.blend")])
        equal(pack_textures.collect_scenes(os.path.join(work, "missing")), [])
        # The helper resolves the package from a project-root copy too.
        equal(pack_textures._find_bootstrap(
            os.path.join(layout.root, "pack_textures.py")),
            os.path.join(layout.root, os.path.basename(PACKAGE_ROOT), "_bootstrap.py"))

    return suite


if __name__ == "__main__":
    raise SystemExit(build_suite().run())
