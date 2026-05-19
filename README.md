# MadBench

Lightweight benchmarking framework for MadGraph performance tests.

## Overview

MadBench orchestrates standalone benchmark scripts, captures their output in
real time, bundles logs with metadata, aggregates results into a per-group
CSV, and optionally renders Plotly figures. Scripts are treated as black
boxes — MadBench never parses their stdout.

The framework supports parameter sweeps (cartesian / zipped), per-test
selection of one or more MadGraph versions, MadGraph process-directory
generation from proc-card files, and statistical repetition with automatic
mean/std aggregation.

## Installation

```bash
pip install -e .           # core only (pyyaml dependency)
pip install -e ".[plot]"   # with plotly + pandas for the plot subcommand
pip install -e ".[dev]"    # core + pytest + ruff for development
```

## Quickstart

```bash
# Initialize a new workspace
mkdir my-workspace && cd my-workspace
git init
madbench init

# Create a test script
cat > scripts/run.sh << 'EOF'
#!/bin/bash
echo "ncores=$1 nevents=$2 seed=$3"
echo "{\"throughput\": $2}" > "$MADBENCH_OUTPUT_FILE"
EOF
chmod +x scripts/run.sh

# Define a test
cat > tests/my_test.yml << 'EOF'
name: my_test
description: "Example benchmark"
script: run.sh
args:
  ncores: [1, 2, 4]
  nevents: 250000
  seed: 42
outputs: [throughput]
EOF

# Preview commands (no execution)
madbench run tests/my_test.yml --dry-run

# Run the benchmark
madbench run tests/my_test.yml

# Check available tests
madbench status

# Show a plot (requires a plots/<name>.py module)
madbench plot tests/my_test.yml
```

## Workspace structure

```
my-workspace/
├── madbench.yml      # workspace configuration
├── .gitignore
├── scripts/          # executable benchmark scripts
├── tests/            # test YAML definitions
├── plots/            # optional plot modules (.py)
├── inputs/           # cards and other input files referenced by tests
├── gridpacks/        # reusable gridpacks (own folder so they survive runs)
├── results/          # CSVs and per-rep artifacts (gitignored)
├── logs/             # captured logs + .tar.gz bundles (gitignored)
├── scratch/          # per-test working directories (gitignored)
├── analysis/
└── MadGraph/         # one folder per MadGraph install (e.g. MadGraph/v3.5.4)
```

`madbench init` is idempotent: running it inside an already-initialized
workspace (or one cloned from a remote) **does not** overwrite the existing
`madbench.yml` or `.gitignore` — it just tops up any missing folders. This
makes the typical workflow `git clone <repo> && cd <repo> && madbench init`
safe: cloned repos pick up `scratch/` and `MadGraph/` from the local init
without disturbing the version-controlled config.

## Test YAML format

Every field except `name`, `script`, and `args` is optional.

```yaml
name: my_test
description: "Human-readable description"
script: run.sh            # relative to scripts/
args:
  ncores: [1, 2, 4]       # list args expand into a sweep (cartesian by default)
  nevents: 250000         # scalars are reused on every sweep point
  seed: 42
zip: [[ncores, seed]]     # optional: zip listed args together as one axis

# I/O for the script
inputs:                   # workspace-relative paths/globs, staged into $MADBENCH_INPUTS
  - inputs/Cards/*
  - gridpacks/mg5/proc.dat
outputs: [throughput]     # scalar values the script reports via $MADBENCH_OUTPUT_FILE
artifacts:                # files the script produces, relative to $MADBENCH_WORKDIR
  - timings.txt
  - gridpack_{seed}/out.log   # {arg_name} substitution is supported

# MadGraph integration
mg_version: [v3.5.4]      # folder name(s) under MadGraph/. Sweep dimension.
proc_cards:               # workspace-relative MG proc-card files
  - inputs/proc_pp_tt.dat

# Statistics
repeat: 5                 # how many times to run each (mg_version, arg-combo)
stats: [throughput]       # optional: subset of outputs to aggregate in summary.csv
                          # (defaults to all outputs, with a warning when repeat>1)

# Misc
workdir: scratch          # base scratch path (default: workspace scratch_dir)
plot: my_plot             # optional: plots/my_plot.py
```

