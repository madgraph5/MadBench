from __future__ import annotations

import os
from pathlib import Path

import pytest

from madbench.utils import detect_hardware, get_git_sha, get_timestamp


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
    assert "cpu_count" in hw
    assert "platform" in hw
    assert "gpu_detected" in hw
