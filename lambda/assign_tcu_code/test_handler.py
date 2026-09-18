"""Local unit tests for handler.py.
Run: python -m pytest lambda/assign_tcu_code/test_handler.py -v
"""
from __future__ import annotations

import os
os.environ["TCU_TABLE_NAME"] = "test-table"

from unittest.mock import MagicMock, patch

import pytest

import handler as H
from botocore.exceptions import ClientError


class FakeTable:
    """In-memory DynamoDB-like store good enough for handler.py's usage."""

    def __init__(self, prefills: dict | None = None):
        self.items: dict[str, dict] = dict(prefills or {})

    def get_item(self, Key):
        item = self.items.get(Key["id"])
        return {"Item": item} if item is not None else {}

    def put_item(self, Item, ConditionExpression: str | None = None):
        if ConditionExpression == "attribute_not_exists(id)" and Item["id"] in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                "PutItem",
            )
        self.items[Item["id"]] = Item
        return {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ReturnValues):
        # Only supports our single "SET last_assigned = if_not_exists(last_assigned, :start) + :incr".
        current = self.items.get(Key["id"], {})
        prior = current.get("last_assigned", ExpressionAttributeValues[":start"])
        new_val = prior + ExpressionAttributeValues[":incr"]
        self.items[Key["id"]] = {"id": Key["id"], "last_assigned": new_val}
        return {"Attributes": {"last_assigned": new_val}}


def _run(event, prefills=None):
    fake = FakeTable(prefills=prefills)
    with patch.object(H, "table", fake):
        result = H.lambda_handler(event, None)
    return result, fake


def test_first_assignment_starts_at_1001():
    result, table = _run({"cognito_sub": "user-A", "cognito_username": "alice"})
    assert result["tcu_code"] == "1001"
    assert result["assigned_now"] is True
    assert result["cognito_sub"] == "user-A"
    assert table.items["user#user-A"]["tcu_code"] == "1001"
    assert table.items["code#1001"]["cognito_sub"] == "user-A"


def test_second_user_gets_1002():
    _, table = _run({"cognito_sub": "user-A", "cognito_username": "alice"})
    result, _ = _run({"cognito_sub": "user-B", "cognito_username": "bob"},
                     prefills=table.items)
    assert result["tcu_code"] == "1002"


def test_same_user_reinvocation_returns_existing_code():
    _, table = _run({"cognito_sub": "user-A", "cognito_username": "alice"})
    result, _ = _run({"cognito_sub": "user-A", "cognito_username": "alice"},
                     prefills=table.items)
    assert result["tcu_code"] == "1001"
    assert result["assigned_now"] is False


def test_reserved_codes_are_skipped():
    prefills = {
        "code#1001": {"id": "code#1001", "cognito_sub": "reserved-test-1234"},
        "code#1002": {"id": "code#1002", "cognito_sub": "reserved-test-2323"},
    }
    result, _ = _run({"cognito_sub": "user-A", "cognito_username": "alice"},
                     prefills=prefills)
    assert result["tcu_code"] == "1003"


def test_api_gateway_body_wrapped_payload():
    import json as _json
    event = {"body": _json.dumps({"cognito_sub": "user-A", "cognito_username": "alice"})}
    result, _ = _run(event)
    assert result["tcu_code"] == "1001"


def test_missing_cognito_sub_raises():
    with pytest.raises(H.HandlerError, match="missing cognito_sub"):
        _run({"cognito_username": "alice"})


def test_race_condition_returns_winner_code():
    """Simulate: user-A already has code 1005 written by a parallel invocation.
    Our attempt to write user-A → 1006 should fail with ConditionCheck, then
    look up and return 1005.
    """
    prefills = {
        "user#user-A": {"id": "user#user-A", "tcu_code": "1005"},
        "counter": {"id": "counter", "last_assigned": 1005},
    }
    result, _ = _run({"cognito_sub": "user-A", "cognito_username": "alice"},
                     prefills=prefills)
    # Existing lookup fires first, so we get 1005 without bumping.
    assert result["tcu_code"] == "1005"
    assert result["assigned_now"] is False


def test_counter_starts_at_1001_not_1000():
    """First assignment must be 1001, not 1000. Confirms the +1 semantics."""
    result, table = _run({"cognito_sub": "user-A"})
    assert result["tcu_code"] == "1001"
    assert table.items["counter"]["last_assigned"] == 1001