`args` are passed as positional CLI arguments in YAML order. Scripts receive
`$1`, `$2`, `$3`, etc. Other fields surface as environment variables (next
section).

## What MadBench gives the script

For each script execution, MadBench sets:

| Variable | Meaning |
|----------|---------|
| `MADBENCH_WORKDIR` | Per-rep working directory; cwd is set here when the script launches. Write whatever you want. |
| `MADBENCH_INPUTS` | Staged input tree (read-only by convention; on disk it lives in `<run_dir>/staged/` — distinct from the workspace's own `inputs/` so the staged tree doesn't doublename). Patterns listed under `inputs:` are copied here preserving their workspace-relative structure — `inputs/Cards/foo.dat` lands at `$MADBENCH_INPUTS/inputs/Cards/foo.dat`, `gridpacks/mg5/proc.dat` lands at `$MADBENCH_INPUTS/gridpacks/mg5/proc.dat`. Staged once **per `mg_version`** and shared across all reps of that version. |
| `MADBENCH_PROCESSES` | Per-version `processes/` directory. When `proc_cards:` is set, MadGraph emits one process folder here per card before the script runs. Always defined, even when empty. |
| `MADBENCH_OUTPUT_FILE` | Path the script may write to as a single JSON object whose keys match the declared `outputs:` labels. Per rep. MadBench reads it after the script exits and writes one column per key to the CSV. |
| `MADBENCH_REPETITION` | Zero-padded rep number for this execution (`"01"`, `"02"`, ...). Useful for seeding. |
| `MG_VERSION` | The current `mg_version` for this execution (`"none"` if unset). |
| `MG_BIN` | Path to `MadGraph/<mg_version>/bin/mg5_aMC` if a version is set; empty string otherwise. |

Use JSON to avoid separator headaches with strings — from bash:

```bash
echo "{\"throughput\": 1234, \"note\": \"$some_string\"}" > "$MADBENCH_OUTPUT_FILE"
```

If a declared output key is missing from the JSON (or the JSON wasn't
written at all), MadBench warns and writes a blank cell — the CSV row is
still recorded, with the correct `exit_code` and `wall_time`.

## Workdir / results layout

The exact layout depends on whether `mg_version` is set. With no MG version
(`mg_version` unset, the default), for a run with two sweep invocations and
`repeat: 3`:

```
scratch/my_test_20260515T140000/
    staged/                       # $MADBENCH_INPUTS (per-version) — staged copy of
        inputs/                   # workspace files, preserving the workspace-relative
            Cards/...             # path (so "inputs/Cards/*" lands at "staged/inputs/Cards/*")
        gridpacks/
            ...
    processes/                    # $MADBENCH_PROCESSES (empty unless proc_cards set)
    invocation_001/
        01/                       # $MADBENCH_WORKDIR for rep 01
            [whatever the script wrote]
            .madbench_output.json # $MADBENCH_OUTPUT_FILE (consumed by MadBench)
        02/
            ...
        03/
            ...
    invocation_002/
        01/ 02/ 03/

results/my_test/
    myLaptop_20260515T140000/            # one folder per madbench run
        results.csv                       # this run only — one row per (invocation, rep)
        summary.csv                       # this run only — one row per arg-combo
        metadata.yml                      # this run's environment (host, hardware, git_sha, …)
        invocation_001/
            01/                           # artifacts for invocation_001, rep 01
                timings.txt
            02/
            03/
        invocation_002/
            01/ 02/ 03/
```

Each `madbench run` writes only inside its own `<hostname>_<timestamp>/`
subfolder under `results/<test_name>/` — never into anything shared. Two
machines (or two consecutive runs on one machine) can push the same
`results/` into a central git repo with zero conflicts, and re-running a
test never overwrites a previous run's artifacts. Cross-run aggregation
(merging CSVs across runs) is a post-processing concern.

When `mg_version:` is set, an extra version segment is inserted under
`scratch/` and inside the per-run results folder:

