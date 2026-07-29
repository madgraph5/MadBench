# MadBench

MadBench is a lightweight pipeline runner for reproducible MadGraph benchmarks.
It expands parameter matrices, runs ordered preparation and measurement steps,
passes files between steps as named artifacts, records structured outputs, and
aggregates repetitions of the final measurement step.

## Installation

```bash
pip install -e .
pip install -e ".[dev]"
```

Initialize a workspace with:

```bash
madbench init
```

The default workspace layout is:

```text
workspace/
├── madbench.yml
├── scripts/
├── tests/
├── inputs/
├── MadGraph/
├── scratch/
├── results/
└── logs/
```

## Pipeline overview

A test contains a global matrix and an ordered list of steps:

```yaml
name: compile_and_benchmark
description: Compile each process configuration, then scan block sizes

matrix:
  mg_version: [v3.7.1]
  process: [ggttx, ggttxg]
  proc_card:
    - inputs/proc_cards/ggttx.dat
    - inputs/proc_cards/ggttxg.dat
  backend: [cuda, cppavx2]
  blocks: [1, 2, 4, 8]

zip:
  - [process, proc_card]

inputs:
  - inputs/proc_cards/*.dat

steps:
  - id: process
    action: madgraph/process
    with:
      proc_card:
        input: ${{ matrix.proc_card }}

  - id: compile
    script: compile.sh
    with:
      process: ${{ matrix.process }}
      backend: ${{ matrix.backend }}
      process_dir: ${{ steps.process.artifacts.process_dir }}
    outputs:
      compile_seconds: number
    artifacts:
      executable:
        path: executable
        save: false
      build_log:
        path: build.log
        save: true
    cache:
      enabled: true
      inputs:
        - scripts/compile.sh

  - id: benchmark
    script: benchmark.sh
    with:
      process: ${{ matrix.process }}
      backend: ${{ matrix.backend }}
      blocks: ${{ matrix.blocks }}
      executable: ${{ steps.compile.artifacts.executable }}
    repeat: 5
    outputs:
      runtime_seconds: number
    stats: [runtime_seconds]
```

Steps execute strictly in declaration order. A step uses exactly one of
`script` or `action`.

## Work and cache placement

Each test may select its work root independently of the workspace:

```yaml
name: compile_and_benchmark
workdir: /scratch/madbench
```

MadBench then keeps both execution work and the default step cache below that
root:

```text
/scratch/madbench/
├── compile_and_benchmark_<timestamp>/  # staged inputs and step workdirs
└── .madbench-cache/
    └── compile_and_benchmark/           # reusable step caches
```

Relative `workdir` values are resolved from the workspace root. When it is
omitted, the workspace-level `workspace.scratch_dir` setting in
`madbench.yml` is used.

This setting does not move durable run records. Result JSON, CSV files, logs,
and saved artifacts remain under the workspace's configured `results_dir`.
A step-level `cache.path` still overrides the default cache location for that
step.

## Matrix expansion

List-valued matrix entries form a Cartesian product:

```yaml
matrix:
  backend: [cuda, cppavx2]
  blocks: [1, 2, 4]
```

This produces six global matrix points.

Scalar entries are constants:

```yaml
matrix:
  threads: 256
```

### Zipped dimensions

`zip` relates values by position instead of forming their Cartesian product:

```yaml
matrix:
  process: [ggttx, ggttxg]
  proc_card: [inputs/ggttx.dat, inputs/ggttxg.dat]

zip:
  - [process, proc_card]
```

This produces two pairs:

```text
ggttx  + inputs/ggttx.dat
ggttxg + inputs/ggttxg.dat
```

The short form is accepted for one group:

```yaml
zip: [process, proc_card]
```

All members of a zip group must be lists of equal length. A dimension cannot
belong to more than one zip group.

## Step dimensions are inferred

Users do not declare `dimensions` or `foreach`. MadBench infers a step's
dimensions from the matrix values referenced by `with`.

```yaml
- id: compile
  script: compile.sh
  with: [process, backend]
```

The list form is shorthand for:

```yaml
with:
  process: ${{ matrix.process }}
  backend: ${{ matrix.backend }}
```

The list order is the positional argument order.

If the global matrix contains `process`, `backend`, and `blocks`, the compile
step above runs once for every unique `(process, backend)` pair. It does not
repeat for `blocks`.

A later step can reference `blocks`:

```yaml
- id: benchmark
  script: benchmark.sh
  with:
    executable: ${{ steps.compile.artifacts.executable }}
    blocks: ${{ matrix.blocks }}
```

