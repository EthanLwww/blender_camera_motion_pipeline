"""Pluggable character support.

The public entry point is :func:`build_character_provider`, which returns an
object implementing :class:`CharacterProvider`.  The no-character workflow never
depends on any of this beyond :func:`character_variants`.
"""

from .base_provider import (
    STATUS_AVAILABLE,
    STATUS_DEGRADED,
    STATUS_NOT_IMPLEMENTED,
    STATUS_UNAVAILABLE,
    AnimationDescriptor,
    CharacterDescriptor,
    CharacterPlacement,
    CharacterProvider,
    CharacterValidation,
    character_variants,
    summarise_variants,
)
from .library import (
    CharacterLibrary,
    available_provider_names,
    build_provider,
    load_character_library,
)
from .null_provider import NullCharacterProvider, UnrealMetaHumanProvider


def build_character_provider(
    provider_name: str,
    *,
    asset_root: str = "",
    animation_root: str = "",
    config=None,
    mappings=(),
    logger=None,
):
    """Load a library (when configured) and build the requested provider."""
    library = None
    if asset_root:
        library = load_character_library(
            asset_root,
            animation_root=animation_root,
            mappings=mappings,
            logger=logger,
        )
    return build_provider(provider_name, config=config, library=library, logger=logger)


# ``NullCharacterProvider`` lives in ``null_provider``; re-export the whole
# adapter set here so ``from ..character import NullCharacterProvider`` works.
__all__ = [
    "STATUS_AVAILABLE",
    "STATUS_DEGRADED",
    "STATUS_NOT_IMPLEMENTED",
    "STATUS_UNAVAILABLE",
    "AnimationDescriptor",
    "CharacterDescriptor",
    "CharacterLibrary",
    "CharacterPlacement",
    "CharacterProvider",
    "CharacterValidation",
    "NullCharacterProvider",
    "UnrealMetaHumanProvider",
    "available_provider_names",
    "build_character_provider",
    "build_provider",
    "character_variants",
    "load_character_library",
    "summarise_variants",
]
