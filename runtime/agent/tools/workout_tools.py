import os
import boto3
from datetime import datetime, timezone
from boto3.dynamodb.conditions import Key

# Injected by CDK at deploy time — see infra/pt_agent_stack.py
TABLE_NAME = os.environ["WORKOUT_TABLE_NAME"]

_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(TABLE_NAME)


def get_workout_plan(user_id: str, day_of_week: str | None = None) -> dict:
    """
    Retrieve a user's workout plan from DynamoDB.

    DynamoDB key structure:
      PK = "USER#<user_id>"
      SK = "PLAN#<day_of_week>"  e.g. "PLAN#monday"

    If day_of_week is provided: use _table.get_item() with the full PK + SK.
    If day_of_week is None: use _table.query() with PK and SK begins_with("PLAN#")
                            to return the full weekly split.

    Returns:
        dict with the plan item(s)
    """
    if day_of_week:
        result = _table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": f"PLAN#{day_of_week.lower()}"}
        )
        item = result.get("Item")
        if not item:
            return {"found": False, "day": day_of_week}
        return {
            "found": True,
            "day": day_of_week,
            "muscle_group": item["muscle_group"],
            "exercises": item["exercises"],
        }

    # No day provided — return the full weekly split
    result = _table.query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
        & Key("SK").begins_with("PLAN#")
    )
    items = result.get("Items", [])
    return {
        "plan": [
            {
                "day": item["SK"].replace("PLAN#", ""),
                "muscle_group": item["muscle_group"],
                "exercises": item["exercises"],
            }
            for item in items
        ]
    }


def log_workout(
    user_id: str,
    day_of_week: str,
    muscle_group: str,
    exercises: list[dict],
) -> dict:
    """
    Save a completed workout session to DynamoDB.

    DynamoDB key structure:
      PK = "USER#<user_id>"
      SK = "LOG#<ISO timestamp>"   — timestamp makes each log entry unique

    The item stores: day_of_week, muscle_group, exercises, logged_at.
    exercises is a list of dicts with keys: name, sets, reps, weight_kg.

    Returns:
        confirmation dict with the SK of the saved record
    """
    timestamp = datetime.now(timezone.utc).isoformat()

    item = {
        "PK": f"USER#{user_id}",
        "SK": f"LOG#{timestamp}",
        "day_of_week": day_of_week.lower(),
        "muscle_group": muscle_group.lower(),
        "exercises": exercises,
        "logged_at": timestamp,
    }

    _table.put_item(Item=item)

    return {"status": "saved", "record_key": item["SK"], "logged_at": timestamp}


def update_workout_plan(
    user_id: str,
    day_of_week: str,
    muscle_group: str,
    exercises: list[dict],
) -> dict:
    """
    Overwrite one day in the user's weekly plan.

    DynamoDB key structure:
      PK = "USER#<user_id>"
      SK = "PLAN#<day_of_week>"

    Writing to the same SK overwrites the existing plan for that day.

    Returns:
        confirmation dict
    """
    _table.put_item(
        Item={
            "PK": f"USER#{user_id}",
            "SK": f"PLAN#{day_of_week.lower()}",
            "muscle_group": muscle_group.lower(),
            "exercises": exercises,
        }
    )

    return {"status": "updated", "day": day_of_week, "muscle_group": muscle_group}


def get_recent_history(user_id: str, muscle_group: str, limit: int = 5) -> dict:
    """
    Retrieve the last N workout sessions for a specific muscle group.

    Queries all LOG# items for the user and filters by muscle_group.
    Used by the agent to reason about progressive overload and recovery.

    Returns:
        dict with a list of recent sessions sorted newest first
    """
    result = _table.query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
        & Key("SK").begins_with("LOG#"),
        ScanIndexForward=False,  # newest first
    )

    sessions = [
        item
        for item in result.get("Items", [])
        if item.get("muscle_group") == muscle_group.lower()
    ][:limit]

    return {
        "muscle_group": muscle_group,
        "sessions": [
            {
                "date": item["logged_at"],
                "exercises": item["exercises"],
            }
            for item in sessions
        ],
    }


def get_hours_since_last_trained(user_id: str, muscle_group: str) -> dict:
    """
    Calculate how many hours have passed since the user last trained a muscle group.

    Used to enforce the 48-hour recovery rule deterministically.
    The agent should read this value and advise the user — never trust the LLM
    to calculate time differences itself.

    Returns:
        dict with hours_since and whether the muscle is recovered (>= 48 hours)
    """
    result = _table.query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
        & Key("SK").begins_with("LOG#"),
        ScanIndexForward=False,  # newest first
        Limit=20,
    )

    for item in result.get("Items", []):
        if item.get("muscle_group") == muscle_group.lower():
            last_trained = datetime.fromisoformat(item["logged_at"])
            now = datetime.now(timezone.utc)
            hours_since = (now - last_trained).total_seconds() / 3600
            return {
                "muscle_group": muscle_group,
                "hours_since_last_trained": round(hours_since, 1),
                "recovered": hours_since >= 48,
            }

    # Never trained this muscle group
    return {
        "muscle_group": muscle_group,
        "hours_since_last_trained": None,
        "recovered": True,
    }
