# Pipeline Work — Notes for Future You

Living documentation of every change made to this fork beyond upstream Screenlake, why we made it, and what state everything is in.

## Files in this folder

**[CHANGELOG.md](CHANGELOG.md)** — reverse-chronological log of every commit + what changed + why. Read this to catch up on history.

**[ARCHITECTURE.md](ARCHITECTURE.md)** — snapshot of the current deployed state: S3 layout, Android pipeline steps, Lambda behavior, IAM setup. Read this to understand "how does the whole thing work today."

**[DECISIONS.md](DECISIONS.md)** — architecture-decision records. Why zips instead of individual images, why 200 per batch, why in-place `_index.json` updates, etc. Read this before proposing a change to challenge or extend a previous call.

## How to update these docs

When you (or a future contributor) makes a meaningful change:

1. **Add a new entry to `CHANGELOG.md`** at the top with the commit hash, files touched, and one-paragraph "why" explanation.
2. **Update `ARCHITECTURE.md`** if the S3 layout, Android pipeline, or Lambda behavior changed. Keep it as a current-state snapshot, not a history.
3. **Add a new decision to `DECISIONS.md`** if the change reflects a non-obvious tradeoff someone might want to revisit.

Trivial changes (typo fixes, formatting) don't need doc updates. Anything that changes behavior, storage layout, cost, or deployment surface does.

## Quick reference

**S3 bucket:** `s3://my-tcu-bucket/` (us-east-2)
- `academia/` — raw immutable (do not touch)
- `data/` — derived CSVs (Lambda-produced, regenerable)
- `_manifests/` — per-participant `_index.json` (Lambda-produced, regenerable)

**GitHub fork:** `https://github.com/thumybanh/screenlake-research-kit`
- Branch: `main`
- Latest commit: run `git log --oneline -3` to see.

**AWS Lambda:** `screenlake-unpack-zips` in `us-east-2`, account `842294335158`
- Role: `screenlake-unpack-zips-role`
- Source: `lambda/unpack_zips/handler.py`

**Test participants in bucket:** `1234` (169 screenshots, Sep 2–4), `2323` (54 screenshots, Sep 4). Do NOT reuse these codes for real participants.

## When to read what

- **"Why does the pipeline work this way?"** → DECISIONS.md
- **"What did we change from upstream Screenlake?"** → CHANGELOG.md
- **"What does the current system look like end-to-end?"** → ARCHITECTURE.md
- **"How do I run the Lambda / build the APK / query the data?"** → the relevant `README.md` inside `lambda/unpack_zips/` or in the repo root.
