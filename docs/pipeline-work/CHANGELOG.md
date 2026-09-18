# Pipeline Work — Changelog

Running log of every change made to the fork beyond upstream Screenlake, in reverse chronological order (newest first). Each entry links the commit + why it was made.

---

## In progress — TCU code auto-assignment (Lambda + Cognito)

**Problem being solved:** the current invite code screen accepts any 4-digit number a participant types. Two participants can type the same code → data collides under the same `panelist=<code>/` prefix. At 60 participants, random 4-digit assignment has a 16% collision rate. Manual code assignment is possible but risks researcher error.

**Design chosen:** Option 1 — Lambda-assigned sequential codes backed by DynamoDB atomic counter. Guaranteed unique. No manual assignment. Preserves 4-digit readability. Reinstall-safe via Cognito attribute lookup.

**Rejected alternatives:**
- Random 4-digit: 16% collision at 60 users, unsafe.
- Researcher pre-assigns via Cognito Console: eliminates uniqueness bug but requires manual per-participant work.
- Client-side S3 existence check: race conditions, false positives on reinstall, still requires IAM changes.

**Deliverables in flight:**
- `lambda/assign_tcu_code/` — Lambda handler, IAM policy, DynamoDB setup, tests, README.
- Android changes: remove invite code dialog, call Lambda after Cognito signup, save code to Cognito custom attribute, display as `TCU-<code>` in-app.
- Cognito schema: add `custom:tcu_code` attribute.
- DynamoDB pre-populated with reserved codes 1234 and 2323 (existing test data).

---

## d4d1d0a — Fix debug log recording per-screenshot IDs

**Commit:** `d4d1d0a`
**Files:** `app/src/main/java/com/screenlake/recorder/services/ZipFileWorker.kt`
**One-line change:** `screenshots.map { id }` → `screenshots.map { it.id }`.

Pre-existing bug from the initial Screenlake commit. The zip debug log line at `ZipFileWorker.kt:129` was capturing an outer-scope variable instead of iterating each screenshot's row ID. Result: every log entry showed the same UUID repeated 50 times instead of 50 distinct screenshot IDs. Cosmetic only — no functional impact on upload, zipping, or manifests.

---

## f343feb — Slim S3 derived layout; raise zip batch to 200

**Commit:** `f343feb`
**Scope:** Lambda + one Android constant.

**Why:** the original derived layout (`unpacked/panel=/panelist=/date=/{images,data}/` + per-zip `_manifests/`) was noisy. Individual JPGs extracted to S3 were browsable but created hundreds of files per participant. Per-zip manifests meant researchers had to scroll through dozens of JSON files. Simplified to two prefixes: `data/` for CSVs, `_manifests/` for one rolling summary per participant.

**Lambda changes** (`lambda/unpack_zips/handler.py`):
- No longer extracts JPGs. Zips are already in `academia/tenant/`; researchers download the specific zip when they need images.
- Extracts only CSVs to `data/panel=/panelist=/date=YYYY-MM-DD/{screenshots,sessions,app_segments,accessibility,log}_{zipUuid}.csv`. One CSV set per source zip. Never merged or appended.
- Writes single rolling summary per participant to `_manifests/panel=/panelist=/_index.json`:
  ```json
  {
    "panel", "panelist",
    "study_start_utc", "latest_capture_utc",
    "days_participating", "total_screenshots",
    "updated_at",
    "_processed_zip_uuids": [...]
  }
  ```
- Idempotency: `_processed_zip_uuids` ledger. Same zip reprocessed = no-op.
- Reserved concurrency = 1 recommended but blocked by AWS account quota (needs >=10 unreserved). Race protection currently relies on the idempotency ledger alone, which is sufficient at expected event rates.

**Android change:**
- `ZipFileWorker.kt`: `zipUpScreenshots(50)` → `zipUpScreenshots(200)`. Each zip now bundles up to 200 screenshots instead of 50. Result: ~4× fewer zips per participant, ~20 MB per zip at 100 KB/JPG, same OCR loop and no meaningful battery cost.

**Also:** IAM policy updated. Added `s3:GetObject` on `_manifests/*` (needed to read the index for upsert). Swapped `s3:PutObject` from `unpacked/*` to `data/*`. Explicit `s3:Delete*` bucket-wide deny still applies.

**Tests:** rewrote `test_handler.py` — 11 pytest cases covering happy path, midnight-straddling zip, missing/empty fields, non-matching key, replay idempotency. All green.

**Verified end-to-end:** backfilled all 10 raw zips. Produced `_index.json` for two participants (panelist=1234 with 169 screenshots over 3 days, panelist=2323 with 54 screenshots over 1 day).

