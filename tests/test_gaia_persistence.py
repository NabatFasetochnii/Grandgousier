from __future__ import annotations

from contextlib import nullcontext

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.selectable import Select

from grandgousier.catalogs import gaia_query as gq
from grandgousier.db.models import ObjectCatalogLink


class _Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, *, existing_rows=(), returned_ids=None):
        self.existing_rows = list(existing_rows)
        self.returned_ids = dict(returned_ids or {})
        self.statements = []
        self.upsert_payloads = []
        self.added = []
        self.commit_count = 0
        self.no_autoflush = nullcontext()

    def execute(self, statement):
        self.statements.append(statement)
        if isinstance(statement, Select):
            return _Result(self.existing_rows)
        if isinstance(statement, Insert):
            payloads = list(statement._multi_values[0])
            self.upsert_payloads.extend(payloads)
            return _Result(
                (row["source_id"], self.returned_ids[row["source_id"]])
                for row in payloads
            )
        raise AssertionError(f"unexpected statement type: {type(statement).__name__}")

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commit_count += 1
        raise AssertionError("Gaia promotion must not commit")


def _catalog():
    return gq.GaiaWorkingCatalog(
        [
            {"source_id": "2", "ra": 10.25, "dec": -3.5, "phot_g_mean_mag": 15.2},
            {"source_id": "99", "ra": 11.5, "dec": 4.25, "phot_g_mean_mag": 16.3},
            {"source_id": "100", "ra": 12.5, "dec": 5.25, "phot_g_mean_mag": 17.4},
        ]
    )


def _session(existing_rows=()):
    return _FakeSession(
        existing_rows=existing_rows,
        returned_ids={"2": 202, "99": 299, "100": 300},
    )


def test_selected_gaia_promotion_upserts_minimal_payload_and_returns_canonical_mapping():
    session = _session()

    mapping = gq.promote_selected_gaia_sources(session, _catalog(), ["99", 2, "99"])

    assert mapping == {"2": 202, "99": 299}
    assert [row["source_id"] for row in session.upsert_payloads] == ["2", "99"]
    assert all(row["catalog_name"] == "gaia_dr3" for row in session.upsert_payloads)
    assert [
        (row["ra_deg"], row["dec_deg"], row["ref_mags"])
        for row in session.upsert_payloads
    ] == [(10.25, -3.5, {"G": 15.2}), (11.5, 4.25, {"G": 16.3})]
    for row in session.upsert_payloads:
        assert row["epoch"] == 2016.0
        assert row["pmra_masyr"] is None
        assert row["pmdec_masyr"] is None
        assert row["colors"] == {}
        assert row["source_flags"] == {}
        assert row["raw_json"] == {}
    assert session.added == []
    assert session.commit_count == 0

    compiled_select = session.statements[0].compile(dialect=postgresql.dialect())
    select_params = compiled_select.params
    selected_filter_values = [value for value in select_params.values() if value == ["2", "99"]]
    assert selected_filter_values == [["2", "99"]]
    assert "catalog_sources.catalog_name LIKE" in str(compiled_select)
    assert "ESCAPE" in str(compiled_select)
    assert "gaia_dr3" in select_params.values()


def test_existing_canonical_link_to_same_legacy_object_is_not_duplicated():
    session = _FakeSession(
        existing_rows=[
            ("gaia_dr3", "99", 901, 41),
            ("auto_catalog_field_7", "99", 700, 41),
        ],
        returned_ids={"99": 901},
    )

    mapping = gq.promote_selected_gaia_sources(session, _catalog(), ["99"])

    assert mapping == {"99": 901}
    assert session.added == []
    assert len(session.upsert_payloads) == 1


def test_exact_legacy_gaia_link_reuses_object_and_keeps_historical_link():
    historical_link_row = ("auto_catalog_field_7", "99", 700, 41)
    session = _session([historical_link_row])

    mapping = gq.promote_selected_gaia_sources(session, _catalog(), ["99"])

    assert mapping == {"99": 299}
    assert len(session.added) == 1
    link = session.added[0]
    assert isinstance(link, ObjectCatalogLink)
    assert link.object_id == 41
    assert link.catalog_source_id == 299
    assert link.association_method == "LEGACY_GAIA_SOURCE_ID"
    assert link.is_primary is False
    assert link.evidence == {
        "match": "exact_gaia_source_id",
        "legacy_catalog_names": ["auto_catalog_field_7"],
    }
    assert session.existing_rows == [historical_link_row]
    assert len(session.upsert_payloads) == 1


@pytest.mark.parametrize(
    "existing_rows",
    [
        [
            ("gaia_dr3", "99", 901, 41),
            ("auto_catalog_field_7", "99", 700, 42),
        ],
        [
            ("auto_catalog_field_7", "99", 700, 41),
            ("auto_catalog_field_8", "99", 800, 42),
        ],
    ],
    ids=["canonical-legacy-disagree", "multiple-legacy-objects"],
)
def test_identity_conflict_stops_before_upsert_or_link_write(existing_rows):
    session = _session(existing_rows)

    with pytest.raises(RuntimeError, match="conflicting canonical/legacy Gaia object links"):
        gq.promote_selected_gaia_sources(session, _catalog(), ["99"])

    assert len(session.statements) == 1
    assert isinstance(session.statements[0], Select)
    assert session.upsert_payloads == []
    assert session.added == []
    assert session.commit_count == 0


def test_closed_gaia_working_catalog_is_rejected_before_session_access():
    catalog = _catalog()
    catalog.close()
    session = _session()

    with pytest.raises(RuntimeError, match="working catalog is closed"):
        gq.promote_selected_gaia_sources(session, catalog, ["99"])

    assert session.statements == []
    assert session.upsert_payloads == []
    assert session.added == []
    assert session.commit_count == 0


def test_selected_gaia_promotion_never_commits():
    session = _session()

    gq.promote_selected_gaia_sources(session, _catalog(), ["2"])

    assert session.commit_count == 0
    assert len(session.upsert_payloads) == 1
