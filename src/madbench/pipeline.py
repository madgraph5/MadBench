from __future__ import annotations

import csv
import hashlib
import itertools
import json
import os
import re
import shutil
import statistics
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .utils import (
    detect_hardware,
    detect_software_versions,
    get_git_sha,
    get_timestamp,
)
from .workspace import WorkspaceConfig, resolve_script, stage_inputs


OUTPUT_FILE_NAME = ".madbench_output.json"
ARGS_FILE_NAME = ".madbench_args.json"
STAGED_DIR_NAME = "staged"
MG_VERSION_NONE = "none"
EXPR_RE = re.compile(r"^\s*\$\{\{\s*([^}]+?)\s*\}\}\s*$")
MATRIX_REF_RE = re.compile(r"^matrix\.([A-Za-z_][A-Za-z0-9_]*)$")
STEP_REF_RE = re.compile(
    r"^steps\.([A-Za-z_][A-Za-z0-9_-]*)\.(outputs|artifacts)"
    r"\.([A-Za-z_][A-Za-z0-9_-]*)$"
)
INPUT_REF_RE = re.compile(r"^inputs\.([A-Za-z_][A-Za-z0-9_-]*)$")


@dataclass(frozen=True)
class InputArgument:
    """A value which must resolve below the staged input root."""

    value: Any


@dataclass
class ArtifactDefinition:
    path: str
    save: bool = False


@dataclass
class CacheDefinition:
    enabled: bool = False
    inputs: list[str] = field(default_factory=list)
    version: Any = 1
    path: Optional[str] = None


@dataclass
class StepDefinition:
    id: str
    script: Optional[str]
    action: Optional[str]
    arguments: dict[str, Any]
    outputs: dict[str, Optional[str]]
    artifacts: dict[str, ArtifactDefinition]
    cache: CacheDefinition
    repeat: int
    stats: list[str]
    needs: list[str]
    raw: dict[str, Any]
    direct_dimensions: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    upstream_steps: list[str] = field(default_factory=list)


@dataclass
class PipelineDefinition:
    name: str
    description: str
    matrix: dict[str, Any]
    zip_groups: list[list[str]]
    inputs: list[str]
    steps: list[StepDefinition]
    workdir: Optional[str]
    raw: dict[str, Any]


@dataclass
class StepExecution:
    step: StepDefinition
    values: dict[str, Any]
    identity: str
    repetition: int = 1


@dataclass
class ExecutionResult:
    step_id: str
    identity: str
    dimensions: dict[str, Any]
    repetition: int
    status: str
    exit_code: Optional[int]
    wall_time: float
    cache: str
    arguments: dict[str, Any]
    outputs: dict[str, Any]
    artifacts: dict[str, str]
    artifact_digests: dict[str, str]
    workdir: str
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    blocked_by: list[str] = field(default_factory=list)
    saved_artifacts: dict[str, str] = field(default_factory=dict)


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"{field_name!r} must be a list of strings")
    return list(value)


def _positive_int(value: Any, field_name: str) -> int:
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name!r} must be a positive integer")
    return value


def _normalize_zip_groups(value: Any) -> list[list[str]]:
    if not value:
        return []
    if not isinstance(value, list):
        raise ValueError("'zip' must be a list")
    if all(isinstance(x, str) for x in value):
        return [list(value)]
    if all(
        isinstance(group, list)
        and group
        and all(isinstance(x, str) for x in group)
        for group in value
    ):
        return [list(group) for group in value]
    raise ValueError(
        "'zip' must be a list of matrix names or a list of non-empty groups"
    )


def _normalize_outputs(value: Any, step_id: str) -> dict[str, Optional[str]]:
    if value is None:
        return {}
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return {x: None for x in value}
    if isinstance(value, dict):
        allowed = {"number", "integer", "boolean", "string"}
        normalized: dict[str, Optional[str]] = {}
        for name, output_type in value.items():
            if not isinstance(name, str):
                raise ValueError(f"step {step_id!r} output names must be strings")
            if output_type is not None and output_type not in allowed:
                raise ValueError(
                    f"step {step_id!r} output {name!r} has unsupported type "
                    f"{output_type!r}; expected one of {sorted(allowed)}"
                )
            normalized[name] = output_type
        return normalized
    raise ValueError(
        f"step {step_id!r} 'outputs' must be a list or a name-to-type mapping"
    )


def _normalize_artifacts(value: Any, step_id: str) -> dict[str, ArtifactDefinition]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"step {step_id!r} 'artifacts' must be a mapping")
    result: dict[str, ArtifactDefinition] = {}
    for name, spec in value.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"step {step_id!r} artifact names must be strings")
        if isinstance(spec, str):
            spec = {"path": spec}
        if not isinstance(spec, dict) or not isinstance(spec.get("path"), str):
            raise ValueError(
                f"step {step_id!r} artifact {name!r} requires a string 'path'"
            )
        save = spec.get("save", False)
        if not isinstance(save, bool):
            raise ValueError(
                f"step {step_id!r} artifact {name!r} 'save' must be boolean"
            )
        path = Path(spec["path"])
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(
                f"step {step_id!r} artifact {name!r} path must be safe and relative"
            )
        result[name] = ArtifactDefinition(path=spec["path"], save=save)
    return result


