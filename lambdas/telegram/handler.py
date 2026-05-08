"""
Telegram webhook receiver Lambda.

Flow:
  Telegram -> API Gateway -> this Lambda -> SQS -> processor Lambda -> AgentCore -> Telegram

This Lambda only validates the message, checks the whitelist, puts it on SQS,
and returns 200 immediately. All AgentCore work happens in the processor Lambda.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BOT_TOKEN_PARAM = os.environ["BOT_TOKEN_PARAM"]
ALLOWED_USER_IDS_PARAM = os.environ["ALLOWED_USER_IDS_PARAM"]
QUEUE_URL = os.environ["QUEUE_URL"]

_ssm = boto3.client("ssm")
_sqs = boto3.client("sqs")
_allowed_user_ids: set[str] | None = None


def _get_allowed_user_ids() -> set[str]:
    global _allowed_user_ids
    if _allowed_user_ids is None:
        response = _ssm.get_parameter(Name=ALLOWED_USER_IDS_PARAM, WithDecryption=False)
        _allowed_user_ids = set(response["Parameter"]["Value"].split(","))
    return _allowed_user_ids


def handler(event: dict, context) -> dict:
    try:
        body = json.loads(event.get("body", "{}"))
        message = body.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        user_id = str(message.get("from", {}).get("id", ""))
        text = message.get("text", "").strip()

        if not chat_id or not text:
            return {"statusCode": 200, "body": "ok"}

        allowed = _get_allowed_user_ids()
        if allowed and user_id not in allowed:
            logger.warning(f"Blocked unauthorised user: {user_id}")
            return {"statusCode": 200, "body": "ok"}

        logger.info(f"Queuing message from user {user_id}")
        _sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps(
                {"user_id": user_id, "chat_id": chat_id, "text": text}
            ),
        )

    except Exception:
        logger.exception("Error receiving Telegram webhook")

    # Always return 200 — Telegram retries on anything else
    return {"statusCode": 200, "body": "ok"}