```
scratch/v3.5.4/my_test_20260515T140000/
    staged/
    processes/
    invocation_001/
        01/ 02/ ...
scratch/v3.5.5/my_test_20260515T140000/
    ...

results/my_test/
    myLaptop_20260515T140000/
        results.csv
        summary.csv
        metadata.yml
        v3.5.4/
            invocation_001/
                01/ 02/ ...
        v3.5.5/
            ...
```

Invocation IDs **restart per version** — `invocation_002` under `v3.5.4`
holds the same arg-combo as `invocation_002` under `v3.5.5`, so per-version
results are directly comparable by path. The scratch workdir is left in
place after the run (you manage cleanup); the per-run results folder
holds the curated subset declared via `artifacts:` plus the CSVs and
`metadata.yml`.

## `results.csv` (per run)

Inside each `results/<test_name>/<hostname>_<timestamp>/` folder, one row per
`(invocation, rep)`. Columns, in order:

- `timestamp`
- `mg_version` — `"none"` when unset
- every `args:` key, in YAML order (including scalars)
- every `outputs:` label
- `exit_code` — `0` on success, `-2` if interrupted, `-3` if MG process
  generation failed for this version (no script ran), otherwise the
  script's own exit code
- `wall_time` — seconds, rounded to 2 decimals
- `invocation_id` — `invocation_NNN` (restarts per `mg_version`)
- `repetition` — zero-padded (`"01"`, `"02"`, ...)

`hostname` is **not** a column: every row in this file belongs to the
same run, and the host is recorded once in the sibling `metadata.yml`
(and also encoded in the folder name).

## `summary.csv` (per run)

Written automatically alongside `results.csv`. One row per
`(mg_version, arg-combo)` for **this run**, aggregating across all reps
of that combo. Columns:

- `timestamp`, `mg_version`, every `args:` key
- For each label in `stats:` **and** `wall_time`: `<name>_mean` and `<name>_std`
- `n_successful` — number of reps with `exit_code == 0` that contributed
  to the average
- `invocation_id`

Only **successful** reps (exit_code 0) are averaged. `_std` is the sample
standard deviation (n-1 denominator); empty when `n_successful < 2`. If
every rep of a combo failed, the row is still written with
`n_successful=0` and empty stats so failed combos remain visible.

### Choosing what to aggregate (`stats`)

`stats:` declares which `outputs:` labels are measurements MadBench should
average across reps. Anything not listed (e.g. a status string, a build
label, a filename) stays in `results.csv` but gets no `_mean`/`_std`
columns in `summary.csv`, keeping the summary header free of useless
blank columns.

```yaml
outputs: [throughput, latency, status]
stats:   [throughput, latency]          # `status` is a label, not a measurement
```

If `stats:` is **omitted**, MadBench falls back to "all outputs" — and
when `repeat > 1` it logs a warning into `main.log` so you know to
declare it explicitly. In that fallback path, any non-numeric value in a
successful rep also produces a `WARN: cannot aggregate stats column …`
line and leaves the `_mean`/`_std` cells blank, so silent corruption of
the summary CSV is avoided.

A column listed in `stats:` whose value isn't numeric is treated as a
likely bug — the run does **not** crash, but it warns loudly per
(column, arg-combo) so you can chase it down. `wall_time` is always
aggregated since MadBench measures it itself.

## `metadata.yml` (per run)

Sibling of `results.csv` / `summary.csv`. Records the environment that
produced this run's CSVs — host, hardware, git SHA, sweep parameters,
scratch run dirs. Each `madbench run` writes exactly one of these into
its own folder; no file is ever shared between runs.

```yaml
test_name: my_test
timestamp: 20260515T140000
hostname: myLaptop
git_sha: fc041ae
mg_versions: [v3.5.4]
repeat: 5
hardware:
  hostname: myLaptop
  fqdn: user.work
  cpu_model: Intel(R) Xeon(R) Gold 6248
  cpu_arch: x86_64
  cpu_count_logical: 40              # total threads on the host (SMT/HT included)
  cpu_count_physical: 20             # distinct physical cores
  cpu_count_available: 40            # what this process can schedule on
                                     # (= len(os.sched_getaffinity(0))); diverges
                                     # from logical inside VMs, containers, or
                                     # cgroup/cpuset/taskset slices
  platform: Linux-...
  gpus:
    - {vendor: nvidia, index: 0, name: NVIDIA A100-SXM4-80GB, memory_mb: 81920,
       driver_version: "550.54.15", compute_cap: "8.0"}
  cuda_visible_devices: "0"          # only when set
run_dirs:
  v3.5.4: scratch/v3.5.4/my_test_20260515T140000
test_yml: test.yml                  # the executed test definition is the
                                    # sibling file in this dir — see below.
retry_of: /abs/path/to/original_run_dir/   # only on retry runs
```

