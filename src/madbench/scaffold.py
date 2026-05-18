from __future__ import annotations

import shutil
from pathlib import Path

_TEMPLATES_DIR = Path(__file__).parent / "templates"

_WORKSPACE_DIRS = [
    "scripts",
    "tests",
    "plots",
    "results",
    "logs",
    "scratch",
    "analysis",
    "inputs",
    "gridpacks",
    "MadGraph",
]


def init_workspace(target: Path) -> None:
    """Create the workspace directory structure and madbench.yml in `target`.

    Creates: scripts/, tests/, plots/, results/, logs/, scratch/,
             analysis/, inputs/, gridpacks/, MadGraph/

    On first init, writes madbench.yml, .gitignore, and README.md from
    templates. If madbench.yml already exists (e.g. the workspace was cloned
    from a remote), the template files are left alone and only missing
    directories are created — so a fresh clone can be topped up with the
    local-only dirs (scratch/, MadGraph/) without touching version-controlled
    files.
    """
    target.mkdir(parents=True, exist_ok=True)
    marker = target / "madbench.yml"
    already_initialized = marker.exists()

    if already_initialized:
        print("[madbench] madbench.yml already exists — topping up workspace dirs.")

    for dirname in _WORKSPACE_DIRS:
        d = target / dirname
        if d.exists():
            continue
        d.mkdir()
        print(f"[madbench] Created {dirname}/")

    if not already_initialized:
        shutil.copy(_TEMPLATES_DIR / "madbench.yml.template", marker)
        print("[madbench] Created madbench.yml")

        gitignore_dest = target / ".gitignore"
        if not gitignore_dest.exists():
            shutil.copy(_TEMPLATES_DIR / "gitignore.template", gitignore_dest)
            print("[madbench] Created .gitignore")
        else:
            print("[madbench] .gitignore already exists — skipping")

        readme_dest = target / "README.md"
        if not readme_dest.exists():
            workspace_name = target.resolve().name or "workspace"
            template = (_TEMPLATES_DIR / "README.md.template").read_text()
            readme_dest.write_text(template.replace("__WORKSPACE_NAME__", workspace_name))
            print("[madbench] Created README.md")
        else:
            print("[madbench] README.md already exists — skipping")

    print(f"[madbench] Workspace ready at {target}")
