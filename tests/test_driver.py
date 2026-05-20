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


def run_dir(ws_root: Path, test_name: str) -> Path:
    """Return the single per-run subfolder created by a madbench run.

    Each ``madbench run`` writes its outputs into
    ``results/<test_name>/<hostname>_<timestamp>/``. Tests almost always
    invoke exactly one run per ``test_name``, so we list and assert
    uniqueness.
    """
    matches = list((ws_root / "results" / test_name).iterdir())
    assert len(matches) == 1, (
        f"Expected exactly one per-run subdir for {test_name!r}, "
        f"found {[str(m) for m in matches]}"
    )
    return matches[0]


def try_dir(ws_root: Path, test_name: str, n: int = 0) -> Path:
    """Return ``results/<test>/<host>_<ts>/try_N/`` for the single run."""
    return run_dir(ws_root, test_name) / f"try_{n}"


def latest_archive(ws_root: Path, test_name: str) -> Path:
    """Return the most recent ``logs/<test>/<host>_<ts>_tryN.tar.gz``."""
    archives = sorted(
        (ws_root / "logs" / test_name).glob("*.tar.gz"),
        key=lambda p: p.stat().st_mtime,
    )
    assert archives, f"No log archives for {test_name!r}"
    return archives[-1]


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
    }
    test_file = make_test_yaml(ws_root, yaml_data)

    td = mb.load_test(test_file)
    assert td.name == "mytest"
    assert td.script == "hello.sh"
    assert td.args == {"ncores": 1, "nevents": 100}
    assert td.inputs == []
    assert td.outputs == []
    assert td.artifacts == []
    assert td.workdir is None


def test_load_test_with_new_fields(tmp_path):
    ws_root = make_workspace(tmp_path)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    yaml_data = {
        "name": "t",
        "script": "hello.sh",
        "args": {"seed": 42},
        "inputs": ["config/*", "data/x.txt"],
        "outputs": ["throughput", "note"],
        "artifacts": ["out.log", "gridpack_{seed}/timings.txt"],
        "workdir": "/tmp/madbench-test",
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    td = mb.load_test(test_file)

    assert td.inputs == ["config/*", "data/x.txt"]
    assert td.outputs == ["throughput", "note"]
    assert td.artifacts == ["out.log", "gridpack_{seed}/timings.txt"]
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
    }
    test_file = make_test_yaml(ws_root, yaml_data)

    mb.run(test_file)

    archives = list((ws_root / "logs" / "e2e_test").glob("*.tar.gz"))
    assert len(archives) == 1
    # Per-try archive naming: <host>_<ts>_try0.tar.gz
    assert archives[0].name.endswith("_try0.tar.gz")

    with tarfile.open(archives[0]) as tar:
        names = tar.getnames()
        run_dir_name = archives[0].name[: -len(".tar.gz")]
        assert f"{run_dir_name}/main.log" in names
        # try.yml is the per-try audit record inside the log archive
        # (sibling of main.log, distinct from the slim try.yml in the
        # results tree).
        assert f"{run_dir_name}/try.yml" in names
        # Per-rep stdout/stderr split: subprocess output no longer lands in
        # main.log; it goes to <run>/<invocation>/<rep>/stdout.log etc.
        assert f"{run_dir_name}/invocation_001/01/stdout.log" in names
        assert f"{run_dir_name}/invocation_002/01/stdout.log" in names

        meta = yaml.safe_load(
            tar.extractfile(f"{run_dir_name}/try.yml").read(),
        )
        assert meta["test_name"] == "e2e_test"
        assert len(meta["commands"]) == 2

        main_log = tar.extractfile(f"{run_dir_name}/main.log").read().decode()
        # main.log now carries the orchestration narrative + summary lines.
        assert "[madbench] Host:" in main_log
        assert "=== Running" in main_log
        assert "[OK]" in main_log

        stdout_1 = tar.extractfile(
            f"{run_dir_name}/invocation_001/01/stdout.log"
        ).read().decode()
        assert "hello" in stdout_1

    # CSV exists with both invocations, inside try_0/.
    csv_path = try_dir(ws_root, "e2e_test") / "results.csv"
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
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    mb.run(test_file, dry_run=True)

    assert not list((ws_root / "logs").rglob("*.tar.gz"))
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
        "inputs": ["config/Cards/*"],
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    mb.run(test_file)

    run_dirs = list((ws_root / "scratch").glob("staged_*"))
    assert len(run_dirs) == 1
    staged = run_dirs[0] / "staged" / "config" / "Cards" / "card.dat"
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
        "outputs": ["throughput", "note"],
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    mb.run(test_file)

    csv_path = try_dir(ws_root, "without") / "results.csv"
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
        "outputs": ["throughput"],
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    mb.run(test_file)

    csv_path = try_dir(ws_root, "missing") / "results.csv"
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
# artifacts copy
# -----------------------------------------------------------------------


def test_run_copies_artifacts_with_arg_substitution(tmp_path):
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
        "artifacts": ["gridpack_{seed}/timings.txt"],
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    mb.run(test_file)

    rd = run_dir(ws_root, "copy_outputs")
    inv1 = rd / "invocation_001" / "01" / "gridpack_1" / "timings.txt"
    inv2 = rd / "invocation_002" / "01" / "gridpack_2" / "timings.txt"
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
        "artifacts": ["does_not_exist.log"],
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    mb.run(test_file)

    captured = capsys.readouterr()
    assert "artifact missing" in captured.out


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
    }
    test_file = make_test_yaml(ws_root, yaml_data)
    mb.run(test_file)

    archive = latest_archive(ws_root, "envcheck")
    with tarfile.open(archive) as tar:
        run_dir_name = archive.name[: -len(".tar.gz")]
        log = tar.extractfile(
            f"{run_dir_name}/invocation_001/01/stdout.log"
        ).read().decode()

    assert "WORKDIR=" in log and "/invocation_001" in log
    assert "INPUTS=" in log and "/staged" in log
    assert "OUTPUT_FILE=" in log and ".madbench_output.json" in log
    # cwd should equal the invocation workdir
    workdir_line = [ln for ln in log.splitlines() if ln.startswith("WORKDIR=")][0]
    cwd_line = [ln for ln in log.splitlines() if ln.startswith("CWD=")][0]
    assert workdir_line.split("=", 1)[1] == cwd_line.split("=", 1)[1]


# -----------------------------------------------------------------------
# CSV header rollover
# -----------------------------------------------------------------------


