from __future__ import annotations

import sys
import tarfile
import threading
from datetime import datetime
from pathlib import Path
from typing import IO, Optional

import yaml


class MainLog:
    """Mirror MadBench's own messages to both stdout and ``main.log``.

    Subprocess output is *not* routed through here — each subprocess writes
    directly to its own per-rep ``stdout.log`` / ``stderr.log``. That keeps
    ``main.log`` to the orchestration narrative (which invocation, which
    rep, OK/FAILED, paths to dig into) and makes future parallel execution
    straightforward: each worker writes to disjoint files, and only the
    single shared ``main.log`` line needs locking.

    Usage::

        with MainLog(main_log_path) as tee:
            tee.log("[madbench] starting")
    """

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self._fh: Optional[IO[str]] = None
        self._lock = threading.Lock()

    def __enter__(self) -> "MainLog":
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.log_path, "w", encoding="utf-8", buffering=1)
        return self

    def __exit__(self, *_) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def log(self, msg: str = "") -> None:
        """Write a timestamped line (newline appended) to both stdout and
        main.log.

        Each line of ``msg`` is prefixed with an ``[YYYY-MM-DD HH:MM:SS]``
        stamp so the orchestration narrative is self-dating — a glance at
        main.log tells you when each step ran and how long the gaps were.
        Blank lines are left blank (no stamp) so intentional visual
        separators survive, and multi-line messages (e.g. a dumped YAML
        block) get every content line stamped consistently.
        """
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = (
            "\n".join(f"[{ts}] {ln}" if ln else "" for ln in msg.split("\n"))
            + "\n"
        )
        with self._lock:
            sys.stdout.write(line)
            sys.stdout.flush()
            if self._fh is not None:
                self._fh.write(line)
                self._fh.flush()


def bundle_logs(
    run_log_dir: Path,
    metadata: dict,
    output_archive: Path,
    arcname: Optional[str] = None,
) -> Path:
    """Tar the per-try log directory recursively.

    Writes ``try.yml`` into ``run_log_dir`` first (the run-time audit
    record for this single try — the verbose sibling of the slim
    ``try.yml`` that lives in the results tree), then tars the tree
    with ``arcname`` as the top-level entry (defaults to
    ``run_log_dir.name``) — so extracting the archive yields a single
    self-identifying directory rather than a generic ``try_0/`` slice.
    Returns the archive path.
    """
    output_archive.parent.mkdir(parents=True, exist_ok=True)

    metadata_path = run_log_dir / "try.yml"
    with open(metadata_path, "w", encoding="utf-8") as f:
        yaml.dump(metadata, f, default_flow_style=False, allow_unicode=True)

    with tarfile.open(output_archive, "w:gz") as tar:
        tar.add(run_log_dir, arcname=arcname or run_log_dir.name)

    return output_archive
