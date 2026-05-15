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


def make_script(ws_root: Path, name: str = "hello.sh", body: str | None = None) -> Path:
    script = ws_root / "scripts" / name
    script.write_text(body or "#!/bin/bash\necho \"hello $@\"\n")
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
        "args": {"ncores": 1, "nevents": 100},
        "result_group": "mygroup",
    }
    test_file = make_test_yaml(ws_root, yaml_data)

    td = mb.load_test(test_file)
    assert td.name == "mytest"
    assert td.script == "hello.sh"
    assert td.args == {"ncores": 1, "nevents": 100}
    assert td.inputs == []
    assert td.outputs == []
    assert td.output_files == []
    assert td.workdir is None


def test_load_test_with_new_fields(tmp_path):
    ws_root = make_workspace(tmp_path)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    yaml_data = {
        "name": "t",
        "script": "hello.sh",
        "args": {"seed": 42},
        "result_group": "g",
        "inputs": ["config/*", "data/x.txt"],
        "outputs": ["throughput", "note"],
        "output_files": ["out.log", "gridpack_{seed}/timings.txt"],
        "workdir": "/tmp/madbench-test",
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    td = mb.load_test(test_file)

    assert td.inputs == ["config/*", "data/x.txt"]
    assert td.outputs == ["throughput", "note"]
    assert td.output_files == ["out.log", "gridpack_{seed}/timings.txt"]
    assert td.workdir == "/tmp/madbench-test"


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
        args={"ncores": 4, "nevents": 100, "seed": 42},
        result_group="grp",
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
        args={"ncores": [1, 2, 4], "nevents": 100},
        result_group="grp",
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
        args={"ncores": [1, 2], "nevents": [100, 200], "seed": 42},
        result_group="grp",
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
        args={"nevents": [1000, 2000], "timeout": [10, 20], "seed": 42},
        result_group="grp",
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
        args={
            "ncores": [1, 2, 4],
            "nevents": [1000, 1_000_000],
            "timeout": [10, 600],
            "seed": 42,
        },
        result_group="grp",
        zip_groups=[["nevents", "timeout"]],
    )
    cmds = mb.build_commands(td)
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
        args={
            "a": [1, 2],
            "b": [10, 20],
            "c": ["x", "y", "z"],
            "d": ["X", "Y", "Z"],
        },
        result_group="grp",
        zip_groups=[["a", "b"], ["c", "d"]],
    )
    cmds = mb.build_commands(td)
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
        args={"a": [1, 2], "b": [10, 20, 30]},
        result_group="grp",
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
        args={"a": [1, 2]},
        result_group="grp",
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
        args={"a": [1, 2], "b": 5},
        result_group="grp",
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
        args={"a": [1, 2], "b": [3, 4], "c": [5, 6]},
        result_group="grp",
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
        "args": {"ncores": [1, 2], "nevents": 100},
        "result_group": "e2e",
    }
    test_file = make_test_yaml(ws_root, yaml_data)

    mb.run(test_file)

    archives = list((ws_root / "logs").glob("e2e_test_*.tar.gz"))
    assert len(archives) == 1

    with tarfile.open(archives[0]) as tar:
        names = tar.getnames()
        assert "main.log" in names
        assert "metadata.yml" in names

        meta = yaml.safe_load(tar.extractfile("metadata.yml").read())
        assert meta["test_name"] == "e2e_test"
        assert len(meta["commands"]) == 2

        log_content = tar.extractfile("main.log").read().decode()
        assert "hello" in log_content

    # CSV exists with both invocations
    csv_path = ws_root / "results" / "e2e" / "results.csv"
    assert csv_path.exists()
    lines = csv_path.read_text().splitlines()
    assert len(lines) == 3  # header + 2 rows
    assert "ncores" in lines[0]
    assert "exit_code" in lines[0]


