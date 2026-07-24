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
                    "executable": {"path": "executable", "save": True},
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
    manifest = json.loads((result_dir / "result.json").read_text())
    assert len(manifest["steps"]) == 2 + 8
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
        Path(entry["artifacts"]["executable"]["saved_path"]).is_file()
        or Path(entry["artifacts"]["executable"]["saved_path"]).is_dir()
        for entry in compile_entries
    )
    with open(result_dir / "results.csv", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 8
    assert {row["compile.compile_seconds"] for row in rows} == {"2"}
    assert {row["benchmark.runtime"] for row in rows} == {"1", "2"}
    assert (result_dir / "summary.csv").is_file()
    assert len(list((result_dir / "artifacts" / "compile").rglob("executable"))) == 2


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
        (only_result_dir(root, "branches") / "result.json").read_text(),
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
    second = json.loads((result_dirs[-1] / "result.json").read_text())
    assert second["steps"][0]["cache"] == "hit"
    assert second["steps"][0]["outputs"]["count"] == 1


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
            "artifacts": {
                "process": {"path": "generated", "save": True},
            },
        }],
    })
    MadBench(find_workspace(root)).run(path)
    result_dir = only_result_dir(root, "action")
    assert list((result_dir / "artifacts").rglob("value"))