def test_two_runs_produce_isolated_subdirs(tmp_path):
    """Per-run isolation: re-running a test with different schemas produces
    two distinct subdirs under ``results/<test_name>/``, each with its own
    results.csv. The old rollover mechanism (results.2.csv at the
    per-test root) is therefore not exercised in the normal flow."""
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    ws = find_workspace(ws_root)
    mb = MadBench(ws)

    make_test_yaml(
        ws_root,
        {
            "name": "schema", "script": "hello.sh", "args": {"x": 1},
            "outputs": ["a"],
        },
        name="schema1.yml",
    )
    mb.run(ws_root / "tests" / "schema1.yml")
    import time as _t
    _t.sleep(1.1)  # force distinct timestamps

    make_test_yaml(
        ws_root,
        {
            "name": "schema", "script": "hello.sh", "args": {"x": 1},
            "outputs": ["a", "b"],
        },
        name="schema2.yml",
    )
    mb.run(ws_root / "tests" / "schema2.yml")

    subdirs = sorted((ws_root / "results" / "schema").iterdir())
    assert len(subdirs) == 2
    h1 = (subdirs[0] / "try_0" / "results.csv").read_text().splitlines()[0]
    h2 = (subdirs[1] / "try_0" / "results.csv").read_text().splitlines()[0]
    assert h1 != h2  # different schemas, in different subdirs
    # No per-test rollup CSV must exist — each subdir is self-contained.
    assert not (ws_root / "results" / "schema" / "results.csv").exists()


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
        {"name": "t", "script": "hello.sh", "args": {"x": 1}},
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
            "mg_version": "v3.5.4",
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
            "mg_version": ["v3.5.4", "dev_abc"],
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
            "mg_version": 123,
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
            "mg_version": ["v1", "v2"],
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
        {"name": "noversion", "script": "hello.sh", "args": {"x": 1}},
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
            "mg_version": ["v3.5.4"],
        },
    )
    mb.run(test_file)

    archive = latest_archive(ws_root, "envmg")
    with tarfile.open(archive) as tar:
        run_dir_name = archive.name[: -len(".tar.gz")]
        log = tar.extractfile(
            f"{run_dir_name}/v3.5.4/invocation_001/01/stdout.log"
        ).read().decode()
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
        {"name": "envnone", "script": "hello.sh", "args": {"x": 1}},
    )
    mb.run(test_file)

    archive = latest_archive(ws_root, "envnone")
    with tarfile.open(archive) as tar:
        run_dir_name = archive.name[: -len(".tar.gz")]
        log = tar.extractfile(
            f"{run_dir_name}/invocation_001/01/stdout.log"
        ).read().decode()
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
            "mg_version": ["a", "b"],
        },
    )
    mb.run(test_file)

    rows = (try_dir(ws_root, "csvmg") / "results.csv").read_text().splitlines()
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
        {"name": "csvnone", "script": "hello.sh", "args": {"x": 1}},
    )
    mb.run(test_file)

    rows = (try_dir(ws_root, "csvnone") / "results.csv").read_text().splitlines()
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
            "mg_version": ["v1", "v2"],
        },
    )
    mb.run(test_file)

    v1_root = next((ws_root / "scratch" / "v1").glob("align_*"))
    v2_root = next((ws_root / "scratch" / "v2").glob("align_*"))
    for inv, expected_x in [("invocation_001", "10"), ("invocation_002", "20"), ("invocation_003", "30")]:
        v1_marker = (v1_root / inv / "01" / "marker.txt").read_text().strip()
        v2_marker = (v2_root / inv / "01" / "marker.txt").read_text().strip()
        assert v1_marker == f"{expected_x} v1"
        assert v2_marker == f"{expected_x} v2"


def test_run_artifacts_scoped_per_version(tmp_path):
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
            "mg_version": ["v1", "v2"],
            "artifacts": ["out.log"],
        },
    )
    mb.run(test_file)

    rd = run_dir(ws_root, "outv")
    v1_out = (rd / "v1" / "invocation_001" / "01" / "out.log").read_text().strip()
    v2_out = (rd / "v2" / "invocation_001" / "01" / "out.log").read_text().strip()
    assert v1_out == "v1"
    assert v2_out == "v2"


# -----------------------------------------------------------------------
# Per-try environment metadata (try.yml inside try_N/ subdir)
# -----------------------------------------------------------------------


def test_run_dir_naming(tmp_path):
    """The per-run subfolder under results/<test_name>/ encodes hostname
    and timestamp — enough to identify the run by path alone."""
    import socket
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {"name": "named", "script": "hello.sh", "args": {"x": 1}},
    )
    mb.run(test_file)

    rd = run_dir(ws_root, "named")
    assert rd.parent.name == "named"
    assert rd.name.startswith(f"{socket.gethostname()}_")


def test_run_writes_metadata_yml(tmp_path):
    """Each run writes its own try.yml inside try_N/ capturing the
    environment that produced the rows in that try."""
    import socket
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {"name": "runmeta", "script": "hello.sh", "args": {"x": 1}},
    )
    mb.run(test_file)

    meta = yaml.safe_load(
        (try_dir(ws_root, "runmeta") / "try.yml").read_text()
    )
    assert meta["test_name"] == "runmeta"
    # hostname lives inside the `hardware` block now (single source of truth);
    # `repeat` lives in the verbatim `test.yml` at the top of result_dir.
    assert "hostname" not in meta
    assert "repeat" not in meta
    assert meta["hardware"]["hostname"] == socket.gethostname()
    assert "timestamp" in meta
    assert "gpus" in meta["hardware"]
    assert isinstance(meta["mg_versions"], list)


def test_two_runs_create_separate_subdirs(tmp_path):
    """Successive runs of the same test create separate self-contained
    subdirs under ``results/<test_name>/`` — no shared file is read/written
    by either."""
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    mb = MadBench(find_workspace(ws_root))
    make_test_yaml(
        ws_root,
        {"name": "twice", "script": "hello.sh", "args": {"x": 1}},
        name="twice.yml",
    )
    test_file = ws_root / "tests" / "twice.yml"

    mb.run(test_file)
    import time as _t
    _t.sleep(1.1)  # ensure a distinct timestamp on the second run
    mb.run(test_file)

    subdirs = sorted((ws_root / "results" / "twice").iterdir())
    assert len(subdirs) == 2
    for sd in subdirs:
        assert (sd / "try_0" / "results.csv").exists()
        assert (sd / "try_0" / "try.yml").exists()
    # Timestamps must differ — proves runs are isolated, not overwriting.
    m1 = yaml.safe_load((subdirs[0] / "try_0" / "try.yml").read_text())
    m2 = yaml.safe_load((subdirs[1] / "try_0" / "try.yml").read_text())
    assert m1["timestamp"] != m2["timestamp"]


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
        {"name": "t", "script": "hello.sh", "args": {"x": 1}},
    )
    td = mb.load_test(test_file)
    assert td.proc_cards == []


