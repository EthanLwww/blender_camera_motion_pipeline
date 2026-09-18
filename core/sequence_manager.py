"""Inspect, index and prune the generated ``scene/motion/sequence`` tree.

Used by:

* ``--dry-run`` / ``--list`` on the CLI, to show what a render pass would pick up;
* the resume logic in the renderer, to skip sequences already on disk;
* the add-on's "Open output folder" / summary view.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..io.json_io import load_json_file
from ..io.path_utils import normalize_path, relative_to, to_forward_slashes
from ..utils.version import GENERATOR_VERSION

#: Files a complete sequence folder is expected to contain.
REQUIRED_SEQUENCE_FILES = ("sequence_config.json",)
#: Files produced for every sequence.
EXPECTED_ARTIFACTS = (
    "sequence_config.json",
    "generation_log.txt",
)


@dataclass
class SequenceInfo:
    """One sequence folder on disk."""

    sequence_dir: str
    sequence_id: str = ""
    scene_name: str = ""
    motion_name: str = ""
    camera_name: str = ""
    has_character: bool = False
    character_name: str = ""
    character_animation: str = ""
    frame_start: int | None = None
    frame_end: int | None = None
    fps: float | None = None
    generator_version: str = ""
    status: str = "complete"
    problems: "list[str]" = field(default_factory=list)
    files: "dict[str, list[str]]" = field(default_factory=dict)
    config: dict = field(default_factory=dict)

    @property
    def relative_dir(self) -> str:
        return self.sequence_dir

    @property
    def has_blend(self) -> bool:
        return bool(self.files.get("blend"))

    @property
    def animation_block(self) -> dict:
        """The ``camera_animation`` block the generator wrote into the config."""
        block = (self.config or {}).get("camera_animation") or {}
        return block if isinstance(block, dict) else {}

    @property
    def has_animation_payload(self) -> bool:
        return bool(self.animation_block.get("available"))

    @property
    def storage_mode(self) -> str:
        """``blend`` (self-contained scene copy) or ``animation`` (replayed payload)."""
        if self.has_blend:
            return "blend"
        return "animation" if self.has_animation_payload else "none"

    @property
    def complete(self) -> bool:
        return not self.problems

    @property
    def frame_count(self) -> int:
        if self.frame_start is None or self.frame_end is None:
            return 0
        return max(0, int(self.frame_end) - int(self.frame_start) + 1)

    def video_paths(self) -> "list[str]":
        return list(self.files.get("video", []))

    def has_video(self) -> bool:
        return bool(self.files.get("video"))

    def to_dict(self, *, root: str = "") -> dict:
        return {
            "sequence_id": self.sequence_id,
            "sequence_dir": to_forward_slashes(self.sequence_dir),
            "relative_dir": relative_to(self.sequence_dir, root) if root else to_forward_slashes(self.sequence_dir),
            "scene_name": self.scene_name,
            "motion_name": self.motion_name,
            "camera_name": self.camera_name,
            "has_character": bool(self.has_character),
            "character_name": self.character_name,
            "character_animation": self.character_animation,
            "frame_start": self.frame_start,
            "frame_end": self.frame_end,
            "frame_count": self.frame_count,
            "fps": self.fps,
            "status": self.status,
            "complete": self.complete,
            "has_blend": self.has_blend,
            "storage_mode": self.storage_mode,
            "has_animation_payload": self.has_animation_payload,
            "problems": list(self.problems),
            "generator_version": self.generator_version,
            "files": {kind: [to_forward_slashes(p) for p in paths] for kind, paths in self.files.items()},
        }

    def flat_files(self) -> "list[str]":
        out = []
        for paths in self.files.values():
            out.extend(paths)
        return sorted(out)


def _classify(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".blend"):
        return "blend"
    if lower.endswith((".mp4", ".mkv", ".webm", ".avi", ".mov")):
        return "video"
    if lower.endswith("_camera.txt") or lower.endswith("_trajectory.txt"):
        return "camera_trajectory"
    if lower.endswith(".txt"):
        return "text"
    if lower.endswith(".json"):
        if lower == "sequence_config.json":
            return "config"
        if lower == "validation_report.json":
            return "validation_report"
        if lower == "failure_report.json":
            return "failure_report"
        if lower == "manifest.json":
            return "manifest"
        return "metadata"
    if lower.endswith((".png", ".jpg", ".jpeg", ".exr")):
        return "frame"
    return "other"


class SequenceManager:
    """Read-only view over an output tree."""

    def __init__(self, output_root: str):
        self.output_root = normalize_path(output_root)

    # -- discovery -------------------------------------------------------
    def find_sequences(self) -> "list[SequenceInfo]":
        root = self.output_root
        found: "list[SequenceInfo]" = []
        if not os.path.isdir(root):
            return found
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "_scratch"]
            if "sequence_config.json" not in filenames:
                continue
            found.append(self._describe(current, filenames))
        return sorted(found, key=lambda info: info.sequence_dir)

    def _describe(self, directory: str, filenames) -> SequenceInfo:
        config = {}
        path = os.path.join(directory, "sequence_config.json")
        try:
            payload = load_json_file(path)
            if isinstance(payload, dict):
                config = payload
        except Exception:
            config = {}
        sequence = config.get("sequence") or {}
        frames = config.get("frames") or {}
        camera = config.get("camera") or {}
        rel = relative_to(directory, self.output_root)
        parts = [p for p in rel.split("/") if p and p != "."]
        scene_name = sequence.get("scene_name") or (parts[0] if parts else "")
        motion_name = sequence.get("motion_name") or (parts[1] if len(parts) > 1 else "")
        info = SequenceInfo(
            sequence_dir=normalize_path(directory),
            sequence_id=sequence.get("sequence_id") or os.path.basename(directory),
            scene_name=scene_name,
            motion_name=motion_name,
            camera_name=sequence.get("camera_name") or (camera.get("original", {}) or {}).get("camera_name", ""),
            has_character=bool(sequence.get("has_character")),
            character_name=sequence.get("character_name", ""),
            character_animation=sequence.get("character_animation", ""),
            frame_start=frames.get("frame_start"),
            frame_end=frames.get("frame_end"),
            fps=frames.get("fps"),
            generator_version=config.get("generator_version", ""),
            config=config,
        )
        for name in sorted(filenames):
            info.files.setdefault(_classify(name), []).append(os.path.join(directory, name))

        # Problems are advisory: the renderer decides what to do about them.
        if info.files.get("failure_report"):
            info.status = "failed"
            info.problems.append(
                "this sequence was recorded as a generation failure; see failure_report.json"
            )
        if not info.files.get("blend"):
            if info.has_animation_payload:
                # Not a defect any more: the animation is stored in the sidecar and
                # the renderer replays it onto ``sequence.source_blend``.
                info.status = "animation_only" if info.status == "complete" else info.status
            else:
                info.status = "no_blend" if info.status == "complete" else info.status
                info.problems.append(
                    "no .blend file and no camera_animation payload: the sequence cannot be rendered"
                )
        if info.frame_start is None or info.frame_end is None:
            info.problems.append("sequence_config.json does not declare a frame range")
        elif int(info.frame_end) < int(info.frame_start):
            info.problems.append("frame range is inverted")
        if info.generator_version and info.generator_version != GENERATOR_VERSION:
            info.problems.append(
                f"generated by version {info.generator_version}, this build is {GENERATOR_VERSION}"
            )
        return info

    # -- filtering -------------------------------------------------------
    def filter_sequences(
        self,
        sequences,
        *,
        scene_filter=(),
        motion_filter=(),
        sequence_filter=(),
    ) -> "list[SequenceInfo]":
        """Apply the ``--scene-filter`` / ``--motion-filter`` / ``--sequence-filter`` globs."""
        import fnmatch

        def matches(value: str, patterns) -> bool:
            if not patterns:
                return True
            return any(fnmatch.fnmatch(value, pattern) for pattern in patterns)

        return [
            info for info in sequences
            if matches(info.scene_name, scene_filter)
            and matches(info.motion_name, motion_filter)
            and (matches(info.sequence_id, sequence_filter) or matches(os.path.basename(info.sequence_dir), sequence_filter))
        ]

    # -- summaries -------------------------------------------------------
    def summary(self) -> dict:
        sequences = self.find_sequences()
        by_scene: "dict[str, dict]" = {}
        problems = 0
        with_video = 0
        for info in sequences:
            entry = by_scene.setdefault(info.scene_name, {"scene_name": info.scene_name, "motions": {}, "sequences": 0})
            motion = entry["motions"].setdefault(info.motion_name, {"motion_name": info.motion_name, "sequences": 0})
            entry["sequences"] += 1
            motion["sequences"] += 1
            if info.problems:
                problems += 1
            if info.has_video():
                with_video += 1
        return {
            "output_root": to_forward_slashes(self.output_root),
            "exists": os.path.isdir(self.output_root),
            "sequence_count": len(sequences),
            "scene_count": len(by_scene),
            "with_video": with_video,
            "problem_count": problems,
            "scenes": [
                {
                    "scene_name": name,
                    "sequence_count": data["sequences"],
                    "motions": sorted(data["motions"]),
                }
                for name, data in sorted(by_scene.items())
            ],
            "generator_version": GENERATOR_VERSION,
        }

    def resume_index(self) -> dict:
        """Map ``sequence_dir -> rendered video path`` for already-finished work."""
        index = {}
        for info in self.find_sequences():
            video = info.files.get("video") or []
            if video:
                index[os.path.normcase(info.sequence_dir)] = video[0]
        return index

    def prune_frames(self, *, keep_video: bool = True) -> int:
        """Delete leftover frame images (``sequence_000001.0001.png`` and friends)."""
        removed = 0
        for info in self.find_sequences():
            for path in info.files.get("frame", []):
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    continue
            if keep_video:
                continue
        return removed

    def to_dict(self, *, sequences=None) -> dict:
        items = list(sequences) if sequences is not None else self.find_sequences()
        return {
            "output_root": to_forward_slashes(self.output_root),
            "sequence_count": len(items),
            "sequences": [info.to_dict(root=self.output_root) for info in items],
        }