Its identity includes the compile step's dimensions and `blocks`. MadBench
selects the appropriate compile artifact by projecting the benchmark point
onto the compile step's dimensions.

`mg_version` is reserved. When present in the global matrix, it is an implicit
dimension of every step and is exposed through `MG_VERSION` and `MG_BIN`.

Use `madbench run TEST.yml --dry-run` to inspect the global matrix size,
inferred dimensions, execution count, repetitions, dependencies, and cache
configuration before running anything.

## Conditional steps

A step can use `if` to run only for selected matrix points:

```yaml
- id: profile
  script: profile.sh
  if: ${{ matrix.backend == 'cuda' }}
  with:
    executable: ${{ steps.compile.artifacts.executable }}
    backend: ${{ matrix.backend }}
```

Conditions support matrix values, literals, `==`, `!=`, `<`, `<=`, `>`, `>=`,
`in`, `not in`, `and`, `or`, `not`, and parentheses. Matrix values referenced
only by `if` are inferred as step dimensions.

A false condition records the execution as `skipped` without invoking its
script, action, or cache. A later execution that consumes a skipped step is
blocked, unless its own condition also skips that execution. Skipped final
steps remain in `results.csv`, along with outputs inherited from successful
earlier steps.

## Step arguments

`with` is an ordered mapping. Values are passed to scripts positionally in
that order:

```yaml
with:
  process: ${{ matrix.process }}
  threads: 256
  executable: ${{ steps.compile.artifacts.executable }}
```

The script receives:

```text
$1 = resolved process
$2 = 256
$3 = absolute path to the selected executable artifact
```

MadBench also writes the resolved name-to-value mapping to
`$MADBENCH_ARGS_FILE` as JSON.

Supported references are:

```yaml
${{ matrix.NAME }}
${{ steps.STEP_ID.outputs.NAME }}
${{ steps.STEP_ID.artifacts.NAME }}
```

A step may reference only earlier steps.

### JSON field arguments

A step argument can select any value from a staged JSON file:

```yaml
inputs:
  - inputs/config.json

steps:
  - id: prepare
    script: prepare.sh
    with:
      settings:
        from:
          json: inputs/config.json
          field: catalogue.defaults.settings
```

Unlike a JSON source under `matrix`, an argument source does not create
executions and its selected value does not need to be an array. Objects,
arrays, strings, numbers, booleans, and null are supported. Objects and arrays
are retained as structured values in `$MADBENCH_ARGS_FILE` and passed to the
script's positional argument as compact JSON.

## Inputs: staging and explicit resolution

Inputs have two related but distinct roles:

1. Top-level `inputs` declares which repository resources MadBench must stage.
2. An `input:` step argument resolves one selected logical path into an
   absolute path inside that staged tree.

This distinction is important.

### Declaring files to stage

Input declarations are workspace-relative literal paths or glob patterns:

```yaml
inputs:
  - inputs/proc_cards/*.dat
  - gridpacks/ggttx.tar.gz
```

MadBench copies matching resources into a staging tree while preserving their
workspace-relative layout:

```text
$MADBENCH_INPUTS/
├── inputs/
│   └── proc_cards/
│       ├── ggttx.dat
│       └── ggttxg.dat
└── gridpacks/
    └── ggttx.tar.gz
```

The YAML continues to use stable repository-relative logical paths such as:

```text
inputs/proc_cards/ggttx.dat
```

It must not contain machine-specific scratch paths.

### Passing a staged file explicitly

Suppose a matrix selects a process and its card:

```yaml
matrix:
  process: [ggttx, ggttxg]
  proc_card:
    - inputs/proc_cards/ggttx.dat
    - inputs/proc_cards/ggttxg.dat

zip:
  - [process, proc_card]

inputs:
  - inputs/proc_cards/*.dat
```

Resolve the selected logical path with:

```yaml
steps:
  - id: generate
    action: madgraph/process
    with:
      proc_card:
        input: ${{ matrix.proc_card }}
```

For the first matrix point, the YAML value is:

```text
inputs/proc_cards/ggttx.dat
```

The action receives:

```text
/absolute/scratch/path/staged/inputs/proc_cards/ggttx.dat
```

MadBench performs the following validation:

1. Evaluate the inner constant or matrix expression.
2. Require a relative path.
3. Reject absolute paths and parent traversal.
4. Resolve it below `$MADBENCH_INPUTS`.
5. Require the staged file or directory to exist.
6. Pass its absolute path to the script or action.

Constants work too:

