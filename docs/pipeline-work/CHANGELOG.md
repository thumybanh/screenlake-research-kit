# Pipeline Work — Changelog

Running log of every change made to the fork beyond upstream Screenlake, in reverse chronological order (newest first). Each entry links the commit + why it was made.

---

## Latest — Auto-fetch TCU code via Cognito custom attribute

**Scope:** Cognito schema + Lambda write + Android read. Replaces the earlier "researcher tells participant the code, participant types it" workflow with a fully automatic one, while keeping the manual invite dialog as fallback.

**Problem this fixed:** the manual invite screen accepted any 4-digit code. Participant could type the wrong number (typo, misheard, forgot which was theirs) and the app would happily route their data to a different `panelist=<code>/` prefix. Uniqueness enforcement lived only on the researcher side.

**How it works now:**
1. Participant registers a Cognito account via the app (email + password).
2. Researcher looks up their `sub` in Cognito Console.
3. Researcher runs `assign-code.sh <sub> <email>`. Lambda assigns a code AND writes it to the participant's `custom:tcu_code` attribute on Cognito.
4. Participant taps "Start recording" in the app. Before showing the invite dialog, `ScreenRecordFragment.tryFetchTcuCodeFromCognito()` calls `Amplify.Auth.fetchUserAttributes()` and looks for `custom:tcu_code`. If found and valid (4 digits), sets it silently and continues. If not found or fetch fails, falls back to the manual dialog.

**Deployed changes:**
- Cognito user pool `us-east-2_J52HUsGbQ`: added `custom:tcu_code` attribute (String, mutable, 4 chars).
- Lambda `screenlake-assign-tcu`:
  - New helper `_write_cognito_attribute` — calls `cognito-idp:AdminUpdateUserAttributes` after both first-time assignment AND idempotent reruns (re-syncs the attribute in case it was cleared).
  - New env var `COGNITO_USER_POOL_ID=us-east-2_J52HUsGbQ`.
  - Failures on the Cognito write are logged but do not fail the Lambda — DynamoDB is source of truth.
- Lambda IAM role: added `cognito-idp:AdminUpdateUserAttributes` on the user pool.
- Android `ScreenRecordFragment`:
  - `toggleRecording` first attempts auto-fetch via `tryFetchTcuCodeFromCognito()`.
  - On success: sets `user.emailHash`, toasts "Participant ID: TCU-1005", proceeds to start recording.
  - On fail (offline, missing attribute, invalid code): falls back to `showMissingIdDialog()`.
  - Manual dialog now shows "Enter the 4-digit code your researcher assigned. Your ID will look like TCU-XXXX." and strips optional `TCU-` prefix from typed input.
  - Success toast changed from "Invite code: XXXX" to "Participant ID: TCU-XXXX" for consistency.

**Not touched:** the Lambda invocation still happens researcher-side via `assign-code.sh`. The mobile client only READS the attribute, never invokes the Lambda directly. No AWS SDK dependency added to the APK.

---

## d214f1e — Add assign_tcu_code Lambda + DynamoDB (backend only)

**Commit:** `d214f1e`
**Scope:** new Lambda + DynamoDB table + IAM. No Android changes.

**Problem solved:** current invite code screen accepts any 4-digit number a participant types. Two participants can type the same code → data collides under the same `panelist=<code>/` prefix. At 60 participants, random 4-digit assignment has a 16% collision rate.

**Design chosen:** Lambda-assigned sequential codes backed by DynamoDB atomic counter. Guaranteed unique. Reinstall-safe (same Cognito user always gets the same code back).

**What's deployed:**
- DynamoDB table `screenlake-tcu-codes` (PAY_PER_REQUEST, us-east-2)
- Counter initialized to 1000; first real code = 1001
- Reserved codes 1234, 2323 pre-seeded to protect existing test data
- Lambda `screenlake-assign-tcu` (Python 3.12, 256 MB)
- IAM role `screenlake-assign-tcu-role` with DynamoDB + Logs access

**Smoke-tested:** first invocation returned 1001, second returned 1002, replay of first returned 1001 (idempotent). Test data cleaned up before real rollout.

**Not integrated with Android yet.** The mobile client still uses the manual invite code screen. Three wiring options are documented in `lambda/assign_tcu_code/README.md`:

1. Direct Lambda invoke from mobile via AWS SDK (Cognito Identity Pool auth role needs `lambda:InvokeFunction` grant; ~30 lines of Kotlin; +10 MB APK for the Lambda SDK)
2. API Gateway HTTPS in front of Lambda + Amplify.API.post from mobile (new API Gateway resource; ~15 lines of Kotlin)
3. Researcher CLI-only workflow — zero mobile changes; researcher runs `aws lambda invoke` per participant, tells them their assigned code, participant types it in the existing invite screen

**Rejected alternatives:**
- Random 4-digit: 16% collision at 60 users, unsafe.
- Researcher pre-assigns via Cognito Console: eliminates uniqueness bug but requires manual per-participant work.
- Client-side S3 existence check: race conditions, false positives on reinstall, still requires IAM changes.

**Decided:** option 3 (researcher CLI). Zero Android changes. Researcher runs `lambda/assign_tcu_code/assign-code.sh <cognito_sub>` per participant, tells them their code, they type it in the existing invite screen. Preserves working onboarding flow; still gets uniqueness guarantee from the Lambda's DynamoDB counter.

Follow-up commit added the `assign-code.sh` helper + updated README.

---

## 4f8f829 — Add pipeline-work docs (changelog, architecture, decisions)

**Commit:** `4f8f829`
**Scope:** docs only.

Created `docs/pipeline-work/` with three files:
- `README.md` — index and update conventions.
- `CHANGELOG.md` — this file.
- `ARCHITECTURE.md` — current-state snapshot of S3 layout, Android pipeline, Lambda behavior, IAM setup.
- `DECISIONS.md` — 10 architecture-decision records for the non-obvious calls (raw immutability, zip-on-upload, 200-batch, per-zip CSVs, in-place `_index.json`, on-device OCR, TCU assignment approach, debug APK distribution, TCU- UI prefix, Cognito ownership).

Purpose: future contributors (or future-me) can catch up on what changed, what the current state is, and why non-obvious calls were made.

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
