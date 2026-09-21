from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_process_archive_cli_prints_untruncated_pipeline_failures():
    module = ast.parse((ROOT / "grandgousier" / "cli.py").read_text(encoding="utf-8"))
    fn = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_process_archive_path"
    )
    strings = {
        node.value
        for node in ast.walk(fn)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "Pipeline failures:" in strings
    assert any("pipeline:" in value for value in strings)