def test_dry_run_no_side_effects(tmp_path, capsys):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    yaml_data = {
        "name": "dry_test",
        "script": "hello.sh",
        "args": {"ncores": [1, 2], "nevents": 100},
        "result_group": "dry",
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    mb.run(test_file, dry_run=True)

    assert not any((ws_root / "logs").glob("*.tar.gz"))
    assert not (ws_root / "results" / "dry").exists()
    assert not (ws_root / "scratch" / "dry_test").exists()

    captured = capsys.readouterr()
    assert "DRY RUN" in captured.out
    assert "hello.sh" in captured.out


# -----------------------------------------------------------------------
# inputs staging
# -----------------------------------------------------------------------


def test_run_stages_inputs(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    (ws_root / "config" / "Cards").mkdir(parents=True)
    (ws_root / "config" / "Cards" / "card.dat").write_text("CARD")
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    yaml_data = {
        "name": "staged",
        "script": "hello.sh",
        "args": {"x": 1},
        "result_group": "g",
        "inputs": ["config/Cards/*"],
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    mb.run(test_file)

    run_dirs = list((ws_root / "scratch").glob("staged_*"))
    assert len(run_dirs) == 1
    staged = run_dirs[0] / "inputs" / "config" / "Cards" / "card.dat"
    assert staged.exists()
    assert staged.read_text() == "CARD"


# -----------------------------------------------------------------------
# outputs JSON read and CSV
# -----------------------------------------------------------------------


def test_run_reads_outputs_json_and_writes_csv(tmp_path):
    ws_root = make_workspace(tmp_path)
    # Script writes a JSON object to MADBENCH_OUTPUT_FILE
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "echo \"running with $@\"\n"
            "echo \"{\\\"throughput\\\": $1, \\\"note\\\": \\\"ok\\\"}\" > \"$MADBENCH_OUTPUT_FILE\"\n"
        ),
    )
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    yaml_data = {
        "name": "without",
        "script": "hello.sh",
        "args": {"throughput": [10, 20]},
        "result_group": "g",
        "outputs": ["throughput", "note"],
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    mb.run(test_file)

    csv_path = ws_root / "results" / "g" / "results.csv"
    rows = csv_path.read_text().splitlines()
    header = rows[0].split(",")
    # arg `throughput` and output `throughput` collide on column name; this
    # is by design (later declaration wins in dict, and we expect users to
    # avoid the collision in practice). Just check that "note" landed.
    assert "note" in header
    # Both data rows have note=ok
    assert "ok" in rows[1]
    assert "ok" in rows[2]


def test_run_missing_outputs_json_writes_blanks(tmp_path, capsys):
    ws_root = make_workspace(tmp_path)
    # Script does NOT write MADBENCH_OUTPUT_FILE
    make_script(ws_root)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    yaml_data = {
        "name": "missing",
        "script": "hello.sh",
        "args": {"x": 1},
        "result_group": "g",
        "outputs": ["throughput"],
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    mb.run(test_file)

    csv_path = ws_root / "results" / "g" / "results.csv"
    rows = csv_path.read_text().splitlines()
    assert len(rows) == 2  # header + 1 row
    # 'throughput' column exists, value is empty
    header = rows[0].split(",")
    throughput_idx = header.index("throughput")
    cells = rows[1].split(",")
    assert cells[throughput_idx] == ""

    captured = capsys.readouterr()
    assert "was not written" in captured.out


# -----------------------------------------------------------------------
# output_files copy
# -----------------------------------------------------------------------


def test_run_copies_output_files_with_arg_substitution(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "seed=$1\n"
            "mkdir -p \"gridpack_${seed}\"\n"
            "echo \"timings for $seed\" > \"gridpack_${seed}/timings.txt\"\n"
        ),
    )
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    yaml_data = {
        "name": "copy_outputs",
        "script": "hello.sh",
        "args": {"seed": [1, 2]},
        "result_group": "g",
        "output_files": ["gridpack_{seed}/timings.txt"],
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    mb.run(test_file)

    inv1 = ws_root / "results" / "g" / "invocation_001" / "gridpack_1" / "timings.txt"
    inv2 = ws_root / "results" / "g" / "invocation_002" / "gridpack_2" / "timings.txt"
    assert inv1.exists() and inv1.read_text().strip() == "timings for 1"
    assert inv2.exists() and inv2.read_text().strip() == "timings for 2"


def test_run_missing_output_file_warns(tmp_path, capsys):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)  # writes nothing
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    yaml_data = {
        "name": "missing_file",
        "script": "hello.sh",
        "args": {"x": 1},
        "result_group": "g",
        "output_files": ["does_not_exist.log"],
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    mb.run(test_file)

    captured = capsys.readouterr()
    assert "output_file missing" in captured.out


# -----------------------------------------------------------------------
# env var wiring
# -----------------------------------------------------------------------


def test_run_sets_env_vars(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "echo \"WORKDIR=$MADBENCH_WORKDIR\"\n"
            "echo \"INPUTS=$MADBENCH_INPUTS\"\n"
            "echo \"OUTPUT_FILE=$MADBENCH_OUTPUT_FILE\"\n"
            "echo \"CWD=$(pwd)\"\n"
        ),
    )
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    yaml_data = {
        "name": "envcheck",
        "script": "hello.sh",
        "args": {"x": 1},
        "result_group": "g",
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    mb.run(test_file)

    archives = list((ws_root / "logs").glob("envcheck_*.tar.gz"))
    with tarfile.open(archives[0]) as tar:
        log = tar.extractfile("main.log").read().decode()

    assert "WORKDIR=" in log and "/invocation_001" in log
    assert "INPUTS=" in log and "/inputs" in log
    assert "OUTPUT_FILE=" in log and ".madbench_output.json" in log
    # cwd should equal the invocation workdir
    workdir_line = [ln for ln in log.splitlines() if ln.startswith("WORKDIR=")][0]
    cwd_line = [ln for ln in log.splitlines() if ln.startswith("CWD=")][0]
    assert workdir_line.split("=", 1)[1] == cwd_line.split("=", 1)[1]


# -----------------------------------------------------------------------
# CSV header rollover
# -----------------------------------------------------------------------


def test_csv_header_rollover_when_schema_changes(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    # Run #1 with outputs=[a]
    make_test_yaml(
        ws_root,
        {
            "name": "schema",
            "script": "hello.sh",
            "args": {"x": 1},
            "result_group": "g",
            "outputs": ["a"],
        },
        name="schema1.yml",
    )
    mb.run(ws_root / "tests" / "schema1.yml")
    assert (ws_root / "results" / "g" / "results.csv").exists()

    # Run #2 with outputs=[a, b] — different header → rollover
    make_test_yaml(
        ws_root,
        {
            "name": "schema",
            "script": "hello.sh",
            "args": {"x": 1},
            "result_group": "g",
            "outputs": ["a", "b"],
        },
        name="schema2.yml",
    )
    mb.run(ws_root / "tests" / "schema2.yml")
    assert (ws_root / "results" / "g" / "results.2.csv").exists()

    h1 = (ws_root / "results" / "g" / "results.csv").read_text().splitlines()[0]
    h2 = (ws_root / "results" / "g" / "results.2.csv").read_text().splitlines()[0]
    assert h1 != h2


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
        "script": "hello.sh",
        "args": {"n": 1},
        "result_group": "grp",
    }
    make_test_yaml(ws_root, yaml_data, "mytest.yml")

    tests = mb.list_tests()
    assert len(tests) == 1
    assert tests[0]["name"] == "mytest"
    assert not tests[0]["has_results"]
    assert not tests[0]["has_plot"]


# -----------------------------------------------------------------------
# mg_version sweep
# -----------------------------------------------------------------------


def test_load_test_mg_version_defaults_to_none(tmp_path):
    ws_root = make_workspace(tmp_path)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {"name": "t", "script": "hello.sh", "args": {"x": 1}, "result_group": "g"},
    )
    td = mb.load_test(test_file)
    assert td.mg_version == ["none"]


def test_load_test_mg_version_accepts_string(tmp_path):
    ws_root = make_workspace(tmp_path)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "t", "script": "hello.sh", "args": {"x": 1},
            "result_group": "g", "mg_version": "v3.5.4",
        },
    )
    td = mb.load_test(test_file)
    assert td.mg_version == ["v3.5.4"]


