from __future__ import annotations

import os
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
    configs_dir: Path
    tests_dir: Path
    plots_dir: Path
    results_dir: Path
    logs_dir: Path
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
        configs_dir=resolve("configs_dir", "configs"),
        tests_dir=resolve("tests_dir", "tests"),
        plots_dir=resolve("plots_dir", "plots"),
        results_dir=resolve("results_dir", "results"),
        logs_dir=resolve("logs_dir", "logs"),
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


def resolve_configs(ws: WorkspaceConfig, config_names: list[str]) -> list[Path]:
    """Resolve config names relative to ws.configs_dir."""
    resolved = []
    missing = []
    for name in config_names:
        p = (ws.configs_dir / name).resolve()
        if not p.exists():
            missing.append(str(p))
        else:
            resolved.append(p)
    if missing:
        raise FileNotFoundError(
            "Config file(s) not found:\n" + "\n".join(f"  {m}" for m in missing)
        )
    return resolved


def resolve_plot_module(ws: WorkspaceConfig, plot_name: str) -> Optional[Path]:
    """Resolve a plot module name to plots/<name>.py. Return None if not declared."""
    if not plot_name:
        return None
    p = (ws.plots_dir / f"{plot_name}.py").resolve()
    return p if p.exists() else None
