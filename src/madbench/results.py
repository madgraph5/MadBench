from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

RESULTS_BASENAME = "results"
RESULTS_EXT = ".csv"


def select_results_csv(
    result_dir: Path,
    expected_header: list[str],
    basename: str = RESULTS_BASENAME,
) -> tuple[Path, bool]:
    """Pick the right ``<basename>.csv`` to append to for ``expected_header``.

    Returns ``(path, must_write_header)``. The selected file is either:

    - ``<basename>.csv`` if it doesn't yet exist (header to be written),
    - ``<basename>.csv`` if its existing header matches ``expected_header``,
    - the first ``<basename>.N.csv`` (N >= 2) whose header matches, or
    - the next unused ``<basename>.N.csv`` if none match (header to be written).

    This keeps every CSV self-consistent: when the test's args or outputs
    change between runs, the new schema rolls over to a fresh file instead
    of silently misaligning columns.
    """
    primary = result_dir / f"{basename}{RESULTS_EXT}"
    if not primary.exists():
        return primary, True

    if _read_header(primary) == expected_header:
        return primary, False

    n = 2
    while True:
        candidate = result_dir / f"{basename}.{n}{RESULTS_EXT}"
        if not candidate.exists():
            return candidate, True
        if _read_header(candidate) == expected_header:
            return candidate, False
        n += 1


def append_row(
    csv_path: Path,
    header: list[str],
    row: dict,
    write_header: bool,
) -> None:
    """Append one row to ``csv_path``, optionally writing the header first.

    ``csv.DictWriter`` handles quoting, so values containing commas, quotes,
    or newlines round-trip safely. Unknown keys in ``row`` are ignored;
    declared keys missing from ``row`` get empty cells.
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if write_header else "a"
    with open(csv_path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in header})


def _read_header(csv_path: Path) -> Optional[list[str]]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            return next(reader)
        except StopIteration:
            return None
