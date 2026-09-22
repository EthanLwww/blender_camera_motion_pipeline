"""Slim project folder for headless rendering.

A run does not write a bare sequence tree any more: it writes one **project
folder** -- data only, no code -- that can be zipped up, unpacked on a render node
or uploaded to a render service::

    <project folder the user picked>/
      blender_camera_20260213/          <- created by this module
        project.json                    what this project is, and how to render it
        RENDER_README.md                the exact commands
        sequence/                       the sequence tree  (--input-root)
        scene/                          the .blend copies  (sequence.source_blend)
        video/                          render output      (--output-root)

What is deliberately **not** in the folder any more: the renderer, the package and
the launchers.  The render image ships all of them (``IMAGE_PACKAGE``, built from
``docker_blender/``), and ``render-all.sh`` inside that image looks for the
renderer in the project first and falls back to its own copy, so shipping a second
copy only added ~2 MB per project and two places to keep in sync.

The scene copies stay byte-for-byte what generation validated: textures are never
downscaled, re-encoded or repacked on the way out (the only way to ship a lighter
scene is to lighten the source scene itself).

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

#: Name of the headless renderer.  It is *not* copied into the project: the render
#: image ships it, and ``render-all.sh`` falls back to the image copy.
RENDER_SCRIPT = "render_sequences.py"

#: Where the render image keeps the package and the entry points.  The project's
#: ``RENDER_README.md`` and ``project.json`` quote these paths so the commands can
#: be pasted straight into a container.
IMAGE_PACKAGE = "/opt/mpp/blender_camera_motion_pipeline"
IMAGE_RENDERER = IMAGE_PACKAGE + "/render/" + RENDER_SCRIPT
IMAGE_RENDER_ALL = "/usr/local/bin/render-all.sh"

PROJECT_JSON = "project.json"
README_NAME = "RENDER_README.md"


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
    source, so re-running does not re-copy gigabytes.  ``refresh=True`` always
    rewrites, for callers that must not keep a stale file around.
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


def renderall_command(root: str, *, render_all: str = IMAGE_RENDER_ALL) -> str:
    """The one-liner that renders every sequence, inside the render image.

    ``render-all.sh <sequence-root> [video-root]`` reads every
    ``sequence_config.json`` below the sequence root and writes the videos to the
    project's ``video/`` folder unless a second path is given.
    """
    root = normalize_path(root)
    return f'{render_all} "{os.path.join(root, SEQUENCE_DIRNAME)}" "{os.path.join(root, VIDEO_DIRNAME)}"'


def renderer_command(root: str, *, blender: str = "blender", renderer: str = "") -> str:
    """The same render, spelled out for a machine that has Blender but no image.

    *renderer* defaults to the copy inside the render image; pass the path of
    ``render_sequences.py`` on that machine to use its own.
    """
    root = normalize_path(root)
    renderer = renderer or IMAGE_RENDERER
    return (
        f'"{blender}" -b -noaudio --factory-startup -P "{renderer}" -- '
        f'--input-root "{os.path.join(root, SEQUENCE_DIRNAME)}" '
        f'--output-root "{os.path.join(root, VIDEO_DIRNAME)}" --recursive'
    )


def readme_text(
    root: str,
    *,
    created_utc: str = "",
    scenes: "list[str]" = (),
    blender_version: str = "",
) -> str:
    """The ``RENDER_README.md`` shipped in the project folder."""
    root = normalize_path(root)

    def row(name: str, note: str, *, indent: int = 2) -> str:
        return " " * indent + name.ljust(26) + note

    lines = [
        "# Headless render project",
        "",
        f"Created by the Blender camera-motion pipeline on {created_utc or 'unknown date'}.",
        f"Blender used for generation: {blender_version or 'unknown'}.",
        "",
        "This folder is **data only** (the renderer lives in the render image):",
        "",
        "```",
        to_forward_slashes(root) + "/",
        row(SEQUENCE_DIRNAME + "/", "the sequence tree (input)"),
        row(SCENE_DIRNAME + "/", "the .blend each sequence renders from"),
        row(VIDEO_DIRNAME + "/", "render output"),
        row(PROJECT_JSON, "what this project is, and the scene mapping"),
        row(README_NAME, "this file"),
        "```",
        "",
        "## Render everything",
        "",
        "Inside the render image (``docker_blender/``), where ``render-all.sh`` is on "
        "``PATH`` -- it reads every ``sequence_config.json`` below the sequence root and "
        "uses the settings each sequence recorded.  Run it from inside this folder "
        "(the commands use relative paths so the folder stays portable):",
        "",
        "```sh",
        "render-all.sh ./" + SEQUENCE_DIRNAME + " ./" + VIDEO_DIRNAME,
        "```",
        "",
        "The videos land in ``" + VIDEO_DIRNAME + "/<scene>/<motion>/<sequence_id>/``.",
        "Pass a second path to write them somewhere else (do keep it on mounted storage, "
        "anything written inside a container disappears with it).",
        "",
        "Anywhere else with Blender 5.2 and the package (or just the image's copy):",
        "",
        "```sh",
        "blender -b -noaudio --factory-startup -P " + IMAGE_RENDERER + " -- \\",
        "    --input-root ./" + SEQUENCE_DIRNAME + " --output-root ./" + VIDEO_DIRNAME
        + " --recursive",
        "```",
        "",
        "Every sequence produces:",
        "",
        "```",
        "<sequence_id>.mp4             the video",
        "<sequence_id>.json            render details (frames, resolution, engine, timings)",
        "<sequence_id>_camera.txt      per-frame world-to-camera camera trajectory",
        "<sequence_id>_motion_plan.json   the compound plan it was generated from",
        "```",
        "",
        "Useful additions: `--dry-run` (preflight + list), `--engine`, `--samples`, "
        "`--device GPU`, `--shards N` (split the tree over N processes), `--force`, "
        "`--retry-failed`.",
        "",
        "## Scenes",
        "",
        f"The `.blend` copies in `{SCENE_DIRNAME}/` are the scenes the sequences were "
        "generated from -- each `sequence_config.json` records its scene as `source_blend` "
        "(absolute) and `source_scene_rel` (relative to this folder). The renderer falls "
        "back to the relative path automatically, so moving the whole folder needs no "
        "arguments at all. Textures are exactly as they were in the source scene: nothing "
        "is downscaled or repacked here.",
        "",
        "If a scene uses files that only exist on the machine that generated it, bridge "
        "them at render time or pack them once:",
        "",
        "```sh",
        "# A. remap the stored paths while rendering (repeatable)",
        "render-all.sh ./" + SEQUENCE_DIRNAME + " --path-map \"E:/assets=/mnt/assets\"",
        "",
        "# B. pack every external file into the scene copies (copies get bigger, nothing",
        "#    else changes; writes pack_report.json next to scene/)",
        "blender -b -P " + IMAGE_PACKAGE + "/render/pack_textures.py -- --scene-root ./"
        + SCENE_DIRNAME,
        "```",
        "",
    ]
    if scenes:
        lines += ["## Scenes in this project", ""]
        lines += [f"* `{to_forward_slashes(name)}`" for name in scenes]
        lines.append("")
    return "\n".join(lines)


@dataclass
class ProjectLayout:
    """Where a run writes its sequences, scenes and video (data only)."""

    project_root: str
    root: str
    sequence_root: str = ""
    scene_root: str = ""
    video_root: str = ""
    created_utc: str = ""
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
        layout.load_manifest()
        if logger is not None:
            logger.info("project folder: %s (data only: sequence/, scene/, video/)", layout.root)
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
    def render_command(self, *, render_all: str = IMAGE_RENDER_ALL) -> str:
        """The command that renders this folder inside the render image."""
        return renderall_command(self.root, render_all=render_all)

    def renderer_command(self, *, blender: str = "blender", renderer: str = "") -> str:
        """The same render without the image (needs Blender + the package)."""
        return renderer_command(self.root, blender=blender, renderer=renderer)

    def describe(self, *, indent: str = "  ") -> str:
        """Multi-line folder summary (panel labels and logs)."""
        lines = [
            f"{indent}{to_forward_slashes(self.root)}   (data only -- no package copy)",
            f"{indent}{indent}{SEQUENCE_DIRNAME}/   sequences (metadata, trajectory, animation payload)",
            f"{indent}{indent}{SCENE_DIRNAME}/   .blend copies the sequences are rendered from",
            f"{indent}{indent}{VIDEO_DIRNAME}/   render output",
        ]
        return "\n".join(lines)

    def render_hint(self) -> str:
        return f"Render the folder with: {self.render_command()}"

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "layout": "slim",
            "created_utc": self.created_utc,
            "project_root": to_forward_slashes(self.project_root),
            "root": to_forward_slashes(self.root),
            "sequence_root": to_forward_slashes(self.sequence_root),
            "scene_root": to_forward_slashes(self.scene_root),
            "video_root": to_forward_slashes(self.video_root),
            "render_command": self.render_command(),
            "renderer_command": self.renderer_command(),
            "image_package": IMAGE_PACKAGE,
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
    """``ProjectLayout.create`` for a data-only project folder.

    *package_root* is accepted for backwards compatibility but unused: the slim
    layout ships data only, and the renderer comes from the render image.
    """
    return ProjectLayout.create(
        project_root,
        package_root=package_root,
        when=when,
        logger=logger,
        blender_version=blender_version,
    )
