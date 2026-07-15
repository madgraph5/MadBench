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
        test.yml                          # verbatim test definition (shared across tries)
        summary.csv                       # aggregate across every try — latest row wins
        metadata.yml                      # per-run manifest: which try ran on which hardware
        invocation_001/
            01/                           # artifacts for invocation_001, rep 01
                timings.txt               # overwritten in place if a retry re-runs this rep
            02/
            03/
        invocation_002/
            01/ 02/ 03/
        try_0/                            # the original `madbench run`
            results.csv                   # one row per (invocation, rep) — this try only
            failed.yml                    # the failures of this try (omitted if all passed)
            try.yml                       # this try's environment (host, hardware, git_sha, …)
        try_1/                            # only present after `madbench retry`
            results.csv                   # rows for the reps this retry re-ran
            failed.yml                    # what still failed; `retry_of: try_0/failed.yml`
            try.yml                       # this retry's environment; `retry_of: try_0/failed.yml`
```

Each `madbench run` creates the result folder above with `try_0/` filled
in; subsequent `madbench retry` invocations add `try_1/`, `try_2/`, … to
the *same* folder, refresh the top-level `summary.csv`, and overwrite
the affected reps' artifacts under `invocation_*/` in place. Two
machines (or two consecutive runs on one machine) can push the same
`results/` into a central git repo with zero conflicts.

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
        test.yml
        summary.csv
        metadata.yml
        v3.5.4/
            invocation_001/
                01/ 02/ ...
        v3.5.5/
            ...
        try_0/
            results.csv
            failed.yml
            try.yml
```

Invocation IDs **restart per version** — `invocation_002` under `v3.5.4`
holds the same arg-combo as `invocation_002` under `v3.5.5`, so per-version
results are directly comparable by path. The scratch workdir is left in
place after the run (you manage cleanup); the per-run results folder
holds the curated subset declared via `artifacts:` plus the CSVs,
`metadata.yml` (the manifest), and one `try_N/try.yml` per try.

## `results.csv` (per try)

Inside each `results/<test_name>/<hostname>_<timestamp>/try_N/` folder,
one row per `(invocation, rep)` that **this try** executed. The original
run writes `try_0/results.csv`; each subsequent `madbench retry` writes
its own `try_N/results.csv` covering only the reps it re-ran. Columns,
in order:

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
same run, and the host is recorded once in the try's `try.yml` (and
also encoded in the folder name).

## `summary.csv` (per run)

Written at the **top** of the result folder, refreshed after every try
so it always reflects the latest state of every rep. One row per
`(mg_version, arg-combo)`, aggregating across all reps of that combo.
When a `madbench retry` re-runs a failing rep, the retry's row replaces
the original rep's row in the aggregation (latest-wins by
`(invocation_id, repetition, mg_version)`). Columns:

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

## `try.yml` (per try)

Sibling of `try_N/results.csv` / `try_N/failed.yml`. Records the
environment that produced *this try's* CSV — host, hardware, toolchain
versions (`software`), git SHA, sweep parameters, scratch run dirs. Each `madbench run` writes
`try_0/try.yml`; each `madbench retry` writes `try_{N+1}/try.yml` with
a `retry_of:` pointer.

```yaml
test_name: my_test
timestamp: 20260515T140000
git_sha: fc041ae
mg_versions: [v3.5.4]
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
software:                            # toolchain versions on PATH; tools not
  gcc:                               # found are omitted. Matters for reproducing
    version: 11.4.0                  # gridpack generation, which shells out to
    path: /usr/bin/gcc               # the system compilers.
    raw: gcc (GCC) 11.4.0            # the selected --version line, verbatim
  g++: {version: 11.4.0, path: /usr/bin/g++, raw: "g++ (GCC) 11.4.0"}
  gfortran: {version: 11.4.0, path: /usr/bin/gfortran, raw: "GNU Fortran (GCC) 11.4.0"}
  nvcc: {version: "12.2", path: /usr/local/cuda/bin/nvcc, raw: "Cuda compilation tools, release 12.2, V12.2.140"}
  ldd: {version: "2.35", path: /usr/bin/ldd, raw: "ldd (GNU libc) 2.35"}
  madbench_python:                   # the interpreter running madbench itself,
    version: 3.11.5                  # which need not be the PATH python/python3
    path: /usr/bin/python3
run_dirs:
  v3.5.4: scratch/v3.5.4/my_test_20260515T140000
test_yml: test.yml                  # shared verbatim test definition;
                                    # see "path convention" below.
retry_of: try_0/failed.yml          # only on retry runs (try_N>0); the
                                    # previous try's failed.yml.
```

