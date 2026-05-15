from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from madbench.utils import (
    _parse_nvidia_smi,
    _parse_rocm_smi,
    detect_hardware,
    format_hardware_summary,
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
    assert "cpu_count" in hw
    assert "platform" in hw
    assert "gpus" in hw and isinstance(hw["gpus"], list)


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
        "cpu_count": 32, "platform": "Linux",
        "gpus": [],
    })
    assert "wn-12" in s
    assert "32 CPU" in s
    assert "no GPU detected" in s


def test_format_hardware_summary_collapses_duplicates():
    s = format_hardware_summary({
        "hostname": "h", "fqdn": "h.example.com",
        "cpu_count": 8, "platform": "Linux",
        "gpus": [
            {"vendor": "nvidia", "index": 0, "name": "A100", "memory_mb": 81920},
            {"vendor": "nvidia", "index": 1, "name": "A100", "memory_mb": 81920},
        ],
        "cuda_visible_devices": "0",
    })
    assert "h (h.example.com)" in s
    assert "2× A100 (81920MB)" in s
    assert "visible=0" in s
