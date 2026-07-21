from __future__ import annotations

import json
import os
import platform
from pathlib import Path

import pytest
import madbench.utils as utils

from madbench.utils import (
    _extract_version,
    _parse_nvidia_smi,
    _parse_rocm_smi,
    detect_hardware,
    detect_software_versions,
    format_hardware_summary,
    format_software_summary,
    get_git_sha,
    get_timestamp,
)


def test_get_timestamp_format():
    ts = get_timestamp()
    # Expected format: 20250308T143022 (15 chars)
    assert len(ts) == 15
    assert "T" in ts
    assert ts[:8].isdigit()
    assert ts[9:].isdigit()


def test_get_git_sha_valid_repo(tmp_path):
    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    sha = get_git_sha(tmp_path)
    assert sha is not None
    assert len(sha) <= 12  # short SHA


def test_get_git_sha_non_repo(tmp_path):
    sha = get_git_sha(tmp_path)
    assert sha is None


def test_detect_hardware_keys():
    hw = detect_hardware()
    assert "hostname" in hw
    assert "fqdn" in hw
    assert "cpu_count_logical" in hw
    assert "cpu_arch" in hw
    assert "platform" in hw
    assert "gpus" in hw and isinstance(hw["gpus"], list)


def test_detect_hardware_cpu_details_on_linux():
    """On Linux we should be able to derive a model string and a physical
    core count from /proc/cpuinfo. Skip elsewhere since the parser is the
    only thing that gives us these fields."""
    if not os.path.exists("/proc/cpuinfo"):
        pytest.skip("requires /proc/cpuinfo")
    hw = detect_hardware()
    assert "cpu_model" in hw and hw["cpu_model"]
    assert "cpu_count_physical" in hw
    assert hw["cpu_count_physical"] >= 1
    assert hw["cpu_count_physical"] <= hw["cpu_count_logical"]
    # sched_getaffinity is Linux-only; on Linux we should always have it.
    assert "cpu_count_available" in hw
    assert hw["cpu_count_available"] >= 1
    assert hw["cpu_count_available"] <= hw["cpu_count_logical"]


# -----------------------------------------------------------------------
# GPU output parsing (no real GPU required)
# -----------------------------------------------------------------------


def test_parse_nvidia_smi_single():
    out = "0, NVIDIA A100-SXM4-80GB, 81920\n"
    assert _parse_nvidia_smi(out) == [{
        "vendor": "nvidia",
        "index": 0,
        "name": "NVIDIA A100-SXM4-80GB",
        "memory_mb": 81920,
    }]


def test_parse_nvidia_smi_with_driver_and_compute_cap():
    out = "0, NVIDIA H100, 81559, 550.54.15, 9.0\n"
    assert _parse_nvidia_smi(out) == [{
        "vendor": "nvidia",
        "index": 0,
        "name": "NVIDIA H100",
        "memory_mb": 81559,
        "driver_version": "550.54.15",
        "compute_cap": "9.0",
    }]


def test_parse_nvidia_smi_skips_na_compute_cap():
    """Older drivers report [N/A] for compute_cap on some cards; that should
    not get stored as a literal '[N/A]' string."""
    out = "0, NVIDIA Tesla K80, 11441, 470.57.02, [N/A]\n"
    gpu = _parse_nvidia_smi(out)[0]
    assert gpu["driver_version"] == "470.57.02"
    assert "compute_cap" not in gpu


def test_parse_nvidia_smi_keeps_mig_device_with_na_memory():
    """MIG-backed/container views may not expose physical total memory."""
    assert _parse_nvidia_smi("0, NVIDIA H100, [N/A], 550.54.15, 9.0\n") == [{
        "vendor": "nvidia",
        "index": 0,
        "name": "NVIDIA H100",
        "memory_mb": None,
        "driver_version": "550.54.15",
        "compute_cap": "9.0",
    }]