**Path convention.** Every relative path inside `try.yml` resolves
from the **result_dir root** (the folder that contains `test.yml`,
`summary.csv`, `metadata.yml`, and the `try_*/` subdirs) — **not** from
this `try.yml`'s own location. So `test_yml: test.yml` and
`retry_of: try_0/failed.yml` share the same anchor: open the result
dir, follow the path. No `..` traversal, one consistent rule for every
field that takes a path.

`hostname` is **not** a top-level key — it lives inside `hardware` so
there's a single source of truth. `repeat` isn't here either; it's part
of the verbatim `test.yml` one level up. Aggregating across runs is
straightforward: walk `results/<test_name>/*/try_*/try.yml`, and
the `(hostname, timestamp)` pair encoded in the result-folder name is
the stable key linking back to the hardware that produced the rows.
`retry_of:` (when present) threads a retry try back to the previous
try's `failed.yml` — see the "Retrying failed runs" section.

## `metadata.yml` (per run)

The per-run manifest at the top of the result folder. Pairs with
`summary.csv` as the two files you'll look at 99% of the time:
`summary.csv` for the aggregated numbers, `metadata.yml` for "who ran
what, where". Refreshed after every `run` / `retry` so it always lists
every existing try grouped by the hardware it ran on. **Note the
naming**: this file lives at the result_dir top and indexes the run
as a whole; the per-try environment snapshots are called `try.yml`
and live one level down under each `try_N/`.

```yaml
# results/my_test/myLaptop_20260515T140000/metadata.yml
test_name: my_test
n_tries: 3
hardware_index:
  - hardware:
      hostname: myLaptop
      fqdn: myLaptop.local
      cpu_model: 13th Gen Intel(R) Core(TM) i5-1345U
      cpu_arch: x86_64
      cpu_count_physical: 10
      cpu_count_logical: 12
      cpu_count_available: 12
      platform: Linux-...
      gpus: []
    tries: [try_0, try_1]
  - hardware:
      hostname: bigBox
      cpu_model: AMD EPYC 9654 96-Core Processor
      cpu_count_physical: 96
      ...
    tries: [try_2]
```

Behaviour worth knowing:

- **Grouping uses the same fingerprint as the `--force` check.** Hostname,
  fqdn, cpu_model, cpu_arch, core count, and the GPU set
  `(vendor, name, memory_mb)` are what decide whether two tries land in
  the same group or get split. The cross-host check and this manifest
  can never disagree about "is this the same machine?".
- **The hardware block per group is the verbatim dict from the *first*
  try in that group.** That makes `metadata.yml` self-contained — no
  need to open any `try_N/try.yml` to know what machine the listed
  tries belong to.
- **The tries list is in chronological order** (`try_0` before `try_1`,
  etc.), which matches the order on disk and in the `retry_of:` chain.
- **A `--force`-d cross-host retry creates a new group.** Same-host
  retries append to their group. So a glance at the file tells you
  whether anything cross-host happened during this run's lifetime.
- **Cheap and idempotent.** Rebuilt from scratch by walking
  `try_*/try.yml`, so dropping/renaming a try directory and
  re-running fixes the manifest automatically.

## `test.yml` (per run)

