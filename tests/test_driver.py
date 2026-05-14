from __future__ import annotations

import stat
import tarfile
from pathlib import Path

import pytest
import yaml

from madbench.driver import MadBench, TestDefinition
from madbench.workspace import find_workspace


def make_workspace(tmp_path: Path) -> Path:
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


def make_script(ws_root: Path, name: str = "hello.sh") -> Path:
    script = ws_root / "scripts" / name
    script.write_text("#!/bin/bash\necho \"hello $@\"\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def make_test_yaml(ws_root: Path, data: dict, name: str = "test.yml") -> Path:
    test_file = ws_root / "tests" / name
    test_file.write_text(yaml.dump(data))
    return test_file


# -----------------------------------------------------------------------
# load_test
# -----------------------------------------------------------------------


def test_load_test_basic(tmp_path):
    ws_root = make_workspace(tmp_path)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    yaml_data = {
        "name": "mytest",
        "description": "A test",
        "script": "hello.sh",
        "configs": [],
        "args": {"ncores": 1, "nevents": 100},
        "result_group": "mygroup",
    }
    test_file = make_test_yaml(ws_root, yaml_data)

    td = mb.load_test(test_file)
    assert td.name == "mytest"
    assert td.script == "hello.sh"
    assert td.args == {"ncores": 1, "nevents": 100}


def test_load_test_missing_fields(tmp_path):
    ws_root = make_workspace(tmp_path)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    test_file = make_test_yaml(ws_root, {"name": "oops"})
    with pytest.raises(ValueError, match="missing required field"):
        mb.load_test(test_file)


# -----------------------------------------------------------------------
# build_commands
# -----------------------------------------------------------------------


def test_build_commands_scalar_only(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    td = TestDefinition(
        name="t", description="", script="hello.sh",
        configs=[], args={"ncores": 4, "nevents": 100, "seed": 42},
        result_group="grp", plot=None, raw={},
    )
    cmds = mb.build_commands(td)
    assert len(cmds) == 1
    assert cmds[0][1:] == ["4", "100", "42"]


def test_build_commands_one_list(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    td = TestDefinition(
        name="t", description="", script="hello.sh",
        configs=[], args={"ncores": [1, 2, 4], "nevents": 100},
        result_group="grp", plot=None, raw={},
    )
    cmds = mb.build_commands(td)
    assert len(cmds) == 3
    assert cmds[0][1:] == ["1", "100"]
    assert cmds[1][1:] == ["2", "100"]
    assert cmds[2][1:] == ["4", "100"]


def test_build_commands_cartesian(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    td = TestDefinition(
        name="t", description="", script="hello.sh",
        configs=[], args={"ncores": [1, 2], "nevents": [100, 200], "seed": 42},
        result_group="grp", plot=None, raw={},
    )
    cmds = mb.build_commands(td)
    assert [c[1:] for c in cmds] == [
        ["1", "100", "42"],
        ["1", "200", "42"],
        ["2", "100", "42"],
        ["2", "200", "42"],
    ]


def test_build_commands_single_zip_group(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    td = TestDefinition(
        name="t", description="", script="hello.sh",
        configs=[],
        args={"nevents": [1000, 2000], "timeout": [10, 20], "seed": 42},
        result_group="grp", plot=None, raw={},
        zip_groups=[["nevents", "timeout"]],
    )
    cmds = mb.build_commands(td)
    assert [c[1:] for c in cmds] == [
        ["1000", "10", "42"],
        ["2000", "20", "42"],
    ]


def test_build_commands_zip_and_cartesian(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    td = TestDefinition(
        name="t", description="", script="hello.sh",
        configs=[],
        args={
            "ncores": [1, 2, 4],
            "nevents": [1000, 1_000_000],
            "timeout": [10, 600],
            "seed": 42,
        },
        result_group="grp", plot=None, raw={},
        zip_groups=[["nevents", "timeout"]],
    )
    cmds = mb.build_commands(td)
    # 3 (ncores) * 2 (zipped pair) = 6 runs; nevents/timeout always paired
    assert len(cmds) == 6
    pairs = {(c[2], c[3]) for c in cmds}
    assert pairs == {("1000", "10"), ("1000000", "600")}
    ncores_vals = sorted({c[1] for c in cmds})
    assert ncores_vals == ["1", "2", "4"]


def test_build_commands_multiple_zip_groups(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    td = TestDefinition(
        name="t", description="", script="hello.sh",
        configs=[],
        args={
            "a": [1, 2],
            "b": [10, 20],
            "c": ["x", "y", "z"],
            "d": ["X", "Y", "Z"],
        },
        result_group="grp", plot=None, raw={},
        zip_groups=[["a", "b"], ["c", "d"]],
    )
    cmds = mb.build_commands(td)
    # 2 * 3 = 6 runs
    assert len(cmds) == 6
    for c in cmds:
        a, b, cc, dd = c[1:]
        assert (a, b) in {("1", "10"), ("2", "20")}
        assert (cc, dd) in {("x", "X"), ("y", "Y"), ("z", "Z")}


def test_build_commands_zip_mismatched_length_raises(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    td = TestDefinition(
        name="t", description="", script="hello.sh",
        configs=[], args={"a": [1, 2], "b": [10, 20, 30]},
        result_group="grp", plot=None, raw={},
        zip_groups=[["a", "b"]],
    )
    with pytest.raises(ValueError, match="mismatched lengths"):
        mb.build_commands(td)


def test_build_commands_zip_unknown_arg_raises(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    td = TestDefinition(
        name="t", description="", script="hello.sh",
        configs=[], args={"a": [1, 2]},
        result_group="grp", plot=None, raw={},
        zip_groups=[["a", "missing"]],
    )
    with pytest.raises(ValueError, match="unknown arg"):
        mb.build_commands(td)


def test_build_commands_zip_scalar_member_raises(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    td = TestDefinition(
        name="t", description="", script="hello.sh",
        configs=[], args={"a": [1, 2], "b": 5},
        result_group="grp", plot=None, raw={},
        zip_groups=[["a", "b"]],
    )
    with pytest.raises(ValueError, match="must be a list"):
        mb.build_commands(td)


def test_build_commands_zip_overlap_raises(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    td = TestDefinition(
        name="t", description="", script="hello.sh",
        configs=[], args={"a": [1, 2], "b": [3, 4], "c": [5, 6]},
        result_group="grp", plot=None, raw={},
        zip_groups=[["a", "b"], ["b", "c"]],
    )
    with pytest.raises(ValueError, match="more than one zip group"):
        mb.build_commands(td)


def test_load_test_zip_field_single_group(tmp_path):
    ws_root = make_workspace(tmp_path)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    yaml_data = {
        "name": "z",
        "description": "",
        "script": "hello.sh",
        "args": {"a": [1, 2], "b": [10, 20]},
        "result_group": "g",
        "zip": ["a", "b"],
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    td = mb.load_test(test_file)
    assert td.zip_groups == [["a", "b"]]


def test_load_test_zip_field_multi_group(tmp_path):
    ws_root = make_workspace(tmp_path)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    yaml_data = {
        "name": "z",
        "description": "",
        "script": "hello.sh",
        "args": {"a": [1, 2], "b": [10, 20], "c": [3], "d": [30]},
        "result_group": "g",
        "zip": [["a", "b"], ["c", "d"]],
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    td = mb.load_test(test_file)
    assert td.zip_groups == [["a", "b"], ["c", "d"]]


# -----------------------------------------------------------------------
# end-to-end run
# -----------------------------------------------------------------------


def test_run_end_to_end(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    yaml_data = {
        "name": "e2e_test",
        "description": "End-to-end smoke test",
        "script": "hello.sh",
        "configs": [],
        "args": {"ncores": [1, 2], "nevents": 100},
        "result_group": "e2e",
    }
    test_file = make_test_yaml(ws_root, yaml_data)

    mb.run(test_file)

    # Check that a tar.gz was created in logs/
    archives = list((ws_root / "logs").glob("e2e_test_*.tar.gz"))
    assert len(archives) == 1

    archive = archives[0]
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert "main.log" in names
        assert "metadata.yml" in names

        # Check metadata content
        meta_f = tar.extractfile("metadata.yml")
        assert meta_f is not None
        meta = yaml.safe_load(meta_f.read())
        assert meta["test_name"] == "e2e_test"
        assert len(meta["commands"]) == 2

        # Check main.log has output from both runs
        log_f = tar.extractfile("main.log")
        assert log_f is not None
        log_content = log_f.read().decode()
        assert "hello" in log_content


def test_dry_run_no_side_effects(tmp_path, capsys):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    yaml_data = {
        "name": "dry_test",
        "description": "",
        "script": "hello.sh",
        "configs": [],
        "args": {"ncores": [1, 2], "nevents": 100},
        "result_group": "dry",
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    mb.run(test_file, dry_run=True)

    # No archives or result dirs should have been created
    assert not any((ws_root / "logs").glob("*.tar.gz"))
    assert not (ws_root / "results" / "dry").exists()

    captured = capsys.readouterr()
    assert "DRY RUN" in captured.out
    assert "hello.sh" in captured.out


# -----------------------------------------------------------------------
# list_tests
# -----------------------------------------------------------------------


def test_list_tests(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    yaml_data = {
        "name": "mytest",
        "description": "",
        "script": "hello.sh",
        "configs": [],
        "args": {"n": 1},
        "result_group": "grp",
    }
    make_test_yaml(ws_root, yaml_data, "mytest.yml")

    tests = mb.list_tests()
    assert len(tests) == 1
    assert tests[0]["name"] == "mytest"
    assert not tests[0]["has_results"]
    assert not tests[0]["has_plot"]