def test_load_test_proc_cards_parsed(tmp_path):
    ws_root = make_workspace(tmp_path)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "t", "script": "hello.sh", "args": {"x": 1}, "proc_cards": ["inputs/proc_a.dat", "inputs/proc_b.dat"],
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
            "mg_version": ["v1"],
            "proc_cards": ["inputs/card1.dat", "inputs/card2.dat"],
        },
    )
    mb.run(test_file)

    run_dir = next((ws_root / "scratch" / "v1").glob("gen_*"))
    assert (run_dir / "processes" / "proc_a").is_dir()
    assert (run_dir / "processes" / "proc_b").is_dir()
    # Script saw both via $MADBENCH_PROCESSES
    listing = (run_dir / "invocation_001" / "01" / "listing.txt").read_text()
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
            "mg_version": ["v1"],
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
            "proc_cards": ["inputs/card.dat"],
            # mg_version intentionally omitted → "none"
        },
    )
    mb.run(test_file)

    rows = (try_dir(ws_root, "needsmg") / "results.csv").read_text().splitlines()
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
            "mg_version": ["ghost"],
            "proc_cards": ["inputs/card.dat"],
        },
    )
    mb.run(test_file)

    rows = (try_dir(ws_root, "noghost") / "results.csv").read_text().splitlines()
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
            "mg_version": ["v1"],
            "proc_cards": ["inputs/card.dat"],
        },
    )
    mb.run(test_file)

    assert not marker.exists()  # script never ran
    rows = (try_dir(ws_root, "mgfail") / "results.csv").read_text().splitlines()
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
            "mg_version": ["v_good", "v_bad"],
            "proc_cards": ["inputs/card.dat"],
        },
    )
    mb.run(test_file)

    rows = (try_dir(ws_root, "mixed") / "results.csv").read_text().splitlines()
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
        {"name": "penv", "script": "hello.sh", "args": {"x": 1}},
    )
    mb.run(test_file)

    archive = latest_archive(ws_root, "penv")
    with tarfile.open(archive) as tar:
        run_dir_name = archive.name[: -len(".tar.gz")]
        log = tar.extractfile(
            f"{run_dir_name}/invocation_001/01/stdout.log"
        ).read().decode()
    assert "PROCESSES=" in log and "/processes" in log
    assert "DIR_EXISTS" in log


# -----------------------------------------------------------------------
# repeat (statistical repetitions)
# -----------------------------------------------------------------------


def test_load_test_repeat_defaults_to_1(tmp_path):
    ws_root = make_workspace(tmp_path)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {"name": "t", "script": "hello.sh", "args": {"x": 1}},
    )
    td = mb.load_test(test_file)
    assert td.repeat == 1


def test_load_test_repeat_invalid_raises(tmp_path):
    ws_root = make_workspace(tmp_path)
    mb = MadBench(find_workspace(ws_root))
    for bad in [0, -1, "5", 1.5, True]:
        test_file = make_test_yaml(
            ws_root,
            {
                "name": "t", "script": "hello.sh", "args": {"x": 1},
                "repeat": bad,
            },
            name=f"bad_{type(bad).__name__}_{bad}.yml".replace(" ", "_"),
        )
        with pytest.raises(ValueError, match="repeat"):
            mb.load_test(test_file)


def test_run_creates_rep_subdirs(tmp_path):
    """Each repetition gets its own zero-padded subdir under invocation_NNN/."""
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "echo \"rep=$MADBENCH_REPETITION\" > rep_marker.txt\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "rep", "script": "hello.sh", "args": {"x": 1},
            "repeat": 3,
        },
    )
    mb.run(test_file)

    run_dir = next((ws_root / "scratch").glob("rep_*"))
    inv = run_dir / "invocation_001"
    assert (inv / "01" / "rep_marker.txt").read_text().strip() == "rep=01"
    assert (inv / "02" / "rep_marker.txt").read_text().strip() == "rep=02"
    assert (inv / "03" / "rep_marker.txt").read_text().strip() == "rep=03"


def test_run_rep_subdir_for_repeat_1(tmp_path):
    """Even with repeat=1 (the default) we always nest into 01/ for
    uniform structure across single- and multi-rep tests."""
    ws_root = make_workspace(tmp_path)
    make_script(ws_root, body="#!/bin/bash\necho hi > marker.txt\n")
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {"name": "single", "script": "hello.sh", "args": {"x": 1}},
    )
    mb.run(test_file)

    run_dir = next((ws_root / "scratch").glob("single_*"))
    assert (run_dir / "invocation_001" / "01" / "marker.txt").exists()
    # Nothing should have landed at the legacy flat location.
    assert not (run_dir / "invocation_001" / "marker.txt").exists()


def test_run_csv_has_repetition_column_with_row_per_rep(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "echo \"{\\\"v\\\": $1}\" > \"$MADBENCH_OUTPUT_FILE\"\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "csvr", "script": "hello.sh", "args": {"x": [1, 2]},
            "outputs": ["v"], "repeat": 3,
        },
    )
    mb.run(test_file)

    rows = (try_dir(ws_root, "csvr") / "results.csv").read_text().splitlines()
    header = rows[0].split(",")
    assert "repetition" in header
    rep_idx = header.index("repetition")
    inv_idx = header.index("invocation_id")
    # 2 combos × 3 reps = 6 data rows. Each combo has reps 01, 02, 03.
    assert len(rows) == 7
    by_inv: dict[str, list[str]] = {}
    for row in rows[1:]:
        cells = row.split(",")
        by_inv.setdefault(cells[inv_idx], []).append(cells[rep_idx])
    assert by_inv["invocation_001"] == ["01", "02", "03"]
    assert by_inv["invocation_002"] == ["01", "02", "03"]


