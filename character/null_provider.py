"""Always-unavailable character provider.

This is the default.  It exists so the pipeline can run the full
"character-free" workflow without any character assets, and so the UI can tell
the user *why* character sequences are missing instead of silently producing
fewer files than expected.  It never reports success.
"""

from __future__ import annotations

from .base_provider import (
    STATUS_NOT_IMPLEMENTED,
    STATUS_UNAVAILABLE,
    AnimationDescriptor,
    CharacterDescriptor,
    CharacterPlacement,
    CharacterProvider,
    CharacterValidation,
)

DEFAULT_REASON = (
    "No Blender character adapter is configured. Unreal MetaHuman assets cannot be "
    "used directly in Blender, so character sequences are skipped. Configure "
    "batch.character_asset_root with a manifest.json to enable them."
)


class NullCharacterProvider(CharacterProvider):
    """Reports unavailability with an explicit, actionable reason."""

    name = "null"
    description = (
        "Placeholder adapter: keeps the full CharacterProvider interface but has no "
        "assets. Character-free sequence generation is unaffected."
    )

    def __init__(self, config=None, *, logger=None, reason: str = DEFAULT_REASON):
        super().__init__(config, logger=logger)
        self.reason = reason
        self.messages.append(self.reason)

    def status(self) -> str:
        return STATUS_UNAVAILABLE

    def list_characters(self) -> "list[CharacterDescriptor]":
        return []

    def list_animations(self) -> "list[AnimationDescriptor]":
        return []

    def import_character(self, scene_context, character_config) -> CharacterPlacement:
        return CharacterPlacement(
            descriptor_id=config_id(character_config),
            status=STATUS_UNAVAILABLE,
            placement_method="none",
            messages=[self.reason],
        )

    def place_character(self, placement: CharacterPlacement, scene_context) -> CharacterPlacement:
        placement.status = STATUS_UNAVAILABLE
        if self.reason not in placement.messages:
            placement.messages.append(self.reason)
        return placement

    def apply_animation(self, placement: CharacterPlacement, animation_config) -> CharacterPlacement:
        placement.status = STATUS_UNAVAILABLE
        if self.reason not in placement.messages:
            placement.messages.append(self.reason)
        return placement

    def validate_character_placement(self, placement: CharacterPlacement, scene_context) -> CharacterValidation:
        return CharacterValidation(
            valid=False,
            overlap=False,
            inside_scene_bounds=False,
            grounded=False,
            messages=[self.reason],
        )


class UnrealMetaHumanProvider(NullCharacterProvider):
    """Explicit adapter for the Unreal-side asset family.

    Kept as a separate class so the logs are unambiguous: this is *not* a
    missing configuration, it is a documented platform boundary.
    """

    name = "unreal_metahuman"
    description = (
        "Documents that MetaHuman assets from the Unreal project cannot be imported "
        "into Blender by this pipeline (no MetaHuman->Blender asset bridge)."
    )

    def __init__(self, config=None, *, logger=None):
        super().__init__(
            config,
            logger=logger,
            reason=(
                "MetaHuman characters live in the Unreal project "
                "(E:\\UE\\MetaHumanScenePipeline) as blueprint/asset bundles with a "
                "UE-specific skeleton. Blender cannot load them without a separate "
                "retarget + export step. Use a Blender-native character library "
                "(see docs/character_assets.md) or run the character-free pipeline."
            ),
        )

    def status(self) -> str:
        return STATUS_NOT_IMPLEMENTED


def config_id(character_config) -> str:
    """Extract an id from a descriptor, a dict or a plain string."""
    if character_config is None:
        return ""
    if isinstance(character_config, str):
        return character_config
    if isinstance(character_config, dict):
        return str(character_config.get("id") or character_config.get("name") or "")
    return str(getattr(character_config, "id", "") or getattr(character_config, "name", "") or "")
