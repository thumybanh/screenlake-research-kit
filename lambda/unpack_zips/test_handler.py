"""Local unit tests for handler.py — do not run in Lambda.
Run: python -m pytest lambda/unpack_zips/test_handler.py
"""
from __future__ import annotations

import io
import json
import zipfile
from unittest.mock import patch

import pytest

import handler as H


def _build_zip(rows, jpg_names, extra_csvs=None) -> bytes:
    """Build an in-memory zip that mimics ZipFileWorker output.
    rows: list of dicts keyed by the screenshot CSV column names.
    jpg_names: list of member names to include as fake JPGs.
    extra_csvs: dict[name -> bytes] to include as additional CSVs.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        header = [
            "id_user", "file", "zipFileId", "apk", "id_session",
            "id_segment", "app", "text", "weekday", "t_epoch_ts_ms",
            "t_natural_utc_ts", "t_natural_second_ts", "t_natural_day_ts",
        ]
        lines = [",".join(f'"{h}"' for h in header)]
        for r in rows:
            lines.append(",".join(f'"{r.get(h, "")}"' for h in header))
        csv_body = ("\n".join(lines) + "\n").encode("utf-8")
        zf.writestr("screenshot_data_csv_batch1.csv", csv_body)
        for name in jpg_names:
            zf.writestr(name, b"\xff\xd8\xff\xe0FAKE-JPEG-BYTES")
        for name, body in (extra_csvs or {}).items():
            zf.writestr(name, body)
    return buf.getvalue()


class FakeS3:
    def __init__(self, zip_bytes):
        self.zip_bytes = zip_bytes
        self.puts = []

    def download_file(self, bucket, key, local_path):
        with open(local_path, "wb") as fh:
            fh.write(self.zip_bytes)

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        return {"ETag": "fake"}


def _run(zip_bytes, key="academia/tenant/T_A/panel/P/V/panelist/1234/image_zip_uuid_2.zip"):
    fake = FakeS3(zip_bytes)
    with patch.object(H, "s3", fake):
        H.process_zip("bucket", key)
    return fake.puts


def test_happy_path_single_date():
    rows = [
        {"file": "/dev/img_a.jpg", "apk": "com.android.chrome",
         "id_session": "sess-1", "t_epoch_ts_ms": "1788526805000"},
        {"file": "/dev/img_b.jpg", "apk": "com.android.chrome",
         "id_session": "sess-1", "t_epoch_ts_ms": "1788526810000"},
    ]
    puts = _run(_build_zip(rows, ["img_a.jpg", "img_b.jpg"]))
    keys = [p["Key"] for p in puts]
    assert any("images/20260904T" in k and k.endswith(".jpg") for k in keys)
    # Manifest exists.
    manifest_puts = [p for p in puts if p["Key"].startswith("_manifests/")]
    assert len(manifest_puts) == 1
    manifest = json.loads(manifest_puts[0]["Body"].decode("utf-8"))
    assert manifest["counts"]["images"] == 2
    assert manifest["session_ids"] == ["sess-1"]
    # Content types set correctly.
    for p in puts:
        if p["Key"].endswith(".jpg"):
            assert p["ContentType"] == "image/jpeg"
        elif p["Key"].endswith(".csv"):
            assert p["ContentType"] == "text/csv"
        elif p["Key"].endswith(".json"):
            assert p["ContentType"] == "application/json"


def test_midnight_split_writes_both_dates():
    rows = [
        {"file": "/dev/img_a.jpg", "apk": "com.a", "id_session": "s1",
         "t_epoch_ts_ms": "1788479999000"},  # 2026-09-03 23:59:59 UTC
        {"file": "/dev/img_b.jpg", "apk": "com.a", "id_session": "s1",
         "t_epoch_ts_ms": "1788480001000"},  # 2026-09-04 00:00:01 UTC
    ]
    puts = _run(_build_zip(rows, ["img_a.jpg", "img_b.jpg"]))
    image_keys = [p["Key"] for p in puts if p["Key"].endswith(".jpg")]
    dates = {k.split("date=")[1].split("/")[0] for k in image_keys}
    assert dates == {"2026-09-03", "2026-09-04"}
    # CSVs replicated to both date partitions.
    csv_keys = [p["Key"] for p in puts if p["Key"].endswith(".csv")]
    csv_dates = {k.split("date=")[1].split("/")[0] for k in csv_keys}
    assert csv_dates == {"2026-09-03", "2026-09-04"}


def test_missing_csv_row_raises():
    rows = [{"file": "/dev/img_a.jpg", "apk": "com.a", "id_session": "s1",
             "t_epoch_ts_ms": "1788480001000"}]
    with pytest.raises(H.HandlerError, match="no screenshot_data row"):
        _run(_build_zip(rows, ["img_a.jpg", "img_MYSTERY.jpg"]))


def test_missing_columns_raises():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("screenshot_data_csv_x.csv", b'"file"\n"a.jpg"\n')
        zf.writestr("img_a.jpg", b"data")
    with pytest.raises(H.HandlerError, match="missing columns"):
        _run(buf.getvalue())


def test_empty_session_id_raises():
    rows = [{"file": "/dev/img_a.jpg", "apk": "com.a", "id_session": "",
             "t_epoch_ts_ms": "1788480001000"}]
    with pytest.raises(H.HandlerError, match="empty id_session"):
        _run(_build_zip(rows, ["img_a.jpg"]))


def test_null_apk_raises():
    rows = [{"file": "/dev/img_a.jpg", "apk": "null", "id_session": "s1",
             "t_epoch_ts_ms": "1788480001000"}]
    with pytest.raises(H.HandlerError, match="empty apk"):
        _run(_build_zip(rows, ["img_a.jpg"]))


def test_bad_epoch_raises():
    rows = [{"file": "/dev/img_a.jpg", "apk": "com.a", "id_session": "s1",
             "t_epoch_ts_ms": "not-a-number"}]
    with pytest.raises(H.HandlerError, match="invalid t_epoch_ts_ms"):
        _run(_build_zip(rows, ["img_a.jpg"]))


def test_key_not_matching_layout_is_skipped():
    """Zip uploaded outside academia/tenant/ prefix should be no-op."""
    fake = FakeS3(_build_zip([], []))
    with patch.object(H, "s3", fake):
        H.process_zip("bucket", "somewhere_else/foo.zip")
    assert fake.puts == []


def test_collision_appends_suffix():
    # Two screenshots at same second, same session, same apk.
    rows = [
        {"file": "/dev/img_a.jpg", "apk": "com.a", "id_session": "s1",
         "t_epoch_ts_ms": "1788480001000"},
        {"file": "/dev/img_b.jpg", "apk": "com.a", "id_session": "s1",
         "t_epoch_ts_ms": "1788480001500"},  # same second
    ]
    puts = _run(_build_zip(rows, ["img_a.jpg", "img_b.jpg"]))
    jpg_keys = [p["Key"] for p in puts if p["Key"].endswith(".jpg")]
    assert len(jpg_keys) == 2
    assert any("__1.jpg" in k for k in jpg_keys)