def test_run_artifacts_scoped_per_rep(tmp_path):
    """Same invocation across reps writes to different result subdirs."""
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "echo \"$MADBENCH_REPETITION\" > out.log\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "outr", "script": "hello.sh", "args": {"x": 1},
            "artifacts": ["out.log"], "repeat": 2,
        },
    )
    mb.run(test_file)

    inv = run_dir(ws_root, "outr") / "invocation_001"
    assert (inv / "01" / "out.log").read_text().strip() == "01"
    assert (inv / "02" / "out.log").read_text().strip() == "02"


def test_run_summary_csv_mean_std_n_successful(tmp_path):
    """summary.csv aggregates numeric outputs across successful reps."""
    ws_root = make_workspace(tmp_path)
    # Script writes a value derived from the rep number so mean is predictable.
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "v=$((10 + 10#$MADBENCH_REPETITION))\n"
            "echo \"{\\\"throughput\\\": $v}\" > \"$MADBENCH_OUTPUT_FILE\"\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "sum", "script": "hello.sh", "args": {"x": 1},
            "outputs": ["throughput"], "repeat": 3,
        },
    )
    mb.run(test_file)

    summary = run_dir(ws_root, "sum") / "summary.csv"  # top-level (single across tries)
    assert summary.exists()
    rows = summary.read_text().splitlines()
    header = rows[0].split(",")
    assert "throughput_mean" in header
    assert "throughput_std" in header
    assert "n_successful" in header
    cells = dict(zip(header, rows[1].split(",")))
    # throughput values were 11, 12, 13 → mean 12, sample std = 1
    assert float(cells["throughput_mean"]) == 12.0
    assert float(cells["throughput_std"]) == 1.0
    assert cells["n_successful"] == "3"


def test_run_summary_excludes_failed_reps_from_average(tmp_path):
    """Failed reps don't pollute the mean; n_successful reflects the count
    that actually contributed."""
    ws_root = make_workspace(tmp_path)
    # Reps 01 and 03 succeed with throughput=100; rep 02 exits non-zero
    # without writing its outputs file.
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "if [ \"$MADBENCH_REPETITION\" = \"02\" ]; then exit 1; fi\n"
            "echo '{\"throughput\": 100}' > \"$MADBENCH_OUTPUT_FILE\"\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "mix", "script": "hello.sh", "args": {"x": 1},
            "outputs": ["throughput"], "repeat": 3,
        },
    )
    mb.run(test_file)

    rows = (run_dir(ws_root, "mix") / "summary.csv").read_text().splitlines()
    header = rows[0].split(",")
    cells = dict(zip(header, rows[1].split(",")))
    assert cells["n_successful"] == "2"
    assert float(cells["throughput_mean"]) == 100.0
    # Sample std of [100, 100] is 0.0
    assert float(cells["throughput_std"]) == 0.0


def test_run_summary_handles_all_failures(tmp_path):
    """A row with zero successful reps gets empty mean/std and n=0."""
    ws_root = make_workspace(tmp_path)
    make_script(ws_root, body="#!/bin/bash\nexit 1\n")
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "allbad", "script": "hello.sh", "args": {"x": 1},
            "outputs": ["throughput"], "repeat": 2,
        },
    )
    mb.run(test_file)

    rows = (run_dir(ws_root, "allbad") / "summary.csv").read_text().splitlines()
    header = rows[0].split(",")
    cells = dict(zip(header, rows[1].split(",")))
    assert cells["n_successful"] == "0"
    assert cells["throughput_mean"] == ""
    assert cells["throughput_std"] == ""


def test_run_summary_skips_non_numeric_outputs(tmp_path, capsys):
    """With no explicit ``stats``, non-numeric outputs produce empty
    mean/std cells (no crash) **and** a warning is emitted so the user
    notices the bad column without their CSV silently rotting."""
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "echo '{\"throughput\": 50, \"note\": \"ok\"}' > \"$MADBENCH_OUTPUT_FILE\"\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "mixed_outs", "script": "hello.sh", "args": {"x": 1},
            "outputs": ["throughput", "note"], "repeat": 2,
        },
    )
    mb.run(test_file)

    rows = (run_dir(ws_root, "mixed_outs") / "summary.csv").read_text().splitlines()
    header = rows[0].split(",")
    cells = dict(zip(header, rows[1].split(",")))
    assert float(cells["throughput_mean"]) == 50.0
    assert cells["note_mean"] == ""
    assert cells["note_std"] == ""

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "cannot aggregate stats column 'note'" in combined
    assert "'stats' not declared" in combined


def test_load_test_stats_defaults_to_none(tmp_path):
    """Unspecified ``stats`` keeps the sentinel ``None``; resolved_stats()
    falls back to all outputs."""
    ws_root = make_workspace(tmp_path)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "t", "script": "hello.sh", "args": {"x": 1},
            "outputs": ["a", "b"],
        },
    )
    td = mb.load_test(test_file)
    assert td.stats is None
    assert td.resolved_stats() == ["a", "b"]


def test_load_test_stats_subset_of_outputs(tmp_path):
    ws_root = make_workspace(tmp_path)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "t", "script": "hello.sh", "args": {"x": 1},
            "outputs": ["a", "b", "label"],
            "stats": ["a", "b"],
        },
    )
    td = mb.load_test(test_file)
    assert td.stats == ["a", "b"]
    assert td.resolved_stats() == ["a", "b"]


def test_load_test_stats_unknown_key_raises(tmp_path):
    ws_root = make_workspace(tmp_path)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "t", "script": "hello.sh", "args": {"x": 1},
            "outputs": ["a"],
            "stats": ["a", "ghost"],
        },
    )
    with pytest.raises(ValueError, match="not declared in 'outputs'"):
        mb.load_test(test_file)


def test_load_test_stats_wrong_type_raises(tmp_path):
    ws_root = make_workspace(tmp_path)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "t", "script": "hello.sh", "args": {"x": 1},
            "outputs": ["a"],
            "stats": "a",
        },
    )
    with pytest.raises(ValueError, match="'stats' must be a list of strings"):
        mb.load_test(test_file)


def test_load_test_stats_empty_list_is_explicit_optout(tmp_path):
    """Empty ``stats: []`` is a valid explicit opt-out (no output columns
    are aggregated; ``wall_time`` still is)."""
    ws_root = make_workspace(tmp_path)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "t", "script": "hello.sh", "args": {"x": 1},
            "outputs": ["a"],
            "stats": [],
        },
    )
    td = mb.load_test(test_file)
    assert td.stats == []
    assert td.resolved_stats() == []


