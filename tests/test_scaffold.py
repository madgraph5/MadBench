from __future__ import annotations

from madbench.scaffold import init_workspace


def test_init_workspace_creates_structure(tmp_path):
    init_workspace(tmp_path)

    assert (tmp_path / "madbench.yml").exists()
    assert (tmp_path / ".gitignore").exists()

    for d in ["scripts", "tests", "plots", "results", "logs", "scratch", "analysis", "inputs", "gridpacks", "MadGraph"]:
        assert (tmp_path / d).is_dir(), f"Expected directory: {d}"


def test_init_workspace_tops_up_missing_dirs(tmp_path):
    """Re-running init on an existing workspace (e.g. a fresh clone) should
    create the local-only dirs without rewriting the version-controlled files."""
    import shutil as _shutil

    init_workspace(tmp_path)

    yml_before = (tmp_path / "madbench.yml").read_text()
    (tmp_path / "madbench.yml").write_text(yml_before + "\n# user edit\n")
    _shutil.rmtree(tmp_path / "scratch")
    _shutil.rmtree(tmp_path / "MadGraph")

    init_workspace(tmp_path)

    assert (tmp_path / "scratch").is_dir()
    assert (tmp_path / "MadGraph").is_dir()
    assert (tmp_path / "madbench.yml").read_text().endswith("# user edit\n")


def test_init_workspace_madbench_yml_content(tmp_path):
    import yaml
    init_workspace(tmp_path)
    data = yaml.safe_load((tmp_path / "madbench.yml").read_text())
    assert "workspace" in data
    assert data["workspace"]["scripts_dir"] == "scripts"
    assert data["workspace"]["scratch_dir"] == "scratch"


def test_init_workspace_gitignore_contains_scratch(tmp_path):
    init_workspace(tmp_path)
    gi = (tmp_path / ".gitignore").read_text()
    assert "scratch/" in gi
