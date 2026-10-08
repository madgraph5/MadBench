from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import yaml

from madbench import _logging, driver, pipeline
from madbench.workspace import find_workspace


@pytest.mark.parametrize("runner", ["pipeline", "legacy"])
@pytest.mark.parametrize("placement", ["absolute", "relative", "default"])
@pytest.mark.parametrize("outcome", ["success", "failure", "interrupt"])
def test_logs_are_retained_between_executions_outside_timers(
    tmp_path, monkeypatch, capsys, runner, placement, outcome,
):
    """Slow synchronous transfers must not change any execution's timing."""
    (tmp_path / "madbench.yml").write_text("workspace: {}\n")
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "run.sh"
    script.write_text("#!/bin/sh\necho hello\n")
    script.chmod(0o755)
    work_root = tmp_path / ("scratch" if placement == "default" else "local-work")
    definition = {"name": "logged"}
    if placement != "default":
        definition["workdir"] = (
            str(work_root) if placement == "absolute" else "local-work"
        )
    if runner == "pipeline":
        definition["steps"] = [{"id": "run", "script": "run.sh", "repeat": 2}]
    else:
        definition.update(script="run.sh", args={}, repeat=2)
    test_yml = tmp_path / "test.yml"
    test_yml.write_text(yaml.safe_dump(definition))
    durable_logs = tmp_path / "logs"

    # Avoid unrelated toolchain/git subprocesses, and make elapsed time exact.
    for module in (driver, pipeline):
        monkeypatch.setattr(module, "detect_hardware", lambda: {"hostname": "vm"})
        monkeypatch.setattr(module, "detect_software_versions", lambda: {})
        monkeypatch.setattr(module, "get_git_sha", lambda _: None)
    clock = [0.0]
    monkeypatch.setattr(driver.time, "monotonic", lambda: clock[0])
    processes = []
    transfers = []

    class Process:
        def __init__(self, cmd, *, stdout, stderr, cwd, env, **kwargs):
            self.stdout = stdout
            self.stderr = stderr
            self.active = True
            self.interrupted = False
            assert Path(cwd).is_relative_to(work_root)
            assert Path(stdout.name).is_relative_to(work_root)
            assert Path(stderr.name).is_relative_to(work_root)
            # Only earlier executions may have durable logs. Their main-log
            # snapshot must already be present when this process starts.
            published = list(durable_logs.rglob("stdout.log"))
            assert len(published) == len(processes)
            if processes:
                main = next(durable_logs.rglob("main.log")).read_text()
                assert "Running" in main
                assert published[0].read_text() == "hello\n"
                if runner == "pipeline":
                    status = "failed" if outcome == "failure" else "success"
                    assert f"{status}; cache=" in main
            processes.append(self)

        def wait(self):
            if self.interrupted:
                return -15
            self.stdout.write("hello\n")
            self.stderr.write("diagnostic\n")
            clock[0] += 2.0
            if outcome == "interrupt":
                self.interrupted = True
                raise KeyboardInterrupt
            self.active = False
            return 1 if outcome == "failure" else 0

        def terminate(self):
            self.active = False

    monkeypatch.setattr(driver.subprocess, "Popen", Process)
    original_publish = _logging.publish_logs

    def slow_publish(source, destination):
        assert not any(proc.active for proc in processes)
        assert source.is_relative_to(work_root)
        assert destination.is_relative_to(durable_logs)
        if source.is_dir():
            assert all(proc.stdout.closed and proc.stderr.closed for proc in processes)
        original_publish(source, destination)
        transfers.append(destination)
        clock[0] += 100.0

    monkeypatch.setattr(_logging, "publish_logs", slow_publish)
    monkeypatch.setattr(driver, "publish_logs", slow_publish)
    bench = driver.MadBench(find_workspace(tmp_path))
    if outcome == "interrupt" and runner == "pipeline":
        with pytest.raises(KeyboardInterrupt):
            bench.run(test_yml)
    else:
        bench.run(test_yml)

    count = 1 if outcome == "interrupt" else 2
    assert len(processes) == count
    assert len(list(durable_logs.rglob("stdout.log"))) == count
    assert all(p.read_text() == "diagnostic\n" for p in durable_logs.rglob("stderr.log"))
    assert transfers
    local_main = next(work_root.rglob("main.log"))
    published_main = next(durable_logs.rglob("main.log"))
    assert local_main.read_bytes() == published_main.read_bytes()
    console = capsys.readouterr().out
    assert f"Main log: {local_main}" in console
    for proc in processes:
        assert f"stdout: {proc.stdout.name}" in console
        assert f"stderr: {proc.stderr.name}" in console

    if runner == "pipeline" and outcome != "interrupt":
        report = next((tmp_path / "results").rglob("report.json"))
        steps = json.loads(report.read_text())["steps"]
        assert len(steps) == count
        assert all(step["execution_time"] == 2.0 for step in steps)
        assert all(step["total_time"] == 2.0 for step in steps)
        assert all(Path(step["stdout"]).is_file() for step in steps)
    elif runner == "legacy":
        result_csv = next((tmp_path / "results").rglob("results.csv"))
        with result_csv.open() as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == count
        assert all(float(row["wall_time"]) == 2.0 for row in rows)
        assert list(durable_logs.rglob("*.tar.gz"))
