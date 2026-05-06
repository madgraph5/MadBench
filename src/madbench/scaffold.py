from __future__ import annotations

import shutil
from pathlib import Path

_TEMPLATES_DIR = Path(__file__).parent / "templates"

_WORKSPACE_DIRS = [
    "scripts",
    "configs",
    "tests",
    "plots",
    "results",
    "logs",
    "analysis",
    "gridpacks",
    "MadGraph",
]


def init_workspace(target: Path) -> None:
    """Create the workspace directory structure and madbench.yml in `target`.

    Creates: scripts/, configs/, tests/, plots/, results/, logs/,
             analysis/, gridpacks/, MadGraph/

    Writes madbench.yml from template.
    Writes .gitignore from template.

    If madbench.yml already exists, print a message and abort (don't overwrite).
    """
    marker = target / "madbench.yml"
    if marker.exists():
        print("[madbench] madbench.yml already exists — workspace already initialized.")
        return

    target.mkdir(parents=True, exist_ok=True)

    for dirname in _WORKSPACE_DIRS:
        (target / dirname).mkdir(exist_ok=True)
        print(f"[madbench] Created {dirname}/")

    shutil.copy(_TEMPLATES_DIR / "madbench.yml.template", marker)
    print("[madbench] Created madbench.yml")

    gitignore_dest = target / ".gitignore"
    if not gitignore_dest.exists():
        shutil.copy(_TEMPLATES_DIR / "gitignore.template", gitignore_dest)
        print("[madbench] Created .gitignore")
    else:
        print("[madbench] .gitignore already exists — skipping")

    print(f"[madbench] Workspace initialized at {target}")