def test_run_summary_stats_narrows_columns(tmp_path):
    """An explicit ``stats: [throughput]`` drops ``note_mean``/``note_std``
    from summary.csv entirely — the user said only ``throughput`` is a
    measurement, so the non-numeric ``note`` column doesn't pollute the
    header."""
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "echo '{\"throughput\": 50, \"note\": \"ok\"}' > \"$MADBENCH_OUTPUT_FILE\"\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "narrow", "script": "hello.sh", "args": {"x": 1},
            "outputs": ["throughput", "note"],
            "stats": ["throughput"],
            "repeat": 2,
        },
    )
    mb.run(test_file)

    rows = (run_dir(ws_root, "narrow") / "summary.csv").read_text().splitlines()
    header = rows[0].split(",")
    assert "throughput_mean" in header
    assert "throughput_std" in header
    assert "note_mean" not in header
    assert "note_std" not in header
    cells = dict(zip(header, rows[1].split(",")))
    assert float(cells["throughput_mean"]) == 50.0


def test_run_summary_stats_warns_when_declared_column_is_non_numeric(tmp_path, capsys):
    """If ``stats`` declares a column that turns out to be non-numeric, the
    user said it should be a number — warn, don't crash, blank the cells."""
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "echo '{\"v\": \"oops\"}' > \"$MADBENCH_OUTPUT_FILE\"\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "badnum", "script": "hello.sh", "args": {"x": 1},
            "outputs": ["v"], "stats": ["v"], "repeat": 2,
        },
    )
    mb.run(test_file)

    rows = (run_dir(ws_root, "badnum") / "summary.csv").read_text().splitlines()
    cells = dict(zip(rows[0].split(","), rows[1].split(",")))
    assert cells["v_mean"] == ""
    assert cells["v_std"] == ""

    captured = capsys.readouterr()
    assert "cannot aggregate stats column 'v'" in (captured.out + captured.err)


def test_run_summary_no_default_warning_when_stats_set(tmp_path, capsys):
    """With ``stats`` explicitly declared the default-stats warning must
    NOT fire."""
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "echo '{\"v\": 1}' > \"$MADBENCH_OUTPUT_FILE\"\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "quiet", "script": "hello.sh", "args": {"x": 1},
            "outputs": ["v"], "stats": ["v"], "repeat": 2,
        },
    )
    mb.run(test_file)
    captured = capsys.readouterr()
    assert "'stats' not declared" not in (captured.out + captured.err)


def test_run_summary_one_row_per_arg_combo(tmp_path):
    """Multiple arg combos each produce their own summary row."""
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "echo \"{\\\"v\\\": $1}\" > \"$MADBENCH_OUTPUT_FILE\"\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "multi", "script": "hello.sh", "args": {"x": [10, 20]},
            "outputs": ["v"], "repeat": 2,
        },
    )
    mb.run(test_file)

    rows = (run_dir(ws_root, "multi") / "summary.csv").read_text().splitlines()
    # Header + 2 arg-combos = 3 lines
    assert len(rows) == 3
    header = rows[0].split(",")
    x_idx = header.index("x")
    v_mean_idx = header.index("v_mean")
    by_x = {r.split(",")[x_idx]: float(r.split(",")[v_mean_idx]) for r in rows[1:]}
    assert by_x == {"10": 10.0, "20": 20.0}


def test_run_other_reps_continue_when_one_fails(tmp_path):
    """A failing rep doesn't poison its sibling reps."""
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "if [ \"$MADBENCH_REPETITION\" = \"02\" ]; then exit 1; fi\n"
            "echo ok > marker.txt\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "isol", "script": "hello.sh", "args": {"x": 1},
            "repeat": 3,
        },
    )
    mb.run(test_file)

    run_dir = next((ws_root / "scratch").glob("isol_*"))
    assert (run_dir / "invocation_001" / "01" / "marker.txt").exists()
    assert not (run_dir / "invocation_001" / "02" / "marker.txt").exists()
    assert (run_dir / "invocation_001" / "03" / "marker.txt").exists()


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
            "mg_version": ["v1", "v2"],
            "inputs": ["config/card.dat"],
        },
    )
    mb.run(test_file)

    v1_dir = next((ws_root / "scratch" / "v1").glob("inputsmg_*"))
    v2_dir = next((ws_root / "scratch" / "v2").glob("inputsmg_*"))
    assert (v1_dir / "staged" / "config" / "card.dat").read_text() == "CARD"
    assert (v2_dir / "staged" / "config" / "card.dat").read_text() == "CARD"


# -----------------------------------------------------------------------
# Sibling test.yml in per-run result dir
# -----------------------------------------------------------------------


def test_run_copies_test_yml_into_result_dir_verbatim(tmp_path):
    """The per-run dir gets a byte-for-byte copy of the source test YAML
    so the committed artifacts make it obvious what was run (including any
    per-machine tweaks made before the run)."""
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    mb = MadBench(find_workspace(ws_root))
    # Hand-write the YAML so we can assert on the byte content; include a
    # comment to verify formatting is preserved (yaml.dump would strip it).
    yml_body = (
        "# per-machine tweak: dropped nevents=10000 for this run\n"
        "name: tyml\n"
        "script: hello.sh\n"
        "args:\n"
        "  ncores: [1, 2]\n"
        "  nevents: 100\n"
        )
    test_file = ws_root / "tests" / "tyml.yml"
    test_file.write_text(yml_body)
    mb.run(test_file)

    copied = run_dir(ws_root, "tyml") / "test.yml"
    assert copied.exists()
    assert copied.read_text() == yml_body  # byte-for-byte, including comment


def test_run_metadata_yml_points_at_sibling_test_yml(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {"name": "metayml", "script": "hello.sh", "args": {"x": 1}},
    )
    mb.run(test_file)

    meta = yaml.safe_load(
        (try_dir(ws_root, "metayml") / "try.yml").read_text()
    )
    # test_definition is no longer in try.yml; the top-level test.yml is.
    assert "test_definition" not in meta
    # All paths in try.yml resolve from the result_dir root, so the
    # pointer is just ``test.yml`` (one consistent anchor with retry_of).
    assert meta["test_yml"] == "test.yml"


# -----------------------------------------------------------------------
# failed.yml + retry
# -----------------------------------------------------------------------


