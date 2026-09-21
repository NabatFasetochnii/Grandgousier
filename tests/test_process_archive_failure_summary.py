from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_process_archive_summary_surfaces_pipeline_error_message():
    path = ROOT / "grandgousier" / "archive" / "auto_process.py"
    module = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "process_archive_summary_rows"
    )
    found = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        values = {}
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                values[key.value] = value
        metric = values.get("metric")
        value = values.get("value")
        if not (isinstance(metric, ast.Constant) and metric.value == "error_message"):
            continue
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and isinstance(value.func.value, ast.Name)
            and value.func.value.id == "pipe"
            and value.func.attr == "get"
            and value.args
            and isinstance(value.args[0], ast.Constant)
            and value.args[0].value == "error_message"
        ):
            found = True
            break
    assert found
