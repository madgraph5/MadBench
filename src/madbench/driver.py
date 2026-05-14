from __future__ import annotations

import importlib.util
import itertools
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .utils import detect_hardware, get_git_sha, get_timestamp
from .workspace import WorkspaceConfig, find_workspace, resolve_configs, resolve_plot_module, resolve_script
from ._logging import TeeLogger, bundle_logs


_REQUIRED_FIELDS = {"name", "script", "args", "result_group"}


@dataclass
class TestDefinition:
    """Parsed content of a test YAML file."""

    name: str
    description: str
    script: str
    configs: list[str]
    args: dict[str, Any]       # values can be scalars or lists
    result_group: str
    plot: Optional[str]
    raw: dict                  # the full parsed YAML, stored in metadata
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
            configs=raw.get("configs", []) or [],
            args=raw["args"] or {},
            result_group=raw["result_group"],
            plot=raw.get("plot"),
            raw=raw,
            zip_groups=_normalize_zip_groups(raw.get("zip")),
        )

    def build_commands(self, test: TestDefinition) -> list[list[str]]:
        """Build the list of commands to execute.

        Arguments are passed as positional args in the order they appear
        in the YAML ``args`` dict. List-valued args produce one command per
        value; multiple list-valued args produce the cartesian product.

        Args listed in a ``zip`` group vary together (one axis of the
        product) instead of independently. Members of a zip group must all
        be lists of equal length.

        Example — pure cartesian::

            args:
              ncores: [1, 2]
              nevents: [100, 200]
              seed: 42

        Produces 4 commands: (1, 100, 42), (1, 200, 42), (2, 100, 42),
        (2, 200, 42).

        Example — zip group + cartesian::

            args:
              ncores: [1, 2, 4]
              nevents: [1000, 1000000]
              timeout: [10, 600]
              seed: 42
            zip: [nevents, timeout]

        Produces 3 * 2 = 6 commands; ``nevents`` and ``timeout`` always
        appear paired ((1000, 10) or (1000000, 600)).
        """
        script_path = resolve_script(self.workspace, test.script)

        self._validate_zip_groups(test)

        # Map each zipped arg name to its group index; zipped args do not
        # form independent axes.
        name_to_group: dict[str, int] = {}
        for i, group in enumerate(test.zip_groups):
            for name in group:
                name_to_group[name] = i

        # Build axes in args insertion order. A zip group is placed at the
        # position of its first member; later members of the same group are
        # absorbed. Each axis yields a list of {arg_name: value} mappings.
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
            # scalars contribute no axis

        commands = []
        for combo in itertools.product(*axes):
            overrides: dict[str, Any] = {}
            for piece in combo:
                overrides.update(piece)
            positional = [
                str(overrides.get(k, v)) for k, v in test.args.items()
            ]
            commands.append([str(script_path)] + positional)

        return commands

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

    def run(self, test_path: Path, dry_run: bool = False) -> None:
        """Main entry point. Loads the test, builds commands, runs them."""
        test = self.load_test(test_path)

        # Resolve script and configs early (fail fast)
        script_path = resolve_script(self.workspace, test.script)
        config_paths = resolve_configs(self.workspace, test.configs)

        commands = self.build_commands(test)

        # Prepare directories
        result_dir = self.workspace.results_dir / test.result_group
        timestamp = get_timestamp()
        run_log_dir = self.workspace.logs_dir / f"{test.name}_{timestamp}"

        # Gather metadata
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
            "configs": [str(p) for p in config_paths],
            "result_dir": str(result_dir),
            "dry_run": dry_run,
        }

        if dry_run:
            print("[madbench] DRY RUN — no files will be created or scripts executed")
            print(f"[madbench] Test: {test.name}")
            print(f"[madbench] Script: {script_path}")
            if config_paths:
                print(f"[madbench] Configs: {[str(p) for p in config_paths]}")
            print(f"[madbench] Result dir: {result_dir}")
            print(f"[madbench] Commands ({len(commands)}):")
            for cmd in commands:
                print(f"  {' '.join(cmd)}")
            print(f"[madbench] Metadata:")
            import yaml as _yaml
            print(_yaml.dump(metadata, default_flow_style=False, allow_unicode=True))
            return

        # Create directories
        result_dir.mkdir(parents=True, exist_ok=True)
        run_log_dir.mkdir(parents=True, exist_ok=True)

        main_log = run_log_dir / "main.log"
        archive_path = self.workspace.logs_dir / f"{test.name}_{timestamp}.tar.gz"

        results: list[dict] = []
        wall_start = time.monotonic()

        try:
            with TeeLogger(main_log) as tee:
                for i, cmd in enumerate(commands, 1):
                    header = f"=== Running ({i}/{len(commands)}): {' '.join(cmd)} ==="
                    print(header)

                    cmd_start = time.monotonic()
                    try:
                        proc = subprocess.Popen(
                            cmd,
                            stdout=tee.write_fd,
                            stderr=subprocess.STDOUT,
                            close_fds=True,
                        )
                        exit_code = proc.wait()
                    except KeyboardInterrupt:
                        proc.terminate()
                        proc.wait()
                        exit_code = -2
                        print("\n[madbench] Interrupted by user.")
                        results.append({"command": " ".join(cmd), "exit_code": exit_code, "wall_time": time.monotonic() - cmd_start})
                        raise

                    wall_time = time.monotonic() - cmd_start
                    results.append({"command": " ".join(cmd), "exit_code": exit_code, "wall_time": round(wall_time, 2)})

        except KeyboardInterrupt:
            print("[madbench] Interrupted — bundling partial logs...")
        finally:
            metadata["results"] = results
            metadata["total_wall_time"] = round(time.monotonic() - wall_start, 2)
            bundle_logs(run_log_dir, main_log, metadata, archive_path)

        # Summary
        total_time = time.monotonic() - wall_start
        print(f"\n[madbench] Run complete in {total_time:.1f}s")
        for r in results:
            status = "OK" if r["exit_code"] == 0 else f"FAILED (exit {r['exit_code']})"
            print(f"  [{status}] {r['command']}  ({r['wall_time']}s)")
        print(f"[madbench] Log archive: {archive_path}")

    def plot(self, test_path: Path) -> None:
        """Load the test, find its plot module, import it, call plot()
        with the result path, and display the figure with plotly.io.show().
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
                # Malformed YAML — still report the file
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
