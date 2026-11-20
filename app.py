"""Canteen Live - backend for the Christ Kengeri cafeteria tracker.

One Lambda behind an API Gateway HTTP API, storing everything in one DynamoDB table.

Table layout (pk / sk):
  CROWD            / TS#<epoch>        a crowd report, expires via TTL
  MENU#<YYYY-MM-DD>/ ITEM#<id>         a menu item for that day (IST), with rating totals
"""
import datetime
import json
import os
import time
import uuid
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key

TABLE_NAME = os.environ.get("TABLE_NAME", "CanteenLive")
_ddb = boto3.resource("dynamodb")

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
CROWD_WINDOW_SECONDS = 30 * 60      # only reports from the last 30 min count
CROWD_TTL_SECONDS = 3 * 3600        # DynamoDB deletes reports after 3 hours
MENU_TTL_SECONDS = 2 * 24 * 3600
MAX_ITEMS_PER_DAY = 60

LEVELS = {"empty": 1, "moderate": 2, "busy": 3, "packed": 4}
LEVEL_BY_SCORE = {v: k for k, v in LEVELS.items()}
MEALS = ("breakfast", "lunch", "snacks", "dinner")


def _table():
    return _ddb.Table(TABLE_NAME)


def _today():
    return datetime.datetime.now(IST).strftime("%Y-%m-%d")


def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body, default=_json_default),
    }


def _body(event):
    try:
        data = json.loads(event.get("body") or "{}")
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


# ---------- handlers ----------

def get_status():
    now = int(time.time())
    t = _table()

    reports = t.query(
        KeyConditionExpression=Key("pk").eq("CROWD")
        & Key("sk").gte(f"TS#{now - CROWD_WINDOW_SECONDS:012d}")
    )["Items"]
    if reports:
        avg = sum(LEVELS[r["level"]] for r in reports) / len(reports)
        level = LEVEL_BY_SCORE[min(4, max(1, round(avg)))]
        last = max(int(r["ts"]) for r in reports)
        crowd = {"level": level, "reports": len(reports), "minutesAgo": (now - last) // 60}
    else:
        crowd = {"level": None, "reports": 0, "minutesAgo": None}

    items = t.query(KeyConditionExpression=Key("pk").eq(f"MENU#{_today()}"))["Items"]
    menu = []
    for it in items:
        count = int(it.get("ratingCount", 0))
        total = int(it.get("ratingSum", 0))
        menu.append({
            "id": it["sk"].split("#", 1)[1],
            "name": it["name"],
            "meal": it["meal"],
            "rating": round(total / count, 1) if count else None,
            "ratingCount": count,
        })
    menu.sort(key=lambda m: (MEALS.index(m["meal"]), m["name"].lower()))

    return _resp(200, {"date": _today(), "crowd": crowd, "menu": menu})


def post_crowd(event):
    data = _body(event)
    level = data.get("level") if data else None
    if level not in LEVELS:
        return _resp(400, {"error": f"level must be one of {list(LEVELS)}"})
    now = int(time.time())
    _table().put_item(Item={
        "pk": "CROWD",
        "sk": f"TS#{now:012d}#{uuid.uuid4().hex[:6]}",
        "level": level,
        "ts": now,
        "ttl": now + CROWD_TTL_SECONDS,
    })
    return _resp(201, {"ok": True})


def post_menu(event):
    data = _body(event)
    if not data:
        return _resp(400, {"error": "invalid JSON body"})
    name = str(data.get("name", "")).strip()
    meal = data.get("meal")
    if not 1 <= len(name) <= 60:
        return _resp(400, {"error": "name must be 1-60 characters"})
    if meal not in MEALS:
        return _resp(400, {"error": f"meal must be one of {list(MEALS)}"})

    pk = f"MENU#{_today()}"
    t = _table()
    existing = t.query(KeyConditionExpression=Key("pk").eq(pk))["Items"]
    if len(existing) >= MAX_ITEMS_PER_DAY:
        return _resp(429, {"error": "menu is full for today"})
    if any(i["name"].lower() == name.lower() and i["meal"] == meal for i in existing):
        return _resp(409, {"error": "that item is already on the menu"})

    t.put_item(Item={
        "pk": pk,
        "sk": f"ITEM#{uuid.uuid4().hex[:12]}",
        "name": name,
        "meal": meal,
        "ratingSum": 0,
        "ratingCount": 0,
        "ttl": int(time.time()) + MENU_TTL_SECONDS,
    })
    return _resp(201, {"ok": True})


def post_rate(event):
    data = _body(event)
    if not data:
        return _resp(400, {"error": "invalid JSON body"})
    item_id = str(data.get("itemId", ""))
    stars = data.get("stars")
    if not item_id or not isinstance(stars, int) or isinstance(stars, bool) or not 1 <= stars <= 5:
        return _resp(400, {"error": "itemId and stars (1-5) are required"})
    try:
        _table().update_item(
            Key={"pk": f"MENU#{_today()}", "sk": f"ITEM#{item_id}"},
            UpdateExpression="ADD ratingSum :s, ratingCount :one",
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={":s": stars, ":one": 1},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _resp(404, {"error": "item not found"})
        raise
    return _resp(200, {"ok": True})


def handler(event, context):
    http = event.get("requestContext", {}).get("http", {})
    method, path = http.get("method"), event.get("rawPath", "")
    if method == "OPTIONS":
        return _resp(204, {})
    try:
        if method == "GET" and path == "/status":
            return get_status()
        if method == "POST" and path == "/crowd":
            return post_crowd(event)
        if method == "POST" and path == "/menu":
            return post_menu(event)
        if method == "POST" and path == "/rate":
            return post_rate(event)
        return _resp(404, {"error": "not found"})
    except Exception as e:  # keep the API from leaking stack traces
        print("ERROR", repr(e))
        return _resp(500, {"error": "server error"})
