# PT Agent — Smart Personal Trainer on AWS AgentCore

A conversational personal trainer assistant you interact with via Telegram.
Send a message, get a response. Your workout data lives in DynamoDB.
Built on AWS AgentCore and Strands Agents, deployed with CDK and GitHub Actions.

---

## What it does

- Tracks your weekly gym split (which muscle groups you train each day)
- Logs completed workout sessions with sets, reps, and weight
- Recommends progressive overload based on your session history
- Enforces 48-hour muscle recovery rules using real data — not LLM guessing
- Responds to natural language messages on Telegram (mobile-first, no UI to build)

---

## Architecture

```
Telegram app
    |
    | HTTPS (message)
    v
API Gateway  ──────────────────────────────────────────────────────────────
    |                                                                      |
    v                                                            CDK provisions this
Lambda (lambdas/telegram/handler.py)                            URL is output after
    | - verifies Telegram HMAC signature                        cdk deploy. Register
    | - checks user whitelist                                   it once with Telegram.
    | - forwards to AgentCore
    v
AgentCore Runtime  (AWS::BedrockAgentCore::Runtime)
    | - managed container runtime hosted by AWS
    | - runs runtime/agent/pt_agent.py
    | - handles /invocations and /ping endpoints
    v
pt_agent.py (@app.entrypoint)
    |
    |── classify_intent()  ──────────────────────> DynamoDB (no LLM, no cost)
    |   keyword match                               get_today / get_week
    |
    └── Strands Agent()  ───────────────────────> Bedrock Converse API (Claude)
        LLM loop                                    |
                                                    v
                                                @tool functions  ──> DynamoDB
                                                get_plan
                                                log_session
                                                update_plan
                                                get_history
                                                check_recovery
```

### Key design decisions

**AgentCore** is the execution environment — it hosts your code as a managed runtime, routes HTTP requests to `@app.entrypoint`, and manages session context. It does not run the LLM loop.

**Strands** is the orchestration layer — it sends your tool schemas to the LLM, executes tool calls locally in the same process, and loops until the LLM produces a final response.

**Intent classification** uses keyword matching for simple reads — no LLM cost for "what's my routine today". Only creative tasks (modifying plans, recommendations) touch the LLM.

**Code packaging** — AgentCore runs your agent from a ZIP file, not a Docker image. CDK bundles `runtime/agent/` and its dependencies into a ZIP at deploy time using a Docker bundling step, uploads it to S3, and points AgentCore at it. No Dockerfile or ECR needed.

**Single-table DynamoDB** — plans and logs share one table, separated by SK prefix:
```
PK=USER#<telegram_id>  SK=PLAN#monday      -> monday's workout plan
PK=USER#<telegram_id>  SK=PLAN#tuesday     -> tuesday's workout plan
PK=USER#<telegram_id>  SK=LOG#<timestamp>  -> completed session record
```

**Telegram as identity provider** — Telegram user IDs are unique and authenticated by Telegram. No Cognito needed. HMAC signature verification proves requests came from Telegram's servers. A user ID whitelist restricts access to known users.

**Swapping DynamoDB for Postgres** — all DynamoDB logic is in `runtime/agent/tools/workout_tools.py`. Swap boto3 calls for SQLAlchemy and nothing else changes. In the stack, replace `dynamodb.Table` with `rds.DatabaseInstance` and switch `NetworkMode` to `VPC`.

---

## Project structure

```
PT_Agent/
├── .github/
│   └── workflows/
│       └── deploy.yml          CI/CD pipeline (lint, security, test, synth, deploy)
│
├── infra/
│   └── pt_agent_stack.py       CDK stack — all AWS infrastructure defined here
│
├── lambdas/
│   └── telegram/
│       ├── handler.py          Telegram webhook receiver
│       └── requirements.txt    Lambda runtime deps (boto3 only)
│
├── runtime/
│   ├── requirements.txt        Agent runtime deps (AgentCore SDK, Strands, boto3)
│   └── agent/
│       ├── pt_agent.py         AgentCore entrypoint + Strands agent + intent routing
│       ├── prompts.py          LLM system prompt
│       ├── requirements.txt    Bundled by CDK into the AgentCore ZIP
│       └── tools/
│           └── workout_tools.py  DynamoDB read/write — swap this file for Postgres
│
├── scripts/
│   └── invoke_agent.py         Local testing — sends requests to the agent server
│
├── tests/
│   └── unit/
│
├── app.py                      CDK entrypoint
├── requirements.txt            CDK deps only
└── requirements-dev.txt        pytest, ruff, bandit, pip-audit
```

---

## Prerequisites

- AWS account with CDK bootstrapped (`cdk bootstrap`)
- AWS CLI configured (`aws configure`)
- Docker installed and running (required during `cdk deploy` for asset bundling)
- Python 3.12
- Node.js 22 (for CDK CLI)
- A Telegram account

---

## One-time setup

### 1. Enable Claude in Bedrock

Go to AWS Console → Bedrock → Model access → enable **Claude Sonnet**.
This is required before the agent can invoke the LLM.

### 2. Create your Telegram bot

1. Open Telegram and message **@BotFather**
2. Send `/newbot` and follow the prompts — save the bot token
3. Message **@userinfobot** — save your Telegram user ID

### 3. Configure GitHub secrets

In your GitHub repo → Settings → Secrets, add:

