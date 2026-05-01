"""
Local testing script for the PT Agent.

Starts the AgentCore server locally and sends a test invocation.
Use this to test the agent without deploying to AWS.

Requirements:
  - AWS credentials configured (aws configure or env vars)
  - DynamoDB table already deployed (cdk deploy) OR use DynamoDB Local
  - WORKOUT_TABLE_NAME environment variable set

Usage:
  # Set the table name from your cdk deploy output
  export WORKOUT_TABLE_NAME=PtAgentStack-WorkoutTable-XXXX

  # Run the agent server in one terminal
  python -m runtime.agent.pt_agent

  # Run this script in another terminal to send a test message
  python scripts/invoke_agent.py
"""

import json
import urllib.request

# AgentCore runs on port 8080 locally
BASE_URL = "http://127.0.0.1:8080"

TEST_CASES = [
    {
        "description": "Get today's plan (deterministic path — no LLM)",
        "payload": {"user_id": "test-user-1", "prompt": "what is my routine today"},
    },
    {
        "description": "Get full week (deterministic path — no LLM)",
        "payload": {"user_id": "test-user-1", "prompt": "show me my full week"},
    },
    {
        "description": "Add exercises to a day (agent path — LLM reasons)",
        "payload": {"user_id": "test-user-1", "prompt": "add delts to my tuesday workout"},
    },
    {
        "description": "Log a completed session (agent path — LLM parses and calls log_session)",
        "payload": {
            "user_id": "test-user-1",
            "prompt": "just finished chest — bench 3x8 at 80kg, incline 3x10 at 60kg",
        },
    },
    {
        "description": "Ask for progression advice (agent path — LLM reads history and recommends)",
        "payload": {
            "user_id": "test-user-1",
            "prompt": "should I increase the weight on my bench press?",
        },
    },
]


def invoke(payload: dict) -> str:
    """Send a POST to /invocations and return the response body."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/invocations",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as response:  # nosec B310 — URL is always http://127.0.0.1 (local dev only)
        return response.read().decode()


def ping() -> bool:
    """Check if the agent server is running."""
    try:
        with urllib.request.urlopen(f"{BASE_URL}/ping") as response:  # nosec B310 — local dev only
            body = json.loads(response.read())
            return body.get("status") == "Healthy"
    except Exception:
        return False


if __name__ == "__main__":
    if not ping():
        print("Agent server is not running.")
        print("Start it first: python -m runtime.agent.pt_agent")
        exit(1)

    print("Agent server is running.\n")

    # MANUAL: choose which test case to run, or run all of them
    for i, test in enumerate(TEST_CASES):
        print(f"Test {i + 1}: {test['description']}")
        print(f"  Payload: {json.dumps(test['payload'])}")
        try:
            result = invoke(test["payload"])
            print(f"  Response: {result}")
        except Exception as e:
            print(f"  Error: {e}")
        print()