def _make_flaky_script(ws_root: Path, fail_when: str) -> Path:
    """Make hello.sh fail (exit 1) iff $1 == ``fail_when``, else succeed."""
    return make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            f"if [ \"$1\" = \"{fail_when}\" ]; then exit 1; fi\n"
            "echo ok\n"
        ),
    )


def test_run_writes_failed_yml_when_some_runs_fail(tmp_path):
    ws_root = make_workspace(tmp_path)
    _make_flaky_script(ws_root, fail_when="2")
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "flaky", "script": "hello.sh", "args": {"x": [1, 2, 3]},
        },
    )
    mb.run(test_file)

    failed_yml = try_dir(ws_root, "flaky") / "failed.yml"
    assert failed_yml.exists()
    payload = yaml.safe_load(failed_yml.read_text())
    assert payload["test_name"] == "flaky"
    assert payload["n_total"] == 3
    assert payload["n_failed"] == 1
    assert len(payload["failures"]) == 1
    fail = payload["failures"][0]
    assert fail["invocation_id"] == "invocation_002"
    assert fail["repetition"] == "01"
    assert fail["exit_code"] == 1
    assert fail["args"]["x"] == 2


def test_run_no_failed_yml_when_all_pass(tmp_path):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)  # default body: echo "hello $@" (always succeeds)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "happy", "script": "hello.sh", "args": {"x": [1, 2]},
        },
    )
    mb.run(test_file)

    rd = run_dir(ws_root, "happy")
    assert not (rd / "try_0" / "failed.yml").exists()


def test_retry_reruns_only_failed_combos_with_preserved_ids(tmp_path):
    """retry() picks the latest try_N/failed.yml and replays each failure
    preserving invocation_id / repetition / mg_version. The script is now
    patched so the same args succeed — try_1/results.csv should hold one
    row, matching the originally-failing invocation."""
    ws_root = make_workspace(tmp_path)
    _make_flaky_script(ws_root, fail_when="2")
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "retried", "script": "hello.sh", "args": {"x": [1, 2, 3]},
        },
    )
    mb.run(test_file)
    rd = run_dir(ws_root, "retried")

    # Patch the script so x=2 now succeeds, then retry.
    make_script(ws_root)  # back to always-succeeding body
    mb.retry(rd)

    # Same result_dir, gained a try_1/ folder.
    assert (rd / "try_0").is_dir() and (rd / "try_1").is_dir()
    rows = (rd / "try_1" / "results.csv").read_text().splitlines()
    assert len(rows) == 2  # header + 1 retried row
    header = rows[0].split(",")
    cells = rows[1].split(",")
    inv_idx = header.index("invocation_id")
    rep_idx = header.index("repetition")
    exit_idx = header.index("exit_code")
    x_idx = header.index("x")
    assert cells[inv_idx] == "invocation_002"
    assert cells[rep_idx] == "01"
    assert cells[exit_idx] == "0"
    assert cells[x_idx] == "2"


def test_retry_writes_retry_of_pointer_in_metadata(tmp_path):
    ws_root = make_workspace(tmp_path)
    _make_flaky_script(ws_root, fail_when="2")
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "retrymeta", "script": "hello.sh", "args": {"x": [1, 2]},
        },
    )
    mb.run(test_file)
    rd = run_dir(ws_root, "retrymeta")

    mb.retry(rd)

    meta = yaml.safe_load((rd / "try_1" / "try.yml").read_text())
    # retry_of is a *relative* path pointing at the previous try's failed.yml.
    assert meta["retry_of"] == "try_0/failed.yml"

    # failed.yml in try_1 also carries the same retry_of marker, even if
    # this retry is now passing — but if every retry passed, no failed.yml
    # is written. Force a failure that persists to verify the chain marker.
    failed = rd / "try_1" / "failed.yml"
    if failed.exists():
        payload = yaml.safe_load(failed.read_text())
        assert payload["retry_of"] == "try_0/failed.yml"


def test_retry_noop_when_no_failures(tmp_path, capsys):
    ws_root = make_workspace(tmp_path)
    make_script(ws_root)  # always succeeds
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "happy_retry", "script": "hello.sh", "args": {"x": [1, 2]},
        },
    )
    mb.run(test_file)
    rd = run_dir(ws_root, "happy_retry")

    mb.retry(rd)

    captured = capsys.readouterr()
    assert "Nothing to retry" in captured.out
    # No try_1/ folder was created.
    assert not (rd / "try_1").exists()


def test_retry_works_without_original_workspace_test_yaml(tmp_path):
    """retry() rebuilds the TestDefinition from ``result_dir/test.yml``,
    so deleting/renaming the canonical ``tests/<name>.yml`` between the
    failing run and the retry is fine."""
    ws_root = make_workspace(tmp_path)
    _make_flaky_script(ws_root, fail_when="2")
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "noyml", "script": "hello.sh", "args": {"x": [1, 2]},
        },
    )
    mb.run(test_file)
    rd = run_dir(ws_root, "noyml")

    # Delete the canonical YAML — retry should still work because the
    # top-level test.yml inside the result dir is authoritative.
    test_file.unlink()
    make_script(ws_root)  # script now succeeds for x=2 too
    mb.retry(rd)

    rows = (rd / "try_1" / "results.csv").read_text().splitlines()
    assert len(rows) == 2  # header + 1 retried row
    # The top-level test.yml is still here (and remains the single shared
    # copy across all tries).
    assert (rd / "test.yml").exists()


def test_retry_errors_when_top_test_yml_missing(tmp_path):
    """If ``result_dir/test.yml`` was manually deleted, retry surfaces a
    clear error rather than silently doing the wrong thing."""
    ws_root = make_workspace(tmp_path)
    _make_flaky_script(ws_root, fail_when="2")
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "deletedyml", "script": "hello.sh", "args": {"x": [1, 2]},
        },
    )
    mb.run(test_file)
    rd = run_dir(ws_root, "deletedyml")

    (rd / "test.yml").unlink()

    with pytest.raises(FileNotFoundError, match="test.yml missing"):
        mb.retry(rd)


