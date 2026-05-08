"""
PT Agent — AgentCore runtime entry point.

How AgentCore works:
  - AgentCore runs this file inside a managed container.
  - It exposes a POST /invocations endpoint that receives your payload.
  - @app.entrypoint registers the single function that handles every request.
  - AgentCore does NOT manage the LLM loop — that is Strands' job.

How Strands works:
  - Agent() handles the full LLM <-> tool call loop using Bedrock Converse API.
  - @tool exposes a Python function to the LLM with an auto-generated schema
    derived from your type hints and docstring.
  - The LLM decides when to call which tool based on the user's message.

Intent classification:
  - Simple keyword matching routes deterministic queries (get today, get week)
    directly to DynamoDB without touching the LLM — cheaper and faster.
  - Everything else (modify plan, log workout, recommendations) goes to the
    full agent loop where the LLM reasons and calls tools as needed.

Flow:
  User message
    -> @app.entrypoint
      -> classify_intent (keyword match)
        -> deterministic path: direct DynamoDB call, no LLM
        -> agent path: Strands Agent -> LLM -> @tool functions -> DynamoDB
    -> response
"""

from datetime import datetime

from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel

from prompts import SYSTEM_PROMPT
from tools.workout_tools import (
    get_workout_plan,
    log_workout,
    update_workout_plan,
    get_recent_history,
    get_hours_since_last_trained,
)

app = BedrockAgentCoreApp()


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------
# Keyword matching is intentionally used here instead of an LLM call.
# For simple, predictable intents this is cheaper, faster, and more reliable.
# The LLM is reserved for cases that require reasoning and creativity.


def classify_intent(prompt: str) -> str:
    """
    Route the user's message to a handler without using the LLM.

    Returns one of: "get_today", "get_week", "agent"
    Anything requiring reasoning falls through to "agent".
    """
    p = prompt.lower()

    today_keywords = ["today", "today's", "this morning", "tonight", "what's on"]
    week_keywords = ["full week", "whole week", "weekly", "all week", "this week"]

    if any(w in p for w in today_keywords):
        return "get_today"
    if any(w in p for w in week_keywords):
        return "get_week"

    return "agent"


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
# @tool wraps your Python function and generates a JSON schema from its
# type hints and docstring. The LLM reads that schema to decide when and
# how to call each tool. The docstring here is for the LLM — make it precise.
#
# Note: these are thin wrappers. All DynamoDB logic lives in workout_tools.py
# so it can be swapped out (e.g. to Postgres) without touching this file.


@tool
def get_plan(user_id: str, day_of_week: str | None = None) -> dict:
    """
    Retrieve the user's workout plan.
    Call with day_of_week (e.g. "monday") for a single day.
    Call without day_of_week to get the full weekly split.
    Always call this before modifying a plan so you know what is already there.
    """
    return get_workout_plan(user_id, day_of_week)


@tool
def log_session(
    user_id: str,
    day_of_week: str,
    muscle_group: str,
    exercises: list[dict],
) -> dict:
    """
    Save a completed workout session.
    Each exercise must include: name (str), sets (int), reps (int), weight_kg (float).
    Call this when the user tells you what they just finished training.
    Always confirm back to the user exactly what was saved.
    """
    return log_workout(user_id, day_of_week, muscle_group, exercises)


@tool
def update_plan(
    user_id: str,
    day_of_week: str,
    muscle_group: str,
    exercises: list[dict],
) -> dict:
    """
    Overwrite the planned exercises for one day of the weekly split.
    Call get_plan first to see what is already scheduled before making changes.
    Use this when the user wants to add exercises, swap movements, or adjust volume.
    """
    return update_workout_plan(user_id, day_of_week, muscle_group, exercises)


@tool
def get_history(user_id: str, muscle_group: str, limit: int = 5) -> dict:
    """
    Retrieve the last N sessions for a muscle group.
    Use this to inform progressive overload recommendations.
    For example: if the user asks how their bench is progressing, call this first.
    """
    return get_recent_history(user_id, muscle_group, limit)


@tool
def check_recovery(user_id: str, muscle_group: str) -> dict:
    """
    Check how many hours have passed since the user last trained a muscle group.
    Returns hours_since_last_trained and a boolean recovered (true if >= 48 hours).
    Always call this before scheduling or recommending training for a muscle group.
    If recovered is false, warn the user and suggest an alternative.
    """
    return get_hours_since_last_trained(user_id, muscle_group)


# ---------------------------------------------------------------------------
# Agent setup
# ---------------------------------------------------------------------------
# Agent() manages the LLM <-> tool loop using the Bedrock Converse API.
# It sends tool schemas to the LLM, executes tool calls, and feeds results
# back until the LLM produces a final text response.
#
# Model IDs: https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html

agent = Agent(
    model=BedrockModel(model_id="us.anthropic.claude-3-5-sonnet-20241022-v2:0"),
    system_prompt=SYSTEM_PROMPT,
    tools=[get_plan, log_session, update_plan, get_history, check_recovery],
)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
# @app.entrypoint registers this as the POST /invocations handler.
# AgentCore calls it for every request with the parsed JSON body as payload.
#
# Expected payload shape:
#   {
#     "user_id": "telegram-123456789",
#     "prompt": "what is my routine today"
#   }


@app.entrypoint
def handle(payload: dict) -> str:
    user_id = payload.get("user_id", "anonymous")
    prompt = payload.get("prompt", "")

    intent = classify_intent(prompt)

    # Deterministic fast paths — no LLM involved
    if intent == "get_today":
        day = datetime.now().strftime("%A").lower()
        plan = get_workout_plan(user_id, day)
        if not plan.get("found"):
            return f"No plan found for {day.capitalize()}. Send me your routine and I will set it up."
        exercises = "\n".join(
            f"  - {e['name']}: {e['sets']}x{e['reps']} @ {e['weight_kg']}kg"
            for e in plan["exercises"]
        )
        return f"{day.capitalize()} — {plan['muscle_group'].title()}\n{exercises}"

    if intent == "get_week":
        result = get_workout_plan(user_id)
        if not result.get("plan"):
            return "No weekly plan found. Send me your routine and I will set it up."
        lines = []
        for day in result["plan"]:
            lines.append(f"{day['day'].capitalize()}: {day['muscle_group'].title()}")
        return "\n".join(lines)

    # Agent path — LLM reasons and calls tools as needed
    # Prefix user_id so the LLM always knows whose data to read and write
    response = agent(f"[user_id={user_id}] {prompt}")
    return str(response)


if __name__ == "__main__":
    # AgentCore calls app.run() automatically inside the container.
    # For local testing: python -m runtime.agent.pt_agent
    app.run()
