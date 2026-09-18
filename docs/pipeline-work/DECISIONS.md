# Architecture Decisions

Why we made the calls we did. Anchor for future revisions — read before proposing changes.

---

## D1. Raw S3 tree is immutable

The `academia/tenant/**/*.zip` prefix is treated as an untouchable audit record. Nothing renames, restructures, deletes, or edits anything under it. All browsable / analysis-friendly views are produced by the Lambda into separate top-level prefixes (`data/`, `_manifests/`).

**Why:** raw immutability guarantees any bug or "improvement" downstream can be recovered by regenerating derived output from the raw zips. Losing raw = losing everything.

**Consequences:**
- Derived output can be freely rebuilt, rewritten, deleted, or restructured.
- Content-Type fixes and layout preferences belong in the Lambda's `put_object`, never in `aws s3 cp --metadata-directive REPLACE` against raw.
- Orphan test objects that were never part of the pipeline (e.g. failed experiment leftovers) may be deleted from raw, but genuine payload zips stay forever.

---

## D2. Zip-on-upload is intentional

Each source zip contains up to 200 JPGs + 5 CSVs, uploaded as a single atomic S3 PUT.

**Why:**
- Minimizes S3 PUT count (bills + rate limits).
- Minimizes phone radio wake-ups and battery drain on participants.
- Batching is atomic — if upload fails, whole zip retries. No half-uploaded state.

**Consequences:**
- Researchers cannot preview individual screenshots directly in S3 console; they must download the specific zip and unzip locally.
- Individual image extraction was tried in an earlier revision (see CHANGELOG `8ea8df5`) and removed in `f343feb`. The visual clutter of hundreds of extracted JPGs per participant outweighed the browse convenience.

---

## D3. 200 screenshots per zip

`ZipFileWorker.zipUpScreenshots(200)`.

**Why:** middle ground between "many small zips = noisy S3 tree + high PUT costs" and "few huge zips = slow uploads + costly retries on network failure."

At ~100 KB per compressed JPG:
- 200 shots → ~20 MB per zip
- Upload time on WiFi: ~10 seconds
- Retry cost on failure: 20 MB re-upload

Larger (500) would be fine on WiFi-only but painful on cellular. Smaller (50) generated too many zips per participant, cluttering S3.

**Not sacred.** Revisit after pilot data shows real capture rates.

---

## D4. CSVs are per-zip, never merged or appended

Each zip's 5 CSVs land at unique S3 keys under `data/panel=/panelist=/date=/`, keyed by `{kind}_{zipUuid}.csv`. Never combined into per-participant or per-day rollups.

**Why:**
- Idempotency: retries and reprocessing overwrite identical keys with identical content. Safe.
- No concurrency race: two Lambda invocations for different zips never touch the same S3 key.
- Small files: each ≤ 200 rows, cheap to read.
- Athena and pandas handle the fanout automatically via Hive partitioning.

**Consequences:**
- 60 participants × ~50 zips × 5 CSVs = ~15,000 CSV files by end of a 28-day study. Cheap in S3 storage cost. Painful for someone browsing manually, but analysis tools don't care.

---

## D5. One `_index.json` per participant, updated in place

`_manifests/panel=/panelist=/_index.json` is a single rolling summary rewritten on every new zip event.

**Why:** researchers want one file per participant to answer "when did they start, are they still active, how much have they contributed." Not one file per zip. Not one file per day.

**How we handle the in-place update race:**
- Idempotency ledger: `_processed_zip_uuids` list. Retries and duplicate invocations skip.
- Reserved concurrency = 1 was intended to serialize writes but is blocked by AWS account quota (needs ≥10 unreserved slots; account total is 10).
- Fallback: relying on the idempotency ledger alone. At current event rates (a few zips per participant per day), the actual race window is negligible.
- If study scales to hundreds of participants and races become measurable, switch to SQS FIFO with `MessageGroupId=panelist` for per-participant serialization.

---

## D6. OCR runs on-device via Tesseract 5

No cloud OCR service. Uses `adaptech.tesseract4Android` (JNI wrapper around open-source Tesseract) with the stock `eng.traineddata` model.

**Why:**
- Privacy: pixels never leave the phone for OCR purposes. Only the extracted text ships with the zip.
- Cost: zero per-invocation cost.
- Offline capable: works with no network at capture time.

**Tradeoff:** Tesseract quality on mobile UI text is mediocre (small fonts, cluttered layouts). Real signal but noisy. Good enough for topic-level questions ("did participant see notification about X"), not for exact-phrase analysis.

**Alternative if quality becomes a blocker:** Google ML Kit Text Recognition v2. Same privacy story, better mobile-text accuracy. ~50-line swap in `Recognize.kt`.

---

## D7. Participant ID via researcher-controlled uniqueness, not client-side random

**In flight — see CHANGELOG for latest.**

At 60 participants using a 4-digit code (10,000 total combinations), random client-side assignment has a 16% collision probability (birthday problem). Unacceptable.

**Path chosen:** Lambda + DynamoDB atomic counter. Sequential assignment (1001, 1002, ...). Reinstall-safe via Cognito custom attribute lookup. Zero manual work per participant.

**Rejected alternatives:**
- Random 4-digit — 16% collision.
- Random 6-digit — 900,000 codes, ~0.002% collision, but breaks user's "4-digit only" requirement.
- Researcher pre-assigns in Cognito Console — no infrastructure needed but manual per-participant.
- Client-side S3 existence check — race conditions, false positives on reinstall, still requires IAM changes.
- Email hash as identifier — perfectly unique but not readable.

---

## D8. Debug APK for distribution, not release/Play Store

Participants install via direct APK link, not via Google Play.

**Why:**
- Study is closed and short-lived (28 days). Play Store review overhead is unjustified.
- Debug keystore signing is universal across Android devices; no per-participant keystore management.
- Updates are pushed by re-sending the APK link, not by Play Store deployment.

**Consequences:**
- Participants see "Play Protect doesn't recognize this app" warning on install. Must be documented in participant onboarding.
- Cognito pool IDs and bucket names are compiled into the APK. Anyone decompiling can see them. Not credentials but identifiers. Acceptable for closed pilot; would need obfuscation or backend proxy for public distribution.
- No auto-update. Every code change requires manual re-install by participants.

---

## D9. `TCU-` prefix is UI-only

**In flight — see CHANGELOG.**

The `TCU-` prefix on the participant ID appears in the app UI only. The S3 path uses the bare 4-digit code (e.g. `panelist=1005/`), matching the current test data (`panelist=1234/`, `panelist=2323/`). Preserves consistency across old and new data.

---

## D10. Cognito owns identity; the invite code is derived

Real authentication is Cognito email + password. The participant ID (currently 4-digit code, soon TCU-assigned) is stored as a Cognito custom attribute (`custom:tcu_code` after in-flight work) so the same participant can reinstall and continue their data collection under the same ID.

**Why:**
- Cognito enforces email uniqueness natively. Identity persistence across devices comes for free.
- Storing the TCU code on the Cognito user record means reinstall = same ID (not new one).
- Researcher lookups: Cognito Console can search by email → find TCU code + all metadata.
