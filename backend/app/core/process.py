import subprocess
from pathlib import Path
from typing import Sequence

from app.core.logging import log


class MediaCommandError(RuntimeError):
    pass


def run_media_command(args: Sequence[str], *, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    if not args or not all(isinstance(item, str) and item for item in args):
        raise ValueError("command arguments must be non-empty strings")
    log.info("media_command", executable=Path(args[0]).name, argument_count=len(args) - 1)
    try:
        result = subprocess.run(
            list(args),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaCommandError(str(exc)) from exc
    if result.returncode != 0:
        detail = result.stderr[-3000:].strip()
        raise MediaCommandError(f"media command failed ({result.returncode}): {detail}")
    return result