When aggregating across runs into a database, the per-run subfolder is
the unit: walk `results/<test_name>/*/metadata.yml` to enumerate runs, and
the `(hostname, timestamp)` pair (encoded in the folder name as
`<hostname>_<timestamp>`) is the stable key linking a row in
`results.csv` to the hardware it came from. A `retry_of:` pointer (when
present) threads a retry run back to the run it was patching up — see
the "Retrying failed runs" section below.

## `test.yml` (per run)

A verbatim, byte-for-byte copy of the test YAML used for this run,
dropped alongside `results.csv` / `metadata.yml`. It exists for two
reasons:

- **Auditability.** When the same test is run on multiple machines and
  some args are tweaked per machine (e.g. fewer `ncores` values on a
  smaller GPU), the committed `results/...` tree shows *exactly* what
  was executed — `diff tests/<name>.yml results/<name>/<run>/test.yml`
  surfaces the per-machine delta at a glance. Comments and formatting
  are preserved.
- **Retry self-containment.** `madbench retry` reads the test definition
  from this file, so renaming or editing `tests/<name>.yml` between the
  failing run and the retry doesn't break anything.

## Selecting a MadGraph version (`mg_version`)

`mg_version: [v3.5.4, v3.5.5]` is a sweep dimension that adds an outer
loop around everything else. Each entry must be a bare folder name under
`MadGraph/`; the binary is resolved as `MadGraph/<mg_version>/bin/mg5_aMC`.
The whole test — every arg-combo, every rep — runs once per version, and
the version is recorded as a column in both CSVs.

A few specifics worth knowing:

- **Omitting `mg_version`** is equivalent to `mg_version: [none]`. The
  workdir / results layout drops the version segment, `MG_VERSION` is
  exposed to the script as `"none"`, and `MG_BIN` is empty. This is the
  right setting for tests that don't run MadGraph but still want to label
  a gridpack with the commit it was built from (set `mg_version: [abc123]`
  even if MG isn't actually invoked — the label flows into the CSV and
  workdir path).
- **Existence of the MadGraph binary is only checked when `proc_cards:` is
  set.** This is deliberate so the metadata-only use case (labeling a
  gridpack with the commit it came from) works without requiring a real
  MG install.
- **Invocation IDs restart per version.** The same arg-combo lands at the
  same `invocation_id` under every version, so you can `diff` two reps
  directly: `scratch/v3.5.4/.../invocation_002/01/` vs
  `scratch/v3.5.5/.../invocation_002/01/`.

## Generating process directories (`proc_cards`)

`proc_cards:` is a list of workspace-relative paths to MadGraph proc-card
files. Before the test script runs, MadBench invokes
`MadGraph/<mg_version>/bin/mg5_aMC <card>` once per card with `cwd` set to
`<run_dir>/processes/`. Whatever directory the proc-card asks MadGraph to
produce (via `output <name>`) lands there, and the script reaches it
through `$MADBENCH_PROCESSES/<name>`.

```yaml
mg_version: [v3.5.4]
proc_cards:
  - inputs/proc_pp_tt.dat
  - inputs/proc_pp_ttg.dat
```

Behaviour to be aware of:

- Generation runs **once per (mg_version, proc_card)**, not per arg-combo
  or per rep. All reps and arg-combos for the same version share the same
  process directories.
- A non-empty `proc_cards:` requires `mg_version` to be set to something
  other than `"none"`, and the resolved `mg5_aMC` binary must exist.
- On any failure (missing binary, missing card, MG exits non-zero, etc.)
  MadBench records each invocation of the affected version with
  `exit_code = -3` in `results.csv` (the script is **not** run). Other
  `mg_version` entries in the same sweep continue independently. The MG
  error output is captured in
  `logs/<test>/<run>/<mg_version>/proc_gen/<card>.stderr.log`.

