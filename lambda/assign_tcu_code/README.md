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

## Researcher workflow

For each new participant, after they've registered a Cognito account via the app:

```bash
./lambda/assign_tcu_code/assign-code.sh <cognito_sub> <email>
```

Example output:
```
  TCU code: TCU-1005
  (bare code to type into app invite screen: 1005)
  Status: newly assigned
```

The Lambda writes the code into the Cognito user's `custom:tcu_code` attribute automatically. The next time the participant opens the app and taps "Start recording," the app fetches the attribute from Cognito and uses it silently. **No participant typing required.** If the fetch fails (offline, no attribute), the app falls back to the manual invite dialog.

Where to find `<cognito_sub>`:
- AWS Console → Cognito → User Pools → your pool → Users
- Click the participant's user
- Copy the `sub` attribute (UUID like `12345678-abcd-ef01-2345-678901234567`)

The `<email>` is used as the Cognito Username (required to update the user's custom attribute).

Reruns for the same `<cognito_sub>` are idempotent — returns the code they already have and re-syncs the Cognito attribute if it was somehow cleared. Safe to re-run.

## Raw API (if not using the helper script)

```bash
aws lambda invoke --function-name screenlake-assign-tcu \
  --payload '{"cognito_sub":"<sub>","cognito_username":"<email>"}' \
  --cli-binary-format raw-in-base64-out \
  --region us-east-2 \
  /tmp/out.json
cat /tmp/out.json
```

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

**Integration path:** Cognito custom attribute. Researcher runs `assign-code.sh`, Lambda writes `custom:tcu_code` to the Cognito user, mobile app fetches it via Amplify Auth on first "Start recording" tap. Manual invite dialog stays as fallback.

**Why this shape and not direct Lambda invoke from mobile:**
- No new AWS SDK dependency on the app (Amplify already has fetchUserAttributes).
- No IAM change to Cognito Identity Pool (would have needed lambda:InvokeFunction grant).
- Cognito already the identity system of record — attribute persists across reinstalls automatically.
- Researcher CLI is the single point of code assignment. Mobile is read-only.

**Cognito schema:** `custom:tcu_code` attribute on user pool `us-east-2_J52HUsGbQ`. String, 4 characters, mutable.
