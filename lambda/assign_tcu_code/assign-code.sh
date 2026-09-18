#!/usr/bin/env bash
# Researcher tool — assign a TCU code to one participant.
#
# Usage:
#   ./assign-code.sh <cognito_sub> [cognito_username]
#
# Example:
#   ./assign-code.sh 12345678-abcd-ef01-2345-678901234567 jane@school.edu
#
# The <cognito_sub> is the participant's Cognito user UUID. Find it in the
# AWS Console: Cognito → User Pools → your pool → Users → click the user →
# copy the "sub" attribute.
#
# Reruns for the same cognito_sub return the same code (idempotent).

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <cognito_sub> [cognito_username]" >&2
    echo "" >&2
    echo "Get cognito_sub from: AWS Console -> Cognito -> Users -> click user -> 'sub' attribute" >&2
    exit 1
fi

COGNITO_SUB="$1"
COGNITO_USERNAME="${2:-}"

PAYLOAD=$(cat <<EOF
{"cognito_sub":"${COGNITO_SUB}","cognito_username":"${COGNITO_USERNAME}"}
EOF
)

TMPFILE=$(mktemp)
trap "rm -f $TMPFILE" EXIT

aws lambda invoke \
    --function-name screenlake-assign-tcu \
    --payload "$PAYLOAD" \
    --cli-binary-format raw-in-base64-out \
    --region us-east-2 \
    "$TMPFILE" >/dev/null

# Parse response.
if command -v jq >/dev/null 2>&1; then
    TCU_CODE=$(jq -r .tcu_code "$TMPFILE")
    ASSIGNED_NOW=$(jq -r .assigned_now "$TMPFILE")
else
    # Fallback for machines without jq.
    TCU_CODE=$(sed -n 's/.*"tcu_code": *"\([^"]*\)".*/\1/p' "$TMPFILE")
    ASSIGNED_NOW=$(sed -n 's/.*"assigned_now": *\([a-z]*\).*/\1/p' "$TMPFILE")
fi

if [ -z "${TCU_CODE:-}" ] || [ "$TCU_CODE" = "null" ]; then
    echo "ERROR: Lambda did not return a tcu_code. Raw response:" >&2
    cat "$TMPFILE" >&2
    exit 2
fi

echo ""
echo "  TCU code: TCU-${TCU_CODE}"
echo "  (bare code to type into app invite screen: ${TCU_CODE})"
if [ "$ASSIGNED_NOW" = "true" ]; then
    echo "  Status: newly assigned"
else
    echo "  Status: already assigned (this Cognito user already had a code)"
fi
echo ""
