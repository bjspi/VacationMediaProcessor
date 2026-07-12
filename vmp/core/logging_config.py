"""Application logging setup."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .settings import fallback_settings_path, settings_dir

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_NAME = "vmp"


class _ResilientRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that tolerates a locked log file on Windows.

    Log files are per-PID, but external tools (tail viewers, AV scans,
    backup agents) can still hold the file open during rollover and trigger
    ``PermissionError`` (WinError 32) on every record. Swallow that error, keep
    appending to the current file, and rotate later. The log may briefly exceed
    ``maxBytes`` while locked.
    """

    def doRollover(self) -> None:  # noqa: D102
        try:
            super().doRollover()
        except OSError:
            # Another process holds the log open; skip rotating this time.
            if self.stream is None:
                self.stream = self._open()


def log_level() -> int:
    """Return the configured log level.

    Checks the VMP_LOG_LEVEL environment variable for DEBUG.
    Falls back to INFO.
    """
    env = os.environ.get("VMP_LOG_LEVEL", "").strip().upper()
    if env in ("DEBUG", "DEBG"):
        return logging.DEBUG
    return logging.INFO


# Each running instance logs to its own PID-specific file so concurrent
# instances never share a file and thus never collide on rotation (Windows
# cannot rename a log another process holds open). Stale files from exited
# instances are pruned on startup by _prune_stale_logs().
_LOG_BASENAME = "vmp"


def _log_filename() -> str:
    """Return the PID-specific log filename for this process."""
    return f"{_LOG_BASENAME}.{os.getpid()}.log"


def _prune_stale_logs(directory: Path, keep_days: int = 7) -> None:
    """Delete PID-specific log files (and rotations) from long-gone instances.

    Removes ``vmp.<pid>.log[.N]`` files not written to in
    the last ``keep_days`` days; an instance still running keeps its file's
    mtime fresh, so only files from exited instances are cleaned. Never touches
    this process's own files. Best-effort: any error is ignored.
    """
    import time

    cutoff = time.time() - keep_days * 86400
    mine = _log_filename()
    try:
        entries = list(directory.glob(f"{_LOG_BASENAME}.*.log*"))
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith(mine):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass


def log_path() -> Path:
    """Return the writable, PID-specific app log path for this process."""
    try:
        directory = settings_dir()
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        directory = fallback_settings_path().parent
        directory.mkdir(parents=True, exist_ok=True)
    return directory / _log_filename()


def configure_logging() -> Path:
    """Configure rotating file logging and return the active log path."""
    path = log_path()
    logger = logging.getLogger(LOG_NAME)
    level = log_level()
    logger.setLevel(level)
    logger.propagate = False
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler):
            handler.setLevel(level)
            return Path(handler.baseFilename)
    try:
        handler = _ResilientRotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    except OSError:
        path = fallback_settings_path().parent / _log_filename()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = _ResilientRotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    _prune_stale_logs(path.parent)
    return path


def add_run_file_handler(path: Path) -> logging.Handler | None:
    """Attach a per-run log file handler to the app logger.

    Writes the same records as the global rotating log into a dedicated file
    for a single pipeline run (e.g. inside the run's work directory). Returns
    the handler so the caller can remove it when the run finishes, or ``None``
    if the file could not be opened.
    """
    logger = logging.getLogger(LOG_NAME)
    level = log_level()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        log_to_file(f"Could not create per-run log file: {path}", level=logging.WARNING, exc_info=False)
        return None
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    return handler


def remove_run_file_handler(handler: logging.Handler | None) -> None:
    """Detach and close a per-run log file handler created by add_run_file_handler."""
    if handler is None:
        return
    logger = logging.getLogger(LOG_NAME)
    logger.removeHandler(handler)
    try:
        handler.close()
    except Exception:  # noqa: BLE001
        pass


def log_to_file(
    msg: str,
    level: int = logging.ERROR,
    exc_info: bool = True,
) -> None:
    """Write directly to the rotating file handler, bypassing all other handlers.

    Use this when the GuiLogHandler itself is failing — prevents infinite
    recursion while still landing the error in the log file.
    Thread-safe via the RotatingFileHandler's own lock.
    """
    logger = logging.getLogger(LOG_NAME)
    # LogRecord expects an (type, value, traceback) tuple, not the bool that
    # Logger.error() accepts — passing True would crash the formatter inside
    # handler.emit() and silently drop this emergency record.
    exc_tuple = sys.exc_info() if exc_info and sys.exc_info()[0] is not None else None
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler):
            try:
                record = logging.LogRecord(
                    name=logger.name,
                    level=level,
                    pathname="(logging_config)",
                    lineno=0,
                    msg=msg,
                    args=(),
                    exc_info=exc_tuple,
                )
                handler.emit(record)
            except Exception:
                pass


class GuiLogHandler(logging.Handler):
    """A logging handler that feeds a callback (e.g. a GUI text widget).

    The callback receives the fully formatted log line as a string.
    Thread-safe: the callback is responsible for any thread-affinity
    (e.g. ``QPlainTextEdit.appendPlainText`` via a signal or
    ``QMetaObject.invokeMethod``).
    """

    def __init__(self, callback: Callable[[str], None]) -> None:
        super().__init__()
        self.callback = callback
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        )

    def emit(self, record: logging.Record) -> None:
        try:
            msg = self.format(record)
            self.callback(msg)
        except Exception as exc:
            self.handleError(record)
            # Write the failure into the log file directly
            log_to_file(
                f"GuiLogHandler.emit() crashed: {exc}",
                exc_info=True,
            )


def setup_gui_logging(callback: Callable[[str], None]) -> GuiLogHandler:
    """Add a GuiLogHandler to the app logger and return it.

    The handler is set to the same level as the root app logger.
    """
    handler = GuiLogHandler(callback)
    logger = logging.getLogger(LOG_NAME)
    handler.setLevel(logger.level)
    logger.addHandler(handler)
    return handler


def get_logger(name: str) -> logging.Logger:
    """Return a child logger below the app logger."""
    if name == LOG_NAME or name.startswith(f"{LOG_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOG_NAME}.{name}")
