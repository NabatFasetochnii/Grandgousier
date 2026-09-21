from __future__ import annotations

from grandgousier.interseries.catalog_identity import audit_field_catalog_identity


class _Mappings:
    def __init__(self, rows):
        self.rows = rows

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return _Mappings(self.rows)


class _FakeSession:
    def __init__(self, series_rows):
        self.series_rows = series_rows

    def execute(self, statement, params=None):
        sql = str(statement)
        if "FROM fields WHERE field_id" in " ".join(sql.split()):
            return _Result([{"field_id": 2, "field_name": "BK_Lyn"}])
        if "FROM field_series fs" in sql and "frame_catalog_links" in sql:
            return _Result(self.series_rows)
        raise AssertionError(f"Unexpected SQL: {sql}")


def _row(series_id, unit, filt, catalogs, *, linked=70, frames=70):
    return {
        "series_id": series_id,
        "instrument_unit_id": unit,
        "filter_name": filt,
        "n_frames": frames,
        "n_linked_frames": linked,
        "n_catalog_sources": 200,
        "catalog_names": catalogs,
    }


def test_repeated_series_with_one_shared_namespace_are_stage3_identity_ready() -> None:
    session = _FakeSession(
        [
            _row(5, 2, "i", ["gaia_bklyn"]),
            _row(3, 2, "i", ["gaia_bklyn"], linked=69, frames=69),
            _row(6, 4, "r", ["gaia_bklyn"]),
            _row(7, 4, "r", ["gaia_bklyn"], linked=69, frames=69),
        ]
    )
    audit = audit_field_catalog_identity(session, field_id=2)

    assert audit.status == "READY_SHARED_IDENTITY"
    assert len(audit.groups) == 2
    assert all(group.status == "READY_SHARED_NAMESPACE" for group in audit.groups)
    assert {tuple(group.series_ids) for group in audit.groups} == {(3, 5), (6, 7)}


def test_different_namespaces_in_same_system_require_reconciliation() -> None:
    session = _FakeSession(
        [
            _row(5, 2, "i", ["catalog_night1"]),
            _row(3, 2, "i", ["catalog_night2"], linked=69, frames=69),
        ]
    )
    audit = audit_field_catalog_identity(session, field_id=2)

    assert audit.status == "NEEDS_RECONCILIATION"
    assert audit.groups[0].status == "NEEDS_RECONCILIATION"
    assert audit.groups[0].common_catalog_names == []


def test_missing_links_are_reported_before_stage3() -> None:
    session = _FakeSession(
        [
            _row(5, 2, "i", ["gaia_bklyn"]),
            _row(3, 2, "i", [], linked=0, frames=69),
        ]
    )
    audit = audit_field_catalog_identity(session, field_id=2)

    assert audit.status == "INCOMPLETE"
    assert audit.groups[0].status == "MISSING_LINKS"