def test_load_test_mg_version_accepts_list(tmp_path):
    ws_root = make_workspace(tmp_path)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "t", "script": "hello.sh", "args": {"x": 1},
            "result_group": "g", "mg_version": ["v3.5.4", "dev_abc"],
        },
    )
    td = mb.load_test(test_file)
    assert td.mg_version == ["v3.5.4", "dev_abc"]


def test_load_test_mg_version_invalid_raises(tmp_path):
    ws_root = make_workspace(tmp_path)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "t", "script": "hello.sh", "args": {"x": 1},
            "result_group": "g", "mg_version": 123,
        },
    )
    with pytest.raises(ValueError, match="mg_version"):
        mb.load_test(test_file)


def test_build_commands_multiplied_by_mg_version(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "t", "script": "hello.sh",
            "args": {"x": [1, 2]},
            "result_group": "g",
            "mg_version": ["a", "b"],
        },
    )
    td = mb.load_test(test_file)
    cmds = mb.build_commands(td)
    # 2 versions × 2 arg combos = 4 invocations. Outer is mg_version, so the
    # first two share mg_version=a, next two share mg_version=b. Commands
    # themselves repeat because mg_version doesn't appear positionally.
    assert len(cmds) == 4
    assert cmds[0][-1] == "1" and cmds[1][-1] == "2"
    assert cmds[2][-1] == "1" and cmds[3][-1] == "2"


