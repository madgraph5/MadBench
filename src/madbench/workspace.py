from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

WORKSPACE_MARKER = "madbench.yml"


@dataclass
class WorkspaceConfig:
    """Parsed and resolved workspace configuration."""

    root: Path
    scripts_dir: Path
    tests_dir: Path
    plots_dir: Path
    results_dir: Path
    logs_dir: Path
    scratch_dir: Path
    defaults: dict = field(default_factory=dict)


def find_workspace(start: Optional[Path] = None) -> WorkspaceConfig:
    """Walk up from `start` (default: cwd) looking for madbench.yml.

    Parse it and return a WorkspaceConfig with resolved absolute paths.
    Raise FileNotFoundError if not found.
    """
    current = (start or Path.cwd()).resolve()
    while True:
        candidate = current / WORKSPACE_MARKER
        if candidate.exists():
            return _parse_workspace(current, candidate)
        parent = current.parent
        if parent == current:
            break
        current = parent

    raise FileNotFoundError(
        "No madbench.yml found. Run 'madbench init' to create a workspace."
    )


def _parse_workspace(root: Path, marker: Path) -> WorkspaceConfig:
    """Parse madbench.yml and build a WorkspaceConfig."""
    with open(marker) as f:
        data = yaml.safe_load(f) or {}

    ws_section = data.get("workspace", {}) or {}
    defaults = data.get("defaults", {}) or {}

    def resolve(key: str, default: str) -> Path:
        rel = ws_section.get(key, default)
        return (root / rel).resolve()

    return WorkspaceConfig(
        root=root,
        scripts_dir=resolve("scripts_dir", "scripts"),
        tests_dir=resolve("tests_dir", "tests"),
        plots_dir=resolve("plots_dir", "plots"),
        results_dir=resolve("results_dir", "results"),
        logs_dir=resolve("logs_dir", "logs"),
        scratch_dir=resolve("scratch_dir", "scratch"),
        defaults=defaults,
    )


def resolve_script(ws: WorkspaceConfig, script_name: str) -> Path:
    """Resolve a script name relative to ws.scripts_dir.

    Verify it exists and is executable. Raise FileNotFoundError or PermissionError.
    """
    script_path = (ws.scripts_dir / script_name).resolve()
    if not script_path.exists():
        raise FileNotFoundError(
            f"Script not found: {script_path}\n"
            f"Expected in scripts_dir: {ws.scripts_dir}"
        )
    if not os.access(script_path, os.X_OK):
        raise PermissionError(
            f"Script is not executable: {script_path}\n"
            f"Run: chmod +x {script_path}"
        )
    return script_path


def resolve_plot_module(ws: WorkspaceConfig, plot_name: str) -> Optional[Path]:
    """Resolve a plot module name to plots/<name>.py. Return None if not declared."""
    if not plot_name:
        return None
    p = (ws.plots_dir / f"{plot_name}.py").resolve()
    return p if p.exists() else None


def stage_inputs(
    workspace_root: Path,
    patterns: list[str],
    dest_dir: Path,
) -> list[Path]:
    """Copy workspace-relative paths/globs into ``dest_dir`` preserving structure.

    Each pattern is interpreted relative to ``workspace_root`` and expanded with
    ``Path.glob``. Matched files are copied to ``dest_dir/<their workspace-relative
    path>``; matched directories are copied recursively. Returns the list of
    destination paths created.

    Raises FileNotFoundError if a pattern matches nothing (catches typos).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    for pattern in patterns:
        # Path.glob with a fixed (non-glob) pattern returns nothing, so detect
        # the literal case and handle it separately.
        if any(ch in pattern for ch in "*?["):
            matches = sorted(workspace_root.glob(pattern))
        else:
            literal = (workspace_root / pattern).resolve()
            matches = [literal] if literal.exists() else []

        if not matches:
            raise FileNotFoundError(
                f"inputs pattern matched nothing: {pattern!r} "
                f"(workspace root: {workspace_root})"
            )

        for src in matches:
            try:
                rel = src.resolve().relative_to(workspace_root.resolve())
            except ValueError:
                raise ValueError(
                    f"inputs pattern {pattern!r} matched a path outside the "
                    f"workspace: {src}"
                )
            target = dest_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, target, dirs_exist_ok=True)
            else:
                shutil.copy2(src, target)
            created.append(target)

    return created
