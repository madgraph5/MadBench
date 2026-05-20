from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    """Entrypoint for the ``madbench`` CLI."""
    parser = argparse.ArgumentParser(
        prog="madbench",
        description="MadBench benchmarking framework for MadGraph performance tests",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # madbench init
    init_parser = subparsers.add_parser("init", help="Initialize a new workspace in the current directory")
    init_parser.add_argument(
        "target",
        nargs="?",
        type=Path,
        default=None,
        help="Target directory (default: current directory)",
    )

    # madbench run <test.yml> [--dry-run]
    run_parser = subparsers.add_parser("run", help="Run a benchmark test")
    run_parser.add_argument("test", type=Path, help="Path to test YAML file")
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without executing them",
    )
    run_parser.add_argument(
        "--note",
        type=str,
        default=None,
        help="Free-form note recorded in metadata.yml for this run",
    )

    # madbench retry <run_dir>
    retry_parser = subparsers.add_parser(
        "retry",
        help="Re-run only the failed rows of a previous run",
    )
    retry_parser.add_argument(
        "run_dir",
        type=Path,
        help=(
            "Path to the per-run results dir to retry "
            "(results/<test>/<host>_<ts>/)"
        ),
    )
    retry_parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Bypass the hardware-mismatch check; allows retrying a run on "
            "a different host than the one that produced try_0."
        ),
    )

    # madbench status
    subparsers.add_parser("status", help="List available tests and their status")

    # madbench plot <test.yml>
    plot_parser = subparsers.add_parser("plot", help="Show plot for a test result")
    plot_parser.add_argument("test", type=Path, help="Path to test YAML file")

    args = parser.parse_args()

    if args.command == "init":
        _cmd_init(args)
    elif args.command == "run":
        _cmd_run(args)
    elif args.command == "retry":
        _cmd_retry(args)
    elif args.command == "status":
        _cmd_status(args)
    elif args.command == "plot":
        _cmd_plot(args)


def _cmd_init(args: argparse.Namespace) -> None:
    from .scaffold import init_workspace

    target = args.target if args.target is not None else Path.cwd()
    init_workspace(target)


def _cmd_run(args: argparse.Namespace) -> None:
    from .driver import MadBench
    from .workspace import find_workspace

    try:
        mb = MadBench()
    except FileNotFoundError as e:
        print(f"[madbench] {e}", file=sys.stderr)
        sys.exit(1)

    try:
        mb.run(args.test, dry_run=args.dry_run, note=args.note)
    except (FileNotFoundError, PermissionError, ValueError) as e:
        print(f"[madbench] Error: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_retry(args: argparse.Namespace) -> None:
    from .driver import MadBench

    try:
        mb = MadBench()
    except FileNotFoundError as e:
        print(f"[madbench] {e}", file=sys.stderr)
        sys.exit(1)

    try:
        mb.retry(args.run_dir, force=args.force)
    except (FileNotFoundError, PermissionError, ValueError) as e:
        print(f"[madbench] Error: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_status(args: argparse.Namespace) -> None:
    from .driver import MadBench

    try:
        mb = MadBench()
    except FileNotFoundError as e:
        print(f"[madbench] {e}", file=sys.stderr)
        sys.exit(1)

    tests = mb.list_tests()
    if not tests:
        print("[madbench] No test YAML files found in tests/")
        return

    print(f"[madbench] Workspace: {mb.workspace.root}")
    print(f"[madbench] Tests ({len(tests)}):\n")
    for t in tests:
        results_tag = "[has results]" if t.get("has_results") else "[no results]"
        plot_tag = "[has plot]" if t.get("has_plot") else ""
        error_tag = f"[ERROR: {t['error']}]" if "error" in t else ""
        flags = " ".join(filter(None, [results_tag, plot_tag, error_tag]))
        print(f"  {t['name']:<30}  {flags}")
        print(f"    {t['path']}")


def _cmd_plot(args: argparse.Namespace) -> None:
    from .driver import MadBench

    try:
        mb = MadBench()
    except FileNotFoundError as e:
        print(f"[madbench] {e}", file=sys.stderr)
        sys.exit(1)

    try:
        mb.plot(args.test)
    except (FileNotFoundError, ValueError) as e:
        print(f"[madbench] Error: {e}", file=sys.stderr)
        sys.exit(1)