```yaml
with:
  run_card:
    input: inputs/cards/common_run_card.dat
```

### Why the path appears in both `matrix` and `inputs`

They express different facts:

```yaml
matrix:
  proc_card:
    - inputs/proc_cards/ggttx.dat
    - inputs/proc_cards/ggttxg.dat

inputs:
  - inputs/proc_cards/*.dat
```

- `matrix.proc_card` selects which card belongs to an execution.
- `inputs` declares which repository files must be copied into staging.

Using a glob avoids maintaining the same file list twice.

### `$MADBENCH_INPUTS` remains available

Explicit `input:` arguments are recommended when a step consumes a specific
file or directory. They make scripts reusable outside MadBench because the
script receives an ordinary path.

`$MADBENCH_INPUTS` remains available for scripts that:

- Discover many files dynamically.
- Derive filenames from another argument.
- Use an existing naming convention.
- Need a data-root directory rather than individual arguments.

For example:

```yaml
matrix:
  process: [ggttx, ggttxg]

inputs:
  - inputs/launch_cards/*.dat

steps:
  - id: run
    script: run.sh
    with: [process]
```

The script may use:

```bash
process=$1
launch_card="$MADBENCH_INPUTS/inputs/launch_cards/${process}.dat"
```

Prefer explicit `input:` arguments for direct dependencies. Treat
`$MADBENCH_INPUTS` as the flexible bulk-access interface.

### Existing gridpacks

Existing gridpacks are ordinary staged inputs:

```yaml
matrix:
  process: [ggttx, ggttxg]
  gridpack:
    - gridpacks/ggttx.tar.gz
    - gridpacks/ggttxg.tar.gz

zip:
  - [process, gridpack]

inputs:
  - gridpacks/*.tar.gz

steps:
  - id: benchmark
    script: run_gridpack.sh
    with:
      process: ${{ matrix.process }}
      gridpack:
        input: ${{ matrix.gridpack }}
```

The script receives the selected gridpack as an absolute local path.

Remote input providers are not implemented yet. They can later download into
the same staging tree without changing step behavior.

## Scripts and built-in actions

A script step names an executable under the workspace `scripts/` directory:

```yaml
- id: benchmark
  script: benchmark.sh
```

A built-in action names behavior supplied by MadBench:

```yaml
- id: generate
  action: madgraph/process
```

`script` and `action` are mutually exclusive.

### `madgraph/cards`

This built-in action turns each process selected from a JSON file into a
paired proc card and launch card. Given `inputs/processes.json`:

```json
{
  "proc_card_preamble": [
    "set group_subprocesses Auto",
    "define lightq = u c d s u~ c~ d~ s~"
  ],
  "launch": {
    "madspin": "OFF",
    "reweight": "OFF",
    "generation.events": 10000
  },
  "processes": [
    {
      "id": "pp_jets",
      "model": "",
      "process": ["p p > j j"],
      "output": "",
      "launch": {}
    },
    {
      "id": "fcc_ee_zh",
      "model": "sm",
      "process": ["e+ e- > z h", "e+ e- > z h j"],
      "proc_card_preamble": [
        "set group_subprocesses False"
      ],
      "output": "standalone",
      "launch": {
        "beam.energy": 120,
        "generation.events": 20000
      }
    }
  ]
}
```

Load its `processes` field as a matrix dimension:

```yaml
inputs:
  - id: processes_json
    path: inputs/processes.json

matrix:
  process:
    from:
      json: ${{ inputs.processes_json }}
      field: processes

steps:
  - id: cards
    action: madgraph/cards
    with:
      process: ${{ matrix.process }}
      proc_card_preamble:
        from:
          json: ${{ inputs.processes_json }}
          field: proc_card_preamble
      default_launch:
        from:
          json: ${{ inputs.processes_json }}
          field: launch

  - id: generate
    action: madgraph/process
    with:
      proc_card: ${{ steps.cards.artifacts.proc_card }}
```

Inputs may remain plain path strings, or use an `id` and `path` mapping when
the path will be referenced more than once. `${{ inputs.processes_json }}`
resolves to that input's staged path in step arguments and JSON argument
sources; matrix JSON sources use the same label to load the workspace file
before staging. A labelled input must name one file: glob patterns and
directories are supported only by unlabelled inputs used for bulk staging.

`json` is either a labelled input expression or a safe workspace-relative
filename. `field` selects the non-empty array used for that dimension and
supports dot-separated nested fields, such as
`catalogue.madgraph.processes`. Matrix sources are loaded before MadBench
plans any executions, so dry runs and downstream dimension inference see one
`matrix.process` value per JSON entry.