def _normalize_cache(value: Any, step_id: str) -> CacheDefinition:
    if value is None or value is False:
        return CacheDefinition()
    if value is True:
        return CacheDefinition(enabled=True)
    if not isinstance(value, dict):
        raise ValueError(f"step {step_id!r} 'cache' must be boolean or a mapping")
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"step {step_id!r} cache.enabled must be boolean")
    path = value.get("path")
    if path is not None and not isinstance(path, str):
        raise ValueError(f"step {step_id!r} cache.path must be a string")
    return CacheDefinition(
        enabled=enabled,
        inputs=_string_list(value.get("inputs"), f"steps.{step_id}.cache.inputs"),
        version=value.get("version", 1),
        path=path,
    )


def _normalize_argument_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"input"}:
        return InputArgument(_normalize_argument_value(value["input"]))
    if isinstance(value, list):
        return [_normalize_argument_value(x) for x in value]
    if isinstance(value, dict):
        return {k: _normalize_argument_value(v) for k, v in value.items()}
    return value


def _normalize_arguments(value: Any, step_id: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, list):
        if not all(isinstance(x, str) for x in value):
            raise ValueError(f"step {step_id!r} 'with' shorthand must be strings")
        if len(set(value)) != len(value):
            raise ValueError(f"step {step_id!r} 'with' contains duplicate names")
        return {name: f"${{{{ matrix.{name} }}}}" for name in value}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) for k in value
    ):
        raise ValueError(f"step {step_id!r} 'with' must be a list or mapping")
    return {k: _normalize_argument_value(v) for k, v in value.items()}


def _extract_references(value: Any) -> tuple[list[str], list[str], list[str]]:
    matrix: list[str] = []
    steps: list[str] = []
    inputs: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, InputArgument):
            visit(item.value)
            return
        if isinstance(item, str):
            match = EXPR_RE.match(item)
            if not match:
                return
            expression = match.group(1).strip()
            matrix_match = MATRIX_REF_RE.match(expression)
            step_match = STEP_REF_RE.match(expression)
            input_match = INPUT_REF_RE.match(expression)
            if matrix_match:
                matrix.append(matrix_match.group(1))
            elif step_match:
                steps.append(step_match.group(1))
            elif input_match:
                inputs.append(input_match.group(1))
            else:
                raise ValueError(f"unsupported expression: {item!r}")
            return
        if isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)

    visit(value)
    return list(dict.fromkeys(matrix)), list(dict.fromkeys(steps)), list(
        dict.fromkeys(inputs)
    )


