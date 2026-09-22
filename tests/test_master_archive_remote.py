from __future__ import annotations

from pathlib import Path

import pytest

import internal_tools.master_remote as master_remote
from internal_tools.master_remote import (
    MasterFetchTask,
    MasterRemoteCredentialError,
    _build_sbox_strings,
    build_master_fetch_tasks,
    download_master_task,
    extract_master_raw_id,
    master_remote_path_to_local,
    master_remote_path_to_url,
    normalize_master_database_url,
    summarize_master_inventory,
    summarize_master_observing_series,
)


def test_master_credentials_fail_fast_without_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRANDGOUSIER_MASTER_HTTP_USER", raising=False)
    monkeypatch.delenv("GRANDGOUSIER_MASTER_HTTP_PASSWORD", raising=False)

    def unexpected_prompt(_message: str) -> str:
        raise AssertionError("interactive credential prompt is not allowed")

    monkeypatch.setattr(master_remote.getpass, "getpass", unexpected_prompt)
    with pytest.raises(MasterRemoteCredentialError, match="GRANDGOUSIER_MASTER_HTTP_USER"):
        master_remote.resolve_master_http_credentials()


def test_master_credentials_reject_whitespace_and_partial_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRANDGOUSIER_MASTER_HTTP_USER", "  ")
    monkeypatch.setenv("GRANDGOUSIER_MASTER_HTTP_PASSWORD", "configured")
    with pytest.raises(MasterRemoteCredentialError, match="GRANDGOUSIER_MASTER_HTTP_USER"):
        master_remote.resolve_master_http_credentials()

    monkeypatch.setenv("GRANDGOUSIER_MASTER_HTTP_USER", "configured-user")
    monkeypatch.setenv("GRANDGOUSIER_MASTER_HTTP_PASSWORD", " \t")
    with pytest.raises(MasterRemoteCredentialError, match="GRANDGOUSIER_MASTER_HTTP_PASSWORD"):
        master_remote.resolve_master_http_credentials()


def test_master_credentials_accept_synthetic_runtime_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRANDGOUSIER_MASTER_HTTP_USER", "synthetic-user")
    monkeypatch.setenv("GRANDGOUSIER_MASTER_HTTP_PASSWORD", "synthetic-value")
    assert master_remote.resolve_master_http_credentials() == ("synthetic-user", "synthetic-value")


def test_master_credentials_interactive_mode_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRANDGOUSIER_MASTER_HTTP_USER", "synthetic-user")
    monkeypatch.delenv("GRANDGOUSIER_MASTER_HTTP_PASSWORD", raising=False)
    monkeypatch.setattr(master_remote.getpass, "getpass", lambda _message: "synthetic-value")
    assert master_remote.resolve_master_http_credentials(prompt_password=True) == (
        "synthetic-user",
        "synthetic-value",
    )


def test_master_remote_path_mapping() -> None:
    path = "/master/imdata/rawdata/2018/10/02/380242.fit.gz"
    assert master_remote_path_to_url(path) == (
        "https://master.kourovka.ru/data/master/imdata/rawdata/2018/10/02/380242.fit.gz"
    )
    assert extract_master_raw_id(path) == 380242
    assert extract_master_raw_id("/master/imdata/procdata/2018/10/02/380242_header.fits") == 380242


def test_master_remote_path_local_mapping_is_mirrored(tmp_path: Path) -> None:
    path = "/master/imdata/procdata/2018/10/02/380242_header.fits"
    result = master_remote_path_to_local(path, tmp_path)
    assert result == tmp_path / "master" / "imdata" / "procdata" / "2018" / "10" / "02" / "380242_header.fits"


def test_master_database_url_accepts_sqlalchemy_driver_prefix() -> None:
    assert normalize_master_database_url("postgresql+psycopg://user:password@example.invalid/imdata") == (
        "postgresql://user:password@example.invalid/imdata"
    )


def test_master_sbox_wraps_ra_zero() -> None:
    boxes = _build_sbox_strings(0.2, 30.0, 0.8)
    assert len(boxes) == 2
    assert "359." in boxes[0]
    assert "0.000000000000 d" in boxes[1]


