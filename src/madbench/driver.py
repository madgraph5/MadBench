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

# Distinct exit code recorded in the CSV when a version's proc_card
# generation step failed and the script was therefore not run.
PROC_GEN_FAILED_EXIT_CODE = -3


MG_VERSION_NONE = "none"


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
    mg_version: list[str] = field(default_factory=lambda: [MG_VERSION_NONE])
    # Outer sweep dimension. Each entry is a folder name under MadGraph/.
    # The sentinel "none" means MadGraph is not selected for this run — no
    # workdir segment, MG_BIN exposed as empty, MG_VERSION="none" in the CSV.
    proc_cards: list[str] = field(default_factory=list)
    # Workspace-relative paths to MadGraph proc-card files. When non-empty,
    # MadGraph is invoked once per (mg_version, proc_card) with cwd set to
    # <run_dir>/processes/ before the test script runs. Requires a real
    # mg_version (i.e. not the "none" sentinel).
    repeat: int = 1
    # Number of statistical repetitions per arg-combo. Each rep gets its own
    # <invocation>/RR/ subdir (zero-padded), its own row in the main CSV, and
    # contributes to the per-(mg_version, arg-combo) summary CSV.


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


def _normalize_repeat(raw: Any) -> int:
    """Accept missing/None → 1, or a positive int. Reject anything else."""
    if raw is None:
        return 1
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"'repeat' must be a positive integer, got {type(raw).__name__}")
    if raw < 1:
        raise ValueError(f"'repeat' must be >= 1, got {raw}")
    return raw


