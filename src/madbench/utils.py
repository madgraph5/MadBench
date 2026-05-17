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
    ``nvidia-smi --query-gpu=index,name,memory.total,driver_version,compute_cap
    --format=csv,noheader,nounits``. Memory is in MiB. ``driver_version`` and
    ``compute_cap`` are optional — older drivers may omit ``compute_cap`` and
    fields beyond the first three are filled in only when present so the parser
    keeps working against a 3-column legacy invocation.
    """
    gpus: list[dict] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3 and parts[0].isdigit() and parts[2].isdigit():
            gpu: dict = {
                "vendor": "nvidia",
                "index": int(parts[0]),
                "name": parts[1],
                "memory_mb": int(parts[2]),
            }
            if len(parts) >= 4 and parts[3] and parts[3] != "[N/A]":
                gpu["driver_version"] = parts[3]
            if len(parts) >= 5 and parts[4] and parts[4] != "[N/A]":
                gpu["compute_cap"] = parts[4]
            gpus.append(gpu)
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
        "--query-gpu=index,name,memory.total,driver_version,compute_cap",
        "--format=csv,noheader,nounits",
    ])
    if rc != 0:
        return []
    return _parse_nvidia_smi(out)


def _detect_amd_driver_version() -> str | None:
    rc, out = _run_silent(["rocm-smi", "--showdriverversion", "--json"])
    if rc != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None
    sysinfo = data.get("system") if isinstance(data, dict) else None
    if isinstance(sysinfo, dict):
        for k in ("Driver version", "Kernel version"):
            v = sysinfo.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _detect_amd_gpus() -> list[dict]:
    rc, out = _run_silent(
        ["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"],
    )
    if rc != 0 or not out.strip():
        return []
    gpus = _parse_rocm_smi(out)
    driver = _detect_amd_driver_version()
    if driver is not None:
        for g in gpus:
            g["driver_version"] = driver
    return gpus


def _detect_cpu_info() -> dict:
    """Return CPU brand string, architecture, and three core counts:

    - ``cpu_count_logical`` — total threads the host advertises (SMT/HT
      included). On Linux: count of ``processor`` lines in ``/proc/cpuinfo``.
      On other platforms: ``os.cpu_count()``.
    - ``cpu_count_physical`` — distinct physical cores, derived from unique
      ``(physical id, core id)`` pairs in ``/proc/cpuinfo``. Linux-only;
      omitted elsewhere.
    - ``cpu_count_available`` — cores the *process* is allowed to schedule
      on, from ``os.sched_getaffinity(0)``. Diverges from
      ``cpu_count_logical`` inside VMs, containers, cgroup cpusets, and
      ``taskset`` slices — this is the right number for normalizing
      benchmark throughput. Linux-only; omitted elsewhere.

    ``os.cpu_count()`` is deliberately avoided on Linux: Python ≥3.13 makes
    it affinity-aware, so it would silently report the cgroup slice instead
    of the host's true capacity. Fields that cannot be determined are
    omitted rather than guessed."""
    info: dict = {
        "cpu_arch": platform.machine() or None,
        "cpu_count_logical": os.cpu_count(),
    }

    # What this process is actually allowed to use. Diverges from the host
    # totals inside containers, cgroups, taskset/cpuset slices, or VMs with
    # pinned vCPUs — important for benchmarks because perf scales with what
    # the kernel hands the process, not with the iron underneath.
    if hasattr(os, "sched_getaffinity"):
        try:
            info["cpu_count_available"] = len(os.sched_getaffinity(0))
        except OSError:
            pass

    cpuinfo_path = Path("/proc/cpuinfo")
    if cpuinfo_path.exists():
        model: str | None = None
        logical_count = 0
        core_keys: set[tuple[str, str]] = set()
        cur_phys: str | None = None
        cur_core: str | None = None
        try:
            text = cpuinfo_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for line in text.splitlines():
            if ":" not in line:
                if not line.strip():
                    if cur_phys is not None and cur_core is not None:
                        core_keys.add((cur_phys, cur_core))
                    cur_phys = cur_core = None
                continue
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if key == "processor":
                logical_count += 1
            elif key == "model name" and model is None:
                model = val
            elif key == "physical id":
                cur_phys = val
            elif key == "core id":
                cur_core = val
        if cur_phys is not None and cur_core is not None:
            core_keys.add((cur_phys, cur_core))
        if model:
            info["cpu_model"] = model
        if core_keys:
            info["cpu_count_physical"] = len(core_keys)
        # Prefer /proc/cpuinfo over os.cpu_count() since the latter is
        # affinity-restricted on Python 3.13+ and inside cgroups/SLURM
        # allocations — we want host capacity, not the process slice.
        if logical_count:
            info["cpu_count_logical"] = logical_count
    else:
        # Best-effort: platform.processor() is often informative on macOS /
        # Windows but typically empty on Linux (handled above).
        proc = platform.processor()
        if proc:
            info["cpu_model"] = proc

    return info


def detect_hardware() -> dict:
    """Return a dict describing the host. Keys:

    - ``hostname`` / ``fqdn`` — identity.
    - ``cpu_model`` — brand string from ``/proc/cpuinfo`` (e.g. ``"AMD EPYC
      9654 96-Core Processor"``). Linux-only; omitted otherwise.
    - ``cpu_arch`` — ``platform.machine()`` (e.g. ``"x86_64"``).
    - ``cpu_count_logical`` — total host threads (SMT included).
    - ``cpu_count_physical`` — distinct physical cores. Linux-only.
    - ``cpu_count_available`` — what this process can schedule on
      (``sched_getaffinity``). Linux-only. < ``logical`` inside
      VMs/containers/cgroups.
    - ``platform`` — ``platform.platform()`` string.
    - ``gpus`` — list of ``{vendor, index, name, memory_mb}``, plus
      ``driver_version`` when available and ``compute_cap`` for NVIDIA.
      Empty list if neither ``nvidia-smi`` nor ``rocm-smi`` is on ``PATH``.
    - ``cuda_visible_devices`` / ``hip_visible_devices`` — only present
      when the corresponding env var is set, recording any constraint on
      which GPU the script actually sees.

    See ``_detect_cpu_info`` for the rationale behind the three CPU counts."""
    info: dict = {
        "hostname": socket.gethostname(),
        "fqdn": socket.getfqdn(),
        **_detect_cpu_info(),
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
    logical = hw.get("cpu_count_logical", hw.get("cpu_count", "?"))
    physical = hw.get("cpu_count_physical")
    available = hw.get("cpu_count_available")
    cpu_str = f"{logical} CPU" if physical is None else f"{physical}c/{logical}t CPU"
    if available is not None and available != logical:
        cpu_str = f"{cpu_str}, {available} available"
    model = hw.get("cpu_model")
    if model:
        cpu_str = f"{cpu_str} ({model})"
    parts.append(cpu_str)
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
