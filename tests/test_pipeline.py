from __future__ import annotations

import csv
import json
import stat
from pathlib import Path

import pytest
import yaml

from madbench.driver import MadBench
from madbench.pipeline import (
    PipelineDefinition,
    build_matrix_points,
    build_step_executions,
    parse_pipeline,
)
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
    }
    (tmp_path / "madbench.yml").write_text(yaml.safe_dump(config))
    for name in ["scripts", "tests", "plots", "results", "logs", "scratch"]:
        (tmp_path / name).mkdir()
    return tmp_path


def make_script(root: Path, name: str, body: str) -> Path:
    path = root / "scripts" / name
    path.write_text("#!/bin/bash\nset -e\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def make_pipeline(root: Path, raw: dict, name: str = "pipeline.yml") -> Path:
    path = root / "tests" / name
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


def only_result_dir(root: Path, name: str) -> Path:
    values = list((root / "results" / name).iterdir())
    assert len(values) == 1
    return values[0]


def test_parse_infers_dimensions_and_projects_matrix():
    pipeline = parse_pipeline({
        "name": "p",
        "matrix": {
            "mg_version": ["v1"],
            "process": ["a", "b"],
            "backend": ["cuda", "cpp"],
            "blocks": [1, 2],
        },
        "steps": [
            {
                "id": "compile",
                "script": "compile.sh",
                "with": ["process", "backend"],
                "artifacts": {"exe": {"path": "bin"}},
            },
            {
                "id": "benchmark",
                "script": "run.sh",
                "with": {
                    "blocks": "${{ matrix.blocks }}",
                    "exe": "${{ steps.compile.artifacts.exe }}",
                },
            },
        ],
    }, source="test")

    assert pipeline.steps[0].dimensions == [
        "mg_version", "process", "backend",
    ]
    assert pipeline.steps[1].dimensions == [
        "mg_version", "process", "backend", "blocks",
    ]
    expanded = build_step_executions(pipeline)
    assert len(expanded["compile"]) == 4
    assert len(expanded["benchmark"]) == 8


def test_repetitions_are_scheduled_outermost():
    pipeline = parse_pipeline({
        "name": "p",
        "matrix": {"process": ["a", "b"]},
        "steps": [{
            "id": "run",
            "script": "run.sh",
            "with": {"process": "${{ matrix.process }}"},
            "repeat": 2,
        }],
    }, source="test")

    executions = build_step_executions(pipeline)["run"]

    assert [
        (execution.repetition, execution.values["process"])
        for execution in executions
    ] == [(1, "a"), (1, "b"), (2, "a"), (2, "b")]


def test_artifact_path_expression_infers_dimension():
    pipeline = parse_pipeline({
        "name": "p",
        "matrix": {"process": ["a", "b"], "backend": ["cuda", "cpp"]},
        "steps": [{
            "id": "build",
            "script": "build.sh",
            "artifacts": {
                "gridpack": {
                    "path": "gridpacks/${{ matrix.process }}.tar.gz",
                },
            },
        }],
    }, source="test")

    assert pipeline.steps[0].dimensions == ["process"]
    assert len(build_step_executions(pipeline)["build"]) == 2


def test_artifact_path_expression_rejects_unknown_dimension():
    with pytest.raises(ValueError, match="unknown matrix dimensions"):
        parse_pipeline({
            "name": "p",
            "steps": [{
                "id": "build",
                "script": "build.sh",
                "artifacts": {
                    "gridpack": {
                        "path": "gridpacks/${{ matrix.missing }}.tar.gz",
                    },
                },
            }],
        }, source="test")


def test_step_condition_infers_dimensions_and_rejects_unsafe_syntax():
    pipeline = parse_pipeline({
        "name": "p",
        "matrix": {"backend": ["cuda", "cpp"], "blocks": [1, 2]},
        "steps": [{
            "id": "profile",
            "script": "profile.sh",
            "if": "${{ matrix.backend == 'cuda' }}",
        }],
    }, source="test")
    assert pipeline.steps[0].dimensions == ["backend"]
    assert len(build_step_executions(pipeline)["profile"]) == 2

    with pytest.raises(ValueError, match="unsupported syntax"):
        parse_pipeline({
            "name": "p",
            "steps": [{
                "id": "bad",
                "script": "bad.sh",
                "if": "${{ __import__('os').system('echo unsafe') }}",
            }],
        }, source="test")


def test_step_condition_rejects_unknown_matrix_dimension():
    with pytest.raises(ValueError, match="unknown matrix dimensions"):
        parse_pipeline({
            "name": "p",
            "steps": [{
                "id": "profile",
                "script": "profile.sh",
                "if": "${{ matrix.backend == 'cuda' }}",
            }],
        }, source="test")


def test_zip_constructs_global_points_before_step_projection():
    pipeline = parse_pipeline({
        "name": "p",
        "matrix": {
            "process": ["a", "b"],
            "card": ["a.dat", "b.dat"],
            "blocks": [1, 2],
        },
        "zip": [["process", "card"]],
        "steps": [{"id": "run", "script": "run.sh", "with": ["process", "card"]}],
    }, source="test")
    assert build_matrix_points(pipeline) == [
        {"process": "a", "card": "a.dat", "blocks": 1},
        {"process": "a", "card": "a.dat", "blocks": 2},
        {"process": "b", "card": "b.dat", "blocks": 1},
        {"process": "b", "card": "b.dat", "blocks": 2},
    ]
    assert len(build_step_executions(pipeline)["run"]) == 2


def test_only_last_step_may_repeat():
    with pytest.raises(ValueError, match="only the last step"):
        parse_pipeline({
            "name": "p",
            "steps": [
                {"id": "one", "script": "one.sh", "repeat": 2},
                {"id": "two", "script": "two.sh"},
            ],
        }, source="test")


def test_action_requires_mg_version_and_proc_card():
    with pytest.raises(ValueError, match="mg_version"):
        parse_pipeline({
            "name": "p",
            "steps": [{
                "id": "generate",
                "action": "madgraph/process",
                "with": {"proc_card": "card.dat"},
            }],
        }, source="test")
    with pytest.raises(ValueError, match="with.proc_card"):
        parse_pipeline({
            "name": "p",
            "matrix": {"mg_version": ["v1"]},
            "steps": [{"id": "generate", "action": "madgraph/process"}],
        }, source="test")

    pipeline = parse_pipeline({
        "name": "p",
        "matrix": {"mg_version": ["v1"]},
        "steps": [{
            "id": "generate",
            "action": "madgraph/process",
            "with": {"proc_card": "card.dat"},
        }],
    }, source="test")
    assert (
        pipeline.steps[0].artifacts["process_workspace"].path
        == "process_workspace"
    )


def test_cards_action_requires_process_and_declares_default_artifacts():
    with pytest.raises(ValueError, match="with.process"):
        parse_pipeline({
            "name": "p",
            "steps": [{"id": "cards", "action": "madgraph/cards"}],
        }, source="test")

    pipeline = parse_pipeline({
        "name": "p",
        "matrix": {
            "process": [{
                "id": "pp_jets",
                "model": "sm",
                "process": ["p p > j j"],
                "launch": {},
            }],
        },
        "steps": [{
            "id": "cards",
            "action": "madgraph/cards",
            "with": {"process": "${{ matrix.process }}"},
        }],
    }, source="test")
    assert pipeline.steps[0].artifacts["proc_card"].path == "proc_card.dat"
    assert pipeline.steps[0].artifacts["launch_card"].path == "launch_card.dat"


def test_json_matrix_source_is_validated_before_expansion():
    pipeline = parse_pipeline({
        "name": "p",
        "matrix": {
            "process": {
                "from": {
                    "json": "inputs/processes.json",
                    "field": "benchmarks.processes",
                },
            },
        },
        "steps": [{
            "id": "cards",
            "action": "madgraph/cards",
            "with": {"process": "${{ matrix.process }}"},
        }],
    }, source="test")
    source = pipeline.matrix["process"]
    assert source.path == "inputs/processes.json"
    assert source.field == "benchmarks.processes"
    with pytest.raises(ValueError, match="unresolved JSON matrix sources"):
        build_matrix_points(pipeline)

    with pytest.raises(ValueError, match="exactly.*'json'.*'field'"):
        parse_pipeline({
            "name": "p",
            "matrix": {
                "process": {
                    "from": {"json": "inputs/processes.json"},
                },
            },
            "steps": [{
                "id": "cards",
                "action": "madgraph/cards",
                "with": {"process": "${{ matrix.process }}"},
            }],
        }, source="test")


def test_nested_matrix_members_are_inferred_and_resolved_in_artifact_paths(
    tmp_path,
):
    root = make_workspace(tmp_path)
    make_script(
        root,
        "build.sh",
        'identifier=$1\n'
        'mkdir -p gridpacks\n'
        'printf "%s" "$identifier" > "gridpacks/${identifier}.tar.gz"\n',
    )
    path = make_pipeline(root, {
        "name": "nested_matrix",
        "matrix": {
            "process": [
                {"id": "dy_0j", "metadata": {"output": "dy"}},
                {"id": "dy_1j", "metadata": {"output": "dyj"}},
            ],
        },
        "steps": [{
            "id": "build",
            "script": "build.sh",
            "with": {
                "identifier": "${{ matrix.process.id }}",
                "output": "${{ matrix.process.metadata.output }}",
            },
            "artifacts": {
                "gridpack": {
                    "path": "gridpacks/${{ matrix.process.id }}.tar.gz",
                    "publish": "${{ matrix.process.id }}.tar.gz",
                },
            },
        }],
    })

    pipeline = parse_pipeline(yaml.safe_load(path.read_text()), source=str(path))
    assert pipeline.steps[0].dimensions == ["process"]
    MadBench(find_workspace(root)).run(path)
    manifest = json.loads(
        (only_result_dir(root, "nested_matrix") / "report.json").read_text()
    )
    assert {
        step["arguments"]["output"] for step in manifest["steps"]
    } == {"dy", "dyj"}
    assert all(
        Path(step["artifacts"]["gridpack"]["path"]).name
        in {"dy_0j.tar.gz", "dy_1j.tar.gz"}
        for step in manifest["steps"]
    )


def test_nested_matrix_member_fails_clearly_when_missing(tmp_path):
    root = make_workspace(tmp_path)
    make_script(root, "run.sh", "exit 0\n")
    path = make_pipeline(root, {
        "name": "missing_member",
        "matrix": {"process": [{"id": "dy"}]},
        "steps": [{
            "id": "run",
            "script": "run.sh",
            "with": {"output": "${{ matrix.process.output }}"},
        }],
    })
    with pytest.raises(ValueError, match="member 'output' is missing"):
        MadBench(find_workspace(root)).run(path)


def test_labelled_input_can_feed_matrix_and_json_arguments(tmp_path):
    root = make_workspace(tmp_path)
    inputs = root / "inputs"
    inputs.mkdir()
    (inputs / "processes.json").write_text(json.dumps({
        "processes": [{"id": "dy_0j"}, {"id": "dy_1j"}],
        "launch": {"events": 10},
    }))
    make_script(
        root,
        "record.sh",
        'test "$1" = "dy_0j" || test "$1" = "dy_1j"\n',
    )
    path = make_pipeline(root, {
        "name": "labelled_inputs",
        "inputs": [{
            "id": "processes_json",
            "path": "inputs/processes.json",
        }],
        "matrix": {
            "process": {
                "from": {
                    "json": "${{ inputs.processes_json }}",
                    "field": "processes",
                },
            },
        },
        "steps": [{
            "id": "record",
            "script": "record.sh",
            "with": {
                "id": "${{ matrix.process.id }}",
                "launch": {
                    "from": {
                        "json": "${{ inputs.processes_json }}",
                        "field": "launch",
                    },
                },
            },
        }],
    })

    MadBench(find_workspace(root)).run(path)
    manifest = json.loads(
        (only_result_dir(root, "labelled_inputs") / "report.json").read_text()
    )
    assert len(manifest["steps"]) == 2
    assert all(step["status"] == "success" for step in manifest["steps"])
    assert all(
        step["arguments"]["launch"] == {"events": 10}
        for step in manifest["steps"]
    )


def test_labelled_inputs_reject_unknown_references():
    with pytest.raises(ValueError, match="unknown labelled inputs"):
        parse_pipeline({
            "name": "p",
            "inputs": [{"id": "known", "path": "inputs/known.json"}],
            "steps": [{
                "id": "run",
                "script": "run.sh",
                "with": {"path": "${{ inputs.missing }}"},
            }],
        }, source="test")


def test_labelled_input_rejects_globs():
    with pytest.raises(
        ValueError,
        match="glob patterns are supported only for unlabelled inputs",
    ):
        parse_pipeline({
            "name": "p",
            "inputs": [{
                "id": "cards",
                "path": "inputs/cards/*.dat",
            }],
            "steps": [{"id": "run", "script": "run.sh"}],
        }, source="test")


def test_labelled_input_must_resolve_to_a_file(tmp_path):
    root = make_workspace(tmp_path)
    (root / "inputs").mkdir()
    (root / "inputs" / "cards").mkdir()
    make_script(root, "run.sh", "exit 0\n")
    path = make_pipeline(root, {
        "name": "labelled_directory",
        "inputs": [{
            "id": "cards",
            "path": "inputs/cards",
        }],
        "steps": [{"id": "run", "script": "run.sh"}],
    })

    with pytest.raises(
        ValueError,
        match="labelled input 'cards' must resolve to a single file",
    ):
        MadBench(find_workspace(root)).run(path)


def test_unknown_or_forward_step_references_are_rejected():
    with pytest.raises(ValueError, match="not earlier"):
        parse_pipeline({
            "name": "p",
            "steps": [
                {
                    "id": "first",
                    "script": "first.sh",
                    "with": {
                        "x": "${{ steps.second.outputs.value }}",
                    },
                },
                {
                    "id": "second",
                    "script": "second.sh",
                    "outputs": ["value"],
                },
            ],
        }, source="test")


def test_dry_run_reports_inferred_execution_counts(tmp_path, capsys):
    root = make_workspace(tmp_path)
    make_script(root, "run.sh", "exit 0\n")
    path = make_pipeline(root, {
        "name": "dry",
        "matrix": {"a": [1, 2], "b": [10, 20]},
        "steps": [{"id": "run", "script": "run.sh", "with": ["a"]}],
    })
    mb = MadBench(find_workspace(root))
    mb.run(path, dry_run=True)
    output = capsys.readouterr().out
    assert "Global matrix: 4 point(s)" in output
    assert "inferred dimensions: ['a']" in output
    assert "executions: 2" in output
    assert not (root / "results" / "dry").exists()
    listed = mb.list_tests()
    assert listed[0]["name"] == "dry"
    assert "error" not in listed[0]


def test_pipeline_reports_live_progress_and_writes_logs_outside_results(
    tmp_path, capsys,
):
    root = make_workspace(tmp_path)
    make_script(root, "run.sh", 'printf "hello\\n"\n')
    path = make_pipeline(root, {
        "name": "logged",
        "matrix": {"process": ["dy"]},
        "steps": [{
            "id": "run",
            "script": "run.sh",
            "with": {"process": "${{ matrix.process }}"},
        }],
    })

    MadBench(find_workspace(root)).run(path)

    console = capsys.readouterr().out
    assert "[madbench] Pipeline: logged" in console
    assert "[madbench] Step run (1/1): run.sh; 1 execution(s)" in console
    assert 'dimensions={"process":"dy"}' in console
    assert "[madbench]   stdout:" in console
    assert "[madbench]   success; cache=disabled;" in console

    log_dirs = list((root / "logs" / "logged").iterdir())
    assert len(log_dirs) == 1
    main_log = (log_dirs[0] / "main.log").read_text()
    assert "Running run execution 1/1" in main_log
    assert str(log_dirs[0] / "run") in main_log
    assert list((log_dirs[0] / "run").rglob("stdout.log"))
    result_dir = only_result_dir(root, "logged")
    assert not (result_dir / "logs").exists()
    result = json.loads((result_dir / "report.json").read_text())
    assert result["steps"][0]["stdout"].startswith(str(log_dirs[0]))


def test_pipeline_updates_partial_csv_views_after_each_final_result(tmp_path):
    root = make_workspace(tmp_path)
    results_root = root / "results" / "live"
    make_script(
        root,
        "run.sh",
        'process=$1\n'
        'if [ "$MADBENCH_REPETITION" = "02" ] && [ "$process" = "a" ]; then\n'
        f'  result_csv=$(find "{results_root}" -name results.csv '
        '-not -path "*/attempts/*")\n'
        f'  summary_csv=$(find "{results_root}" -name summary.csv)\n'
        f'  timings_csv=$(find "{results_root}" -name step_timings.csv '
        '-not -path "*/attempts/*")\n'
        '  test "$(tail -n +2 "$result_csv" | wc -l)" -eq 2\n'
        '  test "$(tail -n +2 "$summary_csv" | wc -l)" -eq 2\n'
        '  test "$(tail -n +2 "$timings_csv" | wc -l)" -eq 2\n'
        'fi\n'
        'printf \'{"value": 1}\' > "$MADBENCH_OUTPUT_FILE"\n',
    )
    path = make_pipeline(root, {
        "name": "live",
        "matrix": {"process": ["a", "b"]},
        "steps": [{
            "id": "run",
            "script": "run.sh",
            "with": {"process": "${{ matrix.process }}"},
            "repeat": 2,
            "outputs": {"value": "number"},
            "stats": ["value"],
        }],
    })

    MadBench(find_workspace(root)).run(path)

    result_dir = only_result_dir(root, "live")
    with open(result_dir / "results.csv", newline="") as file:
        rows = list(csv.DictReader(file))
    assert [
        (row["repetition"], row["process"]) for row in rows
    ] == [("1", "a"), ("1", "b"), ("2", "a"), ("2", "b")]
    with open(result_dir / "summary.csv", newline="") as file:
        summaries = list(csv.DictReader(file))
    assert all(row["n_expected"] == "2" for row in summaries)
    assert all(row["n_completed"] == "2" for row in summaries)
    assert all(row["complete"] == "True" for row in summaries)
    attempt_dir = result_dir / "attempts" / "try_0"
    assert (attempt_dir / "report.json").is_file()
    assert (attempt_dir / "results.csv").read_text() == (
        result_dir / "results.csv"
    ).read_text()
    assert (attempt_dir / "step_timings.csv").read_text() == (
        result_dir / "step_timings.csv"
    ).read_text()


def test_pipeline_resolves_inputs_transfers_artifacts_and_flattens_outputs(
    tmp_path,
):
    root = make_workspace(tmp_path)
    cards = root / "inputs" / "cards"
    cards.mkdir(parents=True)
    (cards / "a.dat").write_text("card-a")
    (cards / "b.dat").write_text("card-b")
    make_script(
        root,
        "compile.sh",
        'process=$1\ncard=$2\n'
        'test -f "$card"\n'
        'mkdir executable\n'
        'printf "%s:%s" "$process" "$(cat "$card")" > executable/value\n'
        'printf \'{"compile_seconds": 2}\' > "$MADBENCH_OUTPUT_FILE"\n',
    )
    make_script(
        root,
        "run.sh",
        'process=$1\nexe=$2\nblocks=$3\n'
        'grep -q "^${process}:" "$exe/value"\n'
        'printf \'{"runtime": %s}\' "$blocks" > "$MADBENCH_OUTPUT_FILE"\n',
    )
    raw = {
        "name": "pipe",
        "matrix": {
            "process": ["a", "b"],
            "proc_card": ["inputs/cards/a.dat", "inputs/cards/b.dat"],
            "blocks": [1, 2],
        },
        "zip": [["process", "proc_card"]],
        "inputs": ["inputs/cards/*.dat"],
        "steps": [
            {
                "id": "compile",
                "script": "compile.sh",
                "with": {
                    "process": "${{ matrix.process }}",
                    "proc_card": {"input": "${{ matrix.proc_card }}"},
                },
                "outputs": {"compile_seconds": "number"},
                "artifacts": {
                    "executable": {
                        "path": "executable",
                        "publish": "executables/${{ matrix.process }}",
                    },
                },
            },
            {
                "id": "benchmark",
                "script": "run.sh",
                "with": {
                    "process": "${{ matrix.process }}",
                    "executable": "${{ steps.compile.artifacts.executable }}",
                    "blocks": "${{ matrix.blocks }}",
                },
                "repeat": 2,
                "outputs": {"runtime": "number"},
                "stats": ["runtime"],
            },
        ],
    }
    path = make_pipeline(root, raw)
    definition = MadBench(find_workspace(root)).load_test(path)
    assert isinstance(definition, PipelineDefinition)
    MadBench(find_workspace(root)).run(path)

    result_dir = only_result_dir(root, "pipe")
    manifest = json.loads((result_dir / "report.json").read_text())
    assert len(manifest["steps"]) == 2 + 8
    assert all(entry["total_time"] >= 0 for entry in manifest["steps"])
    assert all(
        entry["execution_time"] is not None
        for entry in manifest["steps"]
        if entry["status"] == "success"
    )
    assert all(
        Path(entry["arguments"]["proc_card"]).is_absolute()
        for entry in manifest["steps"]
        if entry["step_id"] == "compile"
    )
    assert all(
        "/staged/inputs/cards/" in entry["arguments"]["proc_card"]
        for entry in manifest["steps"]
        if entry["step_id"] == "compile"
    )
    compile_entries = [
        entry for entry in manifest["steps"] if entry["step_id"] == "compile"
    ]
    assert all(
        Path(entry["artifacts"]["executable"]["published_path"]).is_file()
        or Path(entry["artifacts"]["executable"]["published_path"]).is_dir()
        for entry in compile_entries
    )
    with open(result_dir / "results.csv", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 8
    assert {row["compile.compile_seconds"] for row in rows} == {"2"}
    assert {row["benchmark.runtime"] for row in rows} == {"1", "2"}
    assert (result_dir / "summary.csv").is_file()
    with open(result_dir / "step_timings.csv", newline="") as file:
        timing_rows = list(csv.DictReader(file))
    assert len(timing_rows) == 10
    assert {
        "execution_time", "materialization_time", "total_time",
    }.issubset(timing_rows[0])
    published_executables = (
        result_dir / "artifacts" / "compile" / "none" / "01" / "executables"
    )
    assert {path.name for path in published_executables.iterdir()} == {"a", "b"}


def test_pipeline_resolves_dynamic_artifact_path_and_cache(tmp_path):
    root = make_workspace(tmp_path)
    make_script(
        root,
        "build.sh",
        'process=$1\n'
        'mkdir -p gridpacks\n'
        'printf "%s" "$process" > "gridpacks/${process}.tar.gz"\n',
    )
    raw = {
        "name": "dynamic_artifact",
        "matrix": {
            "mg_version": ["v1"],
            "process": ["one", "two"],
        },
        "steps": [{
            "id": "build",
            "script": "build.sh",
            "with": {"process": "${{ matrix.process }}"},
            "artifacts": {
                "gridpack": {
                    "path": "gridpacks/${{ matrix.process }}.tar.gz",
                    "publish": "${{ matrix.process }}.tar.gz",
                },
            },
            "cache": True,
        }],
    }
    path = make_pipeline(root, raw)
    runner = MadBench(find_workspace(root))
    runner.run(path)
    runner.run(path)

    result_dirs = sorted((root / "results" / "dynamic_artifact").iterdir())
    assert result_dirs
    latest = json.loads((result_dirs[-1] / "report.json").read_text())
    assert {step["cache"] for step in latest["steps"]} == {"hit"}
    artifact_paths = {
        Path(step["artifacts"]["gridpack"]["path"])
        for step in latest["steps"]
    }
    assert {path.name for path in artifact_paths} == {
        "one.tar.gz", "two.tar.gz",
    }
    published = (
        result_dirs[-1] / "artifacts" / "build" / "v1" / "01"
    )
    assert {path.name for path in published.iterdir()} == {
        "one.tar.gz", "two.tar.gz",
    }


def test_pipeline_rejects_unsafe_resolved_artifact_path(tmp_path):
    root = make_workspace(tmp_path)
    marker = root / "script-ran"
    make_script(root, "build.sh", f"touch {marker}\n")
    path = make_pipeline(root, {
        "name": "unsafe_artifact",
        "matrix": {"artifact_path": ["../escape"]},
        "steps": [{
            "id": "build",
            "script": "build.sh",
            "artifacts": {
                "product": {"path": "${{ matrix.artifact_path }}"},
            },
        }],
    })
    with pytest.raises(ValueError, match="resolved to unsafe path"):
        MadBench(find_workspace(root)).run(path)
    assert not marker.exists()


def test_published_artifact_collision_is_rejected(tmp_path):
    root = make_workspace(tmp_path)
    make_script(
        root,
        "build.sh",
        'printf "%s" "$1" > product.txt\n',
    )
    path = make_pipeline(root, {
        "name": "collision",
        "matrix": {"process": ["one", "two"]},
        "steps": [{
            "id": "build",
            "script": "build.sh",
            "with": {"process": "${{ matrix.process }}"},
            "artifacts": {
                "product": {
                    "path": "product.txt",
                    "publish": "product.txt",
                },
            },
        }],
    })

    with pytest.raises(
        ValueError,
        match=(
            "automatically namespaces published artifacts by step, "
            "mg_version, and repetition"
        ),
    ):
        MadBench(find_workspace(root)).run(path)


def test_legacy_artifact_save_field_is_rejected():
    with pytest.raises(ValueError, match="optional 'publish'"):
        parse_pipeline({
            "name": "p",
            "steps": [{
                "id": "build",
                "script": "build.sh",
                "artifacts": {
                    "product": {"path": "product.txt", "save": True},
                },
            }],
        }, source="test")


def test_pipeline_skips_conditional_step_and_preserves_upstream_results(
    tmp_path,
):
    root = make_workspace(tmp_path)
    marker = root / "profiled-backends"
    make_script(
        root,
        "prepare.sh",
        'printf \'{"prepared": true}\' > "$MADBENCH_OUTPUT_FILE"\n',
    )
    make_script(
        root,
        "profile.sh",
        f'printf "%s\\n" "$1" >> "{marker}"\n'
        'printf \'{"profiled": true}\' > "$MADBENCH_OUTPUT_FILE"\n',
    )
    path = make_pipeline(root, {
        "name": "conditional",
        "matrix": {"backend": ["cuda", "cpp"]},
        "steps": [
            {
                "id": "prepare",
                "script": "prepare.sh",
                "outputs": {"prepared": "boolean"},
            },
            {
                "id": "profile",
                "script": "profile.sh",
                "if": "${{ matrix.backend == 'cuda' }}",
                "with": {"backend": "${{ matrix.backend }}"},
                "outputs": {"profiled": "boolean"},
            },
        ],
    })
    MadBench(find_workspace(root)).run(path)

    assert marker.read_text().splitlines() == ["cuda"]
    result_dir = only_result_dir(root, "conditional")
    manifest = json.loads((result_dir / "report.json").read_text())
    profile_statuses = {
        entry["dimensions"]["backend"]: entry["status"]
        for entry in manifest["steps"]
        if entry["step_id"] == "profile"
    }
    assert profile_statuses == {"cuda": "success", "cpp": "skipped"}
    with open(result_dir / "results.csv", newline="") as file:
        rows = {row["backend"]: row for row in csv.DictReader(file)}
    assert rows["cuda"]["prepare.prepared"] == "True"
    assert rows["cuda"]["profile.profiled"] == "True"
    assert rows["cpp"]["prepare.prepared"] == "True"
    assert rows["cpp"]["profile.profiled"] == ""
    assert rows["cpp"]["status"] == "skipped"


def test_input_wrapper_requires_staged_file(tmp_path):
    root = make_workspace(tmp_path)
    make_script(root, "run.sh", "exit 0\n")
    path = make_pipeline(root, {
        "name": "missing_input",
        "matrix": {"card": ["inputs/missing.dat"]},
        "steps": [{
            "id": "run",
            "script": "run.sh",
            "with": {"card": {"input": "${{ matrix.card }}"}},
        }],
    })
    with pytest.raises(FileNotFoundError, match="declare it under"):
        MadBench(find_workspace(root)).run(path)


def test_failed_matrix_branch_blocks_only_its_downstream_branch(tmp_path):
    root = make_workspace(tmp_path)
    make_script(
        root,
        "prepare.sh",
        'test "$1" != bad\nmkdir product\nprintf "%s" "$1" > product/value\n',
    )
    make_script(root, "consume.sh", 'test -f "$1/value"\n')
    path = make_pipeline(root, {
        "name": "branches",
        "matrix": {"case": ["good", "bad"]},
        "steps": [
            {
                "id": "prepare",
                "script": "prepare.sh",
                "with": ["case"],
                "artifacts": {"product": {"path": "product"}},
            },
            {
                "id": "consume",
                "script": "consume.sh",
                "with": {
                    "product": "${{ steps.prepare.artifacts.product }}",
                },
            },
        ],
    })
    MadBench(find_workspace(root)).run(path)
    manifest = json.loads(
        (only_result_dir(root, "branches") / "report.json").read_text(),
    )
    statuses = [
        (entry["step_id"], entry["dimensions"]["case"], entry["status"])
        for entry in manifest["steps"]
    ]
    assert statuses == [
        ("prepare", "good", "success"),
        ("prepare", "bad", "failed"),
        ("consume", "good", "success"),
        ("consume", "bad", "blocked"),
    ]


def test_step_cache_restores_outputs_and_artifacts(tmp_path):
    root = make_workspace(tmp_path)
    counter = root / "compile-count"
    (root / "inputs").mkdir()
    (root / "inputs" / "source.dat").write_text("source")
    make_script(
        root,
        "compile.sh",
        f'count_file="{counter}"\n'
        'test -f "$1"\n'
        'count=0\n'
        'test ! -f "$count_file" || count=$(cat "$count_file")\n'
        'count=$((count + 1))\n'
        'printf "%s" "$count" > "$count_file"\n'
        'mkdir bin\nprintf compiled > bin/value\n'
        'printf \'{"count": %s}\' "$count" > "$MADBENCH_OUTPUT_FILE"\n',
    )
    raw = {
        "name": "cached",
        "matrix": {"source": ["inputs/source.dat"]},
        "inputs": ["inputs/source.dat"],
        "steps": [{
            "id": "compile",
            "script": "compile.sh",
            "with": {
                "source": {"input": "${{ matrix.source }}"},
            },
            "cache": True,
            "outputs": {"count": "integer"},
            "artifacts": {"bin": {"path": "bin"}},
        }],
    }
    path = make_pipeline(root, raw)
    mb = MadBench(find_workspace(root))
    mb.run(path)
    mb.run(path)
    assert counter.read_text() == "1"
    result_dirs = sorted((root / "results" / "cached").iterdir())
    second = json.loads((result_dirs[-1] / "report.json").read_text())
    assert second["steps"][0]["cache"] == "hit"
    assert second["steps"][0]["outputs"]["count"] == 1
    assert second["steps"][0]["execution_time"] is None
    assert second["steps"][0]["materialization_time"] is not None
    assert (
        second["steps"][0]["total_time"]
        >= second["steps"][0]["materialization_time"]
    )


def test_test_workdir_contains_work_and_default_cache(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = make_workspace(workspace)
    scratch = tmp_path / "external-scratch"
    make_script(
        root,
        "build.sh",
        'mkdir artifact\n'
        'printf built > artifact/value\n',
    )
    path = make_pipeline(root, {
        "name": "external",
        "workdir": str(scratch),
        "steps": [{
            "id": "build",
            "script": "build.sh",
            "cache": True,
            "artifacts": {"artifact": {"path": "artifact"}},
        }],
    })

    MadBench(find_workspace(root)).run(path)

    run_dirs = list(scratch.glob("external_*"))
    assert len(run_dirs) == 1
    assert (run_dirs[0] / "steps" / "build").is_dir()
    cache = scratch / ".madbench-cache" / "external" / "build"
    assert len(list(cache.glob("*/artifacts.tar.gz"))) == 1
    assert not (root / "scratch" / ".madbench-cache").exists()
def test_madgraph_process_action(tmp_path):
    root = make_workspace(tmp_path)
    card_dir = root / "inputs"
    card_dir.mkdir()
    (card_dir / "proc.dat").write_text("proc")
    mg_bin = root / "MadGraph" / "v1" / "bin" / "mg5_aMC"
    mg_bin.parent.mkdir(parents=True)
    mg_bin.write_text(
        "#!/bin/bash\nset -e\n"
        "test -f \"$1\"\n"
        "mkdir generated\n"
        "printf process > generated/value\n"
        "ln -s value generated/value-link\n"
    )
    mg_bin.chmod(mg_bin.stat().st_mode | stat.S_IEXEC)
    path = make_pipeline(root, {
        "name": "action",
        "matrix": {
            "mg_version": ["v1"],
            "proc_card": ["inputs/proc.dat"],
        },
        "inputs": ["inputs/proc.dat"],
        "steps": [{
            "id": "generate",
            "action": "madgraph/process",
            "with": {
                "proc_card": {"input": "${{ matrix.proc_card }}"},
            },
            "cache": True,
        }],
    })
    runner = MadBench(find_workspace(root))
    runner.run(path)
    runner.run(path)
    result_dir = sorted((root / "results" / "action").iterdir())[-1]
    result = json.loads((result_dir / "report.json").read_text())
    step = result["steps"][0]
    assert step["cache"] == "hit"
    workspace = Path(step["artifacts"]["process_workspace"]["path"])
    assert (workspace / "generated" / "value").read_text() == "process"
    assert (workspace / "generated" / "value-link").is_symlink()


def test_artifact_rejects_symlink_escaping_its_root(tmp_path):
    root = make_workspace(tmp_path)
    make_script(
        root,
        "build.sh",
        'mkdir artifact\n'
        'ln -s /etc/passwd artifact/external\n',
    )
    path = make_pipeline(root, {
        "name": "unsafe_link",
        "steps": [{
            "id": "build",
            "script": "build.sh",
            "artifacts": {"artifact": {"path": "artifact"}},
        }],
    })

    with pytest.raises(ValueError, match="absolute symbolic link"):
        MadBench(find_workspace(root)).run(path)


def test_madgraph_cards_action_materializes_cards_for_downstream_step(tmp_path):
    root = make_workspace(tmp_path)
    make_script(
        root,
        "check_cards.sh",
        'grep -q "^import model sm$" "$1"\n'
        'grep -q "^generate e+ e- > z h$" "$1"\n'
        '! grep -q "^define ignored" "$1"\n'
        'grep -q "^output fcc_ee_zh$" "$1"\n'
        'grep -q "^launch fcc_ee_zh$" "$2"\n'
        'grep -q "^set beam.energy 120$" "$2"\n',
    )
    path = make_pipeline(root, {
        "name": "cards",
        "matrix": {
            "process": [{
                "id": "fcc_ee_zh",
                "model": "sm",
                "process": ["e+ e- > z h"],
                "proc_card_preamble": [],
                "launch": {"beam.energy": 120},
            }],
        },
        "steps": [
            {
                "id": "cards",
                "action": "madgraph/cards",
                "with": {
                    "process": "${{ matrix.process }}",
                    "proc_card_preamble": ["define ignored = u u~"],
                },
            },
            {
                "id": "check",
                "script": "check_cards.sh",
                "with": {
                    "proc_card": "${{ steps.cards.artifacts.proc_card }}",
                    "launch_card": "${{ steps.cards.artifacts.launch_card }}",
                },
            },
        ],
    })
    MadBench(find_workspace(root)).run(path)
    result = json.loads(
        (only_result_dir(root, "cards") / "report.json").read_text()
    )
    assert [step["status"] for step in result["steps"]] == ["success", "success"]


def test_json_process_file_fans_out_paired_cards_to_downstream_steps(tmp_path):
    root = make_workspace(tmp_path)
    inputs = root / "inputs"
    inputs.mkdir()
    (inputs / "processes.json").write_text(json.dumps({
        "proc_card_preamble": [
            "set group_subprocesses Auto",
            "define lightq = u c d s u~ c~ d~ s~",
        ],
        "launch": {
            "madspin": "OFF",
            "reweight": "OFF",
            "generation.events": 10000,
        },
        "catalogue": {
            "processes": [
                {
                    "id": "pp_jets",
                    "model": "",
                    "process": ["p p > j j"],
                    "output": "",
                    "launch": {},
                },
                {
                    "id": "fcc_ee_zh",
                    "model": "sm",
                    "process": ["e+ e- > z h", "e+ e- > z h j"],
                    "proc_card_preamble": [
                        "set group_subprocesses False",
                    ],
                    "output": "standalone",
                    "launch": {
                        "beam.energy": 120,
                        "generation.events": 20000,
                    },
                },
            ],
        },
    }))
    make_script(
        root,
        "record_pair.sh",
        'proc_id=$(awk \'/^output / {print $NF}\' "$1")\n'
        'launch_id=$(sed -n "s/^launch //p" "$2")\n'
        'test "$proc_id" = "$launch_id"\n'
        'grep -q "^set madspin OFF$" "$2"\n'
        'if test "$proc_id" = fcc_ee_zh; then events=20000; '
        'else events=10000; fi\n'
        'grep -q "^set generation.events ${events}$" "$2"\n'
        'printf \'{"id": "%s", "backend": "%s"}\' "$proc_id" "$3" '
        '> "$MADBENCH_OUTPUT_FILE"\n',
    )
    path = make_pipeline(root, {
        "name": "json_fanout",
        "matrix": {
            "process": {
                "from": {
                    "json": "inputs/processes.json",
                    "field": "catalogue.processes",
                },
            },
            "backend": ["cpp", "cuda"],
        },
        "inputs": ["inputs/processes.json"],
        "steps": [
            {
                "id": "cards",
                "action": "madgraph/cards",
                "with": {
                    "process": "${{ matrix.process }}",
                    "proc_card_preamble": {
                        "from": {
                            "json": "inputs/processes.json",
                            "field": "proc_card_preamble",
                        },
                    },
                    "default_launch": {
                        "from": {
                            "json": "inputs/processes.json",
                            "field": "launch",
                        },
                    },
                },
            },
            {
                "id": "consume",
                "script": "record_pair.sh",
                "with": {
                    "proc_card": "${{ steps.cards.artifacts.proc_card }}",
                    "launch_card": "${{ steps.cards.artifacts.launch_card }}",
                    "backend": "${{ matrix.backend }}",
                },
                "outputs": {"id": "string", "backend": "string"},
            },
        ],
    })
    mb = MadBench(find_workspace(root))
    mb.run(path)
    result = json.loads(
        (only_result_dir(root, "json_fanout") / "report.json").read_text()
    )
    cards = [step for step in result["steps"] if step["step_id"] == "cards"]
    consumers = [
        step for step in result["steps"] if step["step_id"] == "consume"
    ]
    assert len(cards) == 2
    assert len(consumers) == 4
    proc_cards = {
        step["dimensions"]["process"]["id"]:
        Path(step["artifacts"]["proc_card"]["path"]).read_text()
        for step in cards
    }
    assert "import model" not in proc_cards["pp_jets"]
    assert "set group_subprocesses Auto\n" in proc_cards["pp_jets"]
    assert proc_cards["pp_jets"].endswith("output pp_jets\n")
    assert proc_cards["fcc_ee_zh"].startswith("import model sm\n")
    assert "set group_subprocesses Auto" not in proc_cards["fcc_ee_zh"]
    assert "set group_subprocesses False\n" in proc_cards["fcc_ee_zh"]
    assert "generate e+ e- > z h\n" in proc_cards["fcc_ee_zh"]
    assert "add process e+ e- > z h j\n" in proc_cards["fcc_ee_zh"]
    assert proc_cards["fcc_ee_zh"].endswith(
        "output standalone fcc_ee_zh\n"
    )
    assert {
        (step["outputs"]["id"], step["outputs"]["backend"])
        for step in consumers
    } == {
        ("pp_jets", "cpp"),
        ("pp_jets", "cuda"),
        ("fcc_ee_zh", "cpp"),
        ("fcc_ee_zh", "cuda"),
    }


def test_json_argument_source_passes_nested_value_as_json_to_script(tmp_path):
    root = make_workspace(tmp_path)
    inputs = root / "inputs"
    inputs.mkdir()
    (inputs / "config.json").write_text(json.dumps({
        "nested": {
            "settings": [["madspin", "OFF"], ["generation.events", 10000]],
        },
    }))
    make_script(
        root,
        "check_json_arg.sh",
        'test "$1" = \'[["madspin","OFF"],["generation.events",10000]]\'\n'
        'python -c \'import json, os; '
        'assert json.load(open(os.environ["MADBENCH_ARGS_FILE"]))["settings"]'
        ' == [["madspin", "OFF"], ["generation.events", 10000]]\'\n',
    )
    path = make_pipeline(root, {
        "name": "json_argument",
        "inputs": ["inputs/config.json"],
        "steps": [{
            "id": "check",
            "script": "check_json_arg.sh",
            "with": {
                "settings": {
                    "from": {
                        "json": "inputs/config.json",
                        "field": "nested.settings",
                    },
                },
            },
        }],
    })
    MadBench(find_workspace(root)).run(path)
    result = json.loads(
        (only_result_dir(root, "json_argument") / "report.json").read_text()
    )
    assert result["steps"][0]["status"] == "success"
