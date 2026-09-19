"""Self-contained project folder for headless rendering.

A run does not write a bare sequence tree any more: it writes one **project
folder** that can be zipped up and unpacked on a render node, because a render
node needs three things that used to live in three different places::

    <project folder the user picked>/
      blender_camera_20260213/          <- created by this module
        project.json                    what this project is, and how to render it
        RENDER_README.md                the exact commands
        render_project.bat / .sh        convenience launchers
        render_sequences.py             the headless renderer (root copy)
        pack_textures.py                optional: make the scene copies portable
        blender_camera_motion_pipeline/ the package the renderer imports
        sequence/                       the sequence tree  (--input-root)
        scene/                          the .blend copies  (sequence.source_blend)
        video/                          render output      (--output-root)

Why the scene copies: an animation-only sequence records where its camera
animation has to be replayed, and pointing that at ``E:\\UE\\...`` is useless on
a server.  Generation therefore happens **from the copy** in ``scene/`` -- what
ships is exactly what was validated -- and the sequence records both the absolute
path of the copy and its path relative to the project root, so the renderer can
find it again after the project is moved.

The date in the folder name is deliberate: re-running on the same day reuses the
folder (which is what makes ``--resume`` work), while a new day gets a fresh one.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field

from ..io.json_io import save_json_file
from ..io.path_utils import ensure_dir, normalize_path, relative_to, to_forward_slashes

#: Folder created inside the folder the user picks.
PROJECT_PREFIX = "blender_camera_"

#: The three subfolders, and the names used for them in ``project.json``.
SEQUENCE_DIRNAME = "sequence"
SCENE_DIRNAME = "scene"
VIDEO_DIRNAME = "video"

#: Files copied into the project root so the folder renders on its own.
RENDER_SCRIPT = "render_sequences.py"
TOOLKIT_SCRIPTS = (RENDER_SCRIPT, "pack_textures.py")

#: Copied verbatim (the package the renderer imports).  Version-control metadata
#: and caches are never shipped: the package itself is a git checkout here, and a
#: project folder must not carry a .git directory (nor fail on a locked one).
PACKAGE_COPY_SKIP = ("__pycache__", ".git", ".hg", ".svn", ".bzr", ".mypy_cache",
                     ".pytest_cache", ".idea", ".vscode", ".uv-cache")
PACKAGE_COPY_SUFFIXES = (".pyc", ".pyo")

PROJECT_JSON = "project.json"
README_NAME = "RENDER_README.md"
LAUNCHER_BAT = "render_project.bat"
LAUNCHER_SH = "render_project.sh"

#: Written when Blender is not on ``PATH`` on the render node.
LAUNCHER_ENV_HINT = "BLENDER"


class ProjectError(RuntimeError):
    """The project folder could not be created."""


def project_folder_name(when: float | None = None) -> str:
    """``blender_camera_YYYYMMDD`` for *when* (defaults to now, local time)."""
    stamp = time.strftime("%Y%m%d", time.localtime(when if when is not None else time.time()))
    return f"{PROJECT_PREFIX}{stamp}"


def blender_version() -> str:
    """The running Blender version ("" when not running inside Blender)."""
    try:
        import bpy
    except Exception:
        return ""
    return str(getattr(bpy.app, "version_string", "") or "")


def _package_root(path: str) -> str:
    """The add-on package folder that contains *path* (or "").

    Identified by ``__init__.py`` **and** ``_bootstrap.py``, walking up: a bare
    ``__init__.py`` is not enough, because ``core/`` is itself a subpackage and the
    workspace this project lives in has its own ``__init__.py`` files.  Getting this
    wrong ships the wrong tree into the project folder.
    """
    current = os.path.dirname(os.path.abspath(path))
    for _ in range(8):
        if os.path.isfile(os.path.join(current, "__init__.py")) and os.path.isfile(
            os.path.join(current, "_bootstrap.py")
        ):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return ""


def _same_file(source: str, target: str) -> bool:
    """True when *target* is already a copy of *source* (size + mtime).

    ``shutil.copy2`` preserves the modification time to the nanosecond, so this is
    a reliable "the copy is up to date" test -- but it cannot prove which source a
    file came from (two files written in the same instant with the same length look
    identical), which is why :meth:`ProjectLayout.scene_copy_name` also consults the
    recorded ``project.json`` before reusing a name.
    """
    try:
        src = os.stat(source)
        dst = os.stat(target)
    except OSError:
        return False
    return src.st_size == dst.st_size and src.st_mtime_ns == dst.st_mtime_ns


def copy_file(source: str, target: str, *, refresh: bool = False) -> bool:
    """Copy *source* to *target*; returns True when it was written.

    ``refresh=False`` (scene copies) keeps an existing copy that matches the
    source, so re-running does not re-copy gigabytes.  ``refresh=True`` (toolkit
    copies) always rewrites, so the renderer shipped in the project always matches
    the add-on version that generated it.
    """
    source = normalize_path(source)
    target = normalize_path(target)
    if not os.path.isfile(source):
        raise ProjectError(f"cannot copy {source}: file does not exist")
    if not refresh and os.path.isfile(target) and _same_file(source, target):
        return False
    ensure_dir(os.path.dirname(target))
    shutil.copy2(source, target)
    return True


def copy_package(source_root: str, target_root: str) -> int:
    """Copy the add-on package so a render node does not need it installed."""
    source_root = normalize_path(source_root)
    if not source_root or not os.path.isdir(source_root):
        return 0
    if os.path.normcase(source_root) == os.path.normcase(normalize_path(target_root)):
        return 0
    count = 0
    for current, dirnames, filenames in os.walk(source_root):
        dirnames[:] = [
            name for name in dirnames
            if name not in PACKAGE_COPY_SKIP and not name.startswith(".")
        ]
        suffix = os.path.relpath(current, source_root)
        destination = target_root if suffix == os.path else os.path.join(target_root, suffix)
        ensure_dir(destination)
        for filename in filenames:
            if filename.endswith(PACKAGE_COPY_SUFFIXES):
                continue
            shutil.copy2(os.path.join(current, filename), os.path.join(destination, filename))
            count += 1
    return count


def copy_toolkit(package_root: str, root: str, *, logger=None) -> dict:
    """Ship the renderer, the package and the launchers inside *root*."""
    package_root = normalize_path(package_root)
    result = {"scripts": [], "package_files": 0, "package_dir": "", "missing": []}
    if not package_root:
        result["missing"].append("package folder could not be located")
        return result

    for name in TOOLKIT_SCRIPTS:
        source = ""
        for candidate in (
            os.path.join(package_root, "render", name),
            os.path.join(package_root, name),
        ):
            if os.path.isfile(candidate):
                source = candidate
                break
        if not source:
            result["missing"].append(name)
            continue
        try:
            if copy_file(source, os.path.join(root, name), refresh=True):
                result["scripts"].append(name)
        except OSError as exc:
            result["missing"].append(f"{name}: {exc}")

    target_package = os.path.join(root, os.path.basename(package_root))
    try:
        result["package_files"] = copy_package(package_root, target_package)
        result["package_dir"] = target_package
    except OSError as exc:
        result["missing"].append(f"package copy: {exc}")

    if logger is not None:
        if result["missing"]:
            logger.warning("project toolkit incomplete: %s", ", ".join(result["missing"]))
        else:
            logger.info(
                "project toolkit: %d script(s) + %d package file(s) copied into %s",
                len(result["scripts"]), result["package_files"], root,
            )
    return result


def render_command(root: str, *, blender: str = "blender") -> str:
    """The one-liner that renders every sequence in a project folder."""
    root = normalize_path(root)
    return (
        f'"{blender}" --background --factory-startup '
        f'--python "{os.path.join(root, RENDER_SCRIPT)}" -- '
        f'--input-root "{os.path.join(root, SEQUENCE_DIRNAME)}" '
        f'--output-root "{os.path.join(root, VIDEO_DIRNAME)}" --recursive'
    )


def launcher_text(root: str, *, windows: bool) -> str:
    """Content of the convenience launcher shipped in the project folder."""
    root = normalize_path(root)
    if windows:
        return (
            "@echo off\r\n"
            "REM Render every sequence in this project folder (Windows).\r\n"
            "REM Set BLENDER to a blender.exe when it is not on PATH, e.g.\r\n"
            "REM   set BLENDER=C:\\Program Files\\Blender Foundation\\Blender 5.2\\blender.exe\r\n"
            "setlocal\r\n"
            f'if "%{LAUNCHER_ENV_HINT}%"=="" set {LAUNCHER_ENV_HINT}=blender\r\n'
            f'"%{LAUNCHER_ENV_HINT}%" --background --factory-startup '
            f'"%~dp0{RENDER_SCRIPT}" -- '
            f'"%~dp0{SEQUENCE_DIRNAME}" --output-root "%~dp0{VIDEO_DIRNAME}" --recursive %*\r\n'
            "endlocal\r\n"
            "exit /b %ERRORLEVEL%\r\n"
        )
    return (
        "#!/bin/sh\n"
        "# Render every sequence in this project folder (Linux / macOS).\n"
        f"# Set {LAUNCHER_ENV_HINT} to a blender binary when it is not on PATH.\n"
        'HERE=$(cd "$(dirname "$0")" && pwd)\n'
        f': "${{{LAUNCHER_ENV_HINT}:=blender}}"\n'
        f'"${LAUNCHER_ENV_HINT}" --background --factory-startup "$HERE/{RENDER_SCRIPT}" -- \\\n'
        f'    --input-root "$HERE/{SEQUENCE_DIRNAME}" --output-root "$HERE/{VIDEO_DIRNAME}" '
        '--recursive "$@"\n'
    )


def readme_text(
    root: str,
    *,
    created_utc: str = "",
    scenes: "list[str]" = (),
    toolkit: "dict | None" = None,
    blender_version: str = "",
) -> str:
    """The ``RENDER_README.md`` shipped in the project folder."""
    root = normalize_path(root)
    toolkit = toolkit or {}
    package_name = os.path.basename(toolkit.get("package_dir") or "blender_camera_motion_pipeline")

    def row(name: str, note: str, *, indent: int = 2) -> str:
        return " " * indent + name.ljust(34) + note

    lines = [
        "# Headless render project",
        "",
        f"Created by the Blender camera-motion pipeline on {created_utc or 'unknown date'}.",
        f"Blender used for generation: {blender_version or 'unknown'}.",
        "",
        "```",
        to_forward_slashes(root) + "/",
        row(RENDER_SCRIPT, "headless renderer (root copy)"),
        row("pack_textures.py", "optional: pack external files into scene/"),
        row(f"{LAUNCHER_BAT} / .sh", "convenience launchers"),
        row(package_name + "/", "the package the renderer imports"),
        row(SEQUENCE_DIRNAME + "/", "the sequence tree (--input-root)"),
        row(SCENE_DIRNAME + "/", "the .blend each sequence is rendered from"),
        row(VIDEO_DIRNAME + "/", "render output (--output-root)"),
        "```",
        "",
        "## Render everything",
        "",
        "```sh",
        f'blender --background --factory-startup --python "{os.path.join(root, RENDER_SCRIPT)}" -- \\',
        f'    --input-root "{os.path.join(root, SEQUENCE_DIRNAME)}" \\',
        f'    --output-root "{os.path.join(root, VIDEO_DIRNAME)}" --recursive',
        "```",
        "",
        f"Or just run `{LAUNCHER_BAT}` (Windows) / `./{LAUNCHER_SH}` (Linux, macOS). Both set and use",
        f"the `{LAUNCHER_ENV_HINT}` environment variable, falling back to `blender` on `PATH`.",
        "",
        "Every sequence gets three files under "
        f"`{VIDEO_DIRNAME}/<scene>/<motion>/<sequence_id>/`:",
        "",
        "```",
        "<sequence_id>.mp4             the video",
        "<sequence_id>.json            render details (frames, resolution, engine, timings)",
        "<sequence_id>_camera.txt      per-frame world-to-camera camera trajectory",
        "```",
        "",
        "Useful additions: `--dry-run` (list what would render), `--list`, `--overwrite`,",
        "`--frame-start/--frame-end`, `--resolution-x/--resolution-y`, `--engine`, `--samples`,",
        "`--workers N`.",
        "",
        "## Scenes, textures and other external files",
        "",
        f"The `.blend` copies in `{SCENE_DIRNAME}/` are the scenes the sequences were generated from",
        "-- each `sequence_config.json` records its scene as `source_blend` (absolute) and",
        "`source_scene_rel` (relative to this project folder). The renderer falls back to the",
        "relative path automatically, so moving the whole folder to a render node needs no",
        "arguments at all. Textures and other linked files inside those `.blend`s still point at",
        "the machine that generated them; pick one of:",
        "",
        "```sh",
        "# A. bridge the paths at render time (repeatable, applied to scene paths and textures)",
        f"blender -b -P {RENDER_SCRIPT} -- --input-root ./{SEQUENCE_DIRNAME} "
        f"--output-root ./{VIDEO_DIRNAME} \\",
        '    --recursive --path-map "E:/UE/DataGenScenes" "/mnt/data/DataGenScenes"',
        "",
        "# B. pack every external file into the scene copies once, then the folder is portable",
        f"blender -b -P pack_textures.py -- --scene-root ./{SCENE_DIRNAME}",
        "```",
        "",
        "Option B rewrites the copies in place (they get bigger, nothing else changes) and writes",
        "`pack_report.json` listing what was packed and what could not be found.",
        "",
        "## Regenerating the sequences (optional)",
        "",
        "The package shipped here also contains the generator, so the project can be regenerated or",
        "extended on another machine that has Blender:",
        "",
        "```sh",
        "blender -b -P blender_camera_motion_pipeline/motion_pipeline_cli.py -- \\",
        f'    --config "{os.path.join(root, SEQUENCE_DIRNAME, "batch_config.json")}" \\',
        '    --scenes "<path to a .blend>" --output-root "<a project folder>"',
        "```",
        "",
    ]
    if scenes:
        lines += ["## Scenes in this project", ""]
        lines += [f"* `{to_forward_slashes(name)}`" for name in scenes]
        lines.append("")
    missing = list(toolkit.get("missing") or [])
    if missing:
        lines += ["> Note: the toolkit copy was incomplete: " + ", ".join(missing), ""]
    return "\n".join(lines)


@dataclass
class ProjectLayout:
    """Where a run writes its sequences, scenes, video and render toolkit."""

    project_root: str
    root: str
    sequence_root: str = ""
    scene_root: str = ""
    video_root: str = ""
    created_utc: str = ""
    toolkit: dict = field(default_factory=dict)
    #: ``(original, copy)`` pairs, in the order they were staged.
    scene_copies: "list[tuple[str, str]]" = field(default_factory=list)
    notes: "list[str]" = field(default_factory=list)

    def __post_init__(self) -> None:
        self.project_root = normalize_path(self.project_root)
        self.root = normalize_path(self.root)
        self.sequence_root = self.sequence_root or os.path.join(self.root, SEQUENCE_DIRNAME)
        self.scene_root = self.scene_root or os.path.join(self.root, SCENE_DIRNAME)
        self.video_root = self.video_root or os.path.join(self.root, VIDEO_DIRNAME)

    # -- construction ----------------------------------------------------
    @classmethod
    def create(
        cls,
        project_root: str,
        *,
        package_root: str = "",
        when: float | None = None,
        created_utc: str = "",
        blender_version: str = "",
        logger=None,
    ) -> "ProjectLayout":
        """Create (or reuse) the dated project folder under *project_root*."""
        if not str(project_root or "").strip():
            raise ProjectError(
                "no project folder is configured; pick the folder the project is written into"
            )
        project_root = normalize_path(project_root)
        try:
            ensure_dir(project_root)
        except OSError as exc:
            raise ProjectError(f"project folder is not writable: {project_root} ({exc})") from exc

        layout = cls(project_root=project_root, root=os.path.join(project_root, project_folder_name(when)))
        for directory in (layout.root, layout.sequence_root, layout.scene_root, layout.video_root):
            try:
                ensure_dir(directory)
            except OSError as exc:
                raise ProjectError(f"cannot create {directory}: {exc}") from exc

        layout.created_utc = created_utc or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        layout.toolkit = copy_toolkit(package_root, layout.root, logger=logger)
        layout.load_manifest()
        layout.write_launchers()
        return layout

    # -- previous runs ---------------------------------------------------
    def load_manifest(self) -> dict:
        """Seed the scene mapping from a previous run's ``project.json``.

        Re-running into the same folder (same day) must keep every source scene on
        the copy it already has, otherwise a fresh run could hand an existing
        ``scene/room.blend`` to a different source file that happens to have the
        same name, size and timestamp.
        """
        from ..io.json_io import load_json_file

        payload = load_json_file(os.path.join(self.root, PROJECT_JSON), default={},
                                 required=False)
        if not isinstance(payload, dict):
            return {}
        for entry in payload.get("scenes") or []:
            if not isinstance(entry, dict):
                continue
            original = normalize_path(str(entry.get("original") or ""))
            copy = normalize_path(str(entry.get("copy") or ""))
            if original and copy and os.path.isfile(copy):
                pair = (original, copy)
                if pair not in self.scene_copies:
                    self.scene_copies.append(pair)
        if self.scene_copies:
            self.notes.append(
                f"reusing {len(self.scene_copies)} scene copy/copies recorded by an earlier run"
            )
        return payload

    # -- scenes ----------------------------------------------------------
    def scene_copy_name(self, source: str) -> str:
        """File name to use inside ``scene/`` for *source*.

        Two scenes with the same file name from different folders must not
        overwrite each other, so a name already claimed by a *different* scene gets
        a numeric suffix.  A name claimed by this very file is reused, which is what
        makes a repeated run skip the copy.
        """
        source = normalize_path(source)
        known = self.copy_for(source)
        if known:
            return os.path.basename(known)
        base = os.path.basename(source)
        if not base:
            raise ProjectError(f"cannot copy {source!r}: it has no file name")
        claimed = {
            os.path.normcase(os.path.basename(target))
            for original, target in self.scene_copies
            if os.path.normcase(original) != os.path.normcase(source)
        }
        stem, extension = os.path.splitext(base)
        candidate = base
        index = 2
        while True:
            existing = os.path.join(self.scene_root, candidate)
            taken = os.path.normcase(candidate) in claimed
            if not taken and (not os.path.isfile(existing) or _same_file(source, existing)):
                return candidate
            candidate = f"{stem}_{index}{extension}"
            index += 1

    def stage_scene(self, source: str, *, logger=None) -> str:
        """Copy *source* into ``scene/`` and return the copy's path.

        Idempotent: an unchanged copy is reused, so a repeated run does not copy
        hundreds of megabytes again.
        """
        source = normalize_path(source)
        target = os.path.join(self.scene_root, self.scene_copy_name(source))
        wrote = copy_file(source, target)
        pair = (source, target)
        if pair not in self.scene_copies:
            self.scene_copies.append(pair)
        if logger is not None:
            logger.info(
                "%s scene copy: %s -> %s", "wrote" if wrote else "reused", source, target
            )
        return target

    def relative_scene(self, path: str) -> str:
        """*path* relative to the project root, or "" when it is outside it."""
        raw = normalize_path(path)
        if not raw:
            return ""
        if os.path.normcase(raw).startswith(os.path.normcase(self.root) + os.sep):
            return to_forward_slashes(relative_to(raw, self.root))
        return ""

    def copy_for(self, original: str) -> str:
        """The staged copy of *original*, or "" when it was not staged."""
        wanted = os.path.normcase(normalize_path(original))
        for source, target in self.scene_copies:
            if os.path.normcase(source) == wanted:
                return target
        return ""

    # -- reporting -------------------------------------------------------
    def render_command(self, *, blender: str = "blender") -> str:
        return render_command(self.root, blender=blender)

    def describe(self, *, indent: str = "  ") -> str:
        """Multi-line folder summary (panel labels and logs)."""
        lines = [
            f"{indent}{to_forward_slashes(self.root)}",
            f"{indent}{indent}{SEQUENCE_DIRNAME}/   sequences (metadata, trajectory, animation payload)",
            f"{indent}{indent}{SCENE_DIRNAME}/   .blend copies the sequences are rendered from",
            f"{indent}{indent}{VIDEO_DIRNAME}/   render output",
            f"{indent}{indent}{RENDER_SCRIPT} + "
            f"{os.path.basename(self.toolkit.get('package_dir') or 'the package')} (headless render toolkit)",
        ]
        return "\n".join(lines)

    def render_hint(self) -> str:
        return f"Render the folder with {LAUNCHER_BAT}, or: {self.render_command()}"

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "created_utc": self.created_utc,
            "project_root": to_forward_slashes(self.project_root),
            "root": to_forward_slashes(self.root),
            "sequence_root": to_forward_slashes(self.sequence_root),
            "scene_root": to_forward_slashes(self.scene_root),
            "video_root": to_forward_slashes(self.video_root),
            "render_script": to_forward_slashes(os.path.join(self.root, RENDER_SCRIPT)),
            "render_command": self.render_command(),
            "package_dir": to_forward_slashes(self.toolkit.get("package_dir") or ""),
            "package_files": int(self.toolkit.get("package_files") or 0),
            "toolkit_scripts": list(self.toolkit.get("scripts") or []),
            "toolkit_missing": list(self.toolkit.get("missing") or []),
            "scenes": [
                {
                    "original": to_forward_slashes(source),
                    "copy": to_forward_slashes(target),
                    "relative": self.relative_scene(target),
                }
                for source, target in self.scene_copies
            ],
            "notes": list(self.notes),
        }

    # -- files -----------------------------------------------------------
    def write_launchers(self) -> "list[str]":
        written = []
        for name, windows in ((LAUNCHER_BAT, True), (LAUNCHER_SH, False)):
            path = os.path.join(self.root, name)
            try:
                with open(path, "w", encoding="utf-8", newline="") as handle:
                    handle.write(launcher_text(self.root, windows=windows))
            except OSError as exc:
                self.toolkit.setdefault("missing", []).append(f"{name}: {exc}")
                continue
            if not windows:
                try:
                    os.chmod(path, 0o755)
                except OSError:
                    pass
            written.append(path)
        return written

    def write_manifest(self, *, metadata: "dict | None" = None, blender_version: str = "") -> str:
        """Write ``project.json`` -- what this folder is and how to render it."""
        payload = self.to_dict()
        payload["blender_version"] = blender_version
        payload.update(metadata or {})
        return save_json_file(os.path.join(self.root, PROJECT_JSON), payload)

    def write_readme(
        self,
        *,
        scenes: "list[str]" = (),
        blender_version: str = "",
    ) -> str:
        path = os.path.join(self.root, README_NAME)
        ensure_dir(self.root)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(
                readme_text(
                    self.root,
                    created_utc=self.created_utc,
                    scenes=list(scenes),
                    toolkit=self.toolkit,
                    blender_version=blender_version,
                )
            )
        return path


def create_project(
    project_root: str,
    *,
    package_root: str = "",
    when: float | None = None,
    logger=None,
    blender_version: str = "",
) -> ProjectLayout:
    """``ProjectLayout.create`` with the package folder auto-detected."""
    if not package_root:
        package_root = _package_root(__file__)
    return ProjectLayout.create(
        project_root,
        package_root=package_root,
        when=when,
        logger=logger,
        blender_version=blender_version,
    )
