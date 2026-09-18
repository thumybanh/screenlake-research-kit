# Next Steps — Deferred Work

Ideas + tasks that came up during pipeline work but weren't shipped yet. Pick up from here.

---

## Cognito post-confirmation trigger — automate TCU code assignment on signup

**Status:** designed, not built. Deferred so we can ship the current auto-fetch behavior first.

**Problem this closes:** current flow requires the researcher to manually run `assign-code.sh <sub> <email>` for every new participant AFTER they register a Cognito account. If the participant taps Start recording BEFORE the researcher runs the CLI, they hit the fallback dialog.

**Solution:** attach `screenlake-assign-tcu` Lambda as a **Cognito post-confirmation trigger** on user pool `us-east-2_J52HUsGbQ`. Cognito automatically fires the Lambda the moment a user's email verification succeeds. The TCU code + Cognito attribute are set before the participant ever taps Start recording.

**Result:** zero researcher action per participant. Participant registers → confirms email → opens app → auto-fetch works immediately.

### What's needed to build

1. **Extend `handler.py`** to accept the Cognito trigger event shape:
   ```json
   {
     "userPoolId": "us-east-2_J52HUsGbQ",
     "userName": "<sub-or-username>",
     "request": {
       "userAttributes": {
         "sub": "...",
         "email": "..."
       }
     },
     "response": {}
   }
   ```
   Extract `cognito_sub` from `event["request"]["userAttributes"]["sub"]`, `cognito_username` from `event["userName"]`.
   Cognito triggers require the full event object to be returned (Cognito continues its flow using the response). The current Lambda returns `{tcu_code, assigned_now, cognito_sub}` — needs a branch that detects a Cognito trigger by presence of `userPoolId` and returns the event unmodified after doing the work.

2. **Grant Cognito service invoke permission on the Lambda**:
   ```bash
   aws lambda add-permission --function-name screenlake-assign-tcu \
     --statement-id CognitoInvoke \
     --action lambda:InvokeFunction \
     --principal cognito-idp.amazonaws.com \
     --source-arn arn:aws:cognito-idp:us-east-2:842294335158:userpool/us-east-2_J52HUsGbQ \
     --region us-east-2
   ```

3. **Attach the Lambda as PostConfirmation trigger**:
   ```bash
   aws cognito-idp update-user-pool --user-pool-id us-east-2_J52HUsGbQ \
     --lambda-config PostConfirmation=arn:aws:lambda:us-east-2:842294335158:function:screenlake-assign-tcu \
     --region us-east-2
   ```

4. **Test end-to-end**: register a fresh Cognito account in the emulator, verify email, then check that `custom:tcu_code` gets set within a few seconds without running the CLI.

5. **Keep `assign-code.sh` in place** as a manual re-sync tool for any user whose attribute somehow goes out of sync.

### Risks / gotchas

- **Post-confirmation trigger runs only on email-confirmed signups, not on admin-created users.** If researcher pre-creates accounts via Console → users must confirm email via the "Force Change Password" flow OR researcher runs `assign-code.sh` manually. Test this before real recruitment.
- **The trigger must NOT throw exceptions** — Cognito rolls back the confirmation if the Lambda fails. So the Cognito-attribute write failure should stay best-effort (already implemented) and the trigger path should return the event even on internal errors (log + swallow).
- **Cost impact:** one extra Lambda invocation per real user signup. Negligible at 60-participant scale.

### Estimated time to build

~15 minutes. Handler change + AWS CLI setup + one test signup.

---

## Additional ideas parked for later

- Storage lifecycle: move raw zips older than 90 days to Glacier Instant Retrieval. Cost drops from ~$23/TB/month to ~$4/TB/month. Retrieval latency still milliseconds. Requires an S3 lifecycle rule.
- Redshift / QuickSight dashboard on top of the `data/` tree. Athena works today; a persistent dashboard would make researcher UX nicer once real data starts flowing.
- Debug log CSVs (`log_data_*.csv` inside each zip) currently duplicate what's in `academia/log_events_v2/`. Consider dropping the in-zip log CSV to shrink zips by ~2–5%.
- PII redaction pipeline: automated OCR-text scan for SSN / credit card / health keyword patterns → tag zips flagged for human review. Separate infrastructure decision, out of pipeline scope.
- Better JPG capture quality tuning: `SCREENSHOT_IMAGE_QUALITY` constant in `ConstantSettings.kt` is currently 50 but the `bitmap.compress(..., 50, ...)` call at `ScreenshotService.kt:785` is hardcoded. Constant is unused — worth wiring it through so the setting actually controls output size.
