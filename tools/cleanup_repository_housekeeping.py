from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ROOT_FILE_GLOBS = (
    "README_MVP*.md",
    "README_S1*.md",
    "README_CONTEXT_UPDATE.md",
    "APPLY_PATCH.md",
    "APPLY_CONTEXT_UPDATE.md",
    "NEXT_CHAT_PROMPT.md",
    "Grandgousier_DATA_MODEL_REFACTOR_TZ.md",
    "PATCH_MANIFEST_*.txt",
    "CONTEXT_UPDATE_MANIFEST_*.txt",
    "AUDIT_TOOL_MANIFEST_*.txt",
    "master_test_*.part",
)

RETIRED_DIRS = (
    "docs/context",
    "docs/releases",
    "docs/decisions",
    "docs/architecture",
    "artifacts/context",
    "build",
    "dist",
)

GENERATED_DIR_NAMES = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
LATEX_SUFFIXES = {".aux", ".log", ".toc", ".out"}


def collect_targets() -> list[Path]:
    targets: set[Path] = set()
    for pattern in ROOT_FILE_GLOBS:
        targets.update(path for path in ROOT.glob(pattern) if path.exists())
    for relative in RETIRED_DIRS:
        path = ROOT / relative
        if path.exists():
            targets.add(path)
    for path in ROOT.rglob("*"):
        if path.is_dir() and path.name in GENERATED_DIR_NAMES:
            targets.add(path)
    manual = ROOT / "docs" / "user_manual_ru"
    if manual.exists():
        for path in manual.rglob("*"):
            if path.is_file() and path.suffix.lower() in LATEX_SUFFIXES:
                targets.add(path)
    # Remove children when their parent directory is already scheduled.
    ordered = sorted(targets, key=lambda p: (len(p.parts), str(p).casefold()))
    kept: list[Path] = []
    for path in ordered:
        if any(parent == path or parent in path.parents for parent in kept):
            continue
        kept.append(path)
    return kept


def remove_target(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove retired Grandgousier documentation/build artifacts after the 2026-09-17 consolidation."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete the listed retired/generated files. Without this flag the command is a dry run.",
    )
    args = parser.parse_args()

    targets = collect_targets()
    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"Grandgousier repository housekeeping: mode={mode}, targets={len(targets)}")
    for path in targets:
        try:
            shown = path.relative_to(ROOT)
        except ValueError:
            shown = path
        print(f"  {shown}")
    if args.execute:
        for path in sorted(targets, key=lambda p: len(p.parts), reverse=True):
            if path.exists():
                remove_target(path)
        artifacts = ROOT / "artifacts"
        if artifacts.exists() and not any(artifacts.iterdir()):
            artifacts.rmdir()
        print("Repository housekeeping complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
