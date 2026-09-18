"""Configuration dataclasses, validation and JSON round-tripping."""

from .models import (
    CHARACTER_MODE_BOTH,
    CHARACTER_MODE_LABELS,
    CHARACTER_MODE_NONE,
    CHARACTER_MODE_WITH,
    BatchConfig,
    BatchSection,
    MotionSection,
    RenderSection,
    SearchSection,
    TemplateUnitScale,
    ValidationSection,
    combine_frame_ranges,
    config_from_dict,
    describe_config,
    validate_batch_config,
)
from .defaults import default_config, default_config_dict

__all__ = [
    "CHARACTER_MODE_BOTH",
    "CHARACTER_MODE_LABELS",
    "CHARACTER_MODE_NONE",
    "CHARACTER_MODE_WITH",
    "BatchConfig",
    "BatchSection",
    "MotionSection",
    "RenderSection",
    "SearchSection",
    "TemplateUnitScale",
    "ValidationSection",
    "combine_frame_ranges",
    "config_from_dict",
    "default_config",
    "default_config_dict",
    "describe_config",
    "validate_batch_config",
]
