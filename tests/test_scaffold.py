from __future__ import annotations

from pathlib import Path

import pytest

from madbench.scaffold import init_workspace


def test_init_workspace_creates_structure(tmp_path):
    init_workspace(tmp_path)

    assert (tmp_path / "madbench.yml").exists()
    assert (tmp_path / ".gitignore").exists()

    for d in ["scripts", "configs", "tests", "plots", "results", "logs", "analysis", "gridpacks", "MadGraph"]:
        assert (tmp_path / d).is_dir(), f"Expected directory: {d}"


def test_init_workspace_idempotent(tmp_path, capsys):
    init_workspace(tmp_path)
    init_workspace(tmp_path)  # Should not raise

    captured = capsys.readouterr()
    assert "already initialized" in captured.out


def test_init_workspace_madbench_yml_content(tmp_path):
    import yaml
    init_workspace(tmp_path)
    data = yaml.safe_load((tmp_path / "madbench.yml").read_text())
    assert "workspace" in data
    assert data["workspace"]["scripts_dir"] == "scripts"