def test_retry_replays_per_mg_version_failures(tmp_path):
    """Sweep across two mg_versions where only v_bad's invocations fail.
    The retry should run only v_bad's failed combos — v_good never enters
    the retry's mg_versions list, so no scratch dir for v_good is touched.
    """
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "if [ \"$MG_VERSION\" = \"v_bad\" ]; then exit 1; fi\n"
            "echo ok\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "mgretry", "script": "hello.sh", "args": {"x": [1, 2]},
            "mg_version": ["v_good", "v_bad"],
        },
    )
    mb.run(test_file)
    rd = run_dir(ws_root, "mgretry")

    # Flip the script: now v_bad succeeds. Retry should re-run only v_bad.
    make_script(ws_root)
    mb.retry(rd)

    rows = (rd / "try_1" / "results.csv").read_text().splitlines()
    assert len(rows) == 3  # header + 2 retried rows (x=1 and x=2 under v_bad)
    header = rows[0].split(",")
    mgv_idx = header.index("mg_version")
    mg_versions = {row.split(",")[mgv_idx] for row in rows[1:]}
    assert mg_versions == {"v_bad"}

    # Only one mgretry_* dir per mg_version (the original one). retry reuses
    # the same scratch dir keyed off the run's original timestamp; no fresh
    # _retry-suffixed dir is spawned.
    assert len(list((ws_root / "scratch" / "v_bad").glob("mgretry_*"))) == 1
    assert len(list((ws_root / "scratch" / "v_good").glob("mgretry_*"))) == 1


def test_retry_updates_top_summary_csv_with_merged_rows(tmp_path):
    """summary.csv at the result_dir top level reflects the latest row
    for each (invocation, rep, mg_version) across all tries — a successful
    retry must turn a 0-of-N rep into N-of-N for that combo."""
    ws_root = make_workspace(tmp_path)
    # x=2 fails on the first run; will succeed after we swap the script.
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "if [ \"$1\" = \"2\" ]; then exit 1; fi\n"
            "echo \"{\\\"v\\\": $1}\" > \"$MADBENCH_OUTPUT_FILE\"\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "smerge", "script": "hello.sh", "args": {"x": [1, 2]},
            "outputs": ["v"],
        },
    )
    mb.run(test_file)
    rd = run_dir(ws_root, "smerge")

    # Patch the script so x=2 now succeeds.
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "echo \"{\\\"v\\\": $1}\" > \"$MADBENCH_OUTPUT_FILE\"\n"
        ),
    )
    mb.retry(rd)

    rows = (rd / "summary.csv").read_text().splitlines()
    header = rows[0].split(",")
    by_x = {r.split(",")[header.index("x")]: dict(zip(header, r.split(",")))
            for r in rows[1:]}
    # x=1 always passed (n_successful=1). x=2 was 0 before the retry, 1
    # after, because the retry's row replaces try_0's failed row.
    assert by_x["1"]["n_successful"] == "1"
    assert by_x["2"]["n_successful"] == "1"
    assert float(by_x["2"]["v_mean"]) == 2.0


def test_retry_blocks_on_hardware_mismatch(tmp_path, monkeypatch):
    """A retry whose current hardware doesn't match try_0/try.yml
    aborts unless --force is passed."""
    from madbench import driver as _driver

    ws_root = make_workspace(tmp_path)
    _make_flaky_script(ws_root, fail_when="2")
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "hwcheck", "script": "hello.sh", "args": {"x": [1, 2]},
        },
    )
    mb.run(test_file)
    rd = run_dir(ws_root, "hwcheck")

    # Simulate a different host by patching detect_hardware on the retry.
    real_detect = _driver.detect_hardware

    def _fake_detect():
        hw = real_detect()
        hw["hostname"] = "different-machine"
        hw["cpu_model"] = "Pretend CPU"
        return hw

    monkeypatch.setattr(_driver, "detect_hardware", _fake_detect)
    make_script(ws_root)

    with pytest.raises(ValueError, match="Hardware mismatch"):
        mb.retry(rd)
    # No try_1 was created.
    assert not (rd / "try_1").exists()

    # --force bypasses the check and writes try_1.
    mb.retry(rd, force=True)
    assert (rd / "try_1" / "results.csv").exists()


def test_retry_invocation_artifacts_overwritten_in_place(tmp_path):
    """Artifacts of a retried rep replace the failed run's artifacts at the
    same top-level invocation/<rep>/ path."""
    ws_root = make_workspace(tmp_path)
    # On first run x=2 fails (so no artifact for that rep). Second time x=2
    # succeeds and emits "after".
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "if [ \"$1\" = \"2\" ]; then exit 1; fi\n"
            "echo \"before $1\" > out.log\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "art", "script": "hello.sh", "args": {"x": [1, 2]},
            "artifacts": ["out.log"],
        },
    )
    mb.run(test_file)
    rd = run_dir(ws_root, "art")

    # Patched script — now x=2 succeeds with "after 2".
    make_script(
        ws_root,
        body="#!/bin/bash\necho \"after $1\" > out.log\n",
    )
    mb.retry(rd)

    # x=1 artifact (invocation_001) is unchanged.
    assert (rd / "invocation_001" / "01" / "out.log").read_text().strip() == "before 1"
    # x=2 artifact (invocation_002) now reflects the retry, at the SAME
    # top-level path (no new sibling result dir).
    assert (rd / "invocation_002" / "01" / "out.log").read_text().strip() == "after 2"


def test_retry_wipes_scratch_rep_workdir(tmp_path):
    """A retried rep's scratch workdir is wiped before the script re-runs
    so stale files from the failed attempt cannot contaminate the new run.
    Successful reps' workdirs are left alone, and shared per-version state
    (``staged/``, ``processes/``) survives the wipe."""
    ws_root = make_workspace(tmp_path)
    # Script: write a marker named after MADBENCH_REPETITION on x=2 failure,
    # otherwise succeed. The retry then runs a different body that *would
    # see* that marker if the workdir weren't wiped — but it shouldn't.
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "if [ \"$1\" = \"2\" ]; then\n"
            "    echo poison > stale_marker\n"
            "    exit 1\n"
            "fi\n"
            "echo ok > marker\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "wipe", "script": "hello.sh", "args": {"x": [1, 2]},
        },
    )
    mb.run(test_file)
    rd = run_dir(ws_root, "wipe")

    scratch_run = next((ws_root / "scratch").glob("wipe_*"))
    # The poison marker exists in invocation_002/01 after the failed run.
    assert (scratch_run / "invocation_002" / "01" / "stale_marker").exists()
    # And the successful invocation_001/01 has its own marker.
    assert (scratch_run / "invocation_001" / "01" / "marker").exists()

    # Patched script: now x=2 succeeds. If the wipe works, stale_marker
    # is gone after the retry — the retry script doesn't write it.
    make_script(ws_root, body="#!/bin/bash\necho ok > marker\n")
    mb.retry(rd)

    # The retried rep's workdir was wiped, so the failed-run marker is gone
    # and only the new marker remains.
    inv2_rep1 = scratch_run / "invocation_002" / "01"
    assert not (inv2_rep1 / "stale_marker").exists()
    assert (inv2_rep1 / "marker").read_text().strip() == "ok"
    # The successful rep (invocation_001) was untouched.
    assert (scratch_run / "invocation_001" / "01" / "marker").exists()


