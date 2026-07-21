from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
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
    ``memory.total`` and ``compute_cap`` can be unavailable for some MIG or
    older-driver views. Optional fields are filled in only when present so the
    parser also works with reduced legacy invocations.
    """
    gpus: list[dict] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit() and parts[1]:
            gpu: dict = {
                "vendor": "nvidia",
                "index": int(parts[0]),
                "name": parts[1],
                "memory_mb": (
                    int(parts[2])
                    if len(parts) >= 3 and parts[2].isdigit()
                    else None
                ),
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
    # Query support varies with the driver and with the kind of device exposed
    # to a container.  In particular, MIG devices can report memory.total as
    # N/A, and older drivers reject compute_cap by failing the whole command.
    # Retry with progressively older field sets instead of turning either case
    # into the misleading "no GPU detected" result.
    field_sets = (
        "index,name,memory.total,driver_version,compute_cap",
        "index,name,memory.total,driver_version",
        "index,name,memory.total",
        "index,name",
    )
    for fields in field_sets:
        rc, out = _run_silent([
            "nvidia-smi",
            f"--query-gpu={fields}",
            "--format=csv,noheader,nounits",
        ])
        if rc == 0:
            gpus = _parse_nvidia_smi(out)
            if gpus:
                return gpus
    return []


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


# Toolchain whose versions matter for reproducing a run — especially
# gridpack generation, which shells out to the system Fortran/C/C++
# compilers and will simply refuse to build against an incompatible
# ``g++``/``gfortran``/glibc. Order is roughly "most likely to matter"
# first so the emitted ``software`` block reads top-down.
DEFAULT_SOFTWARE_TOOLS: tuple[str, ...] = (
    "gcc",
    "g++",
    "gfortran",
    "nvcc",
    "hipcc",
    "python3",
    "python",
    "make",
    "cmake",
    "ld",       # binutils linker — ABI/relocation issues surface here
    "ldd",      # `ldd --version` reports the glibc version (GLIBC_x.y errors)
)

# A clean dotted version token: "11.4.0", "12.2", "2.35". Requires at
# least one dot so bare years in copyright lines (e.g. nvcc's
# "2005-2023") don't match. Distro packaging suffixes ("-5.el9",
# "-1ubuntu1") are deliberately dropped here — the full string is kept
# verbatim in each tool's ``raw`` field.
_VERSION_RE = re.compile(r"\b(\d+(?:\.\d+){1,3})\b")


def _extract_version(output: str) -> tuple[str | None, str | None]:
    """From a tool's ``--version`` output, return ``(version, raw_line)``.

    Picks the first non-empty line that contains a dotted version token
    (so nvcc's ``Cuda compilation tools, release 12.2, V12.2.140`` wins
    over its banner/copyright lines); falls back to the first non-empty
    line. ``version`` is the extracted token from that line, or ``None``
    if the chosen line has none."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if not lines:
        return None, None
    chosen = next((ln for ln in lines if _VERSION_RE.search(ln)), lines[0])
    m = _VERSION_RE.search(chosen)
    return (m.group(1) if m else None), chosen


def _detect_tool_version(name: str) -> dict | None:
    """Return ``{version?, path, raw?}`` for a tool on ``PATH``, or ``None``
    if it isn't installed. Version output on either stream is accepted
    (some tools print to stderr); a non-zero exit is tolerated as long as
    something version-looking comes back."""
    path = shutil.which(name)
    if path is None:
        return None
    try:
        r = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"path": path}
    version, raw = _extract_version((r.stdout or "") + "\n" + (r.stderr or ""))
    info: dict = {}
    if version:
        info["version"] = version
    info["path"] = path
    if raw:
        info["raw"] = raw
    return info


def detect_software_versions(tools: tuple[str, ...] | None = None) -> dict:
    """Return a dict of relevant toolchain versions on this host.

    Each present tool maps to ``{version, path, raw}`` (``version``/``raw``
    omitted when they can't be parsed); tools not found on ``PATH`` are
    left out entirely. ``madbench_python`` always records the interpreter
    actually running madbench, which need not be the ``python``/``python3``
    on ``PATH``.

    The default tool set (``DEFAULT_SOFTWARE_TOOLS``) targets what makes a
    run reproducible — chiefly the compilers gridpack generation depends
    on. Pass ``tools`` to override it."""
    names = DEFAULT_SOFTWARE_TOOLS if tools is None else tools
    software: dict = {}
    for name in names:
        info = _detect_tool_version(name)
        if info is not None:
            software[name] = info
    software["madbench_python"] = {
        "version": platform.python_version(),
        "path": sys.executable,
    }
    return software


def format_software_summary(sw: dict) -> str:
    """Compact one-line summary of ``detect_software_versions()`` for the
    run log, e.g. ``gcc 11.4.0 | g++ 11.4.0 | gfortran 11.4.0 | nvcc 12.2``.
    Tools without a parsed version are shown as ``name ?``; the running
    interpreter is folded in as ``python <ver>``."""
    parts: list[str] = []
    for name, info in sw.items():
        if name == "madbench_python":
            continue
        parts.append(f"{name} {info.get('version', '?')}")
    py = sw.get("madbench_python", {}).get("version")
    if py:
        parts.append(f"python {py}")
    return " | ".join(parts) if parts else "no toolchain detected"


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
