"""S3-triggered Lambda: unpack Screenlake research zip batches into a browsable
derived tree.

Read-only against the raw prefix (academia/tenant/...); writes only to
unpacked/ (data) and _manifests/ (provenance). See README.md for deploy steps
and IAM.
"""
from __future__ import annotations

import csv
import io
import json
import os
import posixpath
import re
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import unquote_plus

import boto3

s3 = boto3.client("s3")

RAW_KEY_RE = re.compile(
    r"^academia/tenant/(?P<tenant>[^/]+)/panel/(?P<panel>[^/]+)/"
    r"(?P<version>[^/]+)/panelist/(?P<panelist>[^/]+)/(?P<zip_name>[^/]+\.zip)$"
)

# Zip member basename prefixes → normalized CSV kind used in output filenames.
# Kinds mirror DataTransformation.* producers in the Android app.
CSV_KINDS = {
    "screenshots":   "screenshot_data_csv_",
    "sessions":      "session_data_csv_",
    "app_segments":  "app_segment_data_csv_",
    "accessibility": "app_accessibility_data_csv_",
    "log":           "log_data_",
}

# Required columns on the screenshot CSV. These map JPGs → time / session / app.
# See DataTransformation.createScreenshotCsv in the Android app.
COL_FILE       = "file"
COL_APK        = "apk"
COL_SESSION_ID = "id_session"
COL_EPOCH_MS   = "t_epoch_ts_ms"


class HandlerError(RuntimeError):
    """Raised on any invariant violation. Loud failure — never write placeholders."""


def lambda_handler(event, _context):
    records = event.get("Records", [])
    for record in records:
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        process_zip(bucket, key)
    return {"processed": len(records)}


def process_zip(bucket: str, key: str) -> None:
    match = RAW_KEY_RE.match(key)
    if not match:
        print(f"skip: key does not match raw layout: {key}")
        return

    panel = match["panel"]
    panelist = match["panelist"]
    zip_name = match["zip_name"]
    zip_uuid = os.path.splitext(zip_name)[0]  # image_zip_<uuid>_<n>

    local_path = f"/tmp/{zip_name}"
    s3.download_file(bucket, key, local_path)

    try:
        with zipfile.ZipFile(local_path) as zf:
            _process_zip_open(bucket, key, zf, panel, panelist, zip_uuid)
    finally:
        try:
            os.remove(local_path)
        except OSError:
            pass