## Statistical repetitions (`repeat`)

`repeat: N` runs each `(mg_version, arg-combo)` `N` times. Every rep lands
in its own zero-padded subdirectory (`01/`, `02/`, ...) under both the
scratch invocation dir and the results invocation dir, so per-rep
artifacts and outputs never collide.

The default is `repeat: 1`, and the `01/` nesting is **always** applied,
even for single-rep runs, for layout uniformity.

The script can read the current rep from `$MADBENCH_REPETITION` (zero-padded
string, e.g. `"03"`). A common pattern is to use it as a seed:

```bash
seed=$((42 + 10#$MADBENCH_REPETITION))
```

Each rep is independent — a failure in one rep does not skip its siblings.
The `summary.csv` averages only the successful reps and surfaces the count
in `n_successful`, so partial failures are visible without polluting the
mean.

## Retrying failed runs

Some runs fail (script crashed, env was off, MadGraph hiccup). Rather
than re-running the whole sweep — which would re-do every successful
combo too and waste time — `madbench retry` replays only the failed
rows of a prior run:

```bash
madbench retry results/my_test/myLaptop_20260516T120000/
```

What happens:

- The original `results.csv` is read; every row with `exit_code != 0`
  becomes a retry unit (proc-gen failures, `exit_code = -3`, are
  included — they're eligible if you've fixed the MadGraph install
  since).
- The retry uses the **sibling `test.yml`** inside the original run's
  result dir as the source of truth — you can delete or rename the
  canonical `tests/<name>.yml` between the failing run and the retry
  and it still works. Fixes to the *script* (`scripts/<name>`) ARE
  picked up, since the script is invoked by path each time.
- The retry preserves the original `invocation_id` / `repetition` /
  `mg_version` of each replayed row, so a retried row lands at the same
  on-disk position as the original (`invocation_002/01/`, etc.). Diffing
  the retry's `stdout.log` against the original's is trivial.
- mg_versions whose original runs all passed are skipped entirely — no
  scratch dir, no proc-gen, no work.
- The retry writes to a fresh sibling under
  `results/<test_name>/<host>_<ts>_retry/` (or `_retry2`, ... if the
  basename collides). The original run dir is never mutated, so the
  failure record is preserved as evidence.
- The retry's `metadata.yml` carries
  `retry_of: /abs/path/to/original_run_dir/`, so cross-run aggregators
  can follow the chain back to the source run.

Each per-run dir gets a `failed.yml` whenever at least one row failed —
human-readable summary of which `(invocation_id, repetition,
mg_version, args)` combos went wrong with what `exit_code`. It's a
convenience for grepping; `madbench retry` itself reads `results.csv`,
which is authoritative.

```yaml
# results/my_test/myLaptop_20260516T120000/failed.yml
test_name: my_test
timestamp: 20260516T120000
hostname: myLaptop
n_total: 12
n_failed: 2
failures:
  - invocation_id: invocation_002
    repetition: "01"
    mg_version: v3.5.4
    exit_code: 1
    args: {ncores: 4, nevents: 100000}
  - invocation_id: invocation_005
    repetition: "03"
    mg_version: v3.5.4
    exit_code: -3
    args: {ncores: 16, nevents: 100000}
```

Cross-host retry works the same way: kick off the retry on a different
machine, the new dir naturally carries its own hostname, and the
`retry_of:` pointer still threads back to the source.

## Plotting (deprecated for now)

`madbench plot` is currently disabled. With the per-run results layout
every `madbench run` writes its own CSVs inside
`results/<test_name>/<hostname>_<timestamp>/`, so plotting needs a cross-run
aggregation step that hasn't been designed yet. The CLI command and the
`plot:` field on tests are still parsed (so existing test YAMLs keep
loading) but `madbench plot` prints a deprecation notice and exits. A
future release will reintroduce plotting once aggregation is settled.

## Log bundles

