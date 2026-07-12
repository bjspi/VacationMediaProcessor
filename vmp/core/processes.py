"""Small subprocess helpers for external media tools."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import threading
from string import Template
from threading import Lock
from dataclasses import dataclass
from pathlib import Path

from .i18n import tr
from .logging_config import get_logger

LOGGER = get_logger(__name__)
_ACTIVE_PROCESSES: set[subprocess.Popen[str]] = set()
_ACTIVE_PROCESSES_LOCK = Lock()

# When the app runs without a console (pythonw / .lnk launch on Windows), every
# child console tool (ffprobe, exiftool, ffmpeg, taskkill) would otherwise pop up
# its own black CMD window. CREATE_NO_WINDOW suppresses that. No-op off Windows.
NO_WINDOW_CREATIONFLAGS: int = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


@dataclass(slots=True)
class ProcessResult:
    """Captured process execution result."""

    args: list[str]
    returncode: int
    stdout: str
    stderr: str


class ExternalToolError(RuntimeError):
    """Raised when an external tool exits with a failing status."""


def register_process(proc: subprocess.Popen[str]) -> None:
    """Track a child process so the GUI can abort it on shutdown."""
    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES.add(proc)


def unregister_process(proc: subprocess.Popen[str]) -> None:
    """Stop tracking a child process once it finished."""
    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES.discard(proc)


def active_process_count() -> int:
    """Return the number of tracked external processes."""
    with _ACTIVE_PROCESSES_LOCK:
        return len(_ACTIVE_PROCESSES)


def kill_active_processes() -> int:
    """Forcefully stop all tracked external processes."""
    with _ACTIVE_PROCESSES_LOCK:
        processes = list(_ACTIVE_PROCESSES)
    killed = 0
    for proc in processes:
        try:
            if proc.poll() is not None:
                continue
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                    creationflags=NO_WINDOW_CREATIONFLAGS,
                )
            else:
                proc.kill()
            killed += 1
        except Exception:  # noqa: BLE001
            LOGGER.exception("Failed to kill external process pid=%s", getattr(proc, "pid", None))
    return killed


def resolve_executable(value: str) -> str | None:
    """Resolve an executable from an absolute path or PATH lookup."""
    candidate = Path(value).expanduser()
    if candidate.exists() and candidate.is_file():
        return str(candidate.resolve())
    found = shutil.which(value)
    return found


def run_process(args: list[str], *, check: bool = True) -> ProcessResult:
    """Run a process and capture text output."""
    LOGGER.info("Running external process: %s", " ".join(shlex.quote(part) for part in args))
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=NO_WINDOW_CREATIONFLAGS,
        )
    except OSError:
        LOGGER.exception("External process could not be started: %s", " ".join(shlex.quote(part) for part in args))
        raise
    register_process(proc)
    try:
        stdout, stderr = proc.communicate()
    finally:
        unregister_process(proc)
    LOGGER.info("External process finished with return code %s", proc.returncode)
    if stderr:
        LOGGER.info("External process stderr: %s", stderr.strip()[:4000])
    result = ProcessResult(
        args=args,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    if check and proc.returncode != 0:
        command = " ".join(shlex.quote(part) for part in args)
        LOGGER.error("External process failed (%s): %s", proc.returncode, command)
        raise ExternalToolError(
            f"Command failed ({proc.returncode}): {command}\n{stderr.strip()}"
        )
    return result


def expand_command_template(template: str, *, source: Path, target: Path) -> list[str]:
    """Expand a diff-tool command template into argv parts."""
    rendered = Template(template).safe_substitute(source=str(source), target=str(target))
    args = shlex.split(rendered, posix=os.name != "nt")
    return [part.strip('"') if len(part) >= 2 else part for part in args]


def command_template_error(template: str, *, source: Path | None = None, target: Path | None = None) -> str | None:
    """Return a user-facing validation error for a command template, if any."""
    source = source or Path("source")
    target = target or Path("target")
    try:
        args = expand_command_template(template, source=source, target=target)
    except ValueError as exc:
        return tr("Die Vorlage kann nicht geparst werden: {error}").format(error=exc)
    if not args:
        return tr("Die Vorlage ist leer.")
    executable = args[0]
    candidate = Path(executable).expanduser()
    if candidate.exists() and candidate.is_dir():
        return tr("Der erste Teil der Vorlage ist ein Ordner, keine ausführbare Datei:\n{path}").format(path=candidate)
    resolved = resolve_executable(executable)
    if resolved is None and (candidate.is_absolute() or "\\" in executable or "/" in executable):
        return tr("Die ausführbare Datei wurde nicht gefunden:\n{executable}").format(executable=executable)
    return None


def launch_command_template(template: str, *, source: Path, target: Path) -> subprocess.Popen[str]:
    """Launch a diff-tool template as a detached process."""
    args = expand_command_template(template, source=source, target=target)
    if not args:
        raise ExternalToolError("Diff tool command template resolved to an empty command.")
    error = command_template_error(template, source=source, target=target)
    if error is not None:
        LOGGER.warning("Invalid command template: %s", error.replace("\n", " "))
        raise ExternalToolError(error)
    resolved = resolve_executable(args[0])
    cwd = None
    if resolved is not None:
        executable_path = Path(resolved)
        args[0] = str(executable_path)
        cwd = str(executable_path.parent)
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        startupinfo.wShowWindow = 1  # SW_SHOWNORMAL
    command_text = " ".join(shlex.quote(part) for part in args)
    # Per-launch unique files: two concurrently launched diffs must not truncate
    # each other's diagnostics.
    stdout_fd, stdout_name = tempfile.mkstemp(prefix="vmp_diff_stdout_", suffix=".log")
    stderr_fd, stderr_name = tempfile.mkstemp(prefix="vmp_diff_stderr_", suffix=".log")
    os.close(stdout_fd)
    os.close(stderr_fd)
    stdout_path = Path(stdout_name)
    stderr_path = Path(stderr_name)
    LOGGER.info("Launching command template: %s cwd=%s stdout=%s stderr=%s", command_text, cwd, stdout_path, stderr_path)
    stdout_file = stdout_path.open("w", encoding="utf-8", errors="replace")
    stderr_file = stderr_path.open("w", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            startupinfo=startupinfo,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
            encoding="utf-8",
            errors="replace",
        )  # noqa: S603
    except OSError as exc:
        stdout_file.close()
        stderr_file.close()
        LOGGER.exception("Command template could not be launched: %s", command_text)
        raise ExternalToolError(tr("Diff tool konnte nicht gestartet werden:\n{error}").format(error=exc)) from exc
    finally:
        stdout_file.close()
        stderr_file.close()
    LOGGER.info("Command template launched pid=%s", proc.pid)
    _log_if_process_exits_quickly(proc, command_text, stdout_path, stderr_path)
    return proc


def launch_gui_tool(executable: str, extra_args: list[str] | None = None) -> subprocess.Popen[bytes]:
    """Launch a helper GUI application (XnConvert, XnView, ...) detached.

    ``executable`` must already be resolved to an existing binary. Raises
    ``OSError`` when the process cannot be started; callers present that to the
    user. Shared by the main window and the settings dialog so the Windows
    STARTUPINFO handling lives in exactly one place.
    """
    executable_path = Path(executable)
    command: list[str] = [str(executable_path), *(extra_args or [])]
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        startupinfo.wShowWindow = 1  # SW_SHOWNORMAL
    LOGGER.info("Launching GUI tool command=%s cwd=%s", command, executable_path.parent)
    return subprocess.Popen(  # noqa: S603
        command,
        cwd=str(executable_path.parent),
        startupinfo=startupinfo,
    )


def _log_if_process_exits_quickly(
    proc: subprocess.Popen[str],
    command_text: str,
    stdout_path: Path,
    stderr_path: Path,
) -> None:
    """Log stderr/stdout when a detached GUI tool immediately exits."""

    def _cleanup_logs() -> None:
        for path in (stdout_path, stderr_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _watch() -> None:
        # Best-effort diagnostics on a daemon thread: it may still be running
        # during interpreter shutdown, so it must never raise.
        try:
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                # Long-running GUI tool: wait for it so the per-launch
                # diagnostic logs do not accumulate in %TEMP% forever.
                proc.wait()
                _cleanup_logs()
                return
            stdout = _read_tail(stdout_path)
            stderr = _read_tail(stderr_path)
            LOGGER.warning(
                "Command template exited shortly after launch returncode=%s command=%s stdout=%s stderr=%s",
                proc.returncode,
                command_text,
                stdout,
                stderr,
            )
            _cleanup_logs()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_watch, name="vmp-diff-launch-watch", daemon=True).start()


def _read_tail(path: Path, limit: int = 4000) -> str:
    """Read a short diagnostic tail from a process output file."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()[-limit:]
    except OSError:
        return ""
