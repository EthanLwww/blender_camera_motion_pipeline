"""Logging that works both in the Blender UI and in ``blender -b``.

Two sinks are supported:

* a standard :mod:`logging` logger (file handler + stream handler), which is
  what the headless scripts use;
* a callback sink, which the add-on uses to mirror messages into the Blender
  console *and* an in-panel status line without ever writing to a file that a
  read-only render node cannot create.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Callable, Iterable

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

LOGGER_NAME = "blender_motion_pipeline"
_FORMAT = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


class CallbackHandler(logging.Handler):
    """Forward formatted records to a python callable (Blender console/UI)."""

    def __init__(self, callback: Callable[[str, str], None]):
        super().__init__()
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.callback(record.levelname, self.format(record))
        except Exception:  # never let a UI callback break a render
            pass


def setup_logging(
    *,
    level: str = "INFO",
    log_file: str | None = None,
    callback: Callable[[str, str], None] | None = None,
    force: bool = False,
) -> logging.Logger:
    """Configure the package logger once and return it."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured and not force:
        logger.setLevel(_resolve_level(level))
        if log_file:
            add_file_handler(log_file)
        if callback:
            logger.addHandler(CallbackHandler(callback))
        return logger

    logger.handlers.clear()
    logger.setLevel(_resolve_level(level))
    logger.propagate = False

    stream = logging.StreamHandler(stream=sys.stdout)
    stream.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    logger.addHandler(stream)

    if log_file:
        _attach_file(logger, log_file)
    if callback:
        handler = CallbackHandler(callback)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

    _configured = True
    return logger


def _attach_file(logger: logging.Logger, log_file: str) -> None:
    from ..io.path_utils import ensure_dir, normalize_path

    target = normalize_path(log_file)
    parent = os.path.dirname(target)
    if parent:
        ensure_dir(parent)
    handler = logging.FileHandler(target, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    logger.addHandler(handler)


def add_file_handler(log_file: str, *, level: str | None = None) -> logging.Logger:
    """Attach ``log_file`` to the package logger (idempotent per path)."""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        return setup_logging()
    target = os.path.abspath(log_file)
    for existing in logger.handlers:
        if isinstance(existing, logging.FileHandler):
            if os.path.abspath(getattr(existing, "baseFilename", "")) == target:
                return logger
    _attach_file(logger, log_file)
    if level:
        logger.setLevel(_resolve_level(level))
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")


def _resolve_level(level) -> int:
    if isinstance(level, int):
        return level
    text = str(level or "INFO").strip().upper()
    return getattr(logging, text, logging.INFO) if text in LEVELS else logging.INFO


class NullLogger:
    """Drop-in logger used by unit tests and dry runs."""

    def __init__(self) -> None:
        self.records: "list[tuple[str, str]]" = []

    def _record(self, level: str, message: str, *args) -> None:
        text = message % args if args else str(message)
        self.records.append((level, text))

    def debug(self, message, *args) -> None: self._record("DEBUG", message, *args)
    def info(self, message, *args) -> None: self._record("INFO", message, *args)
    def warning(self, message, *args) -> None: self._record("WARNING", message, *args)
    def error(self, message, *args) -> None: self._record("ERROR", message, *args)
    def exception(self, message, *args) -> None: self._record("ERROR", message, *args)
    def setLevel(self, *_args, **_kwargs) -> None: pass

    def messages(self, level: str | None = None) -> "list[str]":
        return [text for lvl, text in self.records if level is None or lvl == level]


class RunLogger:
    """Collects lines for a per-sequence ``generation_log.txt``."""

    def __init__(self, *, echo: logging.Logger | None = None):
        self.lines: "list[str]" = []
        self.echo = echo

    def log(self, message: str, level: str = "INFO") -> None:
        line = f"[{level}] {message}"
        self.lines.append(line)
        if self.echo is not None:
            getattr(self.echo, level.lower(), self.echo.info)(message)

    def extend(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.log(line)

    def text(self) -> str:
        return "\n".join(self.lines) + ("\n" if self.lines else "")

    def write(self, path: str) -> str:
        from ..io.path_utils import ensure_dir, normalize_path

        target = normalize_path(path)
        parent = os.path.dirname(target)
        if parent:
            ensure_dir(parent)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(self.text())
        return target


def log_environment_summary(logger: logging.Logger | NullLogger) -> dict:
    """Log interpreter/Blender provenance once at start-up."""
    info = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "cwd": os.getcwd(),
        "pid": os.getpid(),
    }
    try:  # pragma: no cover
        import bpy  # type: ignore

        info["blender"] = bpy.app.version_string
        info["background"] = bool(bpy.app.background)
    except Exception:
        info["blender"] = "not running inside Blender"
    logger.info(
        "environment: blender=%s python=%s background=%s cwd=%s",
        info.get("blender"), info["python"], info.get("background"), info["cwd"],
    )
    return info
