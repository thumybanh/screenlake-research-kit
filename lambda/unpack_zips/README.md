# unpack_zips

S3-triggered Lambda. Reads Screenlake research zip batches from
`academia/tenant/...` and writes a browsable derived tree to `unpacked/` and
provenance manifests to `_manifests/`. The raw `academia/` tree is treated as
strictly read-only.

## Derived layout

```
unpacked/panel={panelId}/panelist={panelistId}/date={YYYY-MM-DD}/
├── images/
│   └── {YYYYMMDDThhmmssZ}_{sessionId}_{appPackage}.jpg
└── data/
    ├── screenshots_{zipUuid}.csv
    ├── sessions_{zipUuid}.csv
    ├── app_segments_{zipUuid}.csv
    ├── accessibility_{zipUuid}.csv
    └── log_{zipUuid}.csv   (only if the zip contained log_data_*)

_manifests/panel={panelId}/panelist={panelistId}/{zipUuid}.json
```

Notes:

- `date=` partitions come from screenshot UTC timestamps, not Lambda execution
  time. A zip that straddles midnight writes images into both `date=` partitions
  and duplicates its CSVs into each so every partition is self-describing.
- One CSV per source zip. Never merged, never appended → the function is fully
  idempotent. Any retry (S3 duplicate delivery, cold-start crash, redeploy)
  overwrites the same keys with identical content.
- Hive-style `key=value` partitioning is intentional: Athena, Glue, pandas
  (`pyarrow.dataset`) discover the partitions automatically.
- Content-Type is set at PUT time: `image/jpeg` for JPGs, `text/csv` for CSVs,
  `application/json` for manifests. This is what makes S3 console preview work
  inline for the derived tree.

## Loud failures

Every JPG must map to a row in `screenshot_data_csv_*.csv` (by basename). If a
row is missing, or if any of `file`, `apk`, `id_session`, `t_epoch_ts_ms` is
absent / empty / `"null"`, the handler raises `HandlerError` and the Lambda
invocation fails. No `unknown_unknown.jpg` placeholders. Failed invocations
retry per Lambda default; if the underlying data is genuinely malformed, the
zip lands in the DLQ (attach one — see below) for manual inspection.

## Deploy (one-shot, from a shell with `aws` configured)

Replace `REPLACE_BUCKET`, `REPLACE_REGION`, `REPLACE_ACCOUNT_ID` in the JSON
files first, then:

```bash
BUCKET=my-tcu-bucket
REGION=us-east-2
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
FN=screenlake-unpack-zips
ROLE=screenlake-unpack-zips-role

# 1. Create role.
aws iam create-role \
  --role-name "$ROLE" \
  --assume-role-policy-document file://trust_policy.json

aws iam put-role-policy \
  --role-name "$ROLE" \
  --policy-name unpack-zips-inline \
  --policy-document file://iam_policy.json

# 2. Package + create function (Python 3.12 runtime, boto3 already in runtime).
zip -j handler.zip handler.py

aws lambda create-function \
  --function-name "$FN" \
  --runtime python3.12 \
  --role "arn:aws:iam::${ACCOUNT_ID}:role/${ROLE}" \
  --handler handler.lambda_handler \
  --zip-file fileb://handler.zip \
  --timeout 300 \
  --memory-size 1024 \
  --ephemeral-storage Size=2048 \
  --region "$REGION"

# 3. Let S3 invoke the function.
aws lambda add-permission \
  --function-name "$FN" \
  --statement-id s3invoke \
  --action lambda:InvokeFunction \
  --principal s3.amazonaws.com \
  --source-arn "arn:aws:s3:::${BUCKET}" \
  --source-account "$ACCOUNT_ID" \
  --region "$REGION"

# 4. Wire the notification (only fires for keys under academia/tenant/*.zip).
aws s3api put-bucket-notification-configuration \
  --bucket "$BUCKET" \
  --notification-configuration file://event_notification.json
```

`ephemeral-storage 2048` sizes `/tmp` up from the 512 MB default so large zips
still fit. Memory 1024 MB is plenty; bump if you profile it lower.

Optional but recommended: attach a DLQ so genuinely broken zips are visible.

```bash
QUEUE_ARN=arn:aws:sqs:${REGION}:${ACCOUNT_ID}:screenlake-unpack-zips-dlq
aws lambda update-function-configuration \
  --function-name "$FN" \
  --dead-letter-config TargetArn="$QUEUE_ARN"
```

## Update the function code later

```bash
zip -j handler.zip handler.py
aws lambda update-function-code \
  --function-name "$FN" \
  --zip-file fileb://handler.zip
```

## Backfill existing raw zips

The Lambda only sees future PUTs. To reprocess everything already in the raw
tree, list keys and invoke synchronously:

```bash
aws s3api list-objects-v2 \
  --bucket "$BUCKET" \
  --prefix academia/tenant/ \
  --query 'Contents[?ends_with(Key, `.zip`)].Key' \
  --output text | tr '\t' '\n' | while read key; do
    payload=$(printf '{"Records":[{"s3":{"bucket":{"name":"%s"},"object":{"key":"%s"}}}]}' "$BUCKET" "$key")
    aws lambda invoke \
      --function-name "$FN" \
      --payload "$payload" \
      --cli-binary-format raw-in-base64-out \
      /tmp/out.json > /dev/null
done
```

Because writes are idempotent, this can be re-run as many times as you want.

## Tests

```bash
cd lambda/unpack_zips
pip install pytest
python -m pytest test_handler.py -v
```

The suite exercises: happy path, midnight-straddling zip, missing JPG↔CSV row,
missing required column, empty/null field, invalid epoch, non-matching key,
and same-second collisions.