A verbatim, byte-for-byte copy of the test YAML used for this run,
dropped at the *top* of the result folder. One shared copy across every
try — the test definition is the same for every retry of the same run.
It exists for two reasons:

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
rows of the latest try:

```bash
madbench retry results/my_test/myLaptop_20260516T120000/
madbench retry results/my_test/myLaptop_20260516T120000/ --force  # cross-host
```

What happens:

- `retry` finds the highest existing `try_N/` inside the result dir,
  reads its `failed.yml`, and replays each `failures:` entry as a
  retry unit. Proc-gen failures (`exit_code = -3`) are eligible too —
  retry them after fixing the MadGraph install.
- The **top-level `test.yml`** inside the result dir is the source of
  truth for the test definition — you can delete or rename the
  canonical `tests/<name>.yml` between the failing run and the retry
  and it still works. Fixes to the *script* (`scripts/<name>`) ARE
  picked up, since the script is invoked by path each time.
- The retry preserves the original `invocation_id` / `repetition` /
  `mg_version` of each replayed row, so a retried rep lands at the same
  on-disk position as the original (`invocation_002/01/`, etc.) and
  overwrites its artifacts in place under the result dir's top-level
  invocation tree.
- The new try is written into `try_{N+1}/` inside the same result dir
  (no new sibling directory). The top-level `summary.csv` is
  recomputed across **every** try, with the latest row per
  `(invocation_id, repetition, mg_version)` winning, so a successful
  retry promotes a failing combo to passing in the summary
  automatically.
- mg_versions whose earlier tries' reps all passed are skipped entirely
  — no scratch dir, no proc-gen, no work.
- `try_{N+1}/try.yml` and `try_{N+1}/failed.yml` both carry
  `retry_of: try_N/failed.yml` (a path **relative** to the result_dir
  root), so the chain of tries is self-describing inside the folder.
- The scratch workdir is keyed off the **original** timestamp, so reps
  re-execute in the same `scratch/<test>_<ts>/` tree as the first run
  (recreating it if it was deleted).

### What retry mutates vs preserves

Retries are deliberately a mix of in-place overwrites (where the failed
run's contents are not useful any more) and append-only writes (where
the audit trail matters). The rules below are the contract — counting on
them is safe; everything else is not.

**Overwritten / wiped:**

- `<scratch>/<test>_<ts>/<mg_version>/invocation_NNN/RR/` — the per-rep
  scratch workdir of each **retried** rep is `rmtree`'d before the
  script re-runs, so the new attempt sees a clean directory. Failed
  scripts can leave half-written files, partial gridpacks, stale
  `.madbench_output.json`, etc. that would otherwise contaminate the
  retry. Successful reps' scratch workdirs are left alone, and so are
  the shared per-version `staged/` and `processes/` directories (those
  are re-staged / re-generated idempotently).
- `results/<test>/<host>_<ts>/invocation_NNN/RR/` — the result-side
  artifact directory for each **retried** rep is overwritten by
  `shutil.copy2` / `copytree(..., dirs_exist_ok=True)`. The retry's
  curated `artifacts:` are what live there afterwards. Reps not part
  of this retry are untouched.
- `results/<test>/<host>_<ts>/summary.csv` — recomputed from scratch
  after every try, with the latest row per
  `(invocation_id, repetition, mg_version)` winning.

**Preserved (audit trail):**

- `logs/<test>/<host>_<ts>/try_N/...` — each try writes to its own
  `try_N/` log subtree, so the failed rep's `stdout.log` / `stderr.log`
  from `try_0` survives intact when `try_1` re-runs the same rep. The
  per-try `<host>_<ts>_try{N}.tar.gz` archive is append-only — one
  more file per retry, no prior archive is ever rewritten.
- `results/<test>/<host>_<ts>/try_N/{results.csv,failed.yml,try.yml}`
  — each try's CSV / failure list / environment snapshot is final once
  written. Walking the `try_*/failed.yml` chain reconstructs the full
  failure history.

