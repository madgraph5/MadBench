from __future__ import annotations

import json
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


def _run_silent(cmd: list[str], timeout: int = 5) -> tuple[int, str]:
    """Run a command and capture stdout. Returns (rc, stdout); on missing
    binary or any other launch error, returns (-1, "")."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return -1, ""


def _parse_nvidia_smi(out: str) -> list[dict]:
    """Parse the CSV output of
    ``nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader,nounits``.
    Memory is in MiB.
    """
    gpus: list[dict] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3 and parts[0].isdigit() and parts[2].isdigit():
            gpus.append({
                "vendor": "nvidia",
                "index": int(parts[0]),
                "name": parts[1],
                "memory_mb": int(parts[2]),
            })
    return gpus


def _parse_rocm_smi(out: str) -> list[dict]:
    """Parse JSON output from
    ``rocm-smi --showproductname --showmeminfo vram --json``.
    The key set varies across ROCm versions, so we try a few common ones."""
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return []
    gpus: list[dict] = []
    for key, val in sorted(data.items()):
        if not isinstance(val, dict) or not key.lower().startswith("card"):
            continue
        try:
            idx = int(key[len("card"):])
        except ValueError:
            continue
        name = (
            val.get("Card series")
            or val.get("Card model")
            or val.get("Card SKU")
            or val.get("Product Name")
            or "unknown"
        )
        mem_mb = None
        for k in ("VRAM Total Memory (B)", "VRAM Total Memory"):
            v = val.get(k)
            if isinstance(v, str) and v.isdigit():
                mem_mb = int(v) // (1024 * 1024)
                break
        gpus.append({
            "vendor": "amd",
            "index": idx,
            "name": str(name).strip(),
            "memory_mb": mem_mb,
        })
    return gpus


def _detect_nvidia_gpus() -> list[dict]:
    rc, out = _run_silent([
        "nvidia-smi",
        "--query-gpu=index,name,memory.total",
        "--format=csv,noheader,nounits",
    ])
    if rc != 0:
        return []
    return _parse_nvidia_smi(out)


def _detect_amd_gpus() -> list[dict]:
    rc, out = _run_silent(
        ["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"],
    )
    if rc != 0 or not out.strip():
        return []
    return _parse_rocm_smi(out)


def detect_hardware() -> dict:
    """Return a dict describing the host: identity (hostname, fqdn), CPU
    count, platform string, the GPU inventory queried from ``nvidia-smi``
    / ``rocm-smi`` (model + memory), and any CUDA/HIP visible-device
    overrides that constrain which GPU the script will actually see."""
    info: dict = {
        "hostname": socket.gethostname(),
        "fqdn": socket.getfqdn(),
        "cpu_count": os.cpu_count(),
        "platform": platform.platform(),
        "gpus": _detect_nvidia_gpus() + _detect_amd_gpus(),
    }
    cuda = os.environ.get("CUDA_VISIBLE_DEVICES")
    hip = os.environ.get("HIP_VISIBLE_DEVICES")
    if cuda is not None:
        info["cuda_visible_devices"] = cuda
    if hip is not None:
        info["hip_visible_devices"] = hip
    return info


def format_hardware_summary(hw: dict) -> str:
    """One-line, human-readable summary of ``detect_hardware()`` output,
    suitable for printing at the start of a run so the log archives a clear
    identification of the machine the test ran on."""
    parts = [f"{hw['hostname']}"]
    fqdn = hw.get("fqdn")
    if fqdn and fqdn != hw["hostname"]:
        parts[0] = f"{hw['hostname']} ({fqdn})"
    parts.append(f"{hw.get('cpu_count', '?')} CPU")
    gpus = hw.get("gpus") or []
    if gpus:
        # Collapse identical adjacent models: "2× NVIDIA A100 (80GB)"
        names = [
            f"{g['name']} ({g['memory_mb']}MB)" if g.get("memory_mb") else g["name"]
            for g in gpus
        ]
        from collections import Counter
        counts = Counter(names)
        gpu_str = ", ".join(
            f"{n}× {name}" if n > 1 else name for name, n in counts.items()
        )
        parts.append(gpu_str)
    else:
        parts.append("no GPU detected")
    visible = hw.get("cuda_visible_devices") or hw.get("hip_visible_devices")
    if visible is not None:
        parts.append(f"visible={visible}")
    return " | ".join(parts)
