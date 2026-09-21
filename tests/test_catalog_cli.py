from types import SimpleNamespace

import pytest

pytest.importorskip("astropy")

from grandgousier import cli


class _FakeRow:
    def __init__(self, mapping):
        self._mapping = mapping


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement):
        self.statement = statement
        return _FakeResult(self.rows)


def test_show_catalog_sources_parser_namespace_runs_without_unrelated_filters(monkeypatch):
    rows = [
        _FakeRow(
            {
                "catalog_source_id": 1,
                "catalog_name": "catalog_a",
                "source_id": "source-1",
                "ra_deg": 10.123456789,
                "dec_deg": 20.987654321,
                "pmra_masyr": None,
                "pmdec_masyr": None,
                "ref_mags": {"G": 15.0},
                "colors": {},
            }
        )
    ]
    fake_session = _FakeSession(rows)
    monkeypatch.setattr(cli, "make_session_factory", lambda _url: lambda: fake_session)

    printed = {}
    monkeypatch.setattr(
        cli,
        "_print_rows",
        lambda output_rows, columns, output_format: printed.update(
            rows=output_rows,
            columns=columns,
            output_format=output_format,
        ),
    )

    args = cli.build_parser().parse_args(
        ["show-catalog-sources", "--catalog-name", "catalog_a", "--limit", "5", "--format", "json"]
    )

    assert not hasattr(args, "calibration_name")
    assert not hasattr(args, "instrument_unit_id")
    assert args.func(args) == 0
    assert len(printed["rows"]) == 1
    assert printed["rows"][0]["catalog_name"] == "catalog_a"
    assert printed["rows"][0]["ra_deg"] == 10.12345679
    assert printed["output_format"] == "json"
