"""Small dependency-free helpers: logging, task control, versioning.

``animation`` is Blender-aware (it touches Action datablocks) so it is imported
lazily by its callers rather than re-exported here.
"""

from .logging_utils import (
    LEVELS,
    NullLogger,
    RunLogger,
    add_file_handler,
    get_logger,
    log_environment_summary,
    setup_logging,
)
from .task_control import TaskCancelled, TaskController
from .version import GENERATOR_VERSION, generator_stamp

__all__ = [
    "GENERATOR_VERSION",
    "LEVELS",
    "NullLogger",
    "RunLogger",
    "TaskCancelled",
    "TaskController",
    "add_file_handler",
    "generator_stamp",
    "get_logger",
    "log_environment_summary",
    "setup_logging",
]