def test_master_inventory_summary_groups_nights_and_filters() -> None:
    rows = [
        {"night_utc": "2018-09-23", "filter": "R", "dark_path": "d1", "flat_path": "f1"},
        {"night_utc": "2018-09-23", "filter": "R", "dark_path": "d1", "flat_path": "f1"},
        {"night_utc": "2018-09-23", "filter": "V", "dark_path": "d2", "flat_path": "f2"},
    ]
    summary = summarize_master_inventory(rows)
    assert summary == [
        {"night_utc": "2018-09-23", "filter": "R", "n_frames": 2, "unique_darks": 1, "unique_flats": 1},
        {"night_utc": "2018-09-23", "filter": "V", "n_frames": 1, "unique_darks": 1, "unique_flats": 1},
    ]


def test_master_observing_series_splits_filters_and_time_gaps() -> None:
    rows = [
        {"processed_frame_id": 1, "datetime": "2018-09-07 17:04:22", "coord2000": "21h 50m 06s +72d 11m 00s", "filter": "V", "raw_id": 100, "dark_path": "dV", "flat_path": "fV"},
        {"processed_frame_id": 2, "datetime": "2018-09-07 17:04:22", "coord2000": "21h 50m 33s +72d 07m 15s", "filter": "R", "raw_id": 101, "dark_path": "dR", "flat_path": "fR"},
        {"processed_frame_id": 3, "datetime": "2018-09-07 17:05:36", "coord2000": "21h 50m 06s +72d 11m 00s", "filter": "V", "raw_id": 102, "dark_path": "dV", "flat_path": "fV"},
        {"processed_frame_id": 4, "datetime": "2018-09-07 19:10:00", "coord2000": "21h 50m 06s +72d 11m 00s", "filter": "V", "raw_id": 103, "dark_path": "dV2", "flat_path": "fV"},
    ]

    summary = summarize_master_observing_series(rows, max_series_gap_minutes=60.0)

    assert len(summary) == 3
    assert {row["field_cluster"] for row in summary} == {1}
    assert [(row["filter"], row["n_frames"]) for row in summary] == [("R", 1), ("V", 2), ("V", 1)]
    assert summary[1]["raw_id_first"] == 100
    assert summary[1]["raw_id_last"] == 102


def test_master_fetch_tasks_deduplicate_calibration_files(tmp_path: Path) -> None:
    rows = [
        {
            "raw_path": "/master/imdata/rawdata/2018/10/02/380242.fit.gz",
            "header_path": "/master/imdata/procdata/2018/10/02/380242_header.fits",
            "dark_path": "/master/imdata/rawdata/2018/10/02/380194.fit.gz",
            "flat_path": "/master/imdata/rawdata/2018/09/10/373627.fit.gz",
        },
        {
            "raw_path": "/master/imdata/rawdata/2018/10/02/380241.fit.gz",
            "header_path": "/master/imdata/procdata/2018/10/02/380241_header.fits",
            "dark_path": "/master/imdata/rawdata/2018/10/02/380194.fit.gz",
            "flat_path": "/master/imdata/rawdata/2018/09/10/373627.fit.gz",
        },
    ]
    tasks = build_master_fetch_tasks(rows, output_root=tmp_path)
    assert len(tasks) == 6
    dark = [task for task in tasks if task.remote_path.endswith("380194.fit.gz")][0]
    flat = [task for task in tasks if task.remote_path.endswith("373627.fit.gz")][0]
    assert dark.roles == ("dark",)
    assert flat.roles == ("flat",)


class _FakeResponse:
    def __init__(self, status_code: int, chunks: list[bytes]) -> None:
        self.status_code = status_code
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int):
        yield from self._chunks


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_master_download_uses_part_then_atomic_final(tmp_path: Path) -> None:
    task = MasterFetchTask(
        remote_path="/master/imdata/rawdata/2018/10/02/380242.fit.gz",
        url="https://master.kourovka.ru/data/master/imdata/rawdata/2018/10/02/380242.fit.gz",
        local_path=tmp_path / "master" / "imdata" / "rawdata" / "2018" / "10" / "02" / "380242.fit.gz",
        roles=("raw",),
    )
    session = _FakeSession(_FakeResponse(200, [b"abc", b"def"]))
    result = download_master_task(task, session=session)
    assert result.status == "DOWNLOADED"
    assert task.local_path.read_bytes() == b"abcdef"
    assert not task.local_path.with_name(task.local_path.name + ".part").exists()
    assert result.size_bytes == 6
    assert result.sha256 is not None


