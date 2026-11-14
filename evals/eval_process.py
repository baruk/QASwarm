"""Управление только процессами OpenCode, запущенными eval-скриптами."""

import os
import signal
import subprocess
from contextlib import contextmanager


class EvalProcessCleanupError(RuntimeError):
    """Нельзя безопасно остановить собственный процесс; новые eval запрещены."""


def stop_process(process):
    """Остановить собственное дерево, не затрагивая общий сервис OpenCode."""
    try:
        if os.name == "nt":
            if process.poll() is None:
                result = subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=10,
                )
                if result.returncode != 0 and process.poll() is None:
                    raise EvalProcessCleanupError(
                        f"Не удалось остановить дерево eval-процесса PID={process.pid}"
                    )
            process.wait(timeout=5)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            finally:
                # Родитель может завершиться раньше дочерних процессов.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError) as error:
        raise EvalProcessCleanupError(
            f"Не удалось подтвердить остановку eval-процесса PID={process.pid}: {error}"
        ) from error


@contextmanager
def open_opencode(command, workdir, *, stdin):
    """Запустить изолированный процесс и закрыть его при любом исключении."""
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt" else {"start_new_session": True}
    )
    process = subprocess.Popen(
        command,
        cwd=workdir,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **options,
    )
    failed = False
    try:
        yield process
    except BaseException:
        failed = True
        raise
    finally:
        if failed or process.poll() is None:
            stop_process(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