def test_detect_nvidia_gpus_retries_unsupported_query_fields(monkeypatch):
    calls = []

    def fake_run(cmd, timeout=5):
        calls.append(cmd)
        if "compute_cap" in cmd[1]:
            return 2, ""
        return 0, "0, NVIDIA H100, [N/A], 550.54.15\n"

    monkeypatch.setattr(utils, "_run_silent", fake_run)
    assert utils._detect_nvidia_gpus() == [{
        "vendor": "nvidia",
        "index": 0,
        "name": "NVIDIA H100",
        "memory_mb": None,
        "driver_version": "550.54.15",
    }]
    assert len(calls) == 2


def test_parse_nvidia_smi_multiple():
    out = (
        "0, NVIDIA GeForce RTX 3090, 24576\n"
        "1, NVIDIA GeForce RTX 3090, 24576\n"
    )
    gpus = _parse_nvidia_smi(out)
    assert len(gpus) == 2
    assert [g["index"] for g in gpus] == [0, 1]
    assert all(g["vendor"] == "nvidia" for g in gpus)
    assert all(g["memory_mb"] == 24576 for g in gpus)


def test_parse_nvidia_smi_empty():
    assert _parse_nvidia_smi("") == []


def test_parse_nvidia_smi_garbage_skipped():
    """Lines that don't match the expected shape are silently dropped
    rather than crashing the whole detection."""
    out = (
        "not a real line\n"
        "0, NVIDIA RTX 4090, 24576\n"
        ", , \n"
    )
    gpus = _parse_nvidia_smi(out)
    assert len(gpus) == 1
    assert gpus[0]["name"] == "NVIDIA RTX 4090"


def test_parse_rocm_smi_json():
    out = json.dumps({
        "card0": {
            "Card series": "AMD Instinct MI250X",
            "VRAM Total Memory (B)": "68719476736",  # 64 GiB
        },
        "card1": {
            "Card series": "AMD Instinct MI250X",
            "VRAM Total Memory (B)": "68719476736",
        },
        "system": {"Driver version": "5.6.0"},  # non-card entry, must be ignored
    })
    gpus = _parse_rocm_smi(out)
    assert len(gpus) == 2
    assert gpus[0] == {
        "vendor": "amd",
        "index": 0,
        "name": "AMD Instinct MI250X",
        "memory_mb": 65536,
    }


def test_parse_rocm_smi_alternative_name_keys():
    """ROCm versions use different keys for the model name; the parser
    should fall back through the alternatives."""
    out = json.dumps({"card0": {"Card model": "Radeon Pro VII"}})
    gpus = _parse_rocm_smi(out)
    assert gpus == [{
        "vendor": "amd",
        "index": 0,
        "name": "Radeon Pro VII",
        "memory_mb": None,
    }]


def test_parse_rocm_smi_invalid_json():
    assert _parse_rocm_smi("definitely not json") == []


# -----------------------------------------------------------------------
# format_hardware_summary
# -----------------------------------------------------------------------


def test_format_hardware_summary_no_gpu():
    s = format_hardware_summary({
        "hostname": "wn-12", "fqdn": "wn-12",
        "cpu_count_logical": 32, "cpu_count_physical": 16,
        "cpu_model": "Intel Xeon Gold 6248",
        "platform": "Linux",
        "gpus": [],
    })
    assert "wn-12" in s
    assert "16c/32t CPU" in s
    assert "Intel Xeon Gold 6248" in s
    assert "no GPU detected" in s


def test_format_hardware_summary_logical_only_fallback():
    """Physical-core info isn't available on every platform; the summary
    should still render cleanly with just the logical count."""
    s = format_hardware_summary({
        "hostname": "mac", "fqdn": "mac",
        "cpu_count_logical": 10,
        "platform": "Darwin",
        "gpus": [],
    })
    assert "10 CPU" in s
    assert "c/" not in s  # no "physical/logical" notation when physical missing


def test_format_hardware_summary_shows_restricted_affinity():
    """When the process is allocated fewer CPUs than the host has (VM,
    cgroup, taskset), the summary should call that out."""
    s = format_hardware_summary({
        "hostname": "vm-1", "fqdn": "vm-1",
        "cpu_count_logical": 384, "cpu_count_physical": 192,
        "cpu_count_available": 46,
        "cpu_model": "AMD EPYC 9654 96-Core Processor",
        "platform": "Linux",
        "gpus": [],
    })
    assert "192c/384t CPU" in s
    assert "46 available" in s
    assert "AMD EPYC 9654" in s


