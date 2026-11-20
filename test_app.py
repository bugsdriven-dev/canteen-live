"""Local test: runs the Lambda against a fake DynamoDB (moto). Run: pip install moto boto3 pytest && pytest"""
import json
import os

os.environ.update(AWS_DEFAULT_REGION="ap-south-1", AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x", TABLE_NAME="CanteenLive")

import boto3
import pytest
from moto import mock_aws


@pytest.fixture()
def app():
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName="CanteenLive",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            BillingMode="PAY_PER_REQUEST",
        )
        import importlib
        import app as mod
        importlib.reload(mod)
        yield mod


def call(mod, method, path, body=None):
    event = {"requestContext": {"http": {"method": method}}, "rawPath": path}
    if body is not None:
        event["body"] = json.dumps(body)
    r = mod.handler(event, None)
    return r["statusCode"], json.loads(r["body"] or "{}")


def test_empty_status(app):
    code, data = call(app, "GET", "/status")
    assert code == 200
    assert data["crowd"]["level"] is None and data["menu"] == []


def test_crowd_average(app):
    for lvl in ["busy", "busy", "packed"]:
        assert call(app, "POST", "/crowd", {"level": lvl})[0] == 201
    _, data = call(app, "GET", "/status")
    assert data["crowd"]["reports"] == 3
    assert data["crowd"]["level"] == "busy"


def test_crowd_rejects_bad_level(app):
    assert call(app, "POST", "/crowd", {"level": "chaos"})[0] == 400


def test_menu_and_rating(app):
    assert call(app, "POST", "/menu", {"name": "Veg biryani", "meal": "lunch"})[0] == 201
    assert call(app, "POST", "/menu", {"name": "veg biryani", "meal": "lunch"})[0] == 409
    assert call(app, "POST", "/menu", {"name": "", "meal": "lunch"})[0] == 400
    assert call(app, "POST", "/menu", {"name": "Tea", "meal": "brunch"})[0] == 400
    _, data = call(app, "GET", "/status")
    item_id = data["menu"][0]["id"]
    assert call(app, "POST", "/rate", {"itemId": item_id, "stars": 5})[0] == 200
    assert call(app, "POST", "/rate", {"itemId": item_id, "stars": 3})[0] == 200
    _, data = call(app, "GET", "/status")
    assert data["menu"][0]["rating"] == 4.0 and data["menu"][0]["ratingCount"] == 2


def test_rating_validation(app):
    assert call(app, "POST", "/rate", {"itemId": "nope", "stars": 5})[0] == 404
    assert call(app, "POST", "/rate", {"itemId": "x", "stars": 9})[0] == 400
    assert call(app, "POST", "/rate", {"itemId": "x", "stars": True})[0] == 400


def test_unknown_route(app):
    assert call(app, "GET", "/nope")[0] == 404
