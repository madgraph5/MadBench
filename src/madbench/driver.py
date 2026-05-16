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

from .utils import detect_hardware, format_hardware_summary, get_git_sha, get_timestamp
from .workspace import (
    WorkspaceConfig,
    find_workspace,
    resolve_plot_module,
    resolve_script,
    stage_inputs,
)
from ._logging import MainLog, bundle_logs
from .results import append_row, select_results_csv


_REQUIRED_FIELDS = {"name", "script", "args", "result_group"}

OUTPUT_FILE_NAME = ".madbench_output.json"

# Distinct exit code recorded in the CSV when a version's proc_card
# generation step failed and the script was therefore not run.
PROC_GEN_FAILED_EXIT_CODE = -3


MG_VERSION_NONE = "none"


@dataclass
class _ExecUnit:
    """One concrete (script + args + mg_version + repetition) to execute.

    ``run()`` builds these from the cartesian sweep; ``retry()`` builds them
    from the failed rows of a prior run's ``results.csv``. The IDs are
    preserved across retries so a retried row lands at the same
    ``invocation_id/rep_id`` path as the original — diffs against the
    failing run stay trivial.
    """

    invocation_id: str
    rep_id: str
    mg_version: str
    combo: dict[str, Any]
    cmd: list[str]


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
    artifacts: list[str] = field(default_factory=list)
    # Glob/literal paths (resolved relative to the script's per-rep workdir)
    # of files the script produces that should be preserved alongside the
    # results CSV — gridpacks, reports, plots, log excerpts, etc. Each
    # pattern may use ``{arg}`` placeholders that get substituted with the
    # current invocation's arg values before matching.
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
        return self._test_from_dict(raw, source=str(path))

    @staticmethod
    def _test_from_dict(raw: dict, *, source: str) -> TestDefinition:
        """Build a TestDefinition from an already-parsed YAML dict.

        Factored out of ``load_test`` so the same validation + dataclass
        construction can run against any source — e.g. a dict synthesized
        in tests, without needing a real file on disk.
        """
        missing = _REQUIRED_FIELDS - raw.keys()
        if missing:
            raise ValueError(
                f"Test definition is missing required field(s): "
                f"{', '.join(sorted(missing))}\n"
                f"Source: {source}"
            )

        return TestDefinition(
            name=raw["name"],
            description=raw.get("description", ""),
            script=raw["script"],
            args=raw["args"] or {},
            result_group=raw["result_group"],
            inputs=_as_str_list(raw.get("inputs"), "inputs"),
            outputs=_as_str_list(raw.get("outputs"), "outputs"),
            artifacts=_as_str_list(raw.get("artifacts"), "artifacts"),
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
        # Resolve to an absolute path so the verbatim copy into the
        # result dir is unambiguous even if cwd changes later.
        test_yml_abs = (
            test_path if test_path.is_absolute() else Path.cwd() / test_path
        ).resolve()

        sweep_points = self._build_sweep_points(test)
        commands = [
            [str(script_path)] + [str(combo[k]) for k in test.args]
            for combo, _ in sweep_points
        ]

        # Build the execution unit list. Each sweep point expands into
        # ``test.repeat`` units sharing the same invocation_id; mg_version
        # is the outer loop (matches the order in ``sweep_points``).
        units: list[_ExecUnit] = []
        n_per_version = len(sweep_points) // len(test.mg_version)
        for vi_global, (combo, mgv) in enumerate(sweep_points):
            version_i = (vi_global % n_per_version) + 1
            invocation_id = f"invocation_{version_i:03d}"
            cmd = [str(script_path)] + [str(combo[k]) for k in test.args]
            for rep_i in range(1, test.repeat + 1):
                units.append(_ExecUnit(
                    invocation_id=invocation_id,
                    rep_id=f"{rep_i:02d}",
                    mg_version=mgv,
                    combo=combo,
                    cmd=cmd,
                ))

        timestamp = get_timestamp()
        workdir_base = self._resolve_workdir(test)
        run_dirs = {
            mgv: self._version_run_dir(workdir_base, test.name, timestamp, mgv)
            for mgv in test.mg_version
        }

        git_sha = get_git_sha(self.workspace.root)
        hardware = detect_hardware()
        hostname = hardware["hostname"]

        result_group_dir = self.workspace.results_dir / test.result_group
        result_dir = result_group_dir / f"{test.name}_{timestamp}_{hostname}"
        run_log_dir = (
            self.workspace.logs_dir / test.name
            / f"{test.name}_{timestamp}_{hostname}"
        )

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

        self._execute_units(
            test=test,
            units=units,
            timestamp=timestamp,
            hostname=hostname,
            git_sha=git_sha,
            hardware=hardware,
            run_dirs=run_dirs,
            result_dir=result_dir,
            run_log_dir=run_log_dir,
            test_yml_source=test_yml_abs,
            metadata=metadata,
            retry_of=None,
        )

    def retry(self, original_run_dir: Path) -> None:
        """Re-run only the failed rows of a previous ``madbench run``.

        Reads ``<original_run_dir>/results.csv``, picks rows with
        ``exit_code != 0``, and replays each as a unit preserving the
        original ``invocation_id`` / ``repetition`` / ``mg_version`` so the
        retry's tree mirrors the failing slice of the original on disk.
        The retry lands in a fresh sibling under
        ``results/<group>/<test>_<retry_ts>_<host>/`` with
        ``retry_of: <original_run_dir>`` in its ``metadata.yml`` — the
        original is never mutated, and cross-host retries cohabit cleanly
        because each retry's directory name carries its own host.

        The current ``tests/<test_name>.yml`` is used (script, inputs,
        outputs, artifacts pick up any fixes you made since the failing
        run); the failing args themselves come from the CSV.
        """
        original_run_dir = original_run_dir.resolve()
        if not original_run_dir.is_dir():
            raise FileNotFoundError(
                f"Original run dir not found: {original_run_dir}"
            )

        orig_csv_path = original_run_dir / "results.csv"
        orig_test_yml = original_run_dir / "test.yml"
        if not orig_csv_path.exists():
            raise FileNotFoundError(
                f"results.csv missing in {original_run_dir} — nothing to "
                "retry from."
            )
        if not orig_test_yml.exists():
            raise FileNotFoundError(
                f"test.yml missing in {original_run_dir} — cannot rebuild "
                "the test definition to retry against. (Runs from before "
                "the sibling test.yml landed cannot be retried.)"
            )

        # Use ``load_test`` so the source path is recorded in error
        # messages — the retry's TestDefinition is built from this exact
        # file, not from any embedded metadata, so per-machine tweaks
        # made in the original run carry through.
        test = self.load_test(orig_test_yml)

        failed_rows = self._read_failed_rows(orig_csv_path, test)
        if not failed_rows:
            print(
                f"[madbench] No failed runs in {orig_csv_path}. "
                "Nothing to retry."
            )
            return

        script_path = resolve_script(self.workspace, test.script)
        units: list[_ExecUnit] = []
        for row in failed_rows:
            combo = {k: row[k] for k in test.args}
            cmd = [str(script_path)] + [str(combo[k]) for k in test.args]
            units.append(_ExecUnit(
                invocation_id=row["invocation_id"],
                rep_id=row["repetition"],
                mg_version=row["mg_version"],
                combo=combo,
                cmd=cmd,
            ))

        # Filter run_dirs to mg_versions actually present in the retry
        # so we don't spin up scratch dirs / proc_gen for versions whose
        # original runs all passed.
        retry_mg_versions = list(dict.fromkeys(u.mg_version for u in units))

        workdir_base = self._resolve_workdir(test)
        git_sha = get_git_sha(self.workspace.root)
        hardware = detect_hardware()
        hostname = hardware["hostname"]

        # ``<ts>_retry`` (with a numeric counter on collision) keeps the
        # retry's result/log/scratch dirs textually distinct from the
        # source run and prevents ``results.csv`` corruption when the
        # original and retry land in the same wall-clock second.
        timestamp = self._unique_retry_timestamp(
            test.name, hostname, retry_mg_versions, workdir_base,
        )
        run_dirs = {
            mgv: self._version_run_dir(workdir_base, test.name, timestamp, mgv)
            for mgv in retry_mg_versions
        }

        result_group_dir = self.workspace.results_dir / test.result_group
        result_dir = result_group_dir / f"{test.name}_{timestamp}_{hostname}"
        run_log_dir = (
            self.workspace.logs_dir / test.name
            / f"{test.name}_{timestamp}_{hostname}"
        )

        metadata: dict[str, Any] = {
            "test_name": test.name,
            "description": test.description,
            "git_sha": git_sha,
            "timestamp": timestamp,
            "hardware": hardware,
            "test_definition": test.raw,
            "commands": [" ".join(u.cmd) for u in units],
            "script": str(script_path),
            "mg_versions": retry_mg_versions,
            "run_dirs": {mgv: str(p) for mgv, p in run_dirs.items()},
            "result_dir": str(result_dir),
            "dry_run": False,
            "retry_of": str(original_run_dir),
            "retry_n_units": len(units),
        }

        self._execute_units(
            test=test,
            units=units,
            timestamp=timestamp,
            hostname=hostname,
            git_sha=git_sha,
            hardware=hardware,
            run_dirs=run_dirs,
            result_dir=result_dir,
            run_log_dir=run_log_dir,
            test_yml_source=orig_test_yml,
            metadata=metadata,
            retry_of=original_run_dir,
        )

    def plot(self, test_path: Path) -> None:
        """Plotting is deprecated for now.

        With the per-run result layout each ``madbench run`` produces its own
        ``<test>_<timestamp>_<hostname>/results.csv`` rather than a single
        accumulating CSV per ``result_group``. Plotting therefore needs a
        cross-run aggregation step that hasn't been designed yet, so the
        command short-circuits with a notice instead of guessing a layout.
        """
        print(
            "[madbench] 'plot' is deprecated and not currently supported. "
            "Each madbench run now writes its own results CSV under "
            "results/<group>/<test>_<timestamp>_<hostname>/; a future "
            "release will reintroduce plotting once the cross-run "
            "aggregation story is settled."
        )

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
        tee: MainLog,
        proc_gen_log_dir: Path,
    ) -> bool:
        """Run MadGraph once per proc_card with cwd=<run_dir>/processes/.

        Each card's stdout/stderr lands in
        ``<proc_gen_log_dir>/<card_stem>.{stdout,stderr}.log`` so a failing
        card is easy to pinpoint without scrolling through main.log.
        Returns True on success, False on any failure (missing binary,
        missing card, non-zero MG exit).
        """
        mg_bin = self._resolve_mg_bin(mg_version)
        if mg_bin is None:
            tee.log(
                "[madbench] ERROR: 'proc_cards' is set but mg_version is "
                f"'{MG_VERSION_NONE}'. Set mg_version to a MadGraph install "
                "folder to enable process generation."
            )
            return False
        if not mg_bin.exists():
            tee.log(
                f"[madbench] ERROR: MadGraph binary not found at {mg_bin} "
                f"(required by mg_version='{mg_version}')."
            )
            return False

        processes_dir = run_dir / "processes"
        processes_dir.mkdir(parents=True, exist_ok=True)
        proc_gen_log_dir.mkdir(parents=True, exist_ok=True)

        for card in test.proc_cards:
            card_path = (self.workspace.root / card).resolve()
            if not card_path.exists():
                tee.log(f"[madbench] ERROR: proc_card not found: {card}")
                return False
            card_stem = Path(card).stem
            stdout_log = proc_gen_log_dir / f"{card_stem}.stdout.log"
            stderr_log = proc_gen_log_dir / f"{card_stem}.stderr.log"
            tee.log(
                f"[madbench] Generating processes from {card} "
                f"(mg_version={mg_version})..."
            )
            tee.log(f"  stdout: {stdout_log}")
            tee.log(f"  stderr: {stderr_log}")
            try:
                with open(stdout_log, "w") as so, open(stderr_log, "w") as se:
                    proc = subprocess.Popen(
                        [str(mg_bin), str(card_path)],
                        stdout=so,
                        stderr=se,
                        cwd=processes_dir,
                        close_fds=True,
                    )
                    exit_code = proc.wait()
            except OSError as e:
                tee.log(f"[madbench] ERROR: failed to invoke MadGraph: {e}")
                return False
            if exit_code != 0:
                tee.log(
                    f"[madbench] ERROR: MadGraph exited {exit_code} for "
                    f"proc_card {card} (mg_version={mg_version})."
                )
                return False
        return True

    def _version_result_dir(self, result_dir: Path, mg_version: str) -> Path:
        """Per-mg_version slice of the result dir for ``artifacts`` copies.
        The version segment is dropped when ``mg_version`` is "none" so
        version-less tests keep the ``results/<group>/<invocation>/`` layout."""
        if mg_version == MG_VERSION_NONE:
            return result_dir
        return result_dir / mg_version

    def _unique_retry_timestamp(
        self,
        test_name: str,
        hostname: str,
        mg_versions: list[str],
        workdir_base: Path,
    ) -> str:
        """Pick a ``<wall_ts>_retry[N]`` timestamp that doesn't collide on
        disk anywhere a normal run would write — result dir, log dir, and
        every per-mg_version scratch dir. The ``_retry`` infix doubles as
        a textual marker so retries stand out in directory listings.
        """
        base_ts = get_timestamp()
        result_group = self.workspace.results_dir  # group joined per-test
        log_test_dir = self.workspace.logs_dir / test_name

        def _candidate(suffix: str) -> tuple[str, list[Path]]:
            ts = f"{base_ts}_retry{suffix}"
            run_basename = f"{test_name}_{ts}_{hostname}"
            paths: list[Path] = [log_test_dir / run_basename]
            for mgv in mg_versions:
                paths.append(
                    self._version_run_dir(workdir_base, test_name, ts, mgv),
                )
            # results dir: same basename, but we don't know the group
            # without test.result_group — caller already resolved
            # ``result_group_dir`` via test.result_group, so we walk every
            # group subdir to be safe (cheap; results/<group>/ is shallow).
            if result_group.exists():
                for group_dir in result_group.iterdir():
                    if group_dir.is_dir():
                        paths.append(group_dir / run_basename)
            return ts, paths

        ts, paths = _candidate("")
        if not any(p.exists() for p in paths):
            return ts
        n = 2
        while True:
            ts, paths = _candidate(str(n))
            if not any(p.exists() for p in paths):
                return ts
            n += 1

    def _version_log_dir(self, run_log_dir: Path, mg_version: str) -> Path:
        """Per-mg_version slice of the run log dir, paralleling the result
        dir layout — per-rep ``stdout.log``/``stderr.log`` live under
        ``<run_log_dir>/<mg_version>/<invocation>/<rep>/``, except the
        ``<mg_version>`` segment is dropped for the "none" sentinel."""
        if mg_version == MG_VERSION_NONE:
            return run_log_dir
        return run_log_dir / mg_version

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
        averaged. Hostname is not in this CSV — it lives once in the
        sibling metadata.yml since every row belongs to the same run."""
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
        """Post-script: read outputs JSON, copy artifacts, append CSV row.

        Returns the in-memory row dict so the caller can accumulate it for
        the summary stage. ``result_version_dir`` is the per-mg_version slice
        of the result dir; artifacts land at ``<...>/invocation_id/repetition/``
        so repetitions of the same arg-combo don't overwrite each other.
        """
        output_values = self._read_outputs_json(test, output_file)
        self._copy_artifacts(
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

    def _write_run_metadata(
        self,
        test: TestDefinition,
        timestamp: str,
        hostname: str,
        git_sha: Optional[str],
        hardware: dict,
        run_dirs: dict[str, Path],
        result_dir: Path,
        retry_of: Optional[Path] = None,
    ) -> Path:
        """Write ``metadata.yml`` inside the per-run result dir.

        One file per ``madbench run`` invocation, sibling of the run's own
        ``results.csv`` and ``summary.csv``. The per-run dir name encodes
        ``<test>_<timestamp>_<hostname>``, so the file is path-independent —
        no other run writes here, no merging needed. Cross-run aggregation
        is a post-processing concern (walk ``<result_group>/*/metadata.yml``).

        ``retry_of`` (when set) records the original run dir this is a
        retry of, so aggregators can follow the chain back."""
        import yaml as _yaml

        run_meta: dict[str, Any] = {
            "test_name": test.name,
            "timestamp": timestamp,
            "hostname": hostname,
            "git_sha": git_sha,
            "mg_versions": list(run_dirs.keys()),
            "repeat": test.repeat,
            "hardware": hardware,
            "run_dirs": {mgv: str(p) for mgv, p in run_dirs.items()},
            # The executed test definition is the sibling ``test.yml`` —
            # see ``_execute_units`` for the copy. Kept out of this file
            # to keep ``metadata.yml`` focused on the environment + run
            # bookkeeping, separate from "what was run".
            "test_yml": "test.yml",
        }
        if retry_of is not None:
            run_meta["retry_of"] = str(retry_of)
        result_dir.mkdir(parents=True, exist_ok=True)
        path = result_dir / "metadata.yml"
        path.write_text(
            _yaml.safe_dump(run_meta, sort_keys=False, allow_unicode=True),
        )
        return path

    def _read_failed_rows(
        self, csv_path: Path, test: TestDefinition,
    ) -> list[dict[str, str]]:
        """Read rows with ``exit_code != 0`` from a previous run's CSV.

        Validates that every column in ``test.args`` is present so a schema
        drift between the failing run and the current test YAML surfaces
        immediately rather than producing garbled retries.
        """
        import csv as _csv

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            header = reader.fieldnames or []
            missing = [k for k in test.args if k not in header]
            if missing:
                raise ValueError(
                    f"Cannot retry from {csv_path}: arg columns missing "
                    f"from CSV header: {missing}. The test YAML's args "
                    "must match the args of the original run."
                )
            failed: list[dict[str, str]] = []
            for row in reader:
                try:
                    code = int(row.get("exit_code", "0"))
                except ValueError:
                    code = 0
                if code != 0:
                    failed.append(row)
        return failed

    def _write_failed_summary(
        self,
        test: TestDefinition,
        result_dir: Path,
        results: list[dict[str, Any]],
        csv_rows: list[dict[str, Any]],
        timestamp: str,
        hostname: str,
    ) -> Optional[Path]:
        """Write ``failed.yml`` listing every non-zero-exit row.

        Pure human convenience — the source of truth for ``madbench retry``
        is still ``results.csv``. Returns the file path, or ``None`` when
        every row succeeded (no file is written in that case).
        """
        import yaml as _yaml

        # Pair results (which carry exit_code + command) with the CSV rows
        # (which carry the per-arg values), keyed by (invocation_id, rep_id,
        # mg_version) — unique within a run.
        row_index = {
            (r["invocation_id"], r["repetition"], r["mg_version"]): r
            for r in csv_rows
        }
        failures = []
        for r in results:
            if r["exit_code"] == 0:
                continue
            key = (r["invocation_id"], r["repetition"], r["mg_version"])
            csv_row = row_index.get(key, {})
            failures.append({
                "invocation_id": r["invocation_id"],
                "repetition": r["repetition"],
                "mg_version": r["mg_version"],
                "exit_code": r["exit_code"],
                "args": {k: csv_row.get(k, "") for k in test.args},
            })
        if not failures:
            return None

        payload = {
            "test_name": test.name,
            "timestamp": timestamp,
            "hostname": hostname,
            "n_total": len(results),
            "n_failed": len(failures),
            "failures": failures,
        }
        result_dir.mkdir(parents=True, exist_ok=True)
        path = result_dir / "failed.yml"
        path.write_text(
            _yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        )
        return path

    def _execute_units(
        self,
        *,
        test: TestDefinition,
        units: list[_ExecUnit],
        timestamp: str,
        hostname: str,
        git_sha: Optional[str],
        hardware: dict,
        run_dirs: dict[str, Path],
        result_dir: Path,
        run_log_dir: Path,
        test_yml_source: Path,
        metadata: dict[str, Any],
        retry_of: Optional[Path],
    ) -> None:
        """Shared executor for ``run()`` and ``retry()``.

        Drives a list of ``_ExecUnit``s through the same per-rep machinery
        (proc_gen per mg_version, subprocess into per-rep ``stdout.log`` /
        ``stderr.log``, CSV append, artifacts copy), then writes
        ``summary.csv`` / ``failed.yml`` / ``metadata.yml`` and bundles
        ``main.log`` + the whole log tree into a tar.gz.
        """
        # Prepare per-version run dirs + inputs + processes (once per
        # version actually exercised by this set of units).
        result_dir.mkdir(parents=True, exist_ok=True)
        run_log_dir.mkdir(parents=True, exist_ok=True)
        # Drop a verbatim copy of the test YAML into the result dir so
        # the committed artifacts make it obvious what was actually run
        # (incl. any per-machine tweaks the user made before this run) —
        # diff against the canonical tests/<name>.yml to see the delta.
        # It is also what ``retry()`` reads to rebuild the TestDefinition.
        shutil.copyfile(test_yml_source, result_dir / "test.yml")
        for mgv, rd in run_dirs.items():
            rd.mkdir(parents=True, exist_ok=True)
            inputs_dir = rd / "inputs"
            if test.inputs:
                stage_inputs(self.workspace.root, test.inputs, inputs_dir)
            else:
                inputs_dir.mkdir(parents=True, exist_ok=True)
            (rd / "processes").mkdir(parents=True, exist_ok=True)

        csv_header = self._csv_header(test)
        csv_path, write_header = select_results_csv(result_dir, csv_header)

        main_log = run_log_dir / "main.log"
        archive_path = (
            self.workspace.logs_dir / test.name
            / f"{test.name}_{timestamp}_{hostname}.tar.gz"
        )

        # mg_version groupings as they appear in units (preserves order).
        mg_versions_in_units = list(
            dict.fromkeys(u.mg_version for u in units)
        )

        results: list[dict] = []
        csv_rows: list[dict[str, Any]] = []
        summary_csv_path: Optional[Path] = None
        failed_yml_path: Optional[Path] = None
        wall_start = time.monotonic()
        total_runs = len(units)
        script_idx = 0

        try:
            with MainLog(main_log) as tee:
                tee.log(f"[madbench] Host: {format_hardware_summary(hardware)}")
                if retry_of is not None:
                    tee.log(f"[madbench] Retry of: {retry_of}")
                    tee.log(f"[madbench] Retrying {total_runs} failed unit(s)")
                for mgv in mg_versions_in_units:
                    run_dir = run_dirs[mgv]
                    inputs_dir = run_dir / "inputs"
                    processes_dir = run_dir / "processes"
                    result_version_dir = self._version_result_dir(result_dir, mgv)
                    log_version_dir = self._version_log_dir(run_log_dir, mgv)

                    proc_gen_ok = True
                    if test.proc_cards:
                        proc_gen_ok = self._generate_processes(
                            test, mgv, run_dir, tee, log_version_dir / "proc_gen",
                        )

                    for unit in units:
                        if unit.mg_version != mgv:
                            continue
                        script_idx += 1
                        invocation_id = unit.invocation_id
                        rep_id = unit.rep_id
                        combo = unit.combo
                        cmd = unit.cmd

                        rep_dir = run_dir / invocation_id / rep_id
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
                            tee.log(
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

                        rep_log_dir = log_version_dir / invocation_id / rep_id
                        rep_log_dir.mkdir(parents=True, exist_ok=True)
                        stdout_log = rep_log_dir / "stdout.log"
                        stderr_log = rep_log_dir / "stderr.log"

                        tee.log(
                            f"=== Running ({script_idx}/{total_runs}): "
                            f"{' '.join(cmd)} "
                            f"[{invocation_id} rep={rep_id} mg_version={mgv}] ==="
                        )
                        tee.log(f"  stdout: {stdout_log}")
                        tee.log(f"  stderr: {stderr_log}")

                        invocation_ts = get_timestamp()
                        cmd_start = time.monotonic()
                        try:
                            with open(stdout_log, "w") as so, \
                                    open(stderr_log, "w") as se:
                                proc = subprocess.Popen(
                                    cmd,
                                    stdout=so,
                                    stderr=se,
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
                            tee.log("\n[madbench] Interrupted by user.")
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

                # Summary + failed.yml inside the tee context so their paths
                # land in main.log alongside the OK/FAILED roll-up.
                if csv_rows:
                    summary_csv_path = self._write_summary(
                        test, result_dir, csv_rows,
                    )
                failed_yml_path = self._write_failed_summary(
                    test, result_dir, results, csv_rows, timestamp, hostname,
                )

                total_time = time.monotonic() - wall_start
                tee.log(f"\n[madbench] Run complete in {total_time:.1f}s")
                for r in results:
                    status = (
                        "OK" if r["exit_code"] == 0
                        else f"FAILED (exit {r['exit_code']})"
                    )
                    tee.log(
                        f"  [{status}] {r['invocation_id']} rep={r['repetition']} "
                        f"mg_version={r['mg_version']}  {r['command']}  "
                        f"({r['wall_time']}s)"
                    )
                for mgv, rd in run_dirs.items():
                    tee.log(f"[madbench] Workdir [mg_version={mgv}]: {rd}")
                tee.log(f"[madbench] Results CSV: {csv_path}")
                if summary_csv_path is not None:
                    tee.log(f"[madbench] Summary CSV: {summary_csv_path}")
                if failed_yml_path is not None:
                    tee.log(f"[madbench] Failed summary: {failed_yml_path}")
                tee.log(f"[madbench] Log archive: {archive_path}")
        except KeyboardInterrupt:
            print("[madbench] Interrupted — bundling partial logs...")
        finally:
            # Defensive: on partial interruption the in-tee summary/failed
            # write didn't run, but we still want the partial artifacts on
            # disk for whatever reps completed before the interrupt.
            if csv_rows and summary_csv_path is None:
                summary_csv_path = self._write_summary(
                    test, result_dir, csv_rows,
                )
            if failed_yml_path is None:
                failed_yml_path = self._write_failed_summary(
                    test, result_dir, results, csv_rows, timestamp, hostname,
                )
            metadata["results"] = results
            metadata["total_wall_time"] = round(time.monotonic() - wall_start, 2)
            metadata["csv_path"] = str(csv_path)
            if summary_csv_path is not None:
                metadata["summary_csv_path"] = str(summary_csv_path)
            if failed_yml_path is not None:
                metadata["failed_yml_path"] = str(failed_yml_path)
            metadata_yml_path = self._write_run_metadata(
                test, timestamp, hostname, git_sha, hardware,
                run_dirs, result_dir, retry_of=retry_of,
            )
            metadata["metadata_yml_path"] = str(metadata_yml_path)
            bundle_logs(run_log_dir, metadata, archive_path)

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

    def _copy_artifacts(
        self,
        test: TestDefinition,
        combo: dict[str, Any],
        invocation_dir: Path,
        dest_dir: Path,
    ) -> None:
        if not test.artifacts:
            return
        for pattern in test.artifacts:
            try:
                resolved = pattern.format_map(combo)
            except KeyError as e:
                print(
                    f"[madbench] WARN: artifacts entry {pattern!r} "
                    f"references unknown arg {e}"
                )
                continue
            src = invocation_dir / resolved
            if not src.exists():
                print(f"[madbench] WARN: declared artifact missing: {src}")
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
        print(f"[madbench] Host: {format_hardware_summary(metadata['hardware'])}")
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
        if test.artifacts:
            print(f"[madbench] Artifacts (per rep): {test.artifacts}")
        print(
            f"[madbench] Commands ({len(commands)}, each ×{test.repeat} rep"
            f"{'s' if test.repeat != 1 else ''}):"
        )
        for cmd in commands:
            print(f"  {' '.join(cmd)}")
        print("[madbench] Metadata:")
        print(_yaml.dump(metadata, default_flow_style=False, allow_unicode=True))
