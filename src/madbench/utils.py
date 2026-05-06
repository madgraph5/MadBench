from __future__ import annotations

import os
import platform
import socket
import subprocess
from datetime import datetime
from pathlib import Path


def get_git_sha(path: Path) -> str | None:
    """Return the short git SHA for the repo at `path`, or None."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def get_timestamp() -> str:
    """Return an ISO-ish timestamp suitable for filenames: 20250308T143022"""
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def detect_hardware() -> dict:
    """Return a dict with basic hardware info: hostname, cpu count, GPU presence."""
    info: dict = {
        "hostname": socket.gethostname(),
        "cpu_count": os.cpu_count(),
        "platform": platform.platform(),
    }

    cuda = os.environ.get("CUDA_VISIBLE_DEVICES")
    hip = os.environ.get("HIP_VISIBLE_DEVICES")

    if cuda is not None:
        info["cuda_visible_devices"] = cuda
        info["gpu_detected"] = cuda not in ("", "-1", "NoDevFiles")
    elif hip is not None:
        info["hip_visible_devices"] = hip
        info["gpu_detected"] = hip not in ("", "-1")
    else:
        info["gpu_detected"] = None  # not explicitly set

    return info
