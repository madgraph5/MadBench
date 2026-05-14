from __future__ import annotations

import importlib.util
import itertools
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .utils import detect_hardware, get_git_sha, get_timestamp
from .workspace import (
    WorkspaceConfig,
    find_workspace,
    resolve_plot_module,
    resolve_script,
    stage_inputs,
)
from ._logging import TeeLogger, bundle_logs
from .results import append_row, select_results_csv


_REQUIRED_FIELDS = {"name", "script", "args", "result_group"}

OUTPUT_FILE_NAME = ".madbench_output.json"


@dataclass
class TestDefinition:
    """Parsed content of a test YAML file."""

    name: str
    description: str
    script: str
    args: dict[str, Any]              # values can be scalars or lists
    result_group: str
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    output_files: list[str] = field(default_factory=list)
    workdir: Optional[str] = None
    plot: Optional[str] = None
    raw: dict = field(default_factory=dict)
    zip_groups: list[list[str]] = field(default_factory=list)
    # Each inner list is a group of arg names whose list values are zipped
    # together (must be equal length). Each group contributes a single axis
    # to the cartesian product over the remaining list-valued args.


def _normalize_zip_groups(raw_zip: Any) -> list[list[str]]:
    """Accept ``[a, b]`` (single group) or ``[[a, b], [c, d]]`` (multi-group)."""
    if not raw_zip:
        return []
    if not isinstance(raw_zip, list):
        raise ValueError(
            f"'zip' must be a list of arg names or a list of such lists, "
            f"got {type(raw_zip).__name__}"
        )
    if all(isinstance(x, str) for x in raw_zip):
        return [list(raw_zip)]
    if all(isinstance(x, list) and all(isinstance(s, str) for s in x) for x in raw_zip):
        return [list(x) for x in raw_zip]
    raise ValueError(
        "'zip' must be a list of arg names (single group) "
        "or a list of lists of arg names (multiple groups)"
    )


def _as_str_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"{field_name!r} must be a list of strings")
    return list(value)


