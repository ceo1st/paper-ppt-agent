"""One-window development launcher for backend, worker, and frontend."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"


def _npm_cmd() -> str:
    return "npm.cmd" if sys.platform.startswith("win") else "npm"


def _spawn(name: str, argv: list[str], cwd: Path) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["FORCE_COLOR"] = "1"
    env["PY_COLORS"] = "1"
    env["COLORTERM"] = env.get("COLORTERM", "truecolor")
    env["TERM"] = env.get("TERM", "xterm-256color")
    env["npm_config_color"] = "always"
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    thread = threading.Thread(target=_pipe_output, args=(name, proc), daemon=True)
    thread.start()
    return proc


def _pipe_output(name: str, proc: subprocess.Popen[str]) -> None:
    if proc.stdout is None:
        return
    for line in proc.stdout:
        print(f"[{name}] {line.rstrip()}", flush=True)


def _terminate(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        return


def _kill(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.kill()
    except OSError:
        pass


def main() -> int:
    processes: list[subprocess.Popen[str]] = []
    commands = [
        (
            "backend",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "backend.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--reload",
                "--reload-dir",
                "backend",
                "--reload-include=*.py",
                "--use-colors",
            ],
            ROOT,
        ),
        (
            "worker",
            [sys.executable, "-m", "backend.workers.consumer"],
            ROOT,
        ),
        (
            "frontend",
            [
                _npm_cmd(),
                "run",
                "dev",
                "--",
                "--host",
                "127.0.0.1",
                "--port",
                "5173",
                "--strictPort",
                "--open",
            ],
            FRONTEND_DIR,
        ),
    ]

    stop = False

    def _request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True
        for proc in processes:
            _terminate(proc)

    signal.signal(signal.SIGINT, _request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _request_stop)

    try:
        for name, argv, cwd in commands:
            print(f"==> Starting {name}", flush=True)
            processes.append(_spawn(name, argv, cwd))

        print("", flush=True)
        print("Paper PPT Agent is starting:", flush=True)
        print("  Backend:  http://127.0.0.1:8000", flush=True)
        print("  Frontend: http://127.0.0.1:5173", flush=True)
        print("  Worker:   SQLiteHuey local queue", flush=True)
        print("Press Ctrl+C to stop all processes.", flush=True)

        while not stop:
            for proc in processes:
                code = proc.poll()
                if code is not None:
                    print(f"process exited with code {code}; stopping dev stack", flush=True)
                    stop = True
                    break
            if stop:
                break
            threading.Event().wait(0.5)
    finally:
        for proc in processes:
            _terminate(proc)
        for proc in processes:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill(proc)
        print("Dev stack stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
