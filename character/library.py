"""Character/animation library manifest loading and provider selection.

Manifest format (``<character_asset_root>/manifest.json``)::

    {
      "schema_version": 1,
      "characters": [
        {
          "id": "ch41",
          "blend_path": "ch41/character.blend",
          "object_name": "MH_ch41",
          "collection": "CH_ch41",
          "animations": ["Idle", "Cross_Punch"]
        }
      ],
      "animations": [
        {
          "id": "Cross_Punch",
          "blend_path": "ch41/animations.blend",
          "action_name": "Cross_Punch",
          "frame_start": 0,
          "frame_end": 60,
          "applies_to": ["ch41"]
        }
      ]
    }

All paths are resolved relative to the manifest's own folder (or left absolute
when they already are), which is what makes the library portable between an
artist workstation and a Linux render node.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..io.json_io import JsonError, load_json_file
from ..io.path_utils import apply_path_mappings, normalize_path
from .base_provider import (
    AnimationDescriptor,
    CharacterDescriptor,
    CharacterProvider,
)

MANIFEST_FILENAMES = ("manifest.json", "characters.json", "library.json")


@dataclass
class CharacterLibrary:
    """Parsed manifest plus the provider that can realise it."""

    characters: "list[CharacterDescriptor]"
    animations: "list[AnimationDescriptor]"
    manifest_path: str = ""
    warnings: "list[str]" = None  # type: ignore[assignment]
    raw: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []
        if self.raw is None:
            self.raw = {}

    @property
    def ok(self) -> bool:
        return bool(self.characters)

    @property
    def root(self) -> str:
        return os.path.dirname(self.manifest_path) if self.manifest_path else ""

    def to_dict(self) -> dict:
        return {
            "manifest_path": self.manifest_path,
            "character_count": len(self.characters),
            "animation_count": len(self.animations),
            "characters": [c.to_dict() for c in self.characters],
            "animations": [a.to_dict() for a in self.animations],
            "warnings": list(self.warnings),
        }


def _resolve(path: str, root: str, mappings=()) -> str:
    if not path:
        return ""
    raw = apply_path_mappings(path, mappings) if mappings else path
    if os.path.isabs(raw) or (len(raw) > 2 and raw[1] == ":"):
        return normalize_path(raw)
    return normalize_path(os.path.join(root, raw)) if root else normalize_path(raw)


def load_character_library(
    asset_root: str,
    *,
    animation_root: str = "",
    mappings=(),
    logger=None,
) -> CharacterLibrary:
    """Read the manifest from ``asset_root`` (a folder or a JSON file)."""
    if not asset_root:
        return CharacterLibrary([], [], warnings=["no character asset root configured"])

    candidate = normalize_path(asset_root)
    manifest_path = ""
    if os.path.isfile(candidate):
        manifest_path = candidate
    elif os.path.isdir(candidate):
        for filename in MANIFEST_FILENAMES:
            probe = os.path.join(candidate, filename)
            if os.path.isfile(probe):
                manifest_path = probe
                break
        if not manifest_path:
            # Tolerate a folder of .blend characters with no manifest.
            blends = sorted(
                os.path.join(candidate, name)
                for name in os.listdir(candidate)
                if name.lower().endswith(".blend")
            )
            if blends:
                characters = [
                    CharacterDescriptor(
                        id=os.path.splitext(os.path.basename(path))[0],
                        blend_path=path,
                        source="folder-scan",
                        details={"note": "discovered by scanning the asset root for .blend files"},
                    )
                    for path in blends
                ]
                return CharacterLibrary(
                    characters,
                    [],
                    warnings=[
                        f"{candidate}: no manifest.json found; created {len(characters)} "
                        "character entr(ies) from .blend files in the folder (no animations)"
                    ],
                )
            return CharacterLibrary(
                [], [], warnings=[f"{candidate}: no manifest.json and no .blend files found"]
            )
    else:
        return CharacterLibrary([], [], warnings=[f"character asset root does not exist: {candidate}"])

    try:
        payload = load_json_file(manifest_path)
    except JsonError as exc:
        return CharacterLibrary([], [], manifest_path, warnings=[str(exc)])

    warnings: "list[str]" = []
    if not isinstance(payload, dict):
        return CharacterLibrary(
            [], [], manifest_path,
            warnings=[f"{manifest_path}: root must be a JSON object"],
        )
    root = os.path.dirname(manifest_path)
    animation_base = _resolve(animation_root, "", mappings) if animation_root else root

    characters = []
    for index, entry in enumerate(payload.get("characters") or []):
        if not isinstance(entry, dict):
            warnings.append(f"characters[{index}] is not an object; ignored")
            continue
        cid = str(entry.get("id") or entry.get("name") or "").strip()
        if not cid:
            warnings.append(f"characters[{index}] has no id; ignored")
            continue
        blend_path = _resolve(str(entry.get("blend_path") or entry.get("path") or ""), root, mappings)
        if blend_path and not os.path.isfile(blend_path):
            warnings.append(f"character {cid!r}: blend file not found ({blend_path})")
        animations = entry.get("animations") or []
        if not isinstance(animations, list):
            animations = []
        characters.append(CharacterDescriptor(
            id=cid,
            name=str(entry.get("name") or cid),
            blend_path=blend_path,
            object_name=str(entry.get("object_name") or entry.get("object") or ""),
            collection=str(entry.get("collection") or ""),
            scale=float(entry.get("scale", 1.0) or 1.0),
            offset=tuple(float(v) for v in (entry.get("offset") or (0.0, 0.0, 0.0)))[:3],  # type: ignore[arg-type]
            rotation_euler_deg=tuple(float(v) for v in (entry.get("rotation_euler_deg") or (0.0, 0.0, 0.0)))[:3],  # type: ignore[arg-type]
            animations=[str(a) for a in animations],
            details=dict(entry.get("details") or {}),
            source=manifest_path,
        ))

    animations_out = []
    for index, entry in enumerate(payload.get("animations") or []):
        if not isinstance(entry, dict):
            warnings.append(f"animations[{index}] is not an object; ignored")
            continue
        aid = str(entry.get("id") or entry.get("name") or "").strip()
        if not aid:
            warnings.append(f"animations[{index}] has no id; ignored")
            continue
        raw_blend = str(entry.get("blend_path") or entry.get("path") or "")
        blend_path = _resolve(raw_blend, animation_base or root, mappings) if raw_blend else ""
        if blend_path and not os.path.isfile(blend_path):
            warnings.append(f"animation {aid!r}: blend file not found ({blend_path})")
        applies_to = entry.get("applies_to") or entry.get("characters") or []
        if not isinstance(applies_to, list):
            applies_to = []
        animations_out.append(AnimationDescriptor(
            id=aid,
            name=str(entry.get("name") or aid),
            blend_path=blend_path,
            action_name=str(entry.get("action_name") or entry.get("action") or aid),
            frame_start=int(entry.get("frame_start", 0) or 0),
            frame_end=int(entry.get("frame_end", 0) or 0),
            loop=bool(entry.get("loop", True)),
            applies_to=[str(a) for a in applies_to],
            details=dict(entry.get("details") or {}),
            source=manifest_path,
        ))

    library = CharacterLibrary(characters, animations_out, manifest_path, warnings=warnings, raw=payload)
    if logger is not None:
        for warning in warnings:
            logger.warning("character library: %s", warning)
        logger.info(
            "character library %s: %d character(s), %d animation(s)",
            manifest_path, len(characters), len(animations_out),
        )
    return library


def available_provider_names() -> "list[str]":
    return ["auto", "blender", "null", "unreal_metahuman"]


def build_provider(
    name: str,
    *,
    config=None,
    library: "CharacterLibrary | None" = None,
    logger=None,
) -> CharacterProvider:
    """Factory honouring ``batch.character_provider``.

    ``auto`` picks the Blender adapter when a library with characters exists,
    otherwise the null adapter; that keeps a fresh install runnable without any
    configuration while still using real assets the moment a manifest appears.
    """
    from .blender_provider import BlenderCharacterProvider
    from .null_provider import NullCharacterProvider, UnrealMetaHumanProvider

    requested = (name or "auto").strip().lower()
    if requested not in available_provider_names():
        if logger is not None:
            logger.warning(
                "unknown character provider %r; falling back to 'auto' (known: %s)",
                name, ", ".join(available_provider_names()),
            )
        requested = "auto"

    if requested == "unreal_metahuman":
        return UnrealMetaHumanProvider(config, logger=logger)
    if requested == "null":
        return NullCharacterProvider(config, logger=logger)
    if requested == "blender":
        if library is None or not library.characters:
            provider = NullCharacterProvider(config, logger=logger)
            provider.messages.append(
                "provider 'blender' was requested but no usable character library was loaded"
            )
            return provider
        return BlenderCharacterProvider(config, library=library, logger=logger)

    # auto
    if library is not None and library.characters:
        return BlenderCharacterProvider(config, library=library, logger=logger)
    provider = NullCharacterProvider(config, logger=logger)
    if library is not None and library.warnings:
        provider.messages.extend(library.warnings)
    return provider