def test_master_download_resumes_when_server_returns_206(tmp_path: Path) -> None:
    final_path = tmp_path / "master" / "imdata" / "rawdata" / "x.fit.gz"
    final_path.parent.mkdir(parents=True)
    part_path = final_path.with_name(final_path.name + ".part")
    part_path.write_bytes(b"abc")
    task = MasterFetchTask(
        remote_path="/master/imdata/rawdata/x.fit.gz",
        url="https://master.kourovka.ru/data/master/imdata/rawdata/x.fit.gz",
        local_path=final_path,
        roles=("raw",),
    )
    session = _FakeSession(_FakeResponse(206, [b"def"]))
    result = download_master_task(task, session=session)
    assert result.status == "DOWNLOADED_RESUMED"
    assert final_path.read_bytes() == b"abcdef"
    assert session.calls[0][1]["headers"] == {"Range": "bytes=3-"}



class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self.rows


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self, **kwargs):
        return self._cursor


def test_master_search_normalizes_paths_and_uses_parameterized_sbox() -> None:
    from internal_tools.master_remote import search_master_archive

    cursor = _FakeCursor(
        [
            {
                "processed_frame_id": 135123,
                "datetime": "2018-10-02T17:52:10",
                "coord2000": "21h 50m 43s +72d 12m 01s",
                "filter": "R",
                "exptime": 50,
                "fwhm": 2.4,
                "ell": 0.09,
                "zpoint": 18.47,
                "background": 18.21,
                "raw_path": "/master/imdata/rawdata/2018/10/02/380242.fit.gz",
                "header_path": "/master/imdata/procdata/2018/10/02/380242_header.fits",
                "dark_path": "/master/imdata/rawdata/2018/10/02/380194.fit.gz",
                "flat_path": "/master/imdata/rawdata/2018/09/10/373627.fit.gz",
            }
        ]
    )

    def connect_fn(dsn):
        assert dsn == "postgresql://user:password@example.invalid/imdata"
        return _FakeConnection(cursor)

    rows = search_master_archive(
        database_url="postgresql+psycopg://user:password@example.invalid/imdata",
        ra_deg=327.60625,
        dec_deg=72.1686111,
        filters=["R", "V"],
        connect_fn=connect_fn,
    )
    assert len(rows) == 1
    assert rows[0]["night_utc"] == "2018-10-02"
    assert rows[0]["raw_id"] == 380242
    assert rows[0]["dark_id"] == 380194
    assert rows[0]["flat_id"] == 373627
    assert rows[0]["raw_url"].endswith("/data/master/imdata/rawdata/2018/10/02/380242.fit.gz")
    assert "%(box_0)s::sbox" in cursor.sql
    assert cursor.params["filters"] == ["R", "V"]


def test_master_processing_html_parser_extracts_real_row_shape() -> None:
    from internal_tools.master_remote import parse_master_processing_html

    html = r'''
    <tr class="n" valign="top" bgcolor="#C0C0C0">
      <td>135123</td><td>2018-10-02 17:52:10<p></p></td>
      <td><p class="small"> 21h 50m 43s +72d 12m 01s</p></td>
      <td>R</td><td>50</td><td>2.4</td><td>0.09</td><td>18.47</td><td>18.21</td><td></td>
      <td><a href="https://master.kourovka.ru/master2/index.php?where_long=id+%3D+380242">/master/imdata/rawdata/2018/10/02/380242.fit.gz</a></td>
      <td><a href="https://master.kourovka.ru/master2/index.php?where_long=id+%3D+380194">380194.fit.gz</a></td>
      <td><a href="https://master.kourovka.ru/master2/index.php?where_long=id+%3D+373627">373627.fit.gz</a></td>
      <td><a href="https://master.kourovka.ru/master2/Show_Header.php?header=/master/imdata/procdata/2018/10/02/380242_header.fits">|Header</a></td>
    </tr>
    '''
    rows = parse_master_processing_html(html)
    assert len(rows) == 1
    row = rows[0]
    assert row["processed_frame_id"] == 135123
    assert row["raw_path"] == "/master/imdata/rawdata/2018/10/02/380242.fit.gz"
    assert row["header_path"] == "/master/imdata/procdata/2018/10/02/380242_header.fits"
    assert row["dark_id"] == 380194
    assert row["flat_id"] == 373627


class _HttpSearchResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _HttpSearchSession:
    def __init__(self, responses: list[_HttpSearchResponse]) -> None:
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