def test_format_hardware_summary_omits_available_when_equal():
    """If the available count equals logical, no point cluttering the summary."""
    s = format_hardware_summary({
        "hostname": "h", "fqdn": "h",
        "cpu_count_logical": 8, "cpu_count_physical": 4,
        "cpu_count_available": 8,
        "platform": "Linux", "gpus": [],
    })
    assert "available" not in s


def test_format_hardware_summary_collapses_duplicates():
    s = format_hardware_summary({
        "hostname": "h", "fqdn": "h.example.com",
        "cpu_count_logical": 8, "cpu_count_physical": 8,
        "platform": "Linux",
        "gpus": [
            {"vendor": "nvidia", "index": 0, "name": "A100", "memory_mb": 81920},
            {"vendor": "nvidia", "index": 1, "name": "A100", "memory_mb": 81920},
        ],
        "cuda_visible_devices": "0",
    })
    assert "h (h.example.com)" in s
    assert "2× A100 (81920MB)" in s
    assert "visible=0" in s


# -----------------------------------------------------------------------
# Software / toolchain version detection
# -----------------------------------------------------------------------


@pytest.mark.parametrize("output, version, raw", [
    # gcc / g++ / gfortran: version on the first line.
    ("gcc (GCC) 11.4.0", "11.4.0", "gcc (GCC) 11.4.0"),
    # Distro packaging suffix is dropped from `version`, kept in `raw`.
    (
        "GNU ld version 2.41-5.el9_7.1",
        "2.41",
        "GNU ld version 2.41-5.el9_7.1",
    ),
    # nvcc: banner + copyright year come first; the release line must win
    # (a bare year like 2005-2023 has no dot and must not match).
    (
        "nvcc: NVIDIA (R) Cuda compiler driver\n"
        "Copyright (c) 2005-2023 NVIDIA Corporation\n"
        "Cuda compilation tools, release 12.2, V12.2.140",
        "12.2",
        "Cuda compilation tools, release 12.2, V12.2.140",
    ),
    ("Python 3.9.25", "3.9.25", "Python 3.9.25"),
    # ldd reports the glibc version — the field that decides portability.
    ("ldd (GNU libc) 2.34", "2.34", "ldd (GNU libc) 2.34"),
    # No dotted token anywhere -> version None, raw is the first line.
    ("some tool without a number", None, "some tool without a number"),
    ("", None, None),
])
def test_extract_version(output, version, raw):
    assert _extract_version(output) == (version, raw)


def test_detect_software_versions_records_running_python():
    sw = detect_software_versions()
    assert "madbench_python" in sw
    py = sw["madbench_python"]
    assert py["version"] == platform.python_version()
    assert py["path"]


def test_detect_software_versions_custom_tool_set():
    # A tool that certainly isn't on PATH is simply omitted; a present one
    # (the interpreter running the tests) is reported with a path.
    sw = detect_software_versions(tools=("definitely-not-a-real-binary-xyz",))
    assert "definitely-not-a-real-binary-xyz" not in sw
    # madbench_python is always injected regardless of the tool set.
    assert "madbench_python" in sw


def test_detect_software_versions_present_tool_shape():
    # `python3` should exist in any environment running this test suite.
    sw = detect_software_versions(tools=("python3",))
    if "python3" in sw:
        info = sw["python3"]
        assert "path" in info
        # version/raw are best-effort but python3 --version is well-behaved.
        assert info.get("version")


def test_format_software_summary():
    sw = {
        "gcc": {"version": "11.4.0", "path": "/usr/bin/gcc"},
        "nvcc": {"path": "/usr/bin/nvcc"},  # no parsed version
        "madbench_python": {"version": "3.11.5", "path": "/usr/bin/python"},
    }
    s = format_software_summary(sw)
    assert "gcc 11.4.0" in s
    assert "nvcc ?" in s          # unknown version rendered as '?'
    assert "python 3.11.5" in s   # running interpreter folded in
    # madbench_python is surfaced as `python`, not `madbench_python`.
    assert "madbench_python" not in s


def test_format_software_summary_empty():
    assert format_software_summary({}) == "no toolchain detected"
