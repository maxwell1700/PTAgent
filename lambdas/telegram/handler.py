"""
Telegram webhook Lambda.

Flow:
  Telegram -> API Gateway -> this Lambda -> AgentCore -> response -> Telegram

Telegram sends a POST request to your webhook URL for every message.
This Lambda extracts the message, calls AgentCore, and sends the response
back to the user via the Telegram Bot API.

Security:
  - ALLOWED_USER_IDS env var whitelists specific Telegram user IDs
  - Only whitelisted users get a response — everyone else is silently ignored
  - No Cognito needed for MVP since Telegram user IDs are the identity

Manual steps before deploying:
  1. Create a Telegram bot via @BotFather — you will receive a bot token
  2. Get your own Telegram user ID via @userinfobot
  3. Store the bot token in AWS SSM Parameter Store (see infra/pt_agent_stack.py)
  4. After deploying, register the webhook:
       curl https://api.telegram.org/bot<TOKEN>/setWebhook?url=<API_GATEWAY_URL>/webhook
"""

import json
import logging
import os

import boto3
import urllib.request

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Config — all injected by CDK as Lambda environment variables
# ---------------------------------------------------------------------------

# SSM parameter name where your Telegram bot token is stored securely
BOT_TOKEN_PARAM = os.environ["BOT_TOKEN_PARAM"]

# Comma-separated list of allowed Telegram user IDs e.g. "123456789,987654321"
# MANUAL: set this env var in CDK to your own Telegram user ID
ALLOWED_USER_IDS = set(os.environ.get("ALLOWED_USER_IDS", "").split(","))

# AgentCore runtime endpoint — injected by CDK after agent is deployed
# MANUAL: update this after deploying the AgentCore agent runtime
AGENT_RUNTIME_ID = os.environ.get("AGENT_RUNTIME_ID", "")
AGENT_REGION = os.environ.get("AWS_REGION", "us-east-1")

_ssm = boto3.client("ssm")
_bot_token: str | None = None


def _get_bot_token() -> str:
    """Fetch bot token from SSM — cached for the Lambda container lifetime."""
    global _bot_token
    if _bot_token is None:
        response = _ssm.get_parameter(Name=BOT_TOKEN_PARAM, WithDecryption=True)
        _bot_token = response["Parameter"]["Value"]
    return _bot_token


def _send_telegram_message(chat_id: int, text: str) -> None:
    """Send a message back to the user via the Telegram Bot API."""
    token = _get_bot_token()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req)  # nosec B310 — URL is always https://api.telegram.org


def _invoke_agent(user_id: str, prompt: str) -> str:
    """
    Call the AgentCore agent runtime with the user's message.

    AgentCore exposes an invoke API via the bedrock-agentcore-runtime boto3 client.
    The payload shape matches what @app.entrypoint expects in pt_agent.py.

    MANUAL: verify the exact method name and parameters against the AWS docs
    as AgentCore is a new service and the SDK may have been updated.
    Docs: https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore.html
    """
    client = boto3.client("bedrock-agentcore-runtime", region_name=AGENT_REGION)

    response = client.invoke_agent_runtime(
        agentRuntimeId=AGENT_RUNTIME_ID,
        sessionId=f"telegram-{user_id}",  # session_id scopes conversation memory per user
        payload=json.dumps({"user_id": user_id, "prompt": prompt}),
    )

    # Response body is a streaming blob — read and decode it
    return response["body"].read().decode("utf-8")


def handler(event: dict, context) -> dict:
    """
    Lambda entry point.

    API Gateway passes the Telegram webhook POST body as event["body"].
    We parse it, check the user is allowed, call AgentCore, and reply.
    """
    try:
        body = json.loads(event.get("body", "{}"))
        message = body.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        user_id = str(message.get("from", {}).get("id", ""))
        text = message.get("text", "").strip()

        if not chat_id or not text:
            return {"statusCode": 200, "body": "ok"}

        # Silently ignore anyone not on the whitelist
        if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
            logger.warning(f"Blocked unauthorised user: {user_id}")
            return {"statusCode": 200, "body": "ok"}

        logger.info(f"Message from user {user_id}: {text}")

        response_text = _invoke_agent(user_id=user_id, prompt=text)
        _send_telegram_message(chat_id=chat_id, text=response_text)

        return {"statusCode": 200, "body": "ok"}

    except Exception:
        logger.exception("Error handling Telegram webhook")
        # Always return 200 to Telegram — a non-200 causes Telegram to retry
        return {"statusCode": 200, "body": "ok"}
