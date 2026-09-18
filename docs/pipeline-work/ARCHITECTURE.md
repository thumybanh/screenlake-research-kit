# Current Architecture Snapshot

Live state of the pipeline as of the most recent commit on `thumybanh/main`.

## The three top-level S3 prefixes

```
my-tcu-bucket/
├── academia/                           ← raw, immutable, machine-only
│   ├── log_events_v2/
│   │   └── {emailHash}/
│   │       └── {buildVersion}/
│   │           └── {uuid}.csv          ← app diagnostic logs
│   └── tenant/
│       └── {tenantId}_{tenantName}/
│           └── panel/{panelId}/
│               └── {buildVersion}/
│                   └── panelist/{emailHash}/
│                       └── {uuid}.zip  ← research payload
│
├── data/                               ← derived, browsable, throwaway
│   └── panel={panelId}/
│       └── panelist={participantId}/
│           └── date={YYYY-MM-DD}/
│               ├── screenshots_{zipUuid}.csv
│               ├── sessions_{zipUuid}.csv
│               ├── app_segments_{zipUuid}.csv
│               ├── accessibility_{zipUuid}.csv
│               └── log_{zipUuid}.csv
│
└── _manifests/                         ← derived, one summary per participant
    └── panel={panelId}/
        └── panelist={participantId}/
            └── _index.json             ← rolling summary
```

## What each prefix holds

**`academia/`** — raw, immutable audit record. Nothing renames, deletes, or modifies anything under here. Two subtrees:
- `academia/log_events_v2/*.csv` — app diagnostic events (OCR failed, upload succeeded, worker cycles, exceptions). Small text files. Written continuously by `writeLogsToCsv` at start of every `ZipFileWorker` cycle plus loose CSVs picked up by `UploadWorker.addLocalLogFiles`.
- `academia/tenant/**/*.zip` — actual research payload. Each zip contains up to 200 JPGs + 5 CSVs (screenshots, sessions, app_segments, accessibility, log). UUID filename is intentionally meaningless; contents are self-describing.

**`data/`** — Hive-partitioned CSV output from the unpacker Lambda. Each source zip produces one set of 5 CSVs (`{kind}_{zipUuid}.csv`). Files are never merged or appended; retries overwrite identical keys. Athena and pandas auto-discover the `panel=`, `panelist=`, `date=` partitions.

**`_manifests/`** — one JSON per participant, rewritten in place on every new zip event. Fields:
```json
{
  "panel": "general_1",
  "panelist": "2323",
  "study_start_utc": "2026-09-04T23:46:40Z",
  "latest_capture_utc": "2026-09-04T23:51:13Z",
  "days_participating": 1,
  "total_screenshots": 54,
  "updated_at": "2026-09-05T01:50:20Z",
  "_processed_zip_uuids": ["image_zip_...", "image_zip_..."]
}
```
Underscore prefix on `_manifests/` and `_processed_zip_uuids` intentionally hides them from tools (Athena, Glue crawlers) that auto-discover data tables.

## Android pipeline (what runs on the phone)

1. `ScreenshotService` captures a JPG every 5 seconds while phone is unlocked. Writes to `filesDir/img_<uuid>Screenshot_<date>.jpg`. Inserts row into Room `screenshot_table` with `isOcrComplete=false, text=null`.
2. **OCR trigger** — fires on one of:
   - Screen lock → `onScreenOff` → `beginOcr` at `ScreenshotService.kt:915`
   - Manual "Run OCR now" button → `manualOcr=true` → `beginOcr` at `:554`
   - Periodic `ZipFileWorker` every 1h → `Recognize.runPendingOcr` (our fix)
3. **OCR loop** — Tesseract processes pending screenshots in batches, sets `isOcrComplete=true, text=<cleaned OCR output>`.
4. **App-segment stamping** — `saveAllSessionSegments()` groups consecutive-same-app screenshots and stamps `appSegmentId`.
5. **Zip pipeline** — `ZipFileWorker.zipUpScreenshots(200)` pages through up to 200 screenshots at a time. Builds each zip with:
   - `screenshot_data_csv_<zipUuid>.csv`
   - `session_data_csv_<zipUuid>.csv`
   - `app_segment_data_csv_<zipUuid>.csv`
   - `app_accessibility_data_csv_<zipUuid>.csv`
   - `log_data_<zipUuid>.csv`
   - N JPGs (N <= 200)
6. **Upload** — `UploadWorker` picks up all queued zips + loose log CSVs. `RealUploadHandler.buildUploadPath` routes by extension: `.csv` → `academia/log_events_v2/...`, everything else → `academia/tenant/.../panelist/{emailHash}/{filename}`.
7. On upload success, `handleUploadResult` deletes the local file + DB row.

## Lambda pipeline (what runs on AWS)

**Function:** `screenlake-unpack-zips` (Python 3.12, us-east-2, account `842294335158`)

**Trigger:** S3 `ObjectCreated:*` events with prefix `academia/tenant/` and suffix `.zip`.

**Per invocation:**
1. Match key against `academia/tenant/{tenant}/panel/{panel}/{version}/panelist/{panelist}/{zip_name}.zip` regex. Skip if not matching.
2. Download zip to `/tmp/`.
3. Open zip. Extract the 5 CSVs.
4. Parse `screenshot_data_csv_*.csv`. Require columns: `file`, `apk`, `id_session`, `t_epoch_ts_ms`. Fail loudly if any missing.
5. Validate every JPG in zip has a matching CSV row by basename. Fail loudly if not.
6. Compute per-zip stats: min/max epoch timestamp, distinct date partitions from UTC epoch.
7. Load `_index.json` for this panelist from `_manifests/`. If missing, create with defaults.
8. **Idempotency check:** if `zip_uuid` already in `_processed_zip_uuids`, skip everything and return. Retries and re-invocations are safe no-ops.
9. Write each CSV to `data/panel={panel}/panelist={panelist}/date={date}/{kind}_{zip_uuid}.csv` for every date partition the zip touches. Content-Type = `text/csv`.
10. Update `_index.json`: bump `study_start_utc` if earlier, `latest_capture_utc` if later, sum `total_screenshots`, recompute `days_participating`, append `zip_uuid` to ledger, refresh `updated_at`.
11. Write `_index.json` back to `_manifests/panel={panel}/panelist={panelist}/_index.json`. Content-Type = `application/json`.

**IAM role `screenlake-unpack-zips-role`:**
- `s3:GetObject` on `academia/*` (read raw)
- `s3:GetObject` on `_manifests/*` (read for upsert)
- `s3:PutObject` on `data/*` and `_manifests/*` (write derived)
- `s3:ListBucket` on `academia/*`, `data/*`, `_manifests/*`
- **Explicit Deny on all `s3:Delete*`** bucket-wide (raw and derived both protected)
- CloudWatch Logs write

## Currently live in the test bucket

- **Participant 1234:** 8 zips totaling 169 screenshots over 3 days (Sep 2 → Sep 4).
- **Participant 2323:** 2 zips totaling 54 screenshots on 1 day (Sep 4).

Both have `_index.json` under `_manifests/`. Both have full `data/` CSV trees. Emulator on `emulator-5554` (Pixel 6 API 33) was the capture source.

## What's built but not yet integrated

None currently. All shipped work is live on `thumybanh/main` + deployed to AWS.

## What's in flight (see CHANGELOG.md for details)

- TCU code auto-assignment via Lambda + DynamoDB, replacing the 4-digit invite code screen.
