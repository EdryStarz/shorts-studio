"""Windows launcher for the local Shorts Studio Docker application."""

from __future__ import annotations

import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from tkinter import Tk, messagebox


APP_URL = "http://localhost:3000"
ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
LOG_FILE = ROOT / "Shorts Studio Launcher.log"


def notify(title: str, message: str, *, error: bool = False) -> None:
    root = Tk()
    root.withdraw()
    if error:
        messagebox.showerror(title, message, parent=root)
    else:
        messagebox.showinfo(title, message, parent=root)
    root.destroy()


def run(command: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        result = subprocess.CompletedProcess(command, 124, exc.stdout or "", exc.stderr or "command timed out")
    with LOG_FILE.open("a", encoding="utf-8") as log:
        log.write(f"$ {' '.join(command)}\n{result.stdout}\n{result.stderr}\n")
    return result


def docker_ready() -> bool:
    return run(["docker", "info"], timeout=20).returncode == 0


def start_docker_desktop() -> None:
    docker_desktop = Path.home() / "AppData" / "Local" / "Docker" / "Docker Desktop.exe"
    if not docker_desktop.is_file():
        docker_desktop = Path("C:/Program Files/Docker/Docker/Docker Desktop.exe")
    if docker_desktop.is_file():
        subprocess.Popen([str(docker_desktop)], cwd=str(docker_desktop.parent), shell=False)


def main() -> None:
    if not (ROOT / "docker-compose.yml").is_file():
        notify("Shorts Studio", "В папке программы не найден docker-compose.yml.", error=True)
        return
    LOG_FILE.write_text("", encoding="utf-8")
    if not docker_ready():
        start_docker_desktop()
        for _ in range(36):
            time.sleep(5)
            if docker_ready():
                break
        else:
            notify(
                "Shorts Studio",
                "Docker Engine не запустился. Проверьте в Docker Desktop, что включена виртуализация (WSL 2), затем запустите Shorts Studio снова.",
                error=True,
            )
            return
    result = run(["docker", "compose", "up", "--build", "-d"])
    if result.returncode != 0:
        notify("Shorts Studio", f"Не удалось запустить приложение. Подробности: {LOG_FILE}", error=True)
        return
    for _ in range(36):
        probe = run(["docker", "compose", "ps", "--format", "json"], timeout=20)
        if "running" in probe.stdout.lower():
            webbrowser.open(APP_URL)
            return
        time.sleep(5)
    notify("Shorts Studio", f"Контейнеры не стали готовы за 3 минуты. Подробности: {LOG_FILE}", error=True)


if __name__ == "__main__":
    main()
