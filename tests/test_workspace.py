from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml

from madbench.workspace import (
    find_workspace,
    resolve_mg_bin,
    resolve_script,
    stage_inputs,
)


def make_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace in tmp_path."""
    config = {
        "workspace": {
            "scripts_dir": "scripts",
            "tests_dir": "tests",
            "plots_dir": "plots",
            "results_dir": "results",
            "logs_dir": "logs",
            "scratch_dir": "scratch",
        },
        "defaults": {},
    }
    (tmp_path / "madbench.yml").write_text(yaml.dump(config))
    for d in ["scripts", "tests", "plots", "results", "logs", "scratch"]:
        (tmp_path / d).mkdir()
    return tmp_path


def test_find_workspace_in_root(tmp_path):
    make_workspace(tmp_path)
    ws = find_workspace(tmp_path)
    assert ws.root == tmp_path
    assert ws.scripts_dir == tmp_path / "scripts"
    assert ws.scratch_dir == tmp_path / "scratch"


def test_find_workspace_from_subdir(tmp_path):
    make_workspace(tmp_path)
    subdir = tmp_path / "scripts" / "subdir"
    subdir.mkdir(parents=True)
    ws = find_workspace(subdir)
    assert ws.root == tmp_path


def test_find_workspace_not_found(tmp_path):
    with pytest.raises(FileNotFoundError, match="madbench.yml"):
        find_workspace(tmp_path)


def test_find_workspace_defaults_scratch_dir(tmp_path):
    # No scratch_dir in YAML → defaults to <root>/scratch/
    config = {"workspace": {}, "defaults": {}}
    (tmp_path / "madbench.yml").write_text(yaml.dump(config))
    ws = find_workspace(tmp_path)
    assert ws.scratch_dir == tmp_path / "scratch"


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
    script.chmod(0o644)

    with pytest.raises(PermissionError):
        resolve_script(ws, "noexec.sh")


@pytest.mark.parametrize(
    "preferred", ["executable", "missing", "nonexecutable", "directory", "broken_link"],
)
def test_resolve_mg_bin_selects_first_executable(tmp_path, preferred):
    ws = find_workspace(make_workspace(tmp_path))
    bin_dir = tmp_path / "MadGraph" / "v1" / "bin"
    bin_dir.mkdir(parents=True)
    legacy = bin_dir / "mg5_aMC"
    legacy.write_text("#!/bin/sh\nexit 0\n")
    legacy.chmod(0o755)
    modern = bin_dir / "madgraph"
    if preferred in {"executable", "nonexecutable"}:
        modern.write_text("#!/bin/sh\nexit 0\n")
        modern.chmod(0o755 if preferred == "executable" else 0o644)
    elif preferred == "directory":
        modern.mkdir()
    elif preferred == "broken_link":
        modern.symlink_to("missing")

    expected = modern if preferred == "executable" else legacy
    assert resolve_mg_bin(ws, "v1", required=True) == expected


def test_resolve_mg_bin_accepts_executable_symlink(tmp_path):
    ws = find_workspace(make_workspace(tmp_path))
    bin_dir = tmp_path / "MadGraph" / "v1" / "bin"
    bin_dir.mkdir(parents=True)
    target = bin_dir / "launcher"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    modern = bin_dir / "madgraph"
    modern.symlink_to("launcher")

    assert resolve_mg_bin(ws, "v1", required=True) == modern


@pytest.mark.parametrize("binary", [None, "madgraph", "mg5_aMC"])
def test_resolve_mg_bin_reports_unavailable_candidates(tmp_path, binary):
    ws = find_workspace(make_workspace(tmp_path))
    bin_dir = tmp_path / "MadGraph" / "v1" / "bin"
    if binary is not None:
        bin_dir.mkdir(parents=True)
        path = bin_dir / binary
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o644)
    error = FileNotFoundError if binary is None else PermissionError

    with pytest.raises(error) as exc:
        resolve_mg_bin(ws, "v1", required=True)
    assert str(bin_dir / "madgraph") in str(exc.value)
    assert str(bin_dir / "mg5_aMC") in str(exc.value)


def test_resolve_mg_bin_allows_uninstalled_version_for_scripts(tmp_path):
    ws = find_workspace(make_workspace(tmp_path))
    assert resolve_mg_bin(ws, "v1") == tmp_path / "MadGraph/v1/bin/mg5_aMC"
    assert resolve_mg_bin(ws, "none", required=True) is None


# -----------------------------------------------------------------------
# stage_inputs
# -----------------------------------------------------------------------


def test_stage_inputs_literal_file(tmp_path):
    ws_root = make_workspace(tmp_path)
    (ws_root / "config").mkdir()
    src = ws_root / "config" / "foo.txt"
    src.write_text("hi")

    dest = tmp_path / "dest"
    created = stage_inputs(ws_root, ["config/foo.txt"], dest)

    assert (dest / "config" / "foo.txt").read_text() == "hi"
    assert created == [dest / "config" / "foo.txt"]


def test_stage_inputs_glob_preserves_structure(tmp_path):
    ws_root = make_workspace(tmp_path)
    (ws_root / "config" / "Cards").mkdir(parents=True)
    (ws_root / "config" / "Cards" / "a.dat").write_text("A")
    (ws_root / "config" / "Cards" / "b.dat").write_text("B")
    (ws_root / "gridpacks" / "mg5").mkdir(parents=True)
    (ws_root / "gridpacks" / "mg5" / "x.tar").write_text("X")

    dest = tmp_path / "dest"
    stage_inputs(ws_root, ["config/Cards/*", "gridpacks/mg5/*"], dest)

    assert (dest / "config" / "Cards" / "a.dat").read_text() == "A"
    assert (dest / "config" / "Cards" / "b.dat").read_text() == "B"
    assert (dest / "gridpacks" / "mg5" / "x.tar").read_text() == "X"


def test_stage_inputs_directory(tmp_path):
    ws_root = make_workspace(tmp_path)
    (ws_root / "config" / "Cards").mkdir(parents=True)
    (ws_root / "config" / "Cards" / "a.dat").write_text("A")

    dest = tmp_path / "dest"
    stage_inputs(ws_root, ["config/Cards"], dest)

    assert (dest / "config" / "Cards" / "a.dat").read_text() == "A"


def test_stage_inputs_missing_raises(tmp_path):
    ws_root = make_workspace(tmp_path)
    dest = tmp_path / "dest"
    with pytest.raises(FileNotFoundError, match="matched nothing"):
        stage_inputs(ws_root, ["nonexistent/*"], dest)