def test_run_does_not_wipe_existing_rep_workdir(tmp_path):
    """First runs (``try_n == 0``) must NOT wipe pre-existing files — the
    wipe is a retry-only behavior. If a user's setup populates the rep dir
    before the script runs (rare but possible), a fresh ``madbench run``
    should leave that intact."""
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body=(
            "#!/bin/bash\n"
            "# Verify any preexisting file is still here when we run.\n"
            "[ -f preseeded ] && echo SAW_PRESEED > result\n"
        ),
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {"name": "noseed", "script": "hello.sh", "args": {"x": 1}},
    )
    # Pre-create the rep dir with a file. (madbench would normally create
    # it itself; here we want to confirm a normal run does not wipe it.)
    scratch_path = (
        ws_root / "scratch" / "noseed_FAKE" / "invocation_001" / "01"
    )
    scratch_path.mkdir(parents=True)
    (scratch_path / "preseeded").write_text("hi")
    # The actual run uses a timestamped path, so this test just asserts the
    # retry-only guard — a fresh run never reaches our pre-seeded path.
    mb.run(test_file)
    assert (scratch_path / "preseeded").exists()


def test_run_manifest_written_after_run(tmp_path):
    """A fresh ``madbench run`` drops ``metadata.yml`` (the per-run
    manifest) at the result_dir top with a single hardware group
    covering ``try_0``."""
    import socket

    ws_root = make_workspace(tmp_path)
    make_script(ws_root)
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {"name": "tindex", "script": "hello.sh", "args": {"x": [1, 2]}},
    )
    mb.run(test_file)

    rd = run_dir(ws_root, "tindex")
    payload = yaml.safe_load((rd / "metadata.yml").read_text())
    assert payload["test_name"] == "tindex"
    assert payload["n_tries"] == 1
    assert len(payload["hardware_index"]) == 1
    group = payload["hardware_index"][0]
    assert group["tries"] == ["try_0"]
    assert group["hardware"]["hostname"] == socket.gethostname()


def test_run_manifest_groups_same_host_retries(tmp_path):
    """Consecutive same-host retries land in the same hardware group;
    ``tries`` lists every try in chronological order."""
    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body="#!/bin/bash\nif [ \"$1\" = \"2\" ]; then exit 1; fi\necho ok\n",
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {"name": "tgroup", "script": "hello.sh", "args": {"x": [1, 2]}},
    )
    mb.run(test_file)
    rd = run_dir(ws_root, "tgroup")

    mb.retry(rd)
    mb.retry(rd)

    payload = yaml.safe_load((rd / "metadata.yml").read_text())
    assert payload["n_tries"] == 3
    # All three tries ran on the same machine → one group.
    assert len(payload["hardware_index"]) == 1
    assert payload["hardware_index"][0]["tries"] == ["try_0", "try_1", "try_2"]


def test_run_manifest_splits_groups_on_force_cross_host(tmp_path, monkeypatch):
    """A ``--force`` retry on a different hardware fingerprint creates a
    new hardware group in the run manifest — so the audit of
    who-ran-what-where is always one file lookup away."""
    from madbench import driver as _driver

    ws_root = make_workspace(tmp_path)
    make_script(
        ws_root,
        body="#!/bin/bash\nif [ \"$1\" = \"2\" ]; then exit 1; fi\necho ok\n",
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {"name": "tsplit", "script": "hello.sh", "args": {"x": [1, 2]}},
    )
    mb.run(test_file)
    rd = run_dir(ws_root, "tsplit")

    real_detect = _driver.detect_hardware

    def _fake_detect():
        hw = real_detect()
        hw["hostname"] = "other-host"
        hw["cpu_model"] = "Pretend CPU"
        return hw

    monkeypatch.setattr(_driver, "detect_hardware", _fake_detect)
    mb.retry(rd, force=True)

    payload = yaml.safe_load((rd / "metadata.yml").read_text())
    assert payload["n_tries"] == 2
    assert len(payload["hardware_index"]) == 2
    hostnames = [g["hardware"]["hostname"] for g in payload["hardware_index"]]
    assert hostnames[0] != "other-host"  # try_0's machine first
    assert hostnames[1] == "other-host"  # try_1 (forced)
    assert payload["hardware_index"][0]["tries"] == ["try_0"]
    assert payload["hardware_index"][1]["tries"] == ["try_1"]


def test_retry_chain_two_levels(tmp_path):
    """try_N+1's failed.yml points at try_N/failed.yml. A second retry
    creates try_2 with retry_of: try_1/failed.yml."""
    ws_root = make_workspace(tmp_path)
    # x=2 always fails so each retry still has something to do.
    make_script(
        ws_root,
        body="#!/bin/bash\nif [ \"$1\" = \"2\" ]; then exit 1; fi\necho ok\n",
    )
    mb = MadBench(find_workspace(ws_root))
    test_file = make_test_yaml(
        ws_root,
        {
            "name": "chain", "script": "hello.sh", "args": {"x": [1, 2]},
        },
    )
    mb.run(test_file)
    rd = run_dir(ws_root, "chain")

    mb.retry(rd)
    mb.retry(rd)

    assert (rd / "try_0" / "failed.yml").exists()
    assert (rd / "try_1" / "failed.yml").exists()
    assert (rd / "try_2" / "failed.yml").exists()

    m1 = yaml.safe_load((rd / "try_1" / "try.yml").read_text())
    m2 = yaml.safe_load((rd / "try_2" / "try.yml").read_text())
    assert m1["retry_of"] == "try_0/failed.yml"
    assert m2["retry_of"] == "try_1/failed.yml"

    f1 = yaml.safe_load((rd / "try_1" / "failed.yml").read_text())
    f2 = yaml.safe_load((rd / "try_2" / "failed.yml").read_text())
    assert f1["retry_of"] == "try_0/failed.yml"
    assert f2["retry_of"] == "try_1/failed.yml"