JSON array elements remain intact as matrix values. Members can therefore be
used in step arguments and artifact paths, for example
`${{ matrix.process.id }}` and
`gridpacks/${{ matrix.process.output }}.tar.gz`. Member access may be nested
further and fails clearly when a member is absent or an intermediate value is
not an object.

The action requires each selected value to contain `id` and a non-empty list
of MadGraph process definitions under `process`. The first definition emits
`generate <definition>` and every later definition emits
`add process <definition>`. Definitions must not include the `generate` or
`add process` command prefixes themselves.

`model` may be omitted or empty to use MadGraph's default model without
emitting an `import model` command. A non-empty model emits
`import model <model>`.

The optional `proc_card_preamble` argument is a list of commands placed after
the model import, when present, and before the process commands. A process
inherits this root preamble when its own `proc_card_preamble` field is absent.
A per-process list replaces the root preamble completely; an empty list
explicitly selects no preamble.

The optional per-process `output` string selects the output mode: an empty or
omitted value emits `output <id>`, while `standalone`, for example, emits
`output standalone <id>`.

`launch` defaults to an empty mapping. The optional `default_launch` argument
is a mapping of settings applied to every process. Settings under the
individual process's `launch` mapping override defaults with the same name.

The action automatically declares two artifacts:

```yaml
${{ steps.cards.artifacts.proc_card }}
${{ steps.cards.artifacts.launch_card }}
```

For example, the second matrix entry produces:

```text
# proc_card.dat
import model sm
set group_subprocesses False
generate e+ e- > z h
add process e+ e- > z h j
output standalone fcc_ee_zh

# launch_card.dat
launch fcc_ee_zh
set madspin OFF
set reweight OFF
set generation.events 20000
set beam.energy 120
```

A downstream step referencing both artifacts inherits the `process` dimension.
MadBench therefore selects both files from the same card-generation execution,
while any additional downstream dimensions form the normal Cartesian product.

### `madgraph/process`

The initial built-in action invokes:

```text
MadGraph/<mg_version>/bin/mg5_aMC PROC_CARD
```

Its required argument is:

```yaml
with:
  proc_card:
    input: ${{ matrix.proc_card }}
```

The global matrix must contain `mg_version`. The action automatically declares
one artifact:

```yaml
artifacts:
  process_workspace:
    path: process_workspace
    save: false
```

MadGraph runs inside this workspace, which contains the process directory
named by the proc card's `output` command. Downstream steps can reference it
directly:

```yaml
with:
  process_workspace: ${{ steps.generate_process.artifacts.process_workspace }}
```

As with the two automatic artifacts from `madgraph/cards`, an explicit
artifact declaration with the same name overrides these defaults.

Generated MadGraph workspaces commonly contain relative symbolic links.
MadBench accepts and caches links whose targets remain inside the artifact,
preserving them when the cache is restored. Absolute links and relative links
that escape the artifact remain forbidden.

## Outputs

Outputs are small JSON-compatible values written to
`$MADBENCH_OUTPUT_FILE`.

The list form accepts any JSON value:

```yaml
outputs: [runtime, compiler]
```

The typed form validates values:

```yaml
outputs:
  runtime: number
  registers: integer
  successful: boolean
  compiler: string
```

The script writes exactly the declared keys:

```bash
printf '{
  "runtime": 1.25,
  "registers": 128,
  "successful": true,
  "compiler": "nvcc"
}' > "$MADBENCH_OUTPUT_FILE"
```

Missing or undeclared keys are errors. In results, output names are qualified
by step, for example `compile.registers` and `benchmark.runtime`.

## Artifacts

Artifacts are named files or directories produced in a step work directory:

```yaml
artifacts:
  executable:
    path: executable
    save: false
  gridpack:
    path: gridpack.tar.gz
    save: true
```

Artifact paths may contain matrix expressions. This is useful when a script
produces a meaningfully named file:

```yaml
artifacts:
  gridpack:
    path: gridpacks/${{ matrix.process }}.tar.gz
    save: true
```

Artifact-path expressions are also inferred as step dimensions. After
resolution, paths must remain relative to the step work directory and cannot
contain parent traversal.

Every artifact is available to later steps:

```yaml
with:
  executable: ${{ steps.compile.artifacts.executable }}
```

The reference resolves to an absolute local path.

`save` controls permanent retention:

- `save: false` keeps the artifact for this pipeline run and downstream steps.
- `save: true` also copies it under the permanent result directory.