class MadBench:
    """Core MadBench orchestrator.

    Can be used programmatically::

        mb = MadBench()
        mb.run(Path("tests/hello_test.yml"))

    or via the CLI (``madbench run tests/hello_test.yml``).
    """

    def __init__(self, workspace: Optional[WorkspaceConfig] = None) -> None:
        """If workspace is None, discover it from cwd."""
        self.workspace: WorkspaceConfig = workspace or find_workspace()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_test(self, test_path: Path) -> TestDefinition:
        """Parse a test YAML file into a TestDefinition.

        `test_path` can be absolute or relative to cwd.
        """
        import yaml

        path = test_path if test_path.is_absolute() else Path.cwd() / test_path
        if not path.exists():
            raise FileNotFoundError(f"Test file not found: {path}")

        with open(path) as f:
            raw = yaml.safe_load(f) or {}

        missing = _REQUIRED_FIELDS - raw.keys()
        if missing:
            raise ValueError(
                f"Test YAML is missing required field(s): {', '.join(sorted(missing))}\n"
                f"File: {path}"
            )

        return TestDefinition(
            name=raw["name"],
            description=raw.get("description", ""),
            script=raw["script"],
            args=raw["args"] or {},
            result_group=raw["result_group"],
            inputs=_as_str_list(raw.get("inputs"), "inputs"),
            outputs=_as_str_list(raw.get("outputs"), "outputs"),
            output_files=_as_str_list(raw.get("output_files"), "output_files"),
            workdir=raw.get("workdir"),
            plot=raw.get("plot"),
            raw=raw,
            zip_groups=_normalize_zip_groups(raw.get("zip")),
        )

    def build_commands(self, test: TestDefinition) -> list[list[str]]:
        """Build the list of commands to execute (positional args only)."""
        script_path = resolve_script(self.workspace, test.script)
        combos = self._build_arg_combos(test)
        return [
            [str(script_path)] + [str(combo[k]) for k in test.args]
            for combo in combos
        ]

    def run(self, test_path: Path, dry_run: bool = False) -> None:
        """Main entry point. Loads the test, builds commands, runs them."""
        test = self.load_test(test_path)
        script_path = resolve_script(self.workspace, test.script)

        combos = self._build_arg_combos(test)
        commands = [
            [str(script_path)] + [str(combo[k]) for k in test.args]
            for combo in combos
        ]

        timestamp = get_timestamp()
        workdir_base = self._resolve_workdir(test)
        run_dir = workdir_base / f"{test.name}_{timestamp}"
        inputs_dir = run_dir / "inputs"
        result_dir = self.workspace.results_dir / test.result_group
        run_log_dir = self.workspace.logs_dir / f"{test.name}_{timestamp}"

        git_sha = get_git_sha(self.workspace.root)
        hardware = detect_hardware()
        metadata: dict[str, Any] = {
            "test_name": test.name,
            "description": test.description,
            "git_sha": git_sha,
            "timestamp": timestamp,
            "hardware": hardware,
            "test_definition": test.raw,
            "commands": [" ".join(cmd) for cmd in commands],
            "script": str(script_path),
            "run_dir": str(run_dir),
            "result_dir": str(result_dir),
            "dry_run": dry_run,
        }

        if dry_run:
            self._print_dry_run(test, script_path, run_dir, result_dir, commands, metadata)
            return

        # Prepare run dir + inputs (once per madbench run)
        run_dir.mkdir(parents=True, exist_ok=True)
        result_dir.mkdir(parents=True, exist_ok=True)
        run_log_dir.mkdir(parents=True, exist_ok=True)
        if test.inputs:
            stage_inputs(self.workspace.root, test.inputs, inputs_dir)
        else:
            inputs_dir.mkdir(parents=True, exist_ok=True)

        # CSV setup
        csv_header = self._csv_header(test)
        csv_path, write_header = select_results_csv(result_dir, csv_header)

        main_log = run_log_dir / "main.log"
        archive_path = self.workspace.logs_dir / f"{test.name}_{timestamp}.tar.gz"

        results: list[dict] = []
        wall_start = time.monotonic()

        try:
            with TeeLogger(main_log) as tee:
                for i, (cmd, combo) in enumerate(zip(commands, combos), 1):
                    invocation_id = f"invocation_{i:03d}"
                    invocation_dir = run_dir / invocation_id
                    invocation_dir.mkdir(parents=True, exist_ok=True)
                    output_file = invocation_dir / OUTPUT_FILE_NAME

                    env = os.environ.copy()
                    env["MADBENCH_WORKDIR"] = str(invocation_dir)
                    env["MADBENCH_INPUTS"] = str(inputs_dir)
                    env["MADBENCH_OUTPUT_FILE"] = str(output_file)

                    header = (
                        f"=== Running ({i}/{len(commands)}): {' '.join(cmd)} "
                        f"[{invocation_id}] ==="
                    )
                    print(header)

                    invocation_ts = get_timestamp()
                    cmd_start = time.monotonic()
                    try:
                        proc = subprocess.Popen(
                            cmd,
                            stdout=tee.write_fd,
                            stderr=subprocess.STDOUT,
                            cwd=invocation_dir,
                            env=env,
                            close_fds=True,
                        )
                        exit_code = proc.wait()
                    except KeyboardInterrupt:
                        proc.terminate()
                        proc.wait()
                        exit_code = -2
                        wall_time = time.monotonic() - cmd_start
                        self._finalize_invocation(
                            test, combo, invocation_id, invocation_dir, output_file,
                            result_dir, csv_path, csv_header, write_header,
                            invocation_ts, exit_code, wall_time,
                        )
                        results.append({
                            "command": " ".join(cmd),
                            "invocation_id": invocation_id,
                            "exit_code": exit_code,
                            "wall_time": round(wall_time, 2),
                        })
                        write_header = False
                        print("\n[madbench] Interrupted by user.")
                        raise

                    wall_time = time.monotonic() - cmd_start
                    self._finalize_invocation(
                        test, combo, invocation_id, invocation_dir, output_file,
                        result_dir, csv_path, csv_header, write_header,
                        invocation_ts, exit_code, wall_time,
                    )
                    write_header = False
                    results.append({
                        "command": " ".join(cmd),
                        "invocation_id": invocation_id,
                        "exit_code": exit_code,
                        "wall_time": round(wall_time, 2),
                    })

        except KeyboardInterrupt:
            print("[madbench] Interrupted — bundling partial logs...")
        finally:
            metadata["results"] = results
            metadata["total_wall_time"] = round(time.monotonic() - wall_start, 2)
            metadata["csv_path"] = str(csv_path)
            bundle_logs(run_log_dir, main_log, metadata, archive_path)

        total_time = time.monotonic() - wall_start
        print(f"\n[madbench] Run complete in {total_time:.1f}s")
        for r in results:
            status = "OK" if r["exit_code"] == 0 else f"FAILED (exit {r['exit_code']})"
            print(f"  [{status}] {r['invocation_id']}  {r['command']}  ({r['wall_time']}s)")
        print(f"[madbench] Workdir: {run_dir}")
        print(f"[madbench] Results CSV: {csv_path}")
        print(f"[madbench] Log archive: {archive_path}")

    def plot(self, test_path: Path) -> None:
        """Load the test, import its plot module, call ``plot(result_path)``,
        and display the figure with ``plotly.io.show()``.

        ``result_path`` is ``results/<result_group>/``; modules typically
        read ``result_path / 'results.csv'`` but may also descend into the
        per-invocation subdirectories for additional files.
        """
        try:
            import plotly.io as pio
        except ImportError:
            print("[madbench] plotly is not installed. Run: pip install 'madbench[plot]'")
            return

        test = self.load_test(test_path)

        if not test.plot:
            print(f"[madbench] Test '{test.name}' has no plot module defined.")
            return

        plot_path = resolve_plot_module(self.workspace, test.plot)
        if plot_path is None:
            print(f"[madbench] Plot module not found: {self.workspace.plots_dir / test.plot}.py")
            return

        spec = importlib.util.spec_from_file_location(test.plot, plot_path)
        if spec is None or spec.loader is None:
            print(f"[madbench] Failed to load plot module: {plot_path}")
            return

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]

        if not hasattr(module, "plot"):
            print(f"[madbench] Plot module {plot_path} must define a 'plot(result_path)' function.")
            return

        result_path = self.workspace.results_dir / test.result_group
        fig = module.plot(result_path)
        pio.show(fig)

    def list_tests(self) -> list[dict]:
        """List all .yml files in the tests/ directory.

        Returns a list of dicts with keys: name, path, has_results, has_plot.
        """
        tests_dir = self.workspace.tests_dir
        if not tests_dir.exists():
            return []

        results = []
        for yml_file in sorted(tests_dir.glob("*.yml")):
            try:
                test = self.load_test(yml_file)
            except (ValueError, KeyError):
                results.append({
                    "name": yml_file.stem,
                    "path": str(yml_file),
                    "has_results": False,
                    "has_plot": False,
                    "error": "malformed YAML",
                })
                continue

            result_dir = self.workspace.results_dir / test.result_group
            has_results = result_dir.exists() and any(result_dir.iterdir())

            plot_path = resolve_plot_module(self.workspace, test.plot or "")
            has_plot = plot_path is not None

            results.append({
                "name": test.name,
                "path": str(yml_file),
                "has_results": has_results,
                "has_plot": has_plot,
            })

        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_arg_combos(self, test: TestDefinition) -> list[dict[str, Any]]:
        """Return one dict per invocation, mapping every arg name to its
        resolved value for that sweep point. Scalars carry through unchanged;
        list-valued args expand into cartesian product; zip groups contribute
        a single paired axis.
        """
        self._validate_zip_groups(test)

        name_to_group: dict[str, int] = {}
        for i, group in enumerate(test.zip_groups):
            for name in group:
                name_to_group[name] = i

        axes: list[list[dict[str, Any]]] = []
        emitted_groups: set[int] = set()
        for k, v in test.args.items():
            if k in name_to_group:
                gi = name_to_group[k]
                if gi in emitted_groups:
                    continue
                emitted_groups.add(gi)
                group = test.zip_groups[gi]
                tuples = zip(*(test.args[n] for n in group))
                axes.append([dict(zip(group, t)) for t in tuples])
            elif isinstance(v, list):
                axes.append([{k: item} for item in v])

        combos = []
        for combo in itertools.product(*axes):
            overrides: dict[str, Any] = {}
            for piece in combo:
                overrides.update(piece)
            resolved = {k: overrides.get(k, v) for k, v in test.args.items()}
            combos.append(resolved)
        return combos

    @staticmethod
    def _validate_zip_groups(test: TestDefinition) -> None:
        seen: dict[str, int] = {}
        for i, group in enumerate(test.zip_groups):
            if not group:
                raise ValueError(f"zip group at index {i} is empty")
            for name in group:
                if name in seen:
                    raise ValueError(
                        f"arg {name!r} appears in more than one zip group "
                        f"(groups {seen[name]} and {i})"
                    )
                seen[name] = i
                if name not in test.args:
                    raise ValueError(
                        f"zip group references unknown arg {name!r}; "
                        f"known args: {list(test.args)}"
                    )
                if not isinstance(test.args[name], list):
                    raise ValueError(
                        f"zip group member {name!r} must be a list, "
                        f"got {type(test.args[name]).__name__}"
                    )
            lengths = {len(test.args[n]) for n in group}
            if len(lengths) > 1:
                detail = ", ".join(f"{n}={len(test.args[n])}" for n in group)
                raise ValueError(
                    f"zip group {group} has mismatched lengths: {detail}"
                )

    def _resolve_workdir(self, test: TestDefinition) -> Path:
        if not test.workdir:
            return self.workspace.scratch_dir
        p = Path(test.workdir)
        if not p.is_absolute():
            p = self.workspace.root / p
        return p.resolve()

    def _csv_header(self, test: TestDefinition) -> list[str]:
        return (
            ["timestamp"]
            + list(test.args.keys())
            + list(test.outputs)
            + ["exit_code", "wall_time", "invocation_id"]
        )

    def _finalize_invocation(
        self,
        test: TestDefinition,
        combo: dict[str, Any],
        invocation_id: str,
        invocation_dir: Path,
        output_file: Path,
        result_dir: Path,
        csv_path: Path,
        csv_header: list[str],
        write_header: bool,
        invocation_ts: str,
        exit_code: int,
        wall_time: float,
    ) -> None:
        """Post-script: read outputs JSON, copy output_files, append CSV row."""
        output_values = self._read_outputs_json(test, output_file)
        self._copy_output_files(test, combo, invocation_dir, result_dir / invocation_id)

        row: dict[str, Any] = {
            "timestamp": invocation_ts,
            "exit_code": exit_code,
            "wall_time": round(wall_time, 2),
            "invocation_id": invocation_id,
        }
        for k in test.args:
            row[k] = combo[k]
        for k in test.outputs:
            row[k] = output_values.get(k, "")

        append_row(csv_path, csv_header, row, write_header)

    def _read_outputs_json(
        self,
        test: TestDefinition,
        output_file: Path,
    ) -> dict[str, Any]:
        if not test.outputs:
            return {}
        if not output_file.exists():
            print(
                f"[madbench] WARN: declared 'outputs' but {output_file.name} "
                f"was not written by the script ({output_file})"
            )
            return {}
        try:
            values = json.loads(output_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"[madbench] WARN: failed to parse {output_file}: {e}")
            return {}
        if not isinstance(values, dict):
            print(
                f"[madbench] WARN: {output_file} must contain a JSON object, "
                f"got {type(values).__name__}"
            )
            return {}

        declared = set(test.outputs)
        provided = set(values.keys())
        missing = declared - provided
        extra = provided - declared
        if missing:
            print(
                f"[madbench] WARN: output keys missing from {output_file.name}: "
                f"{sorted(missing)}"
            )
        if extra:
            print(
                f"[madbench] WARN: keys in {output_file.name} not declared in "
                f"'outputs': {sorted(extra)}"
            )
        return values

    def _copy_output_files(
        self,
        test: TestDefinition,
        combo: dict[str, Any],
        invocation_dir: Path,
        dest_dir: Path,
    ) -> None:
        if not test.output_files:
            return
        for pattern in test.output_files:
            try:
                resolved = pattern.format_map(combo)
            except KeyError as e:
                print(
                    f"[madbench] WARN: output_files entry {pattern!r} "
                    f"references unknown arg {e}"
                )
                continue
            src = invocation_dir / resolved
            if not src.exists():
                print(f"[madbench] WARN: declared output_file missing: {src}")
                continue
            target = dest_dir / resolved
            target.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, target, dirs_exist_ok=True)
            else:
                shutil.copy2(src, target)

    def _print_dry_run(
        self,
        test: TestDefinition,
        script_path: Path,
        run_dir: Path,
        result_dir: Path,
        commands: list[list[str]],
        metadata: dict,
    ) -> None:
        import yaml as _yaml

        print("[madbench] DRY RUN — no files will be created or scripts executed")
        print(f"[madbench] Test: {test.name}")
        print(f"[madbench] Script: {script_path}")
        print(f"[madbench] Run dir: {run_dir}")
        print(f"[madbench] Result dir: {result_dir}")
        if test.inputs:
            print(f"[madbench] Inputs (staged into {run_dir / 'inputs'}):")
            for pat in test.inputs:
                print(f"  {pat}")
        if test.outputs:
            print(f"[madbench] Outputs (CSV columns): {test.outputs}")
        if test.output_files:
            print(f"[madbench] Output files (per invocation): {test.output_files}")
        print(f"[madbench] Commands ({len(commands)}):")
        for cmd in commands:
            print(f"  {' '.join(cmd)}")
        print("[madbench] Metadata:")
        print(_yaml.dump(metadata, default_flow_style=False, allow_unicode=True))
