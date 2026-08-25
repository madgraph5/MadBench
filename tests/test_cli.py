from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import madbench.cli as cli
import madbench.driver as driver
import madbench.utils as utils


@pytest.mark.parametrize(
    "affinity_args",
    [["-c", "2,4"], ["--cpu-affinity=2,4"]],
)
def test_run_cpu_affinity_cli_aliases(monkeypatch, affinity_args):
    events = []

    class FakeMadBench:
        def run(self, test, *, dry_run, note):
            events.append(("run", test, dry_run, note))

    monkeypatch.setattr(driver, "MadBench", FakeMadBench)
    monkeypatch.setattr(
        utils,
        "set_cpu_affinity",
        lambda value: events.append(("affinity", value)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["madbench", "run", *affinity_args, "tests/test.yml"],
    )

    cli.main()

    assert events == [
        ("affinity", "2,4"),
        ("run", Path("tests/test.yml"), False, None),
    ]


@pytest.mark.skipif(
    not hasattr(os, "sched_setaffinity"),
    reason="CPU affinity requires Linux",
)
def test_cli_affinity_is_inherited_and_recorded(tmp_path):
    allowed_cpus = sorted(os.sched_getaffinity(0))
    cpu = allowed_cpus[0]

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
    (tmp_path / "madbench.yml").write_text(yaml.safe_dump(config))
    for name in ["scripts", "tests", "plots", "results", "logs", "scratch"]:
        (tmp_path / name).mkdir()

    script = tmp_path / "scripts" / "affinity.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "from pathlib import Path\n"
        "cpus = sorted(os.sched_getaffinity(0))\n"
        "Path('affinity.txt').write_text(','.join(map(str, cpus)))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    test_yml = tmp_path / "tests" / "affinity.yml"
    test_yml.write_text(yaml.safe_dump({
        "name": "affinity",
        "script": "affinity.py",
        "args": {"unused": 1},
        "artifacts": ["affinity.txt"],
    }))

    project_src = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(project_src), env.get("PYTHONPATH"),
    ]))
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from madbench.cli import main; main()",
            "run",
            "-c",
            str(cpu),
            str(test_yml),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    result_dirs = list((tmp_path / "results" / "affinity").iterdir())
    assert len(result_dirs) == 1
    result_dir = result_dirs[0]
    observed = (
        result_dir / "invocation_001" / "01" / "affinity.txt"
    ).read_text()
    assert observed == str(cpu)

    metadata = yaml.safe_load((result_dir / "metadata.yml").read_text())
    hardware = metadata["hardware_index"][0]["hardware"]
    assert hardware["cpu_affinity"] == [cpu]
    assert hardware["cpu_count_available"] == 1