def test_run_per_version_workdir_layout(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "wd", "script": "hello.sh", "args": {"x": 1},
            "result_group": "g", "mg_version": ["v1", "v2"],
        },
    )
    mb.run(test_file)

    # One run_dir per version, each under scratch/<mg_version>/. Invocation
    # IDs restart per version, so the same arg combo lands at the same ID
    # across versions for easy cross-version comparison.
    v1_dirs = list((ws_root / "scratch" / "v1").glob("wd_*"))
    v2_dirs = list((ws_root / "scratch" / "v2").glob("wd_*"))
    assert len(v1_dirs) == 1 and (v1_dirs[0] / "invocation_001").is_dir()
    assert len(v2_dirs) == 1 and (v2_dirs[0] / "invocation_001").is_dir()
    # No top-level wd_* dir when versions are set.
    assert not list((ws_root / "scratch").glob("wd_*"))


def test_run_no_version_segment_when_none(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {"name": "noversion", "script": "hello.sh", "args": {"x": 1}, "result_group": "g"},
    )
    mb.run(test_file)

    # Legacy layout preserved: scratch/<test>_<ts>/, no "none/" segment.
    dirs = list((ws_root / "scratch").glob("noversion_*"))
    assert len(dirs) == 1
    assert not (ws_root / "scratch" / "none").exists()


def test_run_exposes_mg_env_vars(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "echo \"MG_VERSION=$MG_VERSION\"\n"
            "echo \"MG_BIN=$MG_BIN\"\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "envmg", "script": "hello.sh", "args": {"x": 1},
            "result_group": "g", "mg_version": ["v3.5.4"],
        },
    )
    mb.run(test_file)

    archives = list((ws_root / "logs").glob("envmg_*.tar.gz"))
    with tarfile.open(archives[0]) as tar:
        log = tar.extractfile("main.log").read().decode()
    assert "MG_VERSION=v3.5.4" in log
    assert "MG_BIN=" in log
    assert "MadGraph/v3.5.4/bin/mg5_aMC" in log


def test_run_mg_bin_empty_when_version_is_none(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "echo \"MG_VERSION=$MG_VERSION\"\n"
            "echo \"MG_BIN=[$MG_BIN]\"\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {"name": "envnone", "script": "hello.sh", "args": {"x": 1}, "result_group": "g"},
    )
    mb.run(test_file)

    archives = list((ws_root / "logs").glob("envnone_*.tar.gz"))
    with tarfile.open(archives[0]) as tar:
        log = tar.extractfile("main.log").read().decode()
    assert "MG_VERSION=none" in log
    assert "MG_BIN=[]" in log