| Secret | Value |
|---|---|
| `AWS_ACCOUNT_ID` | your 12-digit AWS account ID |
| `AWS_DEPLOY_ROLE_ARN` | ARN of your GitHub OIDC deploy role |
| `ALLOWED_USER_IDS` | your Telegram user ID from @userinfobot |

### 4. Set your Telegram user ID in the stack

Open `infra/pt_agent_stack.py` and replace the placeholder:
```python
"ALLOWED_USER_IDS": "REPLACE_WITH_YOUR_TELEGRAM_USER_ID",
```
Or remove the hardcoded value entirely — it is read from the `ALLOWED_USER_IDS`
environment variable which GitHub Actions injects from secrets.

---

## Deployment

Push to `main`. The GitHub Actions pipeline runs automatically:

```
lint → security → test → synth → deploy
```

Deploy only runs on push to `main` after all checks pass.
PRs run lint, security, test, and synth — but not deploy.

### After first deploy

The CDK stack outputs:
```
WorkoutTableName    = PtAgentStack-WorkoutTable-XXXX
AgentRuntimeArn     = arn:aws:bedrock-agentcore:...
AgentRoleArn        = arn:aws:iam::...
TelegramWebhookUrl  = https://xxxx.execute-api.us-east-1.amazonaws.com/prod/webhook
```

**Manual step 1 — store your Telegram bot token:**
```bash
aws ssm put-parameter \
  --name /pt-agent/telegram-bot-token \
  --value "<YOUR_BOT_TOKEN>" \
  --type SecureString \
  --overwrite
```

**Manual step 2 — register the webhook with Telegram (once only):**
```bash
curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=<TelegramWebhookUrl>"
```

Telegram remembers this permanently. Only repeat if the API Gateway URL changes.

---

## Local testing

Test the agent without deploying. You need the DynamoDB table deployed and AWS credentials configured.

```bash
# Terminal 1 — start the agent server locally on port 8080
export WORKOUT_TABLE_NAME=<WorkoutTableName from cdk deploy output>
python -m runtime.agent.pt_agent

# Terminal 2 — send test messages
python scripts/invoke_agent.py
```

The local server exposes the same `/invocations` and `/ping` endpoints AgentCore uses in production. The `invoke_agent.py` script includes five test cases covering both the deterministic and agent paths.

---

## CI/CD pipeline

Defined in `.github/workflows/deploy.yml`.

| Job | Runs on | What it does |
|---|---|---|
| Lint | all events | ruff check + format |
| Security | all events | bandit SAST + pip-audit CVE scan |
| Test | all events | pytest with 80% coverage gate |
| Synth | all events | `cdk synth --no-bundling` — validates CloudFormation compiles |
| Deploy | push to main | `cdk deploy` with Docker bundling + OIDC auth |

AWS authentication uses OIDC — no long-lived access keys stored anywhere.

---

## AgentCore concepts for learners

### What AgentCore actually is

AgentCore is AWS's managed runtime for AI agents. It:
- Hosts your code (downloaded from a ZIP on S3)
- Exposes `/invocations` and `/ping` HTTP endpoints
- Routes POST requests to your `@app.entrypoint` function
- Manages session context across requests
- Writes logs to CloudWatch under `/aws/bedrock-agentcore/runtimes/`

It does **not** run the LLM or manage tool calls. That is Strands' job.

### What Strands actually is

Strands handles the agent loop. When you call `agent(prompt)`:
1. Sends system prompt + user message + tool schemas to Claude via Bedrock Converse API
2. LLM responds with a tool call request
3. Strands executes your `@tool` Python function locally in the same process
4. Result goes back to the LLM
5. Repeats until the LLM gives a final text response

Your `@tool` functions are plain Python — no HTTP calls, no serialization, just function calls.

### Why docstrings matter here

Strands reads your `@tool` function docstrings at runtime using Python's `inspect` module and sends them to the LLM as the tool description. The LLM uses the docstring to decide when and how to call each tool. A vague docstring produces wrong tool usage. A precise docstring improves agent behaviour directly.

### Deterministic vs AI

| Action | Path | Why |
|---|---|---|
| View today's plan | keyword match → DynamoDB | One right answer |
| View full week | keyword match → DynamoDB | One right answer |
| Add exercises to a day | LLM → tools | Requires creative judgment |
| Log a session | LLM → tools | Requires parsing natural language |
| Progression advice | LLM → tools | Requires reasoning over history |
| Recovery check | deterministic math in tool | 48hr rule is a calculation |

### The LLM never touches your database directly

```
LLM reads docstring  →  decides to call get_plan
Strands calls your Python function  →  hits DynamoDB
Result returned to LLM  →  LLM uses it in its response
```

The LLM only sees the docstring and the return value. It never sees or executes your implementation.

---

## Extending the project

**Add a tool** — add a function to `workout_tools.py`, wrap with `@tool` in `pt_agent.py`, add to `Agent(tools=[...])`. The LLM learns about it automatically from the docstring.

**Add a deterministic intent** — add keywords to `classify_intent()` and a handler branch in `handle()`. Zero LLM cost for the new path.

**Add more users** — add their Telegram user ID to `ALLOWED_USER_IDS` in GitHub secrets and redeploy.

**Swap DynamoDB for Postgres** — replace boto3 calls in `workout_tools.py`. In the stack swap `dynamodb.Table` for `rds.DatabaseInstance` and set `NetworkMode` to `VPC`.