def test_master_http_search_resolves_dark_flat_and_builds_urls() -> None:
    from internal_tools.master_remote import search_master_archive_http

    processing_html = r'''
    <tr class="n"><td>135123</td><td>2018-10-02 17:52:10</td><td>21h 50m 43s +72d 12m 01s</td>
    <td>R</td><td>50</td><td>2.4</td><td>0.09</td><td>18.47</td><td>18.21</td><td></td>
    <td>/master/imdata/rawdata/2018/10/02/380242.fit.gz</td><td>380194.fit.gz</td><td>373627.fit.gz</td>
    <td><a href="https://master.kourovka.ru/master2/Show_Header.php?header=/master/imdata/procdata/2018/10/02/380242_header.fits">Header</a></td></tr>
    '''
    dark_html = '<a href="https://master.kourovka.ru/data/master/imdata/rawdata/2018/10/02/380194.fit.gz">RAW</a>'
    flat_html = '<a href="https://master.kourovka.ru/data/master/imdata/rawdata/2018/09/10/373627.fit.gz">RAW</a>'
    session = _HttpSearchSession([
        _HttpSearchResponse(processing_html),
        _HttpSearchResponse(flat_html),
        _HttpSearchResponse(dark_html),
    ])
    rows = search_master_archive_http(
        session=session,
        ra_deg=327.60625,
        dec_deg=72.1686111,
        radius_deg=0.8,
        limit=1,
        page_size=5,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["dark_path"] == "/master/imdata/rawdata/2018/10/02/380194.fit.gz"
    assert row["flat_path"] == "/master/imdata/rawdata/2018/09/10/373627.fit.gz"
    assert row["dark_url"].endswith("/data/master/imdata/rawdata/2018/10/02/380194.fit.gz")
    assert row["flat_url"].endswith("/data/master/imdata/rawdata/2018/09/10/373627.fit.gz")
    first_call = session.calls[0]
    assert first_call[0].endswith("/master2/processing.php")
    assert first_call[1]["params"]["coord2000"] == "327.60625000 72.16861110"
    assert "where_long" not in first_call[1]["params"]
    assert first_call[1]["params"]["order_by"] == "processed_frames.datetime"
    assert first_call[1]["params"]["maxrec"] == 5


def test_master_database_url_can_be_optional(monkeypatch) -> None:
    from internal_tools.master_remote import get_master_database_url

    monkeypatch.delenv("GRANDGOUSIER_MASTER_DATABASE_URL", raising=False)
    assert get_master_database_url(required=False) is None



def test_master_http_search_filters_official_coord_box_locally() -> None:
    from internal_tools.master_remote import search_master_archive_http

    processing_html = r'''
    <tr class="n"><td>1</td><td>2018-10-02 17:52:10</td><td>21h 50m 43s +72d 12m 01s</td>
    <td>R</td><td>50</td><td>2.4</td><td>0.09</td><td>18.47</td><td>18.21</td><td></td>
    <td>/master/imdata/rawdata/2018/10/02/380242.fit.gz</td><td></td><td></td>
    <td><a href="https://master.kourovka.ru/master2/Show_Header.php?header=/master/imdata/procdata/2018/10/02/380242_header.fits">Header</a></td></tr>
    <tr class="n"><td>2</td><td>2018-10-02 17:53:10</td><td>21h 50m 43s +73d 03m 01s</td>
    <td>R</td><td>50</td><td>2.4</td><td>0.09</td><td>18.47</td><td>18.21</td><td></td>
    <td>/master/imdata/rawdata/2018/10/02/380243.fit.gz</td><td></td><td></td>
    <td><a href="https://master.kourovka.ru/master2/Show_Header.php?header=/master/imdata/procdata/2018/10/02/380243_header.fits">Header</a></td></tr>
    '''
    session = _HttpSearchSession([_HttpSearchResponse(processing_html)])
    rows = search_master_archive_http(
        session=session,
        ra_deg=327.60625,
        dec_deg=72.1686111,
        radius_deg=0.8,
        filters=["R"],
        page_size=500,
    )
    assert [row["processed_frame_id"] for row in rows] == [1]


def test_master_http_search_rejects_radius_larger_than_web_coord_box() -> None:
    import pytest
    from internal_tools.master_remote import search_master_archive_http

    with pytest.raises(ValueError, match="radius_deg <= 0.95"):
        search_master_archive_http(
            session=_HttpSearchSession([]),
            ra_deg=327.60625,
            dec_deg=72.1686111,
            radius_deg=1.0,
        )


def test_master_processing_html_parser_infers_missing_header_link_from_raw_path() -> None:
    from internal_tools.master_remote import parse_master_processing_html

    # Production MASTER may omit the Header action link while the canonical
    # procdata header still exists next to the processed products.
    html = r'''
    <tr class="result"><td>135123</td><td>2018-09-07 17:52:10</td>
    <td>21h 50m 43s +72d 12m 01s</td><td>R</td><td>50</td><td>2.4</td><td>0.09</td>
    <td>18.47</td><td>18.21</td><td></td>
    <td>/master/imdata/rawdata/2018/09/07/375506.fit.gz</td><td>375453.fit.gz</td><td>373624.fit.gz</td>
    </tr>
    '''
    rows = parse_master_processing_html(html)
    assert len(rows) == 1
    assert rows[0]["raw_path"] == "/master/imdata/rawdata/2018/09/07/375506.fit.gz"
    assert rows[0]["header_path"] == "/master/imdata/procdata/2018/09/07/375506_header.fits"


def test_master_header_inference_rejects_noncanonical_paths() -> None:
    from internal_tools.master_remote import infer_master_header_path

    assert infer_master_header_path(None) is None
    assert infer_master_header_path("") is None
    assert infer_master_header_path("/tmp/375506.fit.gz") is None
    assert infer_master_header_path("/master/imdata/rawdata/2018/09/07/not_an_id.fit.gz") is None


def test_master_processing_html_parser_does_not_require_result_css_class() -> None:
    from internal_tools.master_remote import parse_master_processing_html

    html = r'''
    <table>
      <tr class="search-row"><td><table><tr><td>nested form row</td></tr></table></td></tr>
      <tr class="odd-result"><td>135123</td><td>2018-10-02 17:52:10</td>
      <td>21h 50m 43s +72d 12m 01s</td><td>R</td><td>50</td><td>2.4</td><td>0.09</td>
      <td>18.47</td><td>18.21</td><td></td>
      <td><a href="https://master.kourovka.ru/master2/index.php?where_long=id+%3D+380242">/master/imdata/rawdata/2018/10/02/380242.fit.gz</a></td>
      <td>380194.fit.gz</td><td>373627.fit.gz</td>
      <td><a href="https://master.kourovka.ru/master2/Show_Header.php?header=/master/imdata/procdata/2018/10/02/380242_header.fits">Header</a></td></tr>
    </table>
    '''
    rows = parse_master_processing_html(html)
    assert len(rows) == 1
    assert rows[0]["processed_frame_id"] == 135123
    assert rows[0]["raw_path"].endswith("/380242.fit.gz")
    assert rows[0]["dark_id"] == 380194
    assert rows[0]["flat_id"] == 373627


def test_master_processing_html_parser_locates_raw_column_and_action_link_path() -> None:
    from internal_tools.master_remote import parse_master_processing_html

    # Some MASTER deployments omit/rearrange presentation-only columns and may
    # render only a basename in the Raw cell.  The download action still carries
    # the canonical /data/master/... URL and should be enough to recover raw_path.
    html = r'''
    <tr class="N"><td>135123</td><td>2018-10-02 17:52:10</td>
    <td>21h 50m 43s +72d 12m 01s</td><td>R</td><td>50</td><td>2.4</td><td>0.09</td>
    <td>18.47</td><td>18.21</td>
    <td>380242.fit.gz</td><td>380194.fit.gz</td><td>373627.fit.gz</td>
    <td>
      <a href="https://master.kourovka.ru/data/master/imdata/rawdata/2018/10/02/380242.fit.gz">RAW</a>
      <a href="https://master.kourovka.ru/data/master/imdata/procdata/2018/10/02/380242_header.fits">Header</a>
    </td></tr>
    '''
    rows = parse_master_processing_html(html)
    assert len(rows) == 1
    assert rows[0]["raw_path"] == "/master/imdata/rawdata/2018/10/02/380242.fit.gz"
    assert rows[0]["header_path"] == "/master/imdata/procdata/2018/10/02/380242_header.fits"
    assert rows[0]["dark_id"] == 380194
    assert rows[0]["flat_id"] == 373627