def parse_pipeline(raw: dict[str, Any], *, source: str) -> PipelineDefinition:
    """Validate and normalize a pipeline-style MadBench definition."""
    if not isinstance(raw, dict):
        raise ValueError(f"Pipeline definition must be a mapping\nSource: {source}")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"Pipeline requires a non-empty 'name'\nSource: {source}")
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ValueError("'description' must be a string")
    workdir = raw.get("workdir")
    if workdir is not None and not isinstance(workdir, str):
        raise ValueError("'workdir' must be a string path")
    matrix = raw.get("matrix", {}) or {}
    if not isinstance(matrix, dict) or not all(
        isinstance(k, str) for k in matrix
    ):
        raise ValueError("'matrix' must be a mapping")
    for key, value in matrix.items():
        if isinstance(value, list) and not value:
            raise ValueError(f"matrix dimension {key!r} cannot be empty")

    zip_groups = _normalize_zip_groups(raw.get("zip"))
    seen_zip: set[str] = set()
    for group in zip_groups:
        lengths: set[int] = set()
        for key in group:
            if key in seen_zip:
                raise ValueError(f"matrix dimension {key!r} is in multiple zip groups")
            seen_zip.add(key)
            if key not in matrix:
                raise ValueError(f"zip references unknown matrix dimension {key!r}")
            if not isinstance(matrix[key], list):
                raise ValueError(f"zipped matrix dimension {key!r} must be a list")
            lengths.add(len(matrix[key]))
        if len(lengths) != 1:
            raise ValueError(f"zip group {group!r} has mismatched lengths")

    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("'steps' must be a non-empty list")
    steps: list[StepDefinition] = []
    ids: set[str] = set()
    for index, step_raw in enumerate(raw_steps):
        if not isinstance(step_raw, dict):
            raise ValueError(f"step at index {index} must be a mapping")
        step_id = step_raw.get("id")
        if not isinstance(step_id, str) or not re.match(
            r"^[A-Za-z_][A-Za-z0-9_-]*$", step_id
        ):
            raise ValueError(f"step at index {index} requires a valid 'id'")
        if step_id in ids:
            raise ValueError(f"duplicate step id {step_id!r}")
        ids.add(step_id)
        script = step_raw.get("script")
        action = step_raw.get("action")
        if (script is None) == (action is None):
            raise ValueError(
                f"step {step_id!r} must declare exactly one of 'script' or 'action'"
            )
        if script is not None and not isinstance(script, str):
            raise ValueError(f"step {step_id!r} script must be a string")
        if action is not None and action != "madgraph/process":
            raise ValueError(
                f"step {step_id!r} uses unknown action {action!r}; "
                "available actions: ['madgraph/process']"
            )
        arguments = _normalize_arguments(step_raw.get("with"), step_id)
        if action == "madgraph/process":
            if "mg_version" not in matrix:
                raise ValueError(
                    "action 'madgraph/process' requires a 'mg_version' "
                    "matrix dimension"
                )
            if "proc_card" not in arguments:
                raise ValueError(
                    "action 'madgraph/process' requires 'with.proc_card'"
                )
        direct_dimensions, referenced_steps, referenced_inputs = _extract_references(
            arguments
        )
        unknown_dimensions = set(direct_dimensions) - set(matrix)
        if unknown_dimensions:
            raise ValueError(
                f"step {step_id!r} references unknown matrix dimensions "
                f"{sorted(unknown_dimensions)}"
            )
        if referenced_inputs:
            raise ValueError(
                "'inputs.<name>' expressions are not part of the staging model; "
                "declare staged paths under 'inputs' and use an 'input:' argument"
            )
        unknown_steps = set(referenced_steps) - ids
        if unknown_steps:
            raise ValueError(
                f"step {step_id!r} references steps that are not earlier in the "
                f"pipeline: {sorted(unknown_steps)}"
            )
        needs = _string_list(step_raw.get("needs"), f"steps.{step_id}.needs")
        unknown_needs = set(needs) - ids
        if unknown_needs:
            raise ValueError(
                f"step {step_id!r} needs steps that are not earlier: "
                f"{sorted(unknown_needs)}"
            )
        repeat = _positive_int(step_raw.get("repeat"), f"steps.{step_id}.repeat")
        outputs = _normalize_outputs(step_raw.get("outputs"), step_id)
        stats = _string_list(step_raw.get("stats"), f"steps.{step_id}.stats")
        missing_stats = set(stats) - set(outputs)
        if missing_stats:
            raise ValueError(
                f"step {step_id!r} stats reference undeclared outputs "
                f"{sorted(missing_stats)}"
            )
        steps.append(StepDefinition(
            id=step_id,
            script=script,
            action=action,
            arguments=arguments,
            outputs=outputs,
            artifacts=_normalize_artifacts(step_raw.get("artifacts"), step_id),
            cache=_normalize_cache(step_raw.get("cache"), step_id),
            repeat=repeat,
            stats=stats,
            needs=needs,
            raw=step_raw,
            direct_dimensions=direct_dimensions,
            upstream_steps=list(dict.fromkeys(referenced_steps + needs)),
        ))

    for step in steps[:-1]:
        if step.repeat != 1:
            raise ValueError(
                f"only the last step may repeat; step {step.id!r} has "
                f"repeat={step.repeat}"
            )

    by_id = {step.id: step for step in steps}
    for step in steps:
        dimensions = list(step.direct_dimensions)
        # mg_version is a reserved outer dimension: every step runs in a
        # specific MadGraph-version context even when it is not positional.
        if "mg_version" in matrix:
            dimensions.append("mg_version")
        for upstream_id in step.upstream_steps:
            dimensions.extend(by_id[upstream_id].dimensions)
        step.dimensions = [
            name for name in matrix if name in set(dimensions)
        ]
        _validate_step_references(step, by_id)

    return PipelineDefinition(
        name=name,
        description=description,
        matrix=dict(matrix),
        zip_groups=zip_groups,
        inputs=_string_list(raw.get("inputs"), "inputs"),
        steps=steps,
        workdir=workdir,
        raw=raw,
    )


def _validate_step_references(
    step: StepDefinition, by_id: dict[str, StepDefinition],
) -> None:
    def visit(value: Any) -> None:
        if isinstance(value, InputArgument):
            visit(value.value)
            return
        if isinstance(value, str):
            match = EXPR_RE.match(value)
            if not match:
                return
            ref = STEP_REF_RE.match(match.group(1).strip())
            if not ref:
                return
            producer, kind, name = ref.groups()
            target = by_id[producer]
            declared = target.outputs if kind == "outputs" else target.artifacts
            if name not in declared:
                raise ValueError(
                    f"step {step.id!r} references undeclared {kind[:-1]} "
                    f"{producer}.{name}"
                )
            return
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            for child in value.values():
                visit(child)

    visit(step.arguments)


def build_matrix_points(pipeline: PipelineDefinition) -> list[dict[str, Any]]:
    """Expand Cartesian and zipped axes while preserving YAML order."""
    name_to_group: dict[str, int] = {}
    for index, group in enumerate(pipeline.zip_groups):
        for name in group:
            name_to_group[name] = index

    axes: list[list[dict[str, Any]]] = []
    emitted_groups: set[int] = set()
    constants: dict[str, Any] = {}
    for name, value in pipeline.matrix.items():
        if name in name_to_group:
            group_index = name_to_group[name]
            if group_index in emitted_groups:
                continue
            emitted_groups.add(group_index)
            group = pipeline.zip_groups[group_index]
            axes.append([
                dict(zip(group, values))
                for values in zip(*(pipeline.matrix[key] for key in group))
            ])
        elif isinstance(value, list):
            axes.append([{name: item} for item in value])
        else:
            constants[name] = value

    if not axes:
        return [dict(constants)]
    points: list[dict[str, Any]] = []
    for pieces in itertools.product(*axes):
        point = dict(constants)
        for piece in pieces:
            point.update(piece)
        points.append({name: point[name] for name in pipeline.matrix})
    return points


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _short_identity(step_id: str, values: dict[str, Any]) -> str:
    digest = hashlib.sha256(_canonical(values).encode()).hexdigest()[:12]
    return f"{step_id}-{digest}"


