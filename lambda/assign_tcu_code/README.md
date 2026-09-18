# assign_tcu_code

Lambda that hands out sequential 4-digit participant IDs (TCU codes). One code per Cognito user. Reinstall-safe. Race-safe. No researcher pre-assignment.

## Contract

**Input** (direct Lambda invoke payload, or API Gateway body):
```json
{"cognito_sub": "<sub>", "cognito_username": "<username>"}
```

**Output:**
```json
{"tcu_code": "1005", "assigned_now": true, "cognito_sub": "<sub>"}
```

`assigned_now: true` means this call generated a new code. `false` means the user already had one (reinstall / retry).

## How it works

DynamoDB table `screenlake-tcu-codes` holds:
- One `counter` item with `last_assigned` — the atomic counter.
- One `user#<cognito_sub>` item per participant with their `tcu_code`.
- One `code#<code>` item per assigned code with the owner's `cognito_sub` (reverse lookup, and reserves codes so they can't be reassigned).

Lambda logic per call:
1. Look up `user#<cognito_sub>`. If exists → return its `tcu_code`.
2. Otherwise atomically bump the counter (starts at 1000; first assignment = 1001).
3. Skip codes that already have a `code#<code>` item (reserved test data: 1234, 2323).
4. Write both `user#<sub>` and `code#<code>` items.
5. Return the new code.

## Deployment

Table + IAM + Lambda are already deployed. To rebuild code:

```bash
cd lambda/assign_tcu_code
zip -j /tmp/assign_tcu.zip handler.py
aws lambda update-function-code \
  --function-name screenlake-assign-tcu \
  --zip-file fileb:///tmp/assign_tcu.zip \
  --region us-east-2
```

## Reserved codes

Before real recruitment, pre-populate any codes you want to keep out of rotation:

```bash
aws dynamodb put-item --table-name screenlake-tcu-codes \
  --item '{"id":{"S":"code#1234"},"cognito_sub":{"S":"reserved-test"},"assigned_at_epoch":{"N":"0"}}' \
  --region us-east-2
```

Currently reserved: `1234`, `2323` (existing test panelists).

## Manual invocation (researcher tool)

Give a code to a specific Cognito user without touching the mobile app:

```bash
aws lambda invoke --function-name screenlake-assign-tcu \
  --payload '{"cognito_sub":"<sub>","cognito_username":"<email>"}' \
  --cli-binary-format raw-in-base64-out \
  --region us-east-2 \
  /tmp/out.json
cat /tmp/out.json
```

`<sub>` is the Cognito user's UUID (visible in Cognito Console → Users → the user's `sub` attribute).

## Reset counter (destructive — do not run once real participants exist)

```bash
aws dynamodb put-item --table-name screenlake-tcu-codes \
  --item '{"id":{"S":"counter"},"last_assigned":{"N":"1000"}}' \
  --region us-east-2
```

## Tests

```bash
cd lambda/assign_tcu_code
python3 -m venv /tmp/venv && /tmp/venv/bin/pip install pytest boto3
/tmp/venv/bin/python -m pytest test_handler.py -v
```

8 cases: first assignment, sequential, reinstall idempotency, reserved codes, API Gateway payload shape, missing input, race condition, counter starts at 1001.

## Integration status

**Deployed:** Lambda + DynamoDB + IAM role → live in us-east-2.

**Not yet integrated with Android app.** The mobile client cannot yet call this Lambda. Options for how to wire it in:

1. **Direct Lambda invoke from Android via AWS SDK.** Requires:
   - Grant Cognito Identity Pool's authenticated role `lambda:InvokeFunction` on this Lambda ARN.
   - Add `aws-android-sdk-lambda` dependency (~10 MB).
   - ~30 lines of Kotlin in `RegisterLoadingFragment` to invoke after Cognito signup.

2. **API Gateway HTTPS endpoint in front of Lambda.** Requires:
   - Create HTTP API in API Gateway.
   - Attach Cognito authorizer (participant's ID token proves identity).
   - ~15 lines of Kotlin using Amplify's `Amplify.API.post()` (already in codebase).

3. **Researcher CLI-only workflow.** No Android changes. Researcher runs the `aws lambda invoke` command above per new participant, tells them the code, they type it in the existing invite screen.

Path 2 is the cleanest for the app; path 3 is the cheapest for launch. Decision pending.