def _normalize_mg_version(raw: Any) -> list[str]:
    """Accept None / str / list[str]. Empty/missing → [MG_VERSION_NONE]."""
    if raw is None:
        return [MG_VERSION_NONE]
    if isinstance(raw, str):
        return [raw] if raw else [MG_VERSION_NONE]
    if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        return list(raw) if raw else [MG_VERSION_NONE]
    raise ValueError(
        "'mg_version' must be a string or a list of strings (folder names "
        "under MadGraph/), got "
        f"{type(raw).__name__}"
    )


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
            mg_version=_normalize_mg_version(raw.get("mg_version")),
            proc_cards=_as_str_list(raw.get("proc_cards"), "proc_cards"),
            repeat=_normalize_repeat(raw.get("repeat")),
        )

    def build_commands(self, test: TestDefinition) -> list[list[str]]:
        """Build the list of commands to execute (positional args only).

        Returns one command per sweep point. ``mg_version`` does not appear
        in the script arguments — it is exposed to the script via env vars
        — so commands repeat across mg_versions when more than one is set.
        """
        script_path = resolve_script(self.workspace, test.script)
        return [
            [str(script_path)] + [str(combo[k]) for k in test.args]
            for combo, _ in self._build_sweep_points(test)
        ]

    def run(self, test_path: Path, dry_run: bool = False) -> None:
        """Main entry point. Loads the test, builds commands, runs them."""
        test = self.load_test(test_path)
        script_path = resolve_script(self.workspace, test.script)

        sweep_points = self._build_sweep_points(test)
        commands = [
            [str(script_path)] + [str(combo[k]) for k in test.args]
            for combo, _ in sweep_points
        ]

        timestamp = get_timestamp()
        workdir_base = self._resolve_workdir(test)
        run_dirs = {
            mgv: self._version_run_dir(workdir_base, test.name, timestamp, mgv)
            for mgv in test.mg_version
        }
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
            "mg_versions": list(test.mg_version),
            "run_dirs": {mgv: str(p) for mgv, p in run_dirs.items()},
            "result_dir": str(result_dir),
            "dry_run": dry_run,
        }

        if dry_run:
            self._print_dry_run(test, script_path, run_dirs, result_dir, commands, metadata)
            return

        # Prepare per-version run dirs + inputs + processes (once per version)
        result_dir.mkdir(parents=True, exist_ok=True)
        run_log_dir.mkdir(parents=True, exist_ok=True)
        for mgv, rd in run_dirs.items():
            rd.mkdir(parents=True, exist_ok=True)
            inputs_dir = rd / "inputs"
            if test.inputs:
                stage_inputs(self.workspace.root, test.inputs, inputs_dir)
            else:
                inputs_dir.mkdir(parents=True, exist_ok=True)
            (rd / "processes").mkdir(parents=True, exist_ok=True)

        # CSV setup
        csv_header = self._csv_header(test)
        csv_path, write_header = select_results_csv(result_dir, csv_header)

        main_log = run_log_dir / "main.log"
        archive_path = self.workspace.logs_dir / f"{test.name}_{timestamp}.tar.gz"

        results: list[dict] = []
        wall_start = time.monotonic()

        n_per_version = len(sweep_points) // len(test.mg_version)
        total_runs = len(sweep_points) * test.repeat
        global_i = 0
        script_idx = 0
        csv_rows: list[dict[str, Any]] = []

        try:
            with TeeLogger(main_log) as tee:
                for mgv in test.mg_version:
                    run_dir = run_dirs[mgv]
                    inputs_dir = run_dir / "inputs"
                    processes_dir = run_dir / "processes"
                    result_version_dir = self._version_result_dir(result_dir, mgv)

                    proc_gen_ok = True
                    if test.proc_cards:
                        proc_gen_ok = self._generate_processes(
                            test, mgv, run_dir, tee.write_fd,
                        )

                    for version_i in range(1, n_per_version + 1):
                        global_i += 1
                        combo, sweep_mgv = sweep_points[global_i - 1]
                        assert sweep_mgv == mgv  # invariant: outer mg_version order
                        cmd = commands[global_i - 1]

                        invocation_id = f"invocation_{version_i:03d}"
                        invocation_dir = run_dir / invocation_id
                        invocation_dir.mkdir(parents=True, exist_ok=True)

                        for rep_i in range(1, test.repeat + 1):
                            script_idx += 1
                            rep_id = f"{rep_i:02d}"
                            rep_dir = invocation_dir / rep_id
                            rep_dir.mkdir(parents=True, exist_ok=True)
                            output_file = rep_dir / OUTPUT_FILE_NAME

                            if not proc_gen_ok:
                                invocation_ts = get_timestamp()
                                row = self._record_skipped_invocation(
                                    test, combo, mgv, invocation_id, rep_id,
                                    csv_path, csv_header, write_header,
                                    invocation_ts,
                                )
                                write_header = False
                                csv_rows.append(row)
                                results.append({
                                    "command": " ".join(cmd),
                                    "invocation_id": invocation_id,
                                    "repetition": rep_id,
                                    "mg_version": mgv,
                                    "exit_code": PROC_GEN_FAILED_EXIT_CODE,
                                    "wall_time": 0.0,
                                })
                                print(
                                    f"[madbench] SKIP ({script_idx}/{total_runs}): "
                                    f"[{invocation_id} rep={rep_id} mg_version={mgv}] — "
                                    "proc_card generation failed for this version"
                                )
                                continue

                            env = os.environ.copy()
                            env["MADBENCH_WORKDIR"] = str(rep_dir)
                            env["MADBENCH_INPUTS"] = str(inputs_dir)
                            env["MADBENCH_PROCESSES"] = str(processes_dir)
                            env["MADBENCH_OUTPUT_FILE"] = str(output_file)
                            env["MADBENCH_REPETITION"] = rep_id
                            env["MG_VERSION"] = mgv
                            env["MG_BIN"] = str(self._resolve_mg_bin(mgv) or "")

                            header = (
                                f"=== Running ({script_idx}/{total_runs}): "
                                f"{' '.join(cmd)} "
                                f"[{invocation_id} rep={rep_id} mg_version={mgv}] ==="
                            )
                            print(header)

                            invocation_ts = get_timestamp()
                            cmd_start = time.monotonic()
                            try:
                                proc = subprocess.Popen(
                                    cmd,
                                    stdout=tee.write_fd,
                                    stderr=subprocess.STDOUT,
                                    cwd=rep_dir,
                                    env=env,
                                    close_fds=True,
                                )
                                exit_code = proc.wait()
                            except KeyboardInterrupt:
                                proc.terminate()
                                proc.wait()
                                exit_code = -2
                                wall_time = time.monotonic() - cmd_start
                                row = self._finalize_invocation(
                                    test, combo, mgv, invocation_id, rep_id,
                                    rep_dir, output_file,
                                    result_version_dir, csv_path, csv_header,
                                    write_header, invocation_ts, exit_code, wall_time,
                                )
                                csv_rows.append(row)
                                results.append({
                                    "command": " ".join(cmd),
                                    "invocation_id": invocation_id,
                                    "repetition": rep_id,
                                    "mg_version": mgv,
                                    "exit_code": exit_code,
                                    "wall_time": round(wall_time, 2),
                                })
                                write_header = False
                                print("\n[madbench] Interrupted by user.")
                                raise

                            wall_time = time.monotonic() - cmd_start
                            row = self._finalize_invocation(
                                test, combo, mgv, invocation_id, rep_id,
                                rep_dir, output_file,
                                result_version_dir, csv_path, csv_header,
                                write_header, invocation_ts, exit_code, wall_time,
                            )
                            write_header = False
                            csv_rows.append(row)
                            results.append({
                                "command": " ".join(cmd),
                                "invocation_id": invocation_id,
                                "repetition": rep_id,
                                "mg_version": mgv,
                                "exit_code": exit_code,
                                "wall_time": round(wall_time, 2),
                            })

        except KeyboardInterrupt:
            print("[madbench] Interrupted — bundling partial logs...")
        finally:
            summary_csv_path: Optional[Path] = None
            if csv_rows:
                summary_csv_path = self._write_summary(test, result_dir, csv_rows)
            metadata["results"] = results
            metadata["total_wall_time"] = round(time.monotonic() - wall_start, 2)
            metadata["csv_path"] = str(csv_path)
            if summary_csv_path is not None:
                metadata["summary_csv_path"] = str(summary_csv_path)
            bundle_logs(run_log_dir, main_log, metadata, archive_path)

        total_time = time.monotonic() - wall_start
        print(f"\n[madbench] Run complete in {total_time:.1f}s")
        for r in results:
            status = "OK" if r["exit_code"] == 0 else f"FAILED (exit {r['exit_code']})"
            print(
                f"  [{status}] {r['invocation_id']} rep={r['repetition']} "
                f"mg_version={r['mg_version']}  {r['command']}  ({r['wall_time']}s)"
            )
        for mgv, rd in run_dirs.items():
            print(f"[madbench] Workdir [mg_version={mgv}]: {rd}")
        print(f"[madbench] Results CSV: {csv_path}")
        if summary_csv_path is not None:
            print(f"[madbench] Summary CSV: {summary_csv_path}")
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

    def _build_sweep_points(
        self, test: TestDefinition,
    ) -> list[tuple[dict[str, Any], str]]:
        """Cartesian (mg_version × arg_combos) with mg_version as the outer
        loop. Returns (arg_combo, mg_version) tuples in invocation order."""
        combos = self._build_arg_combos(test)
        return [(combo, mgv) for mgv in test.mg_version for combo in combos]

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

    def _version_run_dir(
        self, workdir_base: Path, test_name: str, timestamp: str, mg_version: str,
    ) -> Path:
        """Per-mg_version run dir. The version segment is dropped when
        ``mg_version`` is the "none" sentinel so version-less tests keep the
        ``<workdir>/<test>_<ts>/`` layout they always had."""
        if mg_version == MG_VERSION_NONE:
            return workdir_base / f"{test_name}_{timestamp}"
        return workdir_base / mg_version / f"{test_name}_{timestamp}"

    def _resolve_mg_bin(self, mg_version: str) -> Optional[Path]:
        """Return MadGraph/<mg_version>/bin/mg5_aMC, or None when version is
        the "none" sentinel. Does not check existence — that is the caller's
        job once MG is actually required (step 2: proc_card generation)."""
        if mg_version == MG_VERSION_NONE:
            return None
        return self.workspace.root / "MadGraph" / mg_version / "bin" / "mg5_aMC"

    def _generate_processes(
        self,
        test: TestDefinition,
        mg_version: str,
        run_dir: Path,
        tee_fd: int,
    ) -> bool:
        """Run MadGraph once per proc_card with cwd=<run_dir>/processes/.

        Returns True on success, False on any failure (missing binary, missing
        card, non-zero MG exit). Diagnostic messages are printed to stdout so
        they end up in main.log via ``tee_fd``.
        """
        mg_bin = self._resolve_mg_bin(mg_version)
        if mg_bin is None:
            print(
                "[madbench] ERROR: 'proc_cards' is set but mg_version is "
                f"'{MG_VERSION_NONE}'. Set mg_version to a MadGraph install "
                "folder to enable process generation."
            )
            return False
        if not mg_bin.exists():
            print(
                f"[madbench] ERROR: MadGraph binary not found at {mg_bin} "
                f"(required by mg_version='{mg_version}')."
            )
            return False

        processes_dir = run_dir / "processes"
        processes_dir.mkdir(parents=True, exist_ok=True)

        for card in test.proc_cards:
            card_path = (self.workspace.root / card).resolve()
            if not card_path.exists():
                print(f"[madbench] ERROR: proc_card not found: {card}")
                return False
            print(
                f"[madbench] Generating processes from {card} "
                f"(mg_version={mg_version})..."
            )
            try:
                proc = subprocess.Popen(
                    [str(mg_bin), str(card_path)],
                    stdout=tee_fd,
                    stderr=subprocess.STDOUT,
                    cwd=processes_dir,
                    close_fds=True,
                )
                exit_code = proc.wait()
            except OSError as e:
                print(f"[madbench] ERROR: failed to invoke MadGraph: {e}")
                return False
            if exit_code != 0:
                print(
                    f"[madbench] ERROR: MadGraph exited {exit_code} for "
                    f"proc_card {card} (mg_version={mg_version})."
                )
                return False
        return True

    def _version_result_dir(self, result_dir: Path, mg_version: str) -> Path:
        """Per-mg_version slice of the result dir for ``output_files`` copies.
        The version segment is dropped when ``mg_version`` is "none" so
        version-less tests keep the ``results/<group>/<invocation>/`` layout."""
        if mg_version == MG_VERSION_NONE:
            return result_dir
        return result_dir / mg_version

    def _csv_header(self, test: TestDefinition) -> list[str]:
        return (
            ["timestamp", "mg_version"]
            + list(test.args.keys())
            + list(test.outputs)
            + ["exit_code", "wall_time", "invocation_id", "repetition"]
        )

    def _summary_header(self, test: TestDefinition) -> list[str]:
        """Header for summary.csv: per-(mg_version, arg-combo) stats. Each
        numeric column from ``outputs`` (plus ``wall_time``) becomes a
        ``_mean``/``_std`` pair; ``n_successful`` records the count actually
        averaged."""
        cols = ["timestamp", "mg_version"] + list(test.args.keys())
        for k in test.outputs + ["wall_time"]:
            cols.extend([f"{k}_mean", f"{k}_std"])
        cols.extend(["n_successful", "invocation_id"])
        return cols

    def _finalize_invocation(
        self,
        test: TestDefinition,
        combo: dict[str, Any],
        mg_version: str,
        invocation_id: str,
        repetition: str,
        rep_dir: Path,
        output_file: Path,
        result_version_dir: Path,
        csv_path: Path,
        csv_header: list[str],
        write_header: bool,
        invocation_ts: str,
        exit_code: int,
        wall_time: float,
    ) -> dict[str, Any]:
        """Post-script: read outputs JSON, copy output_files, append CSV row.

        Returns the in-memory row dict so the caller can accumulate it for
        the summary stage. ``result_version_dir`` is the per-mg_version slice
        of the result dir; output_files land at ``<...>/invocation_id/repetition/``
        so repetitions of the same arg-combo don't overwrite each other.
        """
        output_values = self._read_outputs_json(test, output_file)
        self._copy_output_files(
            test, combo, rep_dir, result_version_dir / invocation_id / repetition,
        )

        row: dict[str, Any] = {
            "timestamp": invocation_ts,
            "mg_version": mg_version,
            "exit_code": exit_code,
            "wall_time": round(wall_time, 2),
            "invocation_id": invocation_id,
            "repetition": repetition,
        }
        for k in test.args:
            row[k] = combo[k]
        for k in test.outputs:
            row[k] = output_values.get(k, "")

        append_row(csv_path, csv_header, row, write_header)
        return row

    def _record_skipped_invocation(
        self,
        test: TestDefinition,
        combo: dict[str, Any],
        mg_version: str,
        invocation_id: str,
        repetition: str,
        csv_path: Path,
        csv_header: list[str],
        write_header: bool,
        invocation_ts: str,
    ) -> dict[str, Any]:
        """Write a CSV row marking a repetition that was skipped because
        proc_card generation failed for its mg_version. No script ran, so
        outputs are blank and ``exit_code`` is the proc-gen sentinel."""
        row: dict[str, Any] = {
            "timestamp": invocation_ts,
            "mg_version": mg_version,
            "exit_code": PROC_GEN_FAILED_EXIT_CODE,
            "wall_time": 0.0,
            "invocation_id": invocation_id,
            "repetition": repetition,
        }
        for k in test.args:
            row[k] = combo[k]
        for k in test.outputs:
            row[k] = ""

        append_row(csv_path, csv_header, row, write_header)
        return row

    def _write_summary(
        self,
        test: TestDefinition,
        result_dir: Path,
        csv_rows: list[dict[str, Any]],
    ) -> Path:
        """Aggregate per-rep rows into one summary row per (mg_version, args).

        Each numeric output and ``wall_time`` is averaged over **successful**
        reps only (exit_code == 0). Non-numeric output values yield empty
        mean/std cells. ``n_successful`` records how many reps contributed.
        """
        import statistics
        from collections import OrderedDict

        summary_header = self._summary_header(test)
        arg_keys = list(test.args.keys())

        groups: "OrderedDict[tuple, list[dict[str, Any]]]" = OrderedDict()
        for row in csv_rows:
            key = (row["mg_version"],) + tuple(row[k] for k in arg_keys)
            groups.setdefault(key, []).append(row)

        summary_path, write_header = select_results_csv(
            result_dir, summary_header, basename="summary",
        )

        for key, rows in groups.items():
            successful = [r for r in rows if r.get("exit_code") == 0]
            n_successful = len(successful)

            summary_row: dict[str, Any] = {
                "timestamp": rows[0]["timestamp"],
                "mg_version": key[0],
                "invocation_id": rows[0]["invocation_id"],
                "n_successful": n_successful,
            }
            for i, k in enumerate(arg_keys):
                summary_row[k] = key[i + 1]

            for col in list(test.outputs) + ["wall_time"]:
                mean_col = f"{col}_mean"
                std_col = f"{col}_std"
                values: list[float] = []
                try:
                    for r in successful:
                        v = r.get(col, "")
                        if v == "" or v is None:
                            raise ValueError("blank value")
                        values.append(float(v))
                except (TypeError, ValueError):
                    summary_row[mean_col] = ""
                    summary_row[std_col] = ""
                    continue

                if not values:
                    summary_row[mean_col] = ""
                    summary_row[std_col] = ""
                else:
                    summary_row[mean_col] = statistics.mean(values)
                    summary_row[std_col] = (
                        statistics.stdev(values) if len(values) >= 2 else ""
                    )

            append_row(summary_path, summary_header, summary_row, write_header)
            write_header = False

        return summary_path

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
        run_dirs: dict[str, Path],
        result_dir: Path,
        commands: list[list[str]],
        metadata: dict,
    ) -> None:
        import yaml as _yaml

        print("[madbench] DRY RUN — no files will be created or scripts executed")
        print(f"[madbench] Test: {test.name}")
        print(f"[madbench] Script: {script_path}")
        print(f"[madbench] mg_versions: {list(run_dirs.keys())}")
        for mgv, rd in run_dirs.items():
            print(f"[madbench] Run dir [mg_version={mgv}]: {rd}")
        print(f"[madbench] Result dir: {result_dir}")
        if test.inputs:
            print("[madbench] Inputs (staged per mg_version into <run_dir>/inputs):")
            for pat in test.inputs:
                print(f"  {pat}")
        if test.proc_cards:
            print(
                "[madbench] Proc cards (generated per mg_version into "
                "<run_dir>/processes):"
            )
            for card in test.proc_cards:
                print(f"  {card}")
        if test.outputs:
            print(f"[madbench] Outputs (CSV columns): {test.outputs}")
        if test.output_files:
            print(f"[madbench] Output files (per invocation): {test.output_files}")
        print(
            f"[madbench] Commands ({len(commands)}, each ×{test.repeat} rep"
            f"{'s' if test.repeat != 1 else ''}):"
        )
        for cmd in commands:
            print(f"  {' '.join(cmd)}")
        print("[madbench] Metadata:")
        print(_yaml.dump(metadata, default_flow_style=False, allow_unicode=True))