def build_step_executions(
    pipeline: PipelineDefinition,
) -> dict[str, list[StepExecution]]:
    points = build_matrix_points(pipeline)
    result: dict[str, list[StepExecution]] = {}
    for step in pipeline.steps:
        unique: dict[str, dict[str, Any]] = {}
        for point in points:
            values = {name: point[name] for name in step.dimensions}
            unique.setdefault(_canonical(values), values)
        executions: list[StepExecution] = []
        for values in unique.values():
            identity = _short_identity(step.id, values)
            for repetition in range(1, step.repeat + 1):
                executions.append(StepExecution(
                    step=step,
                    values=values,
                    identity=identity,
                    repetition=repetition,
                ))
        result[step.id] = executions
    return result


def _path_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
        return digest.hexdigest()
    for child in sorted(path.rglob("*")):
        if child.is_file():
            digest.update(str(child.relative_to(path)).encode())
            digest.update(child.read_bytes())
    return digest.hexdigest()


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            target = (destination / member.name).resolve()
            try:
                target.relative_to(destination.resolve())
            except ValueError as exc:
                raise ValueError(
                    f"unsafe path in cache archive: {member.name!r}"
                ) from exc
            if member.issym() or member.islnk():
                raise ValueError(f"links are forbidden in cache: {member.name!r}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tf.extractfile(member)
                if source is None:
                    raise ValueError(f"cannot read cache member {member.name!r}")
                with source, open(target, "wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)
            else:
                raise ValueError(
                    f"unsupported cache archive member: {member.name!r}"
                )


class PipelineRunner:
    """Execute a validated, ordered MadBench pipeline."""

    def __init__(self, workspace: WorkspaceConfig) -> None:
        self.workspace = workspace

    def run(
        self,
        pipeline: PipelineDefinition,
        test_yml: Path,
        *,
        dry_run: bool = False,
        note: Optional[str] = None,
    ) -> None:
        expanded = build_step_executions(pipeline)
        matrix_points = build_matrix_points(pipeline)
        if dry_run:
            self._print_dry_run(pipeline, expanded, matrix_points)
            return

        timestamp = get_timestamp()
        hardware = detect_hardware()
        hostname = hardware["hostname"]
        result_dir = (
            self.workspace.results_dir / pipeline.name / f"{hostname}_{timestamp}"
        )
        base = self._workdir_base(pipeline)
        run_dir = base / f"{pipeline.name}_{timestamp}"
        staged_dir = run_dir / STAGED_DIR_NAME
        result_dir.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, exist_ok=True)
        if pipeline.inputs:
            stage_inputs(self.workspace.root, pipeline.inputs, staged_dir)
        else:
            staged_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(test_yml, result_dir / "test.yml")

        manifest: dict[str, Any] = {
            "schema_version": 1,
            "name": pipeline.name,
            "description": pipeline.description,
            "timestamp": timestamp,
            "note": note,
            "git_sha": get_git_sha(self.workspace.root),
            "hardware": hardware,
            "software": detect_software_versions(),
            "matrix": pipeline.matrix,
            "zip": pipeline.zip_groups,
            "matrix_points": matrix_points,
            "staged_inputs": self._staged_input_manifest(staged_dir),
            "steps": [],
        }

        result_index: dict[str, list[ExecutionResult]] = {}
        all_results: list[ExecutionResult] = []
        for step in pipeline.steps:
            step_results: list[ExecutionResult] = []
            for execution in expanded[step.id]:
                upstream, blocked = self._select_upstream(
                    execution, pipeline, result_index,
                )
                if blocked:
                    result = ExecutionResult(
                        step_id=step.id,
                        identity=execution.identity,
                        dimensions=execution.values,
                        repetition=execution.repetition,
                        status="blocked",
                        exit_code=None,
                        wall_time=0.0,
                        cache="not-applicable",
                        arguments={},
                        outputs={},
                        artifacts={},
                        artifact_digests={},
                        workdir="",
                        blocked_by=blocked,
                    )
                else:
                    result = self._execute(
                        pipeline=pipeline,
                        execution=execution,
                        upstream=upstream,
                        run_dir=run_dir,
                        staged_dir=staged_dir,
                        result_dir=result_dir,
                    )
                step_results.append(result)
                all_results.append(result)
                self._write_manifest(result_dir, manifest, all_results)
            result_index[step.id] = step_results

        self._write_manifest(result_dir, manifest, all_results)
        self._write_flat_csv(pipeline, result_dir, result_index)
        self._write_summary(pipeline, result_dir, result_index)
        print(f"[madbench] Pipeline complete: {result_dir}")

    def _workdir_base(self, pipeline: PipelineDefinition) -> Path:
        if pipeline.workdir is None:
            return self.workspace.scratch_dir
        value = Path(pipeline.workdir)
        if not value.is_absolute():
            value = self.workspace.root / value
        return value.resolve()

    def _staged_input_manifest(self, staged_dir: Path) -> list[dict[str, Any]]:
        values = []
        for path in sorted(staged_dir.rglob("*")):
            if path.is_file():
                values.append({
                    "path": str(path.relative_to(staged_dir)),
                    "sha256": _path_digest(path),
                })
        return values

    def _select_upstream(
        self,
        execution: StepExecution,
        pipeline: PipelineDefinition,
        result_index: dict[str, list[ExecutionResult]],
    ) -> tuple[dict[str, ExecutionResult], list[str]]:
        selected: dict[str, ExecutionResult] = {}
        blocked: list[str] = []
        by_id = {step.id: step for step in pipeline.steps}
        for upstream_id in execution.step.upstream_steps:
            producer = by_id[upstream_id]
            expected = {
                name: execution.values[name] for name in producer.dimensions
            }
            matches = [
                result for result in result_index.get(upstream_id, [])
                if result.repetition == 1
                and _canonical(result.dimensions) == _canonical(expected)
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    f"step {execution.step.id!r} execution {execution.identity} "
                    f"resolved {len(matches)} executions of {upstream_id!r}; "
                    "expected exactly one"
                )
            selected[upstream_id] = matches[0]
            if matches[0].status != "success":
                blocked.append(upstream_id)
        return selected, blocked

    def _resolve_value(
        self,
        value: Any,
        execution: StepExecution,
        upstream: dict[str, ExecutionResult],
        staged_dir: Path,
    ) -> Any:
        if isinstance(value, InputArgument):
            logical = self._resolve_value(
                value.value, execution, upstream, staged_dir,
            )
            if not isinstance(logical, str):
                raise ValueError(
                    f"input argument resolved to {type(logical).__name__}, "
                    "expected a relative path string"
                )
            path = Path(logical)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(
                    f"input path must be safe and workspace-relative: {logical!r}"
                )
            resolved = (staged_dir / path).resolve()
            try:
                resolved.relative_to(staged_dir.resolve())
            except ValueError as exc:
                raise ValueError(f"input escapes staging root: {logical!r}") from exc
            if not resolved.exists():
                raise FileNotFoundError(
                    f"resolved input does not exist: {logical!r} "
                    f"(expected {resolved}); declare it under top-level 'inputs'"
                )
            return str(resolved)
        if isinstance(value, str):
            match = EXPR_RE.match(value)
            if not match:
                return value
            expression = match.group(1).strip()
            matrix_match = MATRIX_REF_RE.match(expression)
            if matrix_match:
                return execution.values[matrix_match.group(1)]
            step_match = STEP_REF_RE.match(expression)
            if step_match:
                step_id, kind, name = step_match.groups()
                result = upstream[step_id]
                source = result.outputs if kind == "outputs" else result.artifacts
                return source[name]
            raise ValueError(f"unsupported expression {value!r}")
        if isinstance(value, list):
            return [
                self._resolve_value(x, execution, upstream, staged_dir)
                for x in value
            ]
        if isinstance(value, dict):
            return {
                key: self._resolve_value(child, execution, upstream, staged_dir)
                for key, child in value.items()
            }
        return value

    def _execute(
        self,
        *,
        pipeline: PipelineDefinition,
        execution: StepExecution,
        upstream: dict[str, ExecutionResult],
        run_dir: Path,
        staged_dir: Path,
        result_dir: Path,
    ) -> ExecutionResult:
        step = execution.step
        rep = f"{execution.repetition:02d}"
        workdir = run_dir / "steps" / step.id / execution.identity / rep
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir(parents=True)
        output_file = workdir / OUTPUT_FILE_NAME
        arguments = {
            name: self._resolve_value(value, execution, upstream, staged_dir)
            for name, value in step.arguments.items()
        }
        args_file = workdir / ARGS_FILE_NAME
        args_file.write_text(json.dumps(arguments, indent=2, default=str))
        mg_version = str(execution.values.get("mg_version", MG_VERSION_NONE))
        mg_bin = self._mg_bin(mg_version)
        env = os.environ.copy()
        env.update({
            "MADBENCH_WORKDIR": str(workdir),
            "MADBENCH_INPUTS": str(staged_dir),
            "MADBENCH_OUTPUT_FILE": str(output_file),
            "MADBENCH_ARGS_FILE": str(args_file),
            "MADBENCH_REPETITION": rep,
            "MADBENCH_STEP_ID": step.id,
            "MADBENCH_EXECUTION_ID": execution.identity,
            "MG_VERSION": mg_version,
            "MG_BIN": str(mg_bin or ""),
        })
        log_dir = (
            result_dir / "logs" / step.id / execution.identity / rep
        )
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout = log_dir / "stdout.log"
        stderr = log_dir / "stderr.log"

        cache_key = self._cache_key(
            pipeline, execution, arguments, upstream,
        ) if step.cache.enabled else None
        cache_dir = self._cache_dir(pipeline, step, cache_key) if cache_key else None
        if cache_dir is not None and self._restore_cache(
            step, cache_dir, workdir,
        ):
            outputs = json.loads((cache_dir / "outputs.json").read_text())
            artifacts, digests, saved = self._collect_artifacts(
                pipeline, execution, workdir, result_dir,
            )
            return ExecutionResult(
                step_id=step.id,
                identity=execution.identity,
                dimensions=execution.values,
                repetition=execution.repetition,
                status="success",
                exit_code=0,
                wall_time=0.0,
                cache="hit",
                arguments=arguments,
                outputs=outputs,
                artifacts=artifacts,
                artifact_digests=digests,
                workdir=str(workdir),
                saved_artifacts=saved,
            )

        started = time.monotonic()
        with open(stdout, "w") as stdout_file, open(stderr, "w") as stderr_file:
            if step.script is not None:
                script = resolve_script(self.workspace, step.script)
                cmd = [str(script)] + [str(value) for value in arguments.values()]
                completed = subprocess.run(
                    cmd,
                    cwd=workdir,
                    env=env,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    check=False,
                )
                exit_code = completed.returncode
            else:
                exit_code = self._run_action(
                    step, arguments, workdir, env, stdout_file, stderr_file,
                )
        wall_time = time.monotonic() - started
        if exit_code != 0:
            return ExecutionResult(
                step_id=step.id,
                identity=execution.identity,
                dimensions=execution.values,
                repetition=execution.repetition,
                status="failed",
                exit_code=exit_code,
                wall_time=round(wall_time, 4),
                cache="miss" if step.cache.enabled else "disabled",
                arguments=arguments,
                outputs={},
                artifacts={},
                artifact_digests={},
                workdir=str(workdir),
                stdout=str(stdout),
                stderr=str(stderr),
            )

        outputs = self._read_outputs(step, output_file)
        artifacts, digests, saved = self._collect_artifacts(
            pipeline, execution, workdir, result_dir,
        )
        if cache_dir is not None:
            self._store_cache(step, cache_dir, workdir, outputs)
        return ExecutionResult(
            step_id=step.id,
            identity=execution.identity,
            dimensions=execution.values,
            repetition=execution.repetition,
            status="success",
            exit_code=exit_code,
            wall_time=round(wall_time, 4),
            cache="miss" if step.cache.enabled else "disabled",
            arguments=arguments,
            outputs=outputs,
            artifacts=artifacts,
            artifact_digests=digests,
            workdir=str(workdir),
            stdout=str(stdout),
            stderr=str(stderr),
            saved_artifacts=saved,
        )

    def _mg_bin(self, mg_version: str) -> Optional[Path]:
        if mg_version == MG_VERSION_NONE:
            return None
        return self.workspace.root / "MadGraph" / mg_version / "bin" / "mg5_aMC"

    def _run_action(
        self,
        step: StepDefinition,
        arguments: dict[str, Any],
        workdir: Path,
        env: dict[str, str],
        stdout: Any,
        stderr: Any,
    ) -> int:
        if step.action != "madgraph/process":
            raise RuntimeError(f"unsupported action {step.action!r}")
        proc_card = arguments.get("proc_card")
        if not isinstance(proc_card, str):
            raise ValueError("action 'madgraph/process' requires 'with.proc_card'")
        mg_bin_value = env["MG_BIN"]
        if not mg_bin_value:
            raise ValueError(
                "action 'madgraph/process' requires an 'mg_version' matrix dimension"
            )
        mg_bin = Path(mg_bin_value)
        if not mg_bin.is_file():
            raise FileNotFoundError(f"MadGraph binary not found: {mg_bin}")
        completed = subprocess.run(
            [str(mg_bin), proc_card],
            cwd=workdir,
            env=env,
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
        return completed.returncode

    def _read_outputs(
        self, step: StepDefinition, output_file: Path,
    ) -> dict[str, Any]:
        if not step.outputs:
            return {}
        if not output_file.is_file():
            raise ValueError(
                f"step {step.id!r} succeeded but did not write "
                f"{OUTPUT_FILE_NAME}"
            )
        try:
            values = json.loads(output_file.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"step {step.id!r} wrote invalid output JSON: {exc}"
            ) from exc
        if not isinstance(values, dict):
            raise ValueError(f"step {step.id!r} output must be a JSON object")
        missing = set(step.outputs) - set(values)
        extra = set(values) - set(step.outputs)
        if missing or extra:
            raise ValueError(
                f"step {step.id!r} output keys differ from declaration; "
                f"missing={sorted(missing)}, extra={sorted(extra)}"
            )
        for name, expected in step.outputs.items():
            self._validate_output_type(step.id, name, values[name], expected)
        return values

    @staticmethod
    def _validate_output_type(
        step_id: str, name: str, value: Any, expected: Optional[str],
    ) -> None:
        if expected is None:
            return
        valid = {
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "string": isinstance(value, str),
        }[expected]
        if not valid:
            raise ValueError(
                f"step {step_id!r} output {name!r} expected {expected}, "
                f"got {type(value).__name__}"
            )

    def _collect_artifacts(
        self,
        pipeline: PipelineDefinition,
        execution: StepExecution,
        workdir: Path,
        result_dir: Path,
    ) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        artifacts: dict[str, str] = {}
        digests: dict[str, str] = {}
        saved: dict[str, str] = {}
        for name, spec in execution.step.artifacts.items():
            source = workdir / spec.path
            if not source.exists():
                raise FileNotFoundError(
                    f"step {execution.step.id!r} declared missing artifact "
                    f"{name!r}: {source}"
                )
            try:
                source.resolve().relative_to(workdir.resolve())
            except ValueError as exc:
                raise ValueError(
                    f"step {execution.step.id!r} artifact {name!r} resolves "
                    "outside its work directory"
                ) from exc
            if source.is_symlink() or (
                source.is_dir()
                and any(child.is_symlink() for child in source.rglob("*"))
            ):
                raise ValueError(
                    f"step {execution.step.id!r} artifact {name!r} contains "
                    "symbolic links, which are not portable artifacts"
                )
            artifacts[name] = str(source.resolve())
            digests[name] = _path_digest(source)
            if spec.save:
                destination = (
                    result_dir / "artifacts" / execution.step.id
                    / execution.identity / f"{execution.repetition:02d}" / name
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, destination, dirs_exist_ok=True)
                else:
                    shutil.copy2(source, destination)
                saved[name] = str(destination.resolve())
        return artifacts, digests, saved

    def _cache_key(
        self,
        pipeline: PipelineDefinition,
        execution: StepExecution,
        arguments: dict[str, Any],
        upstream: dict[str, ExecutionResult],
    ) -> str:
        step = execution.step
        digest = hashlib.sha256()

        def stable_argument(value: Any) -> Any:
            if isinstance(value, str):
                path = Path(value)
                if path.is_absolute() and path.exists():
                    return {
                        "resource_type": (
                            "directory" if path.is_dir() else "file"
                        ),
                        "sha256": _path_digest(path),
                    }
                return value
            if isinstance(value, list):
                return [stable_argument(child) for child in value]
            if isinstance(value, dict):
                return {
                    name: stable_argument(child)
                    for name, child in value.items()
                }
            return value

        payload = {
            "schema": 1,
            "pipeline": pipeline.name,
            "step": step.raw,
            "version": step.cache.version,
            "dimensions": execution.values,
            "repetition": execution.repetition,
            # Scratch and artifact paths contain run timestamps. Hash local
            # resources by content so equivalent runs share a cache key.
            "arguments": stable_argument(arguments),
            "upstream_artifacts": {
                step_id: result.artifact_digests
                for step_id, result in upstream.items()
            },
            "upstream_outputs": {
                step_id: result.outputs for step_id, result in upstream.items()
            },
        }
        digest.update(_canonical(payload).encode())
        if step.script is not None:
            script = resolve_script(self.workspace, step.script)
            digest.update(script.read_bytes())
        for pattern in step.cache.inputs:
            matches = sorted(self.workspace.root.glob(pattern))
            if not matches:
                raise FileNotFoundError(
                    f"step {step.id!r} cache input matched nothing: {pattern!r}"
                )
            for path in matches:
                if path.is_file():
                    digest.update(
                        str(path.resolve().relative_to(
                            self.workspace.root.resolve()
                        )).encode()
                    )
                    digest.update(path.read_bytes())
        return digest.hexdigest()

    def _cache_dir(
        self,
        pipeline: PipelineDefinition,
        step: StepDefinition,
        key: str,
    ) -> Path:
        if step.cache.path:
            base = Path(step.cache.path)
            if not base.is_absolute():
                base = self.workspace.root / base
        else:
            base = self.workspace.scratch_dir / ".madbench-cache"
        return base.resolve() / pipeline.name / step.id / key

    @staticmethod
    def _restore_cache(
        step: StepDefinition, cache_dir: Path, workdir: Path,
    ) -> bool:
        manifest = cache_dir / "outputs.json"
        archive = cache_dir / "artifacts.tar.gz"
        if not manifest.is_file() or not archive.is_file():
            return False
        try:
            with tarfile.open(archive, "r:gz") as tf:
                names = [Path(member.name) for member in tf.getmembers()]
            for artifact in step.artifacts.values():
                expected = Path(artifact.path)
                if not any(
                    name == expected or expected in name.parents
                    for name in names
                ):
                    return False
            json.loads(manifest.read_text())
        except (OSError, tarfile.TarError, json.JSONDecodeError):
            return False
        _safe_extract(archive, workdir)
        for artifact in step.artifacts.values():
            if not (workdir / artifact.path).exists():
                return False
        return True

    @staticmethod
    def _store_cache(
        step: StepDefinition,
        cache_dir: Path,
        workdir: Path,
        outputs: dict[str, Any],
    ) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        archive_tmp = cache_dir / ".artifacts.tar.gz.tmp"
        outputs_tmp = cache_dir / ".outputs.json.tmp"
        with tarfile.open(archive_tmp, "w:gz") as tf:
            for artifact in step.artifacts.values():
                source = workdir / artifact.path
                tf.add(source, arcname=artifact.path)
        outputs_tmp.write_text(json.dumps(outputs, indent=2, sort_keys=True))
        os.replace(archive_tmp, cache_dir / "artifacts.tar.gz")
        os.replace(outputs_tmp, cache_dir / "outputs.json")

    @staticmethod
    def _write_manifest(
        result_dir: Path,
        manifest: dict[str, Any],
        results: list[ExecutionResult],
    ) -> None:
        payload = dict(manifest)
        payload["steps"] = [
            {
                "step_id": result.step_id,
                "execution_id": result.identity,
                "dimensions": result.dimensions,
                "repetition": result.repetition,
                "status": result.status,
                "exit_code": result.exit_code,
                "wall_time": result.wall_time,
                "cache": result.cache,
                "arguments": result.arguments,
                "outputs": result.outputs,
                "artifacts": {
                    name: {
                        "path": path,
                        "sha256": result.artifact_digests.get(name),
                        "saved_path": result.saved_artifacts.get(name),
                    }
                    for name, path in result.artifacts.items()
                },
                "workdir": result.workdir,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "blocked_by": result.blocked_by,
            }
            for result in results
        ]
        path = result_dir / "result.json"
        temporary = result_dir / ".result.json.tmp"
        temporary.write_text(json.dumps(payload, indent=2, default=str))
        os.replace(temporary, path)

    def _lineage(
        self,
        final: ExecutionResult,
        pipeline: PipelineDefinition,
        result_index: dict[str, list[ExecutionResult]],
    ) -> dict[str, ExecutionResult]:
        lineage = {final.step_id: final}
        for step in pipeline.steps[:-1]:
            expected = {
                name: final.dimensions[name] for name in step.dimensions
            }
            matches = [
                result for result in result_index[step.id]
                if result.repetition == 1
                and _canonical(result.dimensions) == _canonical(expected)
            ]
            if len(matches) == 1:
                lineage[step.id] = matches[0]
        return lineage

    def _write_flat_csv(
        self,
        pipeline: PipelineDefinition,
        result_dir: Path,
        result_index: dict[str, list[ExecutionResult]],
    ) -> None:
        final_step = pipeline.steps[-1]
        rows = result_index[final_step.id]
        output_columns = [
            f"{step.id}.{name}"
            for step in pipeline.steps
            for name in step.outputs
        ]
        fieldnames = (
            list(pipeline.matrix)
            + output_columns
            + [
                "repetition",
                "status",
                "exit_code",
                "wall_time",
                "execution_id",
            ]
        )
        with open(result_dir / "results.csv", "w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for result in rows:
                row: dict[str, Any] = {
                    key: result.dimensions.get(key, "")
                    for key in pipeline.matrix
                }
                lineage = self._lineage(result, pipeline, result_index)
                for step in pipeline.steps:
                    source = lineage.get(step.id)
                    for name in step.outputs:
                        row[f"{step.id}.{name}"] = (
                            source.outputs.get(name, "") if source else ""
                        )
                row.update({
                    "repetition": result.repetition,
                    "status": result.status,
                    "exit_code": result.exit_code,
                    "wall_time": result.wall_time,
                    "execution_id": result.identity,
                })
                writer.writerow(row)

    def _write_summary(
        self,
        pipeline: PipelineDefinition,
        result_dir: Path,
        result_index: dict[str, list[ExecutionResult]],
    ) -> None:
        final_step = pipeline.steps[-1]
        if final_step.repeat == 1:
            return
        stats = final_step.stats or [
            name
            for name, kind in final_step.outputs.items()
            if kind in {None, "number", "integer"}
        ]
        fieldnames = list(final_step.dimensions)
        for name in stats:
            fieldnames.extend([f"{name}_mean", f"{name}_std"])
        fieldnames.extend(["n_successful", "n_failed", "execution_id"])
        groups: dict[str, list[ExecutionResult]] = {}
        for result in result_index[final_step.id]:
            groups.setdefault(result.identity, []).append(result)
        with open(result_dir / "summary.csv", "w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for identity, results in groups.items():
                successful = [
                    result for result in results if result.status == "success"
                ]
                row: dict[str, Any] = {
                    **results[0].dimensions,
                    "n_successful": len(successful),
                    "n_failed": len(results) - len(successful),
                    "execution_id": identity,
                }
                for name in stats:
                    values: list[float] = []
                    for result in successful:
                        try:
                            values.append(float(result.outputs[name]))
                        except (KeyError, TypeError, ValueError):
                            values = []
                            break
                    row[f"{name}_mean"] = (
                        statistics.mean(values) if values else ""
                    )
                    row[f"{name}_std"] = (
                        statistics.stdev(values) if len(values) > 1 else ""
                    )
                writer.writerow(row)

    def _print_dry_run(
        self,
        pipeline: PipelineDefinition,
        expanded: dict[str, list[StepExecution]],
        matrix_points: list[dict[str, Any]],
    ) -> None:
        print("[madbench] DRY RUN — no files will be created or scripts executed")
        print(f"[madbench] Pipeline: {pipeline.name}")
        print(f"[madbench] Global matrix: {len(matrix_points)} point(s)")
        if pipeline.inputs:
            print("[madbench] Staged inputs:")
            for pattern in pipeline.inputs:
                print(f"  {pattern}")
        for step in pipeline.steps:
            n_identities = len({
                execution.identity for execution in expanded[step.id]
            })
            print(f"[madbench] Step {step.id}:")
            print(f"  kind: {'script' if step.script else 'action'}")
            print(f"  target: {step.script or step.action}")
            print(f"  inferred dimensions: {step.dimensions}")
            print(f"  executions: {n_identities}")
            print(f"  repetitions: {step.repeat}")
            print(f"  total runs: {len(expanded[step.id])}")
            print(f"  upstream: {step.upstream_steps}")
            print(f"  cache: {'enabled' if step.cache.enabled else 'disabled'}")
