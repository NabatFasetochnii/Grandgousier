from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python >=3.11 is required by the project.
    tomllib = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
CONTEXT_FILES = [
    Path("CONTEXT.md"),
    Path("docs/context/CURRENT_STATE.md"),
    Path("docs/context/NEXT_TASKS.md"),
    Path("docs/context/KNOWN_ISSUES.md"),
    Path("docs/context/TEST_DATASETS.md"),
    Path("docs/context/COMMANDS.md"),
    Path("context/project_state.yaml"),
    Path("docs/CURRENT_ARCHITECTURE.md"),
]

_DATABASE_URL_RE = re.compile(r"(postgresql(?:\+\w+)?://[^:\s/@]+:)([^@\s]+)(@)", re.IGNORECASE)


def _redact(text: str) -> str:
    return _DATABASE_URL_RE.sub(r"\1***\3", text)


def _project_version() -> str:
    pyproject = ROOT / "pyproject.toml"
    if tomllib is not None:
        with pyproject.open("rb") as handle:
            data = tomllib.load(handle)
        return str(data["project"]["version"])
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise RuntimeError("Cannot determine project version from pyproject.toml")
    return match.group(1)


def _git_summary() -> str:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return "Git metadata: unavailable."
    return f"Git commit: `{commit}`. Working tree: {'dirty' if status else 'clean'}."


def _find_pipeline_result(payload: dict[str, Any]) -> dict[str, Any] | None:
    pipeline_results = payload.get("pipeline_results")
    if isinstance(pipeline_results, list) and pipeline_results:
        item = pipeline_results[-1]
        return item if isinstance(item, dict) else None
    if isinstance(payload.get("quality_policy"), dict):
        return payload
    return None


def _manifest_summary(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = _find_pipeline_result(payload)
    if result is None:
        return "Manifest was loaded, but no pipeline result was found."
    qp = result.get("quality_policy") if isinstance(result.get("quality_policy"), dict) else {}
    history = qp.get("candidate_ensemble_exclusion_history")
    last_executed = None
    if isinstance(history, list):
        last_executed = next(
            (entry for entry in reversed(history) if isinstance(entry, dict) and entry.get("executed")),
            None,
        )
    actual_iteration = (
        last_executed.get("iteration")
        if last_executed is not None
        else qp.get("candidate_ensemble_actual_exclusions_iteration")
    )
    actual_ids = (
        list(last_executed.get("actually_excluded_reference_object_ids") or [])
        if last_executed is not None
        else list(qp.get("candidate_ensemble_actual_excluded_reference_object_ids") or [])
    )
    fields = {
        "manifest": str(path),
        "status": result.get("status", payload.get("status")),
        "science_status": result.get("science_status"),
        "series_id": result.get("series_id"),
        "field_name": result.get("field_name"),
        "filter_name": result.get("filter_name"),
        "ensemble_calibration": result.get("ensemble_calibration"),
        "candidate_ensemble_iteration_mode": qp.get("candidate_ensemble_iteration_mode"),
        "candidate_ensemble_converged": qp.get("candidate_ensemble_converged"),
        "candidate_ensemble_stop_reason": qp.get("candidate_ensemble_stop_reason"),
        "candidate_ensemble_exclusion_iterations_executed": qp.get(
            "candidate_ensemble_exclusion_iterations_executed"
        ),
        "candidate_ensemble_actual_exclusions_iteration": actual_iteration,
        "candidate_ensemble_requested_excluded_objects": qp.get(
            "candidate_ensemble_requested_excluded_objects"
        ),
        "candidate_ensemble_actual_excluded_reference_objects": len(actual_ids),
        "candidate_ensemble_final_candidate_objects": qp.get(
            "candidate_ensemble_final_candidate_objects"
        ),
        "accepted_candidate_count": result.get("accepted_candidate_count"),
        "exported_candidate_light_curves": result.get("exported_candidate_light_curves"),
    }
    return "```json\n" + json.dumps(fields, ensure_ascii=False, indent=2) + "\n```"


def build_bundle(output: Path, manifest: Path | None) -> None:
    version = _project_version()
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    sections = [
        "# Grandgousier context bundle",
        "",
        f"Generated: `{generated}`.",
        f"Project version: `{version}`.",
        _git_summary(),
        "",
        "> This file is generated. Update the source documents in `docs/context/` and "
        "`context/project_state.yaml`, not this bundle.",
    ]

    for relative in CONTEXT_FILES:
        source = ROOT / relative
        if not source.exists():
            sections.extend(["", f"## Missing source: `{relative}`", ""])
            continue
        sections.extend(
            [
                "",
                f"---\n\n## Source: `{relative.as_posix()}`\n",
                _redact(source.read_text(encoding="utf-8").strip()),
            ]
        )

    release = ROOT / "docs" / "releases" / f"{version}.md"
    if release.exists():
        sections.extend(
            [
                "",
                f"---\n\n## Source: `docs/releases/{version}.md`\n",
                _redact(release.read_text(encoding="utf-8").strip()),
            ]
        )

    if manifest is not None:
        manifest_path = manifest if manifest.is_absolute() else ROOT / manifest
        sections.extend(["", "---", "", "## Selected manifest summary", ""])
        try:
            sections.append(_manifest_summary(manifest_path))
        except (OSError, ValueError, TypeError) as exc:
            sections.append(f"Could not summarize `{manifest}`: {exc}")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(sections).rstrip() + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a portable Grandgousier context bundle.")
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Optional process manifest to summarize without embedding its full payload.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Output Markdown path. Defaults to artifacts/context/<version>_<date>.md.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    version = _project_version()
    output = args.out
    if output is None:
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d")
        output = ROOT / "artifacts" / "context" / f"Grandgousier_context_{version}_{stamp}.md"
    elif not output.is_absolute():
        output = ROOT / output

    build_bundle(output, args.manifest)
    try:
        shown = output.relative_to(ROOT)
    except ValueError:
        shown = output
    print(f"Context bundle written: {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