def test_run_csv_includes_mg_version_column(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "csvmg", "script": "hello.sh", "args": {"x": [1, 2]},
            "result_group": "g", "mg_version": ["a", "b"],
        },
    )
    mb.run(test_file)

    rows = (ws_root / "results" / "g" / "results.csv").read_text().splitlines()
    header = rows[0].split(",")
    assert "mg_version" in header
    mgv_idx = header.index("mg_version")
    # 4 data rows: outer order is mg_version, so a,a,b,b
    data_mgvs = [r.split(",")[mgv_idx] for r in rows[1:]]
    assert data_mgvs == ["a", "a", "b", "b"]


def test_run_csv_mg_version_column_when_unset(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {"name": "csvnone", "script": "hello.sh", "args": {"x": 1}, "result_group": "g"},
    )
    mb.run(test_file)

    rows = (ws_root / "results" / "g" / "results.csv").read_text().splitlines()
    header = rows[0].split(",")
    assert "mg_version" in header
    mgv_idx = header.index("mg_version")
    assert rows[1].split(",")[mgv_idx] == "none"


def test_run_invocation_ids_align_across_versions(tmp_path):
    """The same arg combo gets the same invocation_id under each mg_version."""
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body="#!/bin/bash\necho \"$1 $MG_VERSION\" > marker.txt\n",
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "align", "script": "hello.sh", "args": {"x": [10, 20, 30]},
            "result_group": "g", "mg_version": ["v1", "v2"],
        },
    )
    mb.run(test_file)

    v1_root = next((ws_root / "scratch" / "v1").glob("align_*"))
    v2_root = next((ws_root / "scratch" / "v2").glob("align_*"))
    for inv, expected_x in [("invocation_001", "10"), ("invocation_002", "20"), ("invocation_003", "30")]:
        v1_marker = (v1_root / inv / "marker.txt").read_text().strip()
        v2_marker = (v2_root / inv / "marker.txt").read_text().strip()
        assert v1_marker == f"{expected_x} v1"
        assert v2_marker == f"{expected_x} v2"


def test_run_output_files_scoped_per_version(tmp_path):
    """Same invocation_id across versions writes to different result subdirs."""
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "echo \"$MG_VERSION\" > out.log\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "outv", "script": "hello.sh", "args": {"x": 1},
            "result_group": "g", "mg_version": ["v1", "v2"],
            "output_files": ["out.log"],
        },
    )
    mb.run(test_file)

    v1_out = (ws_root / "results" / "g" / "v1" / "invocation_001" / "out.log").read_text().strip()
    v2_out = (ws_root / "results" / "g" / "v2" / "invocation_001" / "out.log").read_text().strip()
    assert v1_out == "v1"
    assert v2_out == "v2"


def _install_mock_mg(ws_root: Path, version: str, body: str | None = None) -> Path:
    """Create a fake MadGraph install at MadGraph/<version>/bin/mg5_aMC.

    The default body parses the proc_card and emits one folder per ``output``
    directive, mimicking the parts of MG behaviour MadBench actually cares
    about.
    """
    bin_dir = ws_root / "MadGraph" / version / "bin"
    bin_dir.mkdir(parents=True)
    mg = bin_dir / "mg5_aMC"
    mg.write_text(
        body
        or (
            "#!/bin/bash\n"
            "# Mock MadGraph: read proc card, mkdir each `output` target in cwd.\n"
            "card=\"$1\"\n"
            "while read -r line; do\n"
            "    case \"$line\" in\n"
            "        output\\ *) mkdir -p \"${line#output }\" ;;\n"
            "    esac\n"
            "done < \"$card\"\n"
        )
    )
    mg.chmod(mg.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return mg


def test_load_test_proc_cards_default_empty(tmp_path):
    ws_root = make_workspace(tmp_path)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {"name": "t", "script": "hello.sh", "args": {"x": 1}, "result_group": "g"},
    )
    td = mb.load_test(test_file)
    assert td.proc_cards == []


