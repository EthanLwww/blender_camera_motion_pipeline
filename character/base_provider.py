"""Pluggable character provider interface.

The reference project is an Unreal plugin whose characters are MetaHuman
blueprints.  Those cannot be transferred to Blender as-is, so the brief asks for
a *pluggable adapter* with a complete interface and an honest "not available"
state rather than a faked success.

Implementations live beside this module:

``NullCharacterProvider``
    Always reports "unavailable".  Lets the whole no-character pipeline run.
``BlenderCharacterProvider``
    Appends real ``.blend`` character libraries (Rigify, Mixamo FBX-converted
    rigs, or hand-built characters) described by a small JSON manifest.
``UnrealMetaHumanProvider``
    Documents why MetaHuman assets cannot be used here, and fails cleanly.

Nothing in this package may raise for a *missing* character asset: callers rely
on a structured result so they can log the reason and continue with the
character-free combinations.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..camera.scene_context import CharacterBox, Vec3

#: Status values reported by :meth:`CharacterProvider.status`.
STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE = "unavailable"
STATUS_DEGRADED = "degraded"
STATUS_NOT_IMPLEMENTED = "not_implemented"


@dataclass
class CharacterDescriptor:
    """One selectable character asset."""

    id: str
    name: str = ""
    blend_path: str = ""
    object_name: str = ""
    collection: str = ""
    scale: float = 1.0
    offset: Vec3 = (0.0, 0.0, 0.0)
    rotation_euler_deg: Vec3 = (0.0, 0.0, 0.0)
    animations: "list[str]" = field(default_factory=list)
    details: dict = field(default_factory=dict)
    source: str = ""

    def __post_init__(self):
        if not self.name:
            self.name = self.id

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "blend_path": self.blend_path,
            "object_name": self.object_name,
            "collection": self.collection,
            "scale": float(self.scale),
            "offset": [float(v) for v in self.offset],
            "rotation_euler_deg": [float(v) for v in self.rotation_euler_deg],
            "animations": list(self.animations),
            "details": dict(self.details),
            "source": self.source,
        }


@dataclass
class AnimationDescriptor:
    """One selectable animation clip."""

    id: str
    name: str = ""
    blend_path: str = ""
    action_name: str = ""
    frame_start: int = 0
    frame_end: int = 0
    loop: bool = True
    applies_to: "list[str]" = field(default_factory=list)
    details: dict = field(default_factory=dict)
    source: str = ""

    def __post_init__(self):
        if not self.name:
            self.name = self.id

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "blend_path": self.blend_path,
            "action_name": self.action_name,
            "frame_start": int(self.frame_start),
            "frame_end": int(self.frame_end),
            "loop": bool(self.loop),
            "applies_to": list(self.applies_to),
            "details": dict(self.details),
            "source": self.source,
        }


@dataclass
class CharacterPlacement:
    """Result of importing + placing one character.

    ``ok`` is only ever True when an object actually exists in the scene; the
    providers are forbidden from optimistically reporting success.
    """

    descriptor_id: str = ""
    status: str = STATUS_UNAVAILABLE
    object_name: str = ""
    animation: str = ""
    animation_frame_range: "tuple[int, int] | None" = None
    bbox_min: "Vec3 | None" = None
    bbox_max: "Vec3 | None" = None
    imported_objects: "list[str]" = field(default_factory=list)
    messages: "list[str]" = field(default_factory=list)
    errors: "list[str]" = field(default_factory=list)
    placement_method: str = ""
    details: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_AVAILABLE, STATUS_DEGRADED) and bool(self.object_name)

    def to_character_box(self) -> "CharacterBox | None":
        if not self.object_name or self.bbox_min is None or self.bbox_max is None:
            return None
        return CharacterBox(
            name=self.descriptor_id or self.object_name,
            bbox_min=tuple(float(v) for v in self.bbox_min),  # type: ignore[arg-type]
            bbox_max=tuple(float(v) for v in self.bbox_max),  # type: ignore[arg-type]
            object_name=self.object_name,
            animation=self.animation,
        )

    def to_dict(self) -> dict:
        return {
            "descriptor_id": self.descriptor_id,
            "status": self.status,
            "ok": self.ok,
            "object_name": self.object_name,
            "animation": self.animation,
            "animation_frame_range": list(self.animation_frame_range) if self.animation_frame_range else None,
            "bbox_min": [round(float(v), 6) for v in self.bbox_min] if self.bbox_min else None,
            "bbox_max": [round(float(v), 6) for v in self.bbox_max] if self.bbox_max else None,
            "imported_objects": list(self.imported_objects),
            "messages": list(self.messages),
            "errors": list(self.errors),
            "placement_method": self.placement_method,
            "details": dict(self.details),
        }


@dataclass
class CharacterValidation:
    """Placement sanity report (overlap with scene, inside frame, ...)."""

    valid: bool = True
    overlap: bool = False
    inside_scene_bounds: bool = True
    grounded: bool = True
    messages: "list[str]" = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "valid": bool(self.valid),
            "overlap": bool(self.overlap),
            "inside_scene_bounds": bool(self.inside_scene_bounds),
            "grounded": bool(self.grounded),
            "messages": list(self.messages),
            "metrics": dict(self.metrics),
        }


class CharacterProvider(abc.ABC):
    """Interface every character adapter must implement.

    The signature list mirrors the interface sketched in the project brief.
    """

    #: Short identifier used in configs and logs.
    name: str = "base"
    #: Human readable explanation of what this provider can do.
    description: str = ""

    def __init__(self, config=None, *, logger=None):
        self.config = config
        self.logger = logger
        self.messages: "list[str]" = []

    # -- capability reporting -------------------------------------------
    @abc.abstractmethod
    def status(self) -> str:
        """One of the ``STATUS_*`` constants."""

    def availability(self) -> dict:
        """Structured availability record for logs/JSON artifacts."""
        status = self.status()
        return {
            "provider": self.name,
            "status": status,
            "description": self.description,
            "usable": status in (STATUS_AVAILABLE, STATUS_DEGRADED),
            "messages": list(self.messages),
        }

    def is_usable(self) -> bool:
        return self.status() in (STATUS_AVAILABLE, STATUS_DEGRADED)

    # -- catalogue -------------------------------------------------------
    @abc.abstractmethod
    def list_characters(self) -> "list[CharacterDescriptor]":
        """Available character assets (empty list when unavailable)."""

    @abc.abstractmethod
    def list_animations(self) -> "list[AnimationDescriptor]":
        """Available animation clips (empty list when unavailable)."""

    # -- pipeline actions ------------------------------------------------
    @abc.abstractmethod
    def import_character(self, scene_context, character_config) -> CharacterPlacement:
        """Import ``character_config`` into the loaded scene."""

    @abc.abstractmethod
    def place_character(self, placement: CharacterPlacement, scene_context) -> CharacterPlacement:
        """Position/orient the imported character inside the scene."""

    @abc.abstractmethod
    def apply_animation(self, placement: CharacterPlacement, animation_config) -> CharacterPlacement:
        """Bind and evaluate an animation clip on the placed character."""

    @abc.abstractmethod
    def validate_character_placement(self, placement: CharacterPlacement, scene_context) -> CharacterValidation:
        """Check overlap with scene geometry and scene bounds."""

    # -- helpers ---------------------------------------------------------
    def describe_plan(self, mode: str) -> str:
        """Text used by the UI to explain what the current mode will produce."""
        if mode == "none":
            return "Character dimension disabled: one character-free sequence per camera/motion."
        if not self.is_usable():
            return (
                f"Character mode '{mode}' requested, but provider {self.name!r} is "
                f"{self.status()}. Character sequences will be skipped with a logged reason."
            )
        characters = self.list_characters()
        animations = self.list_animations()
        return (
            f"Provider {self.name!r} offers {len(characters)} character(s) and "
            f"{len(animations)} animation(s)."
        )

    def resolve_character(self, character_id: str) -> "CharacterDescriptor | None":
        for descriptor in self.list_characters():
            if descriptor.id == character_id or descriptor.name == character_id:
                return descriptor
        return None

    def resolve_animation(self, animation_id: str) -> "AnimationDescriptor | None":
        for descriptor in self.list_animations():
            if descriptor.id == animation_id or descriptor.name == animation_id:
                return descriptor
        return None

    def catalogue(self) -> dict:
        return {
            "provider": self.name,
            "status": self.status(),
            "description": self.description,
            "characters": [c.to_dict() for c in self.list_characters()],
            "animations": [a.to_dict() for a in self.list_animations()],
            "messages": list(self.messages),
        }


def character_variants(mode: str, provider: "CharacterProvider | None", *, logger=None):
    """Build the character dimension of the sequence matrix.

    Returns a list of ``(has_character, descriptor|None, animation|None, note)``
    tuples that the sequence generator expands along with camera and motion.
    ``mode`` values match ``CHARACTER_MODE_*`` in ``config.models``.
    """
    from ..config.models import CHARACTER_MODE_BOTH, CHARACTER_MODE_NONE

    if mode == CHARACTER_MODE_NONE:
        return [(False, None, None, "character dimension disabled by batch.mode=none")]

    usable = provider is not None and provider.is_usable()
    reason = ""
    if not usable:
        reason = (
            f"character provider {getattr(provider, 'name', 'none')!r} reports "
            f"status {getattr(provider, 'status', lambda: 'unavailable')()!r}"
        )

    variants = []
    if mode == CHARACTER_MODE_BOTH:
        variants.append((False, None, None, "character-free variant"))
    if usable:
        requested = bool(getattr(provider.config, "character_ids", None)) if provider.config else False
        for character in provider.list_characters():
            animations = provider.list_animations()
            if not animations:
                variants.append((True, character, None, "no animation clips available; static pose"))
                continue
            for animation in animations:
                if animation.applies_to and character.id not in animation.applies_to:
                    continue
                variants.append((True, character, animation, ""))
        if not variants or (len(variants) == 1 and variants[0][0] is False):
            variants.append((False, None, None, "provider is usable but exposes no character assets"))
        del requested
    else:
        if logger is not None:
            logger.warning(
                "character sequences were requested but skipped: %s", reason or "provider unusable"
            )
        variants.append((False, None, None, reason or "character assets unavailable"))
    return variants


def summarise_variants(variants: Sequence[tuple]) -> str:
    with_character = sum(1 for v in variants if v[0])
    return f"{len(variants)} variant(s): {with_character} with character, {len(variants) - with_character} without"