def _process_zip_open(
    bucket: str,
    src_key: str,
    zf: zipfile.ZipFile,
    panel: str,
    panelist: str,
    zip_uuid: str,
) -> None:
    members = zf.namelist()
    csvs = _index_csvs(zf, members)
    _require(csvs["screenshots"] is not None,
             f"screenshot_data_csv_* missing from {src_key}")

    screenshot_rows = _parse_screenshot_csv(csvs["screenshots"]["bytes"], src_key)
    row_by_basename = {}
    for r in screenshot_rows:
        row_by_basename[_basename(r[COL_FILE])] = r

    jpg_members = [n for n in members if n.lower().endswith(".jpg")]
    dates_written: set[str] = set()
    image_mapping: list[dict] = []

    # Track duplicate output names within one zip run (rare but possible if two
    # screenshots share second-resolution timestamp + session + package).
    collisions: dict[str, int] = defaultdict(int)

    for jpg in jpg_members:
        row = row_by_basename.get(_basename(jpg))
        _require(row is not None,
                 f"no screenshot_data row for {jpg} in {src_key}")

        epoch_ms = _to_int_ms(row[COL_EPOCH_MS], jpg, src_key)
        session_id = _require_nonempty(row[COL_SESSION_ID], COL_SESSION_ID, jpg, src_key)
        apk = _require_nonempty(row[COL_APK], COL_APK, jpg, src_key)

        dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
        date_partition = dt.strftime("%Y-%m-%d")
        stamp = dt.strftime("%Y%m%dT%H%M%SZ")

        base_name = f"{stamp}_{_sanitize(session_id)}_{_sanitize(apk)}"
        seed = f"{date_partition}/{base_name}"
        n = collisions[seed]
        collisions[seed] += 1
        out_name = f"{base_name}.jpg" if n == 0 else f"{base_name}__{n}.jpg"

        out_key = (
            f"unpacked/panel={panel}/panelist={panelist}/date={date_partition}/"
            f"images/{out_name}"
        )
        with zf.open(jpg) as fh:
            body = fh.read()
        s3.put_object(
            Bucket=bucket,
            Key=out_key,
            Body=body,
            ContentType="image/jpeg",
        )
        dates_written.add(date_partition)
        image_mapping.append({
            "zip_member": jpg,
            "out_key": out_key,
            "csv_row_file": row[COL_FILE],
            "epoch_ms": epoch_ms,
            "date_partition": date_partition,
        })

    # Zip might contain zero JPGs (metadata-only batch). Pick the earliest
    # screenshot-CSV row date so the CSVs still land somewhere sensible.
    if not dates_written and screenshot_rows:
        earliest = min(
            _to_int_ms(r[COL_EPOCH_MS], "<csv-only>", src_key)
            for r in screenshot_rows
        )
        dates_written.add(
            datetime.fromtimestamp(earliest / 1000.0, tz=timezone.utc)
                    .strftime("%Y-%m-%d")
        )

    # A zip whose screenshots span multiple UTC dates writes each CSV into
    # every relevant date partition so each date= directory is self-describing.
    for date_partition in dates_written:
        for kind, meta in csvs.items():
            if meta is None:
                continue
            out_name = f"{kind}_{zip_uuid}.csv"
            out_key = (
                f"unpacked/panel={panel}/panelist={panelist}/date={date_partition}/"
                f"data/{out_name}"
            )
            s3.put_object(
                Bucket=bucket,
                Key=out_key,
                Body=meta["bytes"],
                ContentType="text/csv",
            )

    session_ids = sorted(
        {r[COL_SESSION_ID] for r in screenshot_rows if r.get(COL_SESSION_ID)}
    )
    manifest = {
        "source_zip_key": src_key,
        "zip_uuid": zip_uuid,
        "panel": panel,
        "panelist": panelist,
        "counts": {
            "images": len(image_mapping),
            "screenshot_rows": len(screenshot_rows),
            "csvs_present": [k for k, v in csvs.items() if v is not None],
        },
        "date_partitions": sorted(dates_written),
        "session_ids": session_ids,
        "images": image_mapping,
    }
    manifest_key = f"_manifests/panel={panel}/panelist={panelist}/{zip_uuid}.json"
    s3.put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    print(
        f"ok: src={src_key} images={len(image_mapping)} "
        f"dates={sorted(dates_written)} manifest={manifest_key}"
    )


def _index_csvs(zf: zipfile.ZipFile, members: list[str]) -> dict[str, Optional[dict]]:
    result: dict[str, Optional[dict]] = {kind: None for kind in CSV_KINDS}
    for member in members:
        bn = _basename(member)
        for kind, prefix in CSV_KINDS.items():
            if bn.startswith(prefix) and bn.lower().endswith(".csv"):
                with zf.open(member) as fh:
                    result[kind] = {"name": member, "bytes": fh.read()}
                break
    return result


def _parse_screenshot_csv(raw: bytes, src_key: str) -> list[dict]:
    text = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    required = {COL_FILE, COL_APK, COL_SESSION_ID, COL_EPOCH_MS}
    fieldnames = set(reader.fieldnames or [])
    missing = required - fieldnames
    _require(not missing,
             f"screenshot_data_csv in {src_key} missing columns: {sorted(missing)}")
    return list(reader)


def _basename(path: str) -> str:
    return posixpath.basename(path.replace("\\", "/"))


def _sanitize(value: str) -> str:
    """Keep alphanumerics, dot, hyphen, underscore. Replace anything else with '-'.
    Package names like com.android.chrome pass through unchanged.
    """
    return re.sub(r"[^A-Za-z0-9._\-]", "-", value.strip())


def _to_int_ms(value, jpg: str, src_key: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise HandlerError(
            f"invalid {COL_EPOCH_MS}='{value}' for {jpg} in {src_key}"
        )


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise HandlerError(msg)


def _require_nonempty(value, field: str, jpg: str, src_key: str) -> str:
    v = "" if value is None else str(value).strip()
    if not v or v.lower() in ("null", "none"):
        raise HandlerError(f"empty {field}='{value}' for {jpg} in {src_key}")
    return v