def test_load_test_proc_cards_parsed(tmp_path):
    ws_root = make_workspace(tmp_path)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "t", "script": "hello.sh", "args": {"x": 1}, "result_group": "g",
            "proc_cards": ["inputs/proc_a.dat", "inputs/proc_b.dat"],
        },
    )
    td = mb.load_test(test_file)
    assert td.proc_cards == ["inputs/proc_a.dat", "inputs/proc_b.dat"]


def test_run_generates_process_dirs(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "ls \"$MADBENCH_PROCESSES\" > listing.txt\n"
        ),
    )
    _install_mock_mg(ws_root, "v1")
    (ws_root / "inputs").mkdir(exist_ok=True)
    (ws_root / "inputs" / "card1.dat").write_text("output proc_a\n")
    (ws_root / "inputs" / "card2.dat").write_text("output proc_b\n")

    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "gen", "script": "hello.sh", "args": {"x": 1},
            "result_group": "g", "mg_version": ["v1"],
            "proc_cards": ["inputs/card1.dat", "inputs/card2.dat"],
        },
    )
    mb.run(test_file)

    run_dir = next((ws_root / "scratch" / "v1").glob("gen_*"))
    assert (run_dir / "processes" / "proc_a").is_dir()
    assert (run_dir / "processes" / "proc_b").is_dir()
    # Script saw both via $MADBENCH_PROCESSES
    listing = (run_dir / "invocation_001" / "listing.txt").read_text()
    assert "proc_a" in listing and "proc_b" in listing


def test_run_generates_once_per_version_not_per_invocation(tmp_path):
    """proc_cards generation must run once per (mg_version, card), not once
    per invocation. The mock MG appends to a counter file to verify."""
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    counter = ws_root / "mg_call_count.txt"
    counter.write_text("0\n")
    _install_mock_mg(
        ws_root,
        "v1",
        body=(
            "#!/bin/bash\n"
            f"n=$(cat {counter} 2>/dev/null || echo 0)\n"
            f"echo $((n + 1)) > {counter}\n"
            "card=\"$1\"\n"
            "while read -r line; do\n"
            "    case \"$line\" in\n"
            "        output\\ *) mkdir -p \"${line#output }\" ;;\n"
            "    esac\n"
            "done < \"$card\"\n"
        ),
    )
    (ws_root / "inputs").mkdir(exist_ok=True)
    (ws_root / "inputs" / "card.dat").write_text("output p\n")

    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "once", "script": "hello.sh",
            "args": {"x": [1, 2, 3]},  # 3 invocations
            "result_group": "g", "mg_version": ["v1"],
            "proc_cards": ["inputs/card.dat"],
        },
    )
    mb.run(test_file)

    # 1 version × 1 card = 1 MG call, regardless of 3 invocations
    assert counter.read_text().strip() == "1"


def test_run_proc_cards_requires_mg_version(tmp_path):
    """proc_cards with mg_version=='none' is a configuration error — the
    version's invocations get the proc-gen-failed exit code."""
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    (ws_root / "inputs").mkdir(exist_ok=True)
    (ws_root / "inputs" / "card.dat").write_text("output p\n")

    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "needsmg", "script": "hello.sh", "args": {"x": 1},
            "result_group": "g",
            "proc_cards": ["inputs/card.dat"],
            # mg_version intentionally omitted → "none"
        },
    )
    mb.run(test_file)

    rows = (ws_root / "results" / "g" / "results.csv").read_text().splitlines()
    header = rows[0].split(",")
    ec_idx = header.index("exit_code")
    assert rows[1].split(",")[ec_idx] == "-3"


def test_run_proc_cards_missing_mg_binary(tmp_path):
    """When the MadGraph binary doesn't exist for the requested version,
    invocations are recorded with the proc-gen-failed sentinel."""
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    (ws_root / "inputs").mkdir(exist_ok=True)
    (ws_root / "inputs" / "card.dat").write_text("output p\n")
    # NOTE: no mock MG installed for "ghost" version.

    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "noghost", "script": "hello.sh", "args": {"x": 1},
            "result_group": "g", "mg_version": ["ghost"],
            "proc_cards": ["inputs/card.dat"],
        },
    )
    mb.run(test_file)

    rows = (ws_root / "results" / "g" / "results.csv").read_text().splitlines()
    header = rows[0].split(",")
    ec_idx = header.index("exit_code")
    assert rows[1].split(",")[ec_idx] == "-3"