A saved gridpack is a user-visible result. A cache entry is not: caches are
disposable performance optimizations and must never be the interface between
independent workflows.

## Caching

Caching is configured per step:

```yaml
cache:
  enabled: true
  version: 1
  inputs:
    - scripts/compile.sh
    - inputs/model/**
```

`cache: true` enables caching with defaults.

The cache key includes:

- Pipeline and normalized step definitions.
- Cache schema and manual `version`.
- Step matrix values and resolved arguments.
- Script contents.
- Declared cache-input contents.
- Consumed upstream output values.
- Consumed upstream artifact digests.

Unrelated global matrix dimensions are excluded because they are absent from
the step identity. A compile step that does not reference `blocks` therefore
does not compile once per block size.

On a hit, MadBench restores declared artifacts and outputs as though the step
had run. Deleting the cache changes performance, not correctness.

The default cache location is:

```text
scratch/.madbench-cache/<pipeline>/<step>/<key>/
```

It can be overridden per step with `cache.path`.

## Repetition and statistics

In the initial pipeline model, only the last step may repeat:

```yaml
- id: benchmark
  script: benchmark.sh
  repeat: 5
  outputs:
    runtime: number
  stats: [runtime]
```

Preparation and compilation steps run once for each inferred matrix identity.
Their artifacts are reused by every repetition of the final step.

Each repetition receives its own work directory, output file, logs, and
`MADBENCH_REPETITION` value.

`summary.csv` reports mean, standard deviation, successful count, and failed
count for declared `stats`. When `stats` is omitted, numeric final-step
outputs are selected.

Repeating an arbitrary range of steps is intentionally deferred.

## Environment variables

Every script and action receives:

| Variable | Meaning |
|---|---|
| `MADBENCH_WORKDIR` | Isolated directory for this step execution. |
| `MADBENCH_INPUTS` | Root of the staged, workspace-relative input tree. |
| `MADBENCH_OUTPUT_FILE` | JSON output file the step may write. |
| `MADBENCH_ARGS_FILE` | JSON mapping of resolved `with` argument names to values. |
| `MADBENCH_REPETITION` | Zero-padded repetition number. |
| `MADBENCH_STEP_ID` | Current step ID. |
| `MADBENCH_EXECUTION_ID` | Stable execution identity for the step matrix point. |
| `MG_VERSION` | Current reserved `mg_version`, or `none`. |
| `MG_BIN` | Selected MadGraph executable, or an empty string. |

Scripts run with `MADBENCH_WORKDIR` as their current directory.

## Results

Every run creates:

```text
results/<pipeline>/<hostname>_<timestamp>/
├── test.yml
├── result.json
├── results.csv
├── step_timings.csv
├── summary.csv
├── logs/
└── artifacts/
```

`result.json` is canonical. It records:

- Pipeline metadata and original matrix.
- Hardware, software, and Git revision.
- Expanded matrix points.
- Staged input paths and digests.
- Every step execution and its dimensions.
- Resolved arguments.
- Status, exit code, detailed timing, and cache status.
- Outputs and artifact digests.
- Per-execution stdout and stderr log paths.
- Repetitions and blocked dependencies.

`results.csv` is a flattened view of final-step observations. Upstream values
are repeated where necessary and use qualified column names.

`step_timings.csv` is the CI-like timing view. It contains one row for every
step execution and repetition, including its matrix dimensions, status,
cache state, and three timing fields:

- `execution_time`: time spent running the script or built-in action. It is
  empty on cache hits because nothing executed.
- `materialization_time`: time spent validating and extracting a cache hit,
  calculating restored artifact digests, and copying any `save: true`
  artifacts. It is empty when the step executes normally.
- `total_time`: complete time spent handling the step, including workdir and
  argument preparation, execution or cache restoration, output validation,
  artifact collection, and cache storage where applicable.

The same fields are retained for every execution in `result.json`. Blocked
steps have no execution or materialization duration and a zero total because
they are recorded without being scheduled.

`summary.csv` is created when the final step repeats.

## Failure behavior

If a step execution fails, downstream executions requiring it are marked
blocked. Independent matrix branches continue. Failed cache entries are not
committed.

Pipeline-aware retry is not yet implemented. Rerunning the pipeline safely
reuses successful cached steps.

## CLI

```bash
madbench run tests/example.yml --dry-run
madbench run tests/example.yml
madbench status
```

The old one-script test format remains executable during the transition but
is deprecated. New tests should use `matrix` and `steps`.
