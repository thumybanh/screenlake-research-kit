"""S3-triggered Lambda: extract CSVs from Screenlake research zips and keep a
per-participant summary JSON up to date.

Reads only from ``academia/`` (immutable raw tree). Writes:

* ``data/panel=/panelist=/date=YYYY-MM-DD/*.csv`` — CSV metadata per source zip.
* ``_manifests/panel=/panelist=/_index.json`` — rolling summary per participant.

The Lambda does not extract JPGs. Researchers open the summary JSON to see
participant scope, then download the specific raw zip when they want images.

Concurrency safety: the summary JSON is updated in-place. Deploy this function
with reserved concurrency = 1 so two zips can never race the read-modify-write.
Retries are still safe because each zip's ``zip_uuid`` is idempotency-keyed
into ``_processed_zip_uuids``.
"""
from __future__ import annotations

import csv
import io
import json
import os
import posixpath
import re
import zipfile
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

s3 = boto3.client("s3")

RAW_KEY_RE = re.compile(
    r"^academia/tenant/(?P<tenant>[^/]+)/panel/(?P<panel>[^/]+)/"
    r"(?P<version>[^/]+)/panelist/(?P<panelist>[^/]+)/(?P<zip_name>[^/]+\.zip)$"
)

CSV_KINDS = {
    "screenshots":   "screenshot_data_csv_",
    "sessions":      "session_data_csv_",
    "app_segments":  "app_segment_data_csv_",
    "accessibility": "app_accessibility_data_csv_",
    "log":           "log_data_",
}

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
    zip_uuid = os.path.splitext(zip_name)[0]

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
    _require(screenshot_rows,
             f"screenshot_data_csv in {src_key} has no rows")

    # Compute per-zip stats before touching the index — failing here means the
    # index stays unchanged and the invocation can be retried cleanly.
    jpg_members = [n for n in members if n.lower().endswith(".jpg")]
    row_by_basename = {}
    for r in screenshot_rows:
        row_by_basename[_basename(r[COL_FILE])] = r
    for jpg in jpg_members:
        _require(_basename(jpg) in row_by_basename,
                 f"no screenshot_data row for {jpg} in {src_key}")

    date_partitions: set[str] = set()
    min_epoch_ms: Optional[int] = None
    max_epoch_ms: Optional[int] = None
    for row in screenshot_rows:
        epoch_ms = _to_int_ms(row[COL_EPOCH_MS], "<row>", src_key)
        _require_nonempty(row[COL_SESSION_ID], COL_SESSION_ID, "<row>", src_key)
        _require_nonempty(row[COL_APK], COL_APK, "<row>", src_key)
        min_epoch_ms = epoch_ms if min_epoch_ms is None else min(min_epoch_ms, epoch_ms)
        max_epoch_ms = epoch_ms if max_epoch_ms is None else max(max_epoch_ms, epoch_ms)
        date_partitions.add(
            datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
                    .strftime("%Y-%m-%d")
        )

    # Idempotency check — bail out before any S3 writes if this zip is already
    # accounted for in the index. Retries and manual re-invocations become no-ops.
    index_key = f"_manifests/panel={panel}/panelist={panelist}/_index.json"
    index = _load_index(bucket, index_key, panel, panelist)
    if zip_uuid in index["_processed_zip_uuids"]:
        print(f"skip: {zip_uuid} already in index for panelist={panelist}")
        return

    # Extract CSVs to data/. Each date partition gets its own copy of every CSV
    # so date= directories stay self-describing without cross-partition joins.
    for date_partition in date_partitions:
        for kind, meta in csvs.items():
            if meta is None:
                continue
            out_key = (
                f"data/panel={panel}/panelist={panelist}/date={date_partition}/"
                f"{kind}_{zip_uuid}.csv"
            )
            s3.put_object(
                Bucket=bucket,
                Key=out_key,
                Body=meta["bytes"],
                ContentType="text/csv",
            )

    # Update the rolling index. Reserved concurrency = 1 keeps this safe from
    # overlapping invocations; without it two Lambdas could clobber each other.
    new_start = datetime.fromtimestamp(min_epoch_ms / 1000.0, tz=timezone.utc)
    new_latest = datetime.fromtimestamp(max_epoch_ms / 1000.0, tz=timezone.utc)

    if index["study_start_utc"] is None:
        index["study_start_utc"] = _iso_z(new_start)
    else:
        existing_start = _parse_iso_z(index["study_start_utc"])
        if new_start < existing_start:
            index["study_start_utc"] = _iso_z(new_start)

    if index["latest_capture_utc"] is None:
        index["latest_capture_utc"] = _iso_z(new_latest)
    else:
        existing_latest = _parse_iso_z(index["latest_capture_utc"])
        if new_latest > existing_latest:
            index["latest_capture_utc"] = _iso_z(new_latest)

    index["total_screenshots"] += len(jpg_members)
    index["_processed_zip_uuids"].append(zip_uuid)

    # Days participating = elapsed calendar days from start to latest, inclusive.
    start_date = _parse_iso_z(index["study_start_utc"]).date()
    latest_date = _parse_iso_z(index["latest_capture_utc"]).date()
    index["days_participating"] = (latest_date - start_date).days + 1

    index["updated_at"] = _iso_z(datetime.now(tz=timezone.utc))

    s3.put_object(
        Bucket=bucket,
        Key=index_key,
        Body=json.dumps(index, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    print(
        f"ok: src={src_key} images={len(jpg_members)} dates={sorted(date_partitions)} "
        f"total_screenshots={index['total_screenshots']} "
        f"days_participating={index['days_participating']}"
    )


def _load_index(bucket: str, key: str, panel: str, panelist: str) -> dict:
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        data = json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("NoSuchKey", "404"):
            raise
        data = None

    if data is None:
        return {
            "panel": panel,
            "panelist": panelist,
            "study_start_utc": None,
            "latest_capture_utc": None,
            "days_participating": 0,
            "total_screenshots": 0,
            "updated_at": None,
            "_processed_zip_uuids": [],
        }

    # Fill in any fields missing from an older index shape.
    data.setdefault("panel", panel)
    data.setdefault("panelist", panelist)
    data.setdefault("study_start_utc", None)
    data.setdefault("latest_capture_utc", None)
    data.setdefault("days_participating", 0)
    data.setdefault("total_screenshots", 0)
    data.setdefault("updated_at", None)
    data.setdefault("_processed_zip_uuids", [])
    return data


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


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_z(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