def test_run_proc_cards_mg_failure_skips_invocations(tmp_path):
    """When MG exits non-zero, the script does not run for that version."""
    ws_root = make_workspace(tmp_path)
    marker = ws_root / "script_ran.txt"
    make_script(
        ws_root,
        body=f"#!/bin/bash\necho ran > {marker}\n",
    )
    _install_mock_mg(
        ws_root,
        "v1",
        body="#!/bin/bash\necho 'pretend MG failure' >&2\nexit 7\n",
    )
    (ws_root / "inputs").mkdir(exist_ok=True)
    (ws_root / "inputs" / "card.dat").write_text("output p\n")

    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "mgfail", "script": "hello.sh", "args": {"x": 1},
            "result_group": "g", "mg_version": ["v1"],
            "proc_cards": ["inputs/card.dat"],
        },
    )
    mb.run(test_file)

    assert not marker.exists()  # script never ran
    rows = (ws_root / "results" / "g" / "results.csv").read_text().splitlines()
    header = rows[0].split(",")
    ec_idx = header.index("exit_code")
    assert rows[1].split(",")[ec_idx] == "-3"


def test_run_proc_cards_one_version_fails_others_continue(tmp_path):
    """If proc-gen fails for one mg_version, the other versions still run."""
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    # v_good has a working mock MG; v_bad's binary will not exist.
    _install_mock_mg(ws_root, "v_good")
    (ws_root / "inputs").mkdir(exist_ok=True)
    (ws_root / "inputs" / "card.dat").write_text("output p\n")

    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "mixed", "script": "hello.sh", "args": {"x": 1},
            "result_group": "g", "mg_version": ["v_good", "v_bad"],
            "proc_cards": ["inputs/card.dat"],
        },
    )
    mb.run(test_file)

    rows = (ws_root / "results" / "g" / "results.csv").read_text().splitlines()
    header = rows[0].split(",")
    ec_idx = header.index("exit_code")
    mgv_idx = header.index("mg_version")
    by_version = {r.split(",")[mgv_idx]: r.split(",")[ec_idx] for r in rows[1:]}
    assert by_version["v_good"] == "0"
    assert by_version["v_bad"] == "-3"


def test_run_processes_env_var_set_even_without_proc_cards(tmp_path):
    """$MADBENCH_PROCESSES is always exposed so scripts can rely on it."""
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "echo \"PROCESSES=$MADBENCH_PROCESSES\"\n"
            "[ -d \"$MADBENCH_PROCESSES\" ] && echo \"DIR_EXISTS\"\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {"name": "penv", "script": "hello.sh", "args": {"x": 1}, "result_group": "g"},
    )
    mb.run(test_file)

    archives = list((ws_root / "logs").glob("penv_*.tar.gz"))
    with tarfile.open(archives[0]) as tar:
        log = tar.extractfile("main.log").read().decode()
    assert "PROCESSES=" in log and "/processes" in log
    assert "DIR_EXISTS" in log


def test_run_inputs_staged_per_version(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    (ws_root / "config").mkdir()
    (ws_root / "config" / "card.dat").write_text("CARD")
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "inputsmg", "script": "hello.sh", "args": {"x": 1},
            "result_group": "g", "mg_version": ["v1", "v2"],
            "inputs": ["config/card.dat"],
        },
    )
    mb.run(test_file)

    v1_dir = next((ws_root / "scratch" / "v1").glob("inputsmg_*"))
    v2_dir = next((ws_root / "scratch" / "v2").glob("inputsmg_*"))
    assert (v1_dir / "inputs" / "config" / "card.dat").read_text() == "CARD"
    assert (v2_dir / "inputs" / "config" / "card.dat").read_text() == "CARD"