---

## 8ea8df5 — Fix screenshot upload pipeline; add S3 unpacker Lambda

**Commit:** `8ea8df5`
**Scope:** major — Android pipeline fix + new Lambda.

### The original bug

Only diagnostic log CSVs were reaching S3 under `academia/log_events_v2/...`. No screenshots ever landed at `academia/tenant/...`. Root cause: the zip pipeline depended on the phone screen locking to trigger OCR (via `ScreenshotService.beginOcr`), and screenshots stayed marked `isOcrComplete=0` and `appSegmentId=NULL` in Room DB forever if the trigger never fired. `getScreenshotCount()` returned 0 → `ZipFileWorker` skipped every run → nothing uploaded.

### Android fixes (~7 files)

**`Recognize.kt`** — added `runPendingOcr(context)` batch method:
- Callable from any worker context, not just `ScreenshotService`.
- Mutex-guarded so it can't race with `ScreenshotService.beginOcr`.
- Initializes Tesseract, iterates pending screenshots, marks each `isOcrComplete=1` regardless of OCR success (so one bad screenshot never blocks the whole pipeline forever), stops Tesseract when done.

**`ZipFileWorker.kt`** — drives OCR + segment stamping itself:
- Before `getZippableScreenshotCount()`, calls `recognize.runPendingOcr(context)` and `generalOperationsRepository.saveAllSessionSegments()`. Removes dependence on screen-lock trigger.
- Now runs hourly via periodic worker AND fires immediately when chained from `beginOcr` after screen-lock.

**`ScreenshotDao.kt`** — added `getZippableCount()`:
- Old count query: `WHERE isOcrComplete = 1 OR isAppRestricted = 1`
- Pagination query: `WHERE (isOcrComplete = 1 AND appSegmentId IS NOT NULL) OR (isAppRestricted = 1 AND appSegmentId IS NOT NULL)`
- They disagreed. Count reported >0 while pagination returned empty → `hasMoreScreenshots=false` → zip skipped silently.
- New count matches pagination WHERE exactly. Aligned.

**`GeneralOperationsRepository.kt`** — thin wrapper for the new count.

**`TestWorkerFactory.kt`** — accepts a `Recognize` mock via default parameter so existing test call sites don't break.

**`RecognizeInstrumentedTest.kt`** — drive-by fix for pre-existing constructor mismatch (was passing 1 arg to 2-arg constructor since initial Screenlake commit).

### New Lambda (initial version, replaced later by f343feb)

`lambda/unpack_zips/handler.py` — Python 3.12 S3-triggered handler. Initial version extracted JPGs to `unpacked/.../images/` with UTC-stamped filenames plus per-zip `_manifests/*.json`. Later slimmed to CSV-only + rolling `_index.json` in commit `f343feb`.

Includes:
- `iam_policy.json` — role permissions: read raw, write derived, explicit deny on all delete actions.
- `trust_policy.json` — Lambda service assume-role.
- `event_notification.json` — S3 event filter for `academia/tenant/**/*.zip`.
- `test_handler.py` — 9 pytest cases at that point.
- `README.md` — deploy + backfill + update-code recipes.

### Verified end-to-end

Emulator captured → OCR ran → zips built → uploaded to `academia/tenant/general_tenant_1_general_tenant/panel/general_1/V_12/panelist/1234/image_zip_*.zip`. 7 zips landed in the first successful cycle. Confirmed via `screenshot_zip_table` DB inspection + S3 listing.

---

## Pre-session state (unchanged from Screenlake upstream)

Base commit `a9f79f7` (Screenlake main). Contains the full Android app: capture engine, MediaProjection service, Tesseract OCR wiring, Room DB, Amplify + Cognito auth, WorkManager plumbing, invite code screen, S3 upload path template. Everything from `academia/` prefix to CSV schema comes from Screenlake.

---

## Files never modified by us

- Entire onboarding UI (`LoginFragment`, `RegisterConfirmPassword*`, `RegisterLoadingFragment` — until the TCU work in progress)
- Cognito auth flow (`AmplifyRepository`, `CloudAuthentication`)
- Screenshot capture engine (`ScreenshotService.takeScreenshot`, `ScreenshotManager`)
- MediaProjection service wiring
- Accessibility event collectors (`TouchAccessibilityService`, all `behaviors/handlers/*`)
- The 5-CSV schema (`DataTransformation.createScreenshotCsv` etc.)
- S3 upload path template (`RealUploadHandler.buildUploadPath`)
- Log event upload flow (`UploadWorker.addLocalLogFiles`)
- Native library bridge (`NativeLib`)
