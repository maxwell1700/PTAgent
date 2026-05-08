"""
Agent processor Lambda.

Flow:
  SQS -> this Lambda -> AgentCore -> Telegram Bot API

Triggered by SQS. Invokes AgentCore with a timeout, sends the reply back to
Telegram. On failure, the message goes to the DLQ after SQS exhausts retries.
"""

import json
import logging
import os
import urllib.request

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BOT_TOKEN_PARAM = os.environ["BOT_TOKEN_PARAM"]
AGENT_RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]
AGENT_REGION = os.environ.get("AWS_REGION", "us-east-1")

_ssm = boto3.client("ssm")
_bot_token: str | None = None


def _get_bot_token() -> str:
    global _bot_token
    if _bot_token is None:
        _bot_token = _ssm.get_parameter(Name=BOT_TOKEN_PARAM, WithDecryption=True)[
            "Parameter"
        ]["Value"]
    return _bot_token


def _send_telegram_message(chat_id: int, text: str) -> None:
    token = _get_bot_token()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req)  # nosec B310 — URL is always https://api.telegram.org


def _invoke_agent(user_id: str, prompt: str) -> str:

    config = Config(read_timeout=10, connect_timeout=5, retries={"max_attempts": 0})
    # disable retries to fail fast on errors
    client = boto3.client(
        "bedrock-agentcore",
        region_name=AGENT_REGION,
        config=config,
    )
    response = client.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        runtimeSessionId=f"telegram-{user_id}-session-persistent",
        payload=json.dumps({"user_id": user_id, "prompt": prompt}),
    )
    return response["body"].read().decode("utf-8")


def handler(event: dict, context) -> None:
    for record in event["Records"]:
        body = json.loads(record["body"])
        user_id = body["user_id"]
        chat_id = body["chat_id"]
        text = body["text"]

        logger.info(f"Processing message from user {user_id}")
        response_text = _invoke_agent(user_id=user_id, prompt=text)
        _send_telegram_message(chat_id=chat_id, text=response_text)
