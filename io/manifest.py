"""Manifest bookkeeping for the ``scene / motion`` output tree.

Two manifest levels are produced, mirroring the documented layout:

``<output_root>/<scene>/<motion>/manifest.json``
    one entry per generated sequence in that motion folder.
``<output_root>/manifest.json``
    roll-up across every scene/motion plus the failure list, so a render farm
    (or a human) can see what was produced without walking the tree.
"""

from __future__ import annotations

import datetime as _dt
import os

from .json_io import load_json_file, save_json_file
from .path_utils import ensure_dir, normalize_path, relative_to, to_forward_slashes

GENERATOR_NAME = "blender_motion_pipeline"


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def new_manifest(*, kind: str, scope: dict | None = None) -> dict:
    return {
        "manifest_kind": kind,
        "generator": GENERATOR_NAME,
        "created_utc": utc_now_iso(),
        "scope": scope or {},
        "sequence_count": 0,
        "sequences": [],
        "failure_count": 0,
        "failures": [],
    }


class ManifestWriter:
    """Accumulate sequence/failure records and flush them to disk."""

    def __init__(self, path: str, *, kind: str, scope: dict | None = None):
        self.path = normalize_path(path)
        self.data = new_manifest(kind=kind, scope=scope)
        existing = load_json_file(self.path, default=None, required=False)
        if isinstance(existing, dict) and existing.get("manifest_kind") == kind:
            # Preserve previous entries so a resumed run appends instead of
            # erasing the record of what already succeeded.
            self.data["sequences"] = list(existing.get("sequences") or [])
            self.data["failures"] = list(existing.get("failures") or [])
            self.data["created_utc"] = existing.get("created_utc", self.data["created_utc"])

    def add_sequence(self, entry: dict) -> None:
        sid = entry.get("sequence_id")
        sequences = self.data["sequences"]
        for index, existing in enumerate(sequences):
            if existing.get("sequence_id") == sid:
                sequences[index] = entry
                break
        else:
            sequences.append(entry)
        self._refresh_counts()

    def add_failure(self, entry: dict) -> None:
        self.data["failures"].append(entry)
        self._refresh_counts()

    def _refresh_counts(self) -> None:
        self.data["sequence_count"] = len(self.data["sequences"])
        self.data["failure_count"] = len(self.data["failures"])

    def flush(self, *, extra: dict | None = None) -> str:
        if extra:
            self.data.update(extra)
        self.data["updated_utc"] = utc_now_iso()
        self._refresh_counts()
        ensure_dir(os.path.dirname(self.path) or ".")
        save_json_file(self.path, self.data)
        return self.path

    # -- convenience -----------------------------------------------------
    def relative(self, path: str) -> str:
        return relative_to(path, os.path.dirname(self.path))

    def forward(self, path: str) -> str:
        return to_forward_slashes(path)
