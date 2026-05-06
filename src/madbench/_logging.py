from __future__ import annotations

import subprocess
import sys
import tarfile
import threading
from pathlib import Path
from typing import IO, Optional

import yaml


class TeeLogger:
    """Captures subprocess stdout+stderr, writes to a log file AND
    forwards to the real stdout/stderr in real time.

    Usage::

        with TeeLogger(log_path) as tee:
            proc = subprocess.Popen(cmd, stdout=tee.write_fd, stderr=subprocess.STDOUT)
            proc.wait()

    Implementation: opens a pipe; a background thread reads from the read end,
    writing each line to both the log file and sys.stdout so output appears
    in real time.
    """

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self._log_file: Optional[IO[str]] = None
        self._thread: Optional[threading.Thread] = None
        self._read_fd: Optional[int] = None
        self.write_fd: Optional[int] = None  # give this to Popen as stdout=

    def __enter__(self) -> "TeeLogger":
        self._log_file = open(self.log_path, "w", encoding="utf-8", buffering=1)
        self._read_fd, self.write_fd = _make_pipe()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()
        return self

    def _drain(self) -> None:
        """Read lines from the pipe and echo to both log file and stdout."""
        import os

        with os.fdopen(self._read_fd, "r", encoding="utf-8", errors="replace") as pipe:
            for line in pipe:
                sys.stdout.write(line)
                sys.stdout.flush()
                if self._log_file:
                    self._log_file.write(line)
                    self._log_file.flush()

    def __exit__(self, *_) -> None:
        # Close the write end so the drain thread sees EOF
        if self.write_fd is not None:
            try:
                import os
                os.close(self.write_fd)
            except OSError:
                pass
            self.write_fd = None
        if self._thread is not None:
            self._thread.join()
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None


def _make_pipe() -> tuple[int, int]:
    """Return (read_fd, write_fd) os-level pipe."""
    import os
    return os.pipe()


def bundle_logs(
    log_dir: Path,
    main_log: Path,
    metadata: dict,
    output_archive: Path,
) -> Path:
    """Create a tar.gz containing:

    - main.log  (the captured output)
    - metadata.yml  (git sha, timestamp, test definition, commands run)
    - any additional files found in log_dir (sub-logs)

    Returns the path to the created archive.
    """
    output_archive.parent.mkdir(parents=True, exist_ok=True)

    # Write metadata.yml to a temp location inside log_dir
    metadata_path = log_dir / "metadata.yml"
    with open(metadata_path, "w", encoding="utf-8") as f:
        yaml.dump(metadata, f, default_flow_style=False, allow_unicode=True)

    with tarfile.open(output_archive, "w:gz") as tar:
        if main_log.exists():
            tar.add(main_log, arcname="main.log")
        if metadata_path.exists():
            tar.add(metadata_path, arcname="metadata.yml")

        # Include any additional files in log_dir (sub-logs, etc.)
        for extra in sorted(log_dir.iterdir()):
            if extra == main_log or extra == metadata_path:
                continue
            if extra.is_file():
                tar.add(extra, arcname=extra.name)

    return output_archive