The contract in one line: **failed scratch and failed result artifacts
are not part of the audit trail — `failed.yml` + the per-try logs are.**

**Hardware check.** Before running the retry, the current host's
hardware is compared against `try_0/try.yml`'s `hardware` block. If it
doesn't match (different hostname, CPU model, core count, or GPU set),
the retry aborts with a clear error — performance tests aren't
meaningful when re-run on a different machine. Pass `--force` to
override (for the deliberate "I want to compare across machines" case);
each try's `try.yml` still records its own hardware snapshot, and the
top-level `metadata.yml` splits its `hardware_index` into one group
per machine, so a forced cross-host retry is fully auditable.

Each `try_N/` gets a `failed.yml` whenever at least one row failed —
human-readable summary of which `(invocation_id, repetition,
mg_version, args)` combos went wrong with what `exit_code`. It's both
the source of truth for `madbench retry` and a convenience for
grepping.

```yaml
# results/my_test/myLaptop_20260516T120000/try_0/failed.yml
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

A `try_1/failed.yml` (and beyond) gets an additional `retry_of:
try_0/failed.yml` header pointing back at the previous try's failures,
so the chain is walkable from inside any try.

## Plotting (deprecated for now)

`madbench plot` is currently disabled. With the per-run results layout
every `madbench run` writes its own CSVs inside
`results/<test_name>/<hostname>_<timestamp>/`, so plotting needs a cross-run
aggregation step that hasn't been designed yet. The CLI command and the
`plot:` field on tests are still parsed (so existing test YAMLs keep
loading) but `madbench plot` prints a deprecation notice and exits. A
future release will reintroduce plotting once aggregation is settled.

## Log bundles

Each try writes its logs into
`logs/<test_name>/<hostname>_<timestamp>/try_N/` and bundles that try's
subtree into a per-try archive `<hostname>_<timestamp>_try{N}.tar.gz`
under `logs/<test_name>/`. The on-disk layout mirrors the result-dir's
`try_N/` structure so a row in `main.log` ("invocation_003 rep=02
mg_version=v3.5.4 FAILED") points directly at the file you need to
open — and old failure logs are preserved naturally because each retry
writes a fresh `try_{N+1}/` subtree instead of overwriting the
previous one.

```
logs/<test_name>/<hostname>_<timestamp>/
├── try_0/                     # the original `madbench run`
│   ├── main.log
│   ├── try.yml
│   └── <mg_version>/          # omitted when mg_version is "none"
│       ├── proc_gen/          # only when proc_cards: is set
│       │   ├── <card>.stdout.log
│       │   └── <card>.stderr.log
│       └── invocation_NNN/
│           └── RR/
│               ├── stdout.log
│               └── stderr.log
└── try_1/                     # only present after `madbench retry`
    ├── main.log
    └── ...                    # logs for the reps this retry re-ran
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
- `try.yml` — the run-time audit log for this try (distinct from the
  slim `try.yml` in the results tree — same name, different schema,
  different file tree). Carries git SHA, timestamp, test definition,
  commands, the full `hardware` block (`hostname`, `fqdn`, `cpu_model`,
  `cpu_arch`, `cpu_count_logical` / `cpu_count_physical` /
  `cpu_count_available`, `platform`, `gpus` list with vendor / index /
  name / memory / `driver_version` and — for NVIDIA — `compute_cap`,
  plus any `cuda_visible_devices` / `hip_visible_devices` overrides),
  per-execution `{exit_code, wall_time, invocation_id, repetition,
  mg_version}`, `csv_path`, `summary_csv_path`, `try_yml_path`
  (pointing at the slim per-try `try.yml` in
  `results/<test_name>/<hostname>_<timestamp>/try_N/`),
  `metadata_yml_path` (pointing at the run manifest one level up),
  `mg_versions`, and `run_dirs` (one entry per `mg_version`).

The `try.yml` inside the log tar is the run-time audit log; the
slim `try.yml` sibling of `results.csv` in the results tree is the
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