Each run writes its logs into
`logs/<test_name>/<hostname>_<timestamp>/` and bundles the whole
directory into a sibling `<hostname>_<timestamp>.tar.gz`.
The on-disk layout mirrors the per-rep nesting of the run dir so a row
in `main.log` ("invocation_003 rep=02 mg_version=v3.5.4 FAILED") points
directly at the file you need to open:

```
logs/<test_name>/<hostname>_<timestamp>/
├── main.log
├── metadata.yml
└── <mg_version>/              # omitted when mg_version is "none"
    ├── proc_gen/              # only when proc_cards: is set
    │   ├── <card>.stdout.log
    │   └── <card>.stderr.log
    └── invocation_NNN/
        └── RR/
            ├── stdout.log
            └── stderr.log
```

- `main.log` — only MadBench's own orchestration messages: host summary,
  one block per invocation (command, mg_version, full paths to its
  `stdout.log` and `stderr.log` so you can `tail -f` from another shell),
  and the final OK/FAILED roll-up. No subprocess output — that lives in
  the per-rep `stdout.log` / `stderr.log` so a chatty script can't drown
  out the run narrative, and parallel reps can write to disjoint files.
- `<invocation>/<rep>/stdout.log` and `stderr.log` — the script's own
  output, split. MadGraph proc-card generation gets its own
  `<mg_version>/proc_gen/<card>.{stdout,stderr}.log` per card.
- `metadata.yml` — git SHA, timestamp, test definition, commands, the
  full `hardware` block (`hostname`, `fqdn`, `cpu_model`, `cpu_arch`,
  `cpu_count_logical` / `cpu_count_physical` / `cpu_count_available`,
  `platform`, `gpus` list with vendor / index / name / memory /
  `driver_version` and — for NVIDIA — `compute_cap`, plus any
  `cuda_visible_devices` / `hip_visible_devices` overrides),
  per-execution `{exit_code, wall_time, invocation_id, repetition,
  mg_version}`, `csv_path`, `summary_csv_path`, `metadata_yml_path`
  (pointing at the per-run `metadata.yml` in
  `results/<test_name>/<hostname>_<timestamp>/`), `mg_versions`, and
  `run_dirs` (one entry per `mg_version`).

This `metadata.yml` inside the log tar is the run-time audit log; the
per-run `metadata.yml` sibling of `results.csv` is the smaller,
analysis-friendly environment snapshot you'd join your CSV rows
against.

GPU detection is best-effort: MadBench shells out to `nvidia-smi` for
NVIDIA cards and `rocm-smi --json` for AMD; if neither is on `PATH`, the
`gpus` list is just empty. If the script's view of the GPU is constrained
(`CUDA_VISIBLE_DEVICES=0` etc.), that constraint is captured separately so
the metadata reflects both "what the machine has" and "what the run could
see".

The CPU side mirrors that distinction:

- `cpu_count_logical` / `cpu_count_physical` come from parsing
  `/proc/cpuinfo` on Linux (counting `processor` lines and unique
  `(physical id, core id)` pairs respectively) and describe the **host's**
  capacity. On non-Linux platforms only `cpu_count_logical` is populated,
  from `os.cpu_count()`.
- `cpu_count_available` comes from `os.sched_getaffinity(0)` and describes
  what the **process** is actually allowed to schedule on. Inside a VM,
  container, cgroup cpuset, or `taskset` slice this can be much smaller
  than `cpu_count_logical` — e.g. on a dual-socket EPYC 9654 host
  (`logical=384`, `physical=192`) a VM allocated 46 vCPUs will record
  `available=46`. This is the right number for normalizing throughput.
- `cpu_model` is the brand string from `/proc/cpuinfo`'s `model name`
  field, captured verbatim so cross-machine result comparisons can group
  by exact silicon.

`os.cpu_count()` is deliberately **not** used as the logical count on
Linux: Python ≥3.13 makes it affinity-aware, which would silently record
the cgroup slice instead of the host's true capacity. Reading
`/proc/cpuinfo` directly sidesteps that.

## Using MadBench from Python

```python
from madbench import MadBench
from pathlib import Path

mb = MadBench()  # auto-discovers workspace from cwd
mb.run(Path("tests/my_test.yml"))
mb.run(Path("tests/my_test.yml"), dry_run=True)
tests = mb.list_tests()
```

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check src/
```
