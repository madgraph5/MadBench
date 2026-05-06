from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml

from madbench.workspace import (
    WorkspaceConfig,
    find_workspace,
    resolve_configs,
    resolve_script,
)


def make_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace in tmp_path."""
    config = {
        "workspace": {
            "scripts_dir": "scripts",
            "configs_dir": "configs",
            "tests_dir": "tests",
            "plots_dir": "plots",
            "results_dir": "results",
            "logs_dir": "logs",
        },
        "defaults": {},
    }
    (tmp_path / "madbench.yml").write_text(yaml.dump(config))
    for d in ["scripts", "configs", "tests", "plots", "results", "logs"]:
        (tmp_path / d).mkdir()
    return tmp_path


def test_find_workspace_in_root(tmp_path):
    make_workspace(tmp_path)
    ws = find_workspace(tmp_path)
    assert ws.root == tmp_path
    assert ws.scripts_dir == tmp_path / "scripts"


def test_find_workspace_from_subdir(tmp_path):
    make_workspace(tmp_path)
    subdir = tmp_path / "scripts" / "subdir"
    subdir.mkdir(parents=True)
    ws = find_workspace(subdir)
    assert ws.root == tmp_path


def test_find_workspace_not_found(tmp_path):
    with pytest.raises(FileNotFoundError, match="madbench.yml"):
        find_workspace(tmp_path)


def test_resolve_script_found(tmp_path):
    ws_root = make_workspace(tmp_path)
    ws = find_workspace(ws_root)

    script = ws_root / "scripts" / "run.sh"
    script.write_text("#!/bin/bash\necho hi")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    path = resolve_script(ws, "run.sh")
    assert path == script.resolve()


def test_resolve_script_not_found(tmp_path):
    ws = find_workspace(make_workspace(tmp_path))
    with pytest.raises(FileNotFoundError):
        resolve_script(ws, "missing.sh")


def test_resolve_script_not_executable(tmp_path):
    ws_root = make_workspace(tmp_path)
    ws = find_workspace(ws_root)

    script = ws_root / "scripts" / "noexec.sh"
    script.write_text("#!/bin/bash\necho hi")
    # Ensure not executable
    script.chmod(0o644)

    with pytest.raises(PermissionError):
        resolve_script(ws, "noexec.sh")


def test_resolve_configs(tmp_path):
    ws_root = make_workspace(tmp_path)
    ws = find_workspace(ws_root)

    cfg = ws_root / "configs" / "my.cfg"
    cfg.write_text("key=value")

    paths = resolve_configs(ws, ["my.cfg"])
    assert len(paths) == 1
    assert paths[0] == cfg.resolve()


def test_resolve_configs_missing(tmp_path):
    ws = find_workspace(make_workspace(tmp_path))
    with pytest.raises(FileNotFoundError):
        resolve_configs(ws, ["nonexistent.cfg"])
