"""
PT Agent — CDK Infrastructure Stack.

What this stack provisions:
  - DynamoDB table (single-table design, PK + SK)
  - IAM role for the AgentCore runtime with least-privilege permissions
  - AgentCore Runtime (AWS::BedrockAgentCore::Runtime L1 CfnResource)
    — packages runtime/agent/ as a ZIP, uploads to S3, AgentCore runs it directly
    — no Docker image or ECR required
  - SSM Parameter for the Telegram bot token (you populate the value manually)
  - Lambda function for the Telegram webhook
  - API Gateway HTTP endpoint that Telegram calls

What requires manual steps before deploying:
  1. Enable Claude Sonnet in Bedrock Model Access (one-time, AWS console)
  2. Create SSM parameters (see comments near bot_token_param below)

What requires manual steps after deployment:
  3. Register the Telegram webhook URL with BotFather

Note on code packaging:
  CDK uses Docker during `cdk deploy` to bundle runtime/agent/ and its dependencies
  into a ZIP file, which is uploaded to S3. AgentCore pulls the ZIP from S3 and
  runs it directly — no Dockerfile needed. Docker must be running on your machine
  during deployment.

Swapping DynamoDB for Postgres later:
  - Replace dynamodb.Table with rds.DatabaseInstance
  - Update agent_role permissions to allow RDS access
  - Only workout_tools.py needs code changes — nothing else changes
"""

import aws_cdk as cdk
from aws_cdk import (
    BundlingOptions,
    DockerImage,
    RemovalPolicy,
    Stack,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_apigateway as apigateway,
    aws_ssm as ssm,
    aws_s3_assets as s3_assets,
    aws_bedrockagentcore as agentcore,
    CfnOutput,
)
from constructs import Construct


class PtAgentStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -------------------------------------------------------------------
        # DynamoDB — single table for all user data
        # -------------------------------------------------------------------
        # Single-table design: plans and logs share one table, distinguished by SK prefix.
        #   Plans:  PK=USER#<id>  SK=PLAN#<day>
        #   Logs:   PK=USER#<id>  SK=LOG#<timestamp>
        #
        # RETAIN means the table survives a cdk destroy — protects user data.
        # Change to DESTROY only in a dev/test environment.

        workout_table = dynamodb.Table(
            self,
            "WorkoutTable",
            partition_key=dynamodb.Attribute(
                name="PK", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery=True,
        )

        # -------------------------------------------------------------------
        # Agent code asset — ZIP packaged by CDK at deploy time
        # -------------------------------------------------------------------
        # CDK spins up a Docker container, installs dependencies from
        # runtime/requirements.txt, copies the agent code, and produces a ZIP.
        # AgentCore pulls this ZIP from S3 and runs it as the agent process.
        #
        # The entry point AgentCore calls is pt_agent.py at the root of the ZIP.
        # Dependencies are installed alongside the code so imports resolve correctly.
        #
        # Requires Docker to be running during cdk deploy.

        agent_asset = s3_assets.Asset(
            self,
            "AgentCodeAsset",
            path="runtime/agent",
            bundling=BundlingOptions(
                image=DockerImage.from_registry("python:3.12-slim"),
                command=[
                    "bash",
                    "-c",
                    # Install dependencies directly into /asset-output so they
                    # are included in the ZIP alongside the agent code.
                    # x86_64 platform targeting ensures compatibility with AgentCore's runtime.
                    """
                    pip install \
                        --target /asset-output \
                        --platform manylinux2014_aarch64 \
                        --only-binary=:all: \
                        --python-version 312 \
                        -r /asset-input/requirements.txt \
                        --quiet
                    cp -r /asset-input/* /asset-output/
                    """,
                ],
            ),
        )

        # -------------------------------------------------------------------
        # IAM role — permissions the AgentCore runtime needs
        # -------------------------------------------------------------------
        # AgentCore assumes this role when running your agent.
        # The service principal and conditions follow the pattern from AWS samples
        # to ensure only your account's AgentCore runtimes can assume this role.

        agent_role = iam.Role(
            self,
            "PtAgentRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com"
            ).with_conditions(
                {
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"
                    },
                }
            ),
            description="Runtime role for the PT AgentCore agent",
        )

        # DynamoDB — read and write workout plans and session logs
        workout_table.grant_read_write_data(agent_role)

        # Bedrock — invoke Claude for LLM calls via Strands
        # MANUAL STEP 1: enable Claude Sonnet in the Bedrock console before deploying.
        # Go to: AWS Console -> Bedrock -> Model access -> Enable Claude Sonnet
        agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:*",
                ],
            )
        )

        # CloudWatch Logs — AgentCore writes agent logs here automatically
        agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup", "logs:DescribeLogStreams"],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/*"
                ],
            )
        )
        agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:DescribeLogGroups"],
                # Docs require broader scope here — AgentCore enumerates log groups
                # across the account to find its own, not just within its prefix.
                resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:*"],
            )
        )
        agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"
                ],
            )
        )

        # X-Ray — distributed tracing for agent invocations
        agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                resources=[f"arn:aws:xray:{self.region}:{self.account}:*"],
            )
        )

        # CloudWatch metrics — AgentCore publishes runtime metrics
        agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}
                },
            )
        )

        # Allow AgentCore to read the packaged agent ZIP from S3
        agent_asset.grant_read(agent_role)

        # -------------------------------------------------------------------
        # AgentCore Runtime — agentcore.CfnRuntime (L1 construct)
        # -------------------------------------------------------------------
        # CfnRuntime is the proper L1 for AWS::BedrockAgentCore::Runtime.
        # It downloads the ZIP from S3, installs it, and runs pt_agent.py.
        #
        # NetworkMode PUBLIC — no VPC required for MVP since DynamoDB uses
        # IAM auth with a public endpoint. Switch to VPC when adding RDS.
        #
        # EntryPoint must be the filename at the root of the ZIP — pt_agent.py
        # is the file that defines @app.entrypoint and calls app.run().

        agent_runtime = agentcore.CfnRuntime(
            self,
            "PtAgentRuntime",
            agent_runtime_name="pt_agent",
            description="PT assistant — tracks workouts, recommends progression",
            role_arn=agent_role.role_arn,
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="PUBLIC",
            ),
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                code_configuration=agentcore.CfnRuntime.CodeConfigurationProperty(
                    code=agentcore.CfnRuntime.CodeProperty(
                        s3=agentcore.CfnRuntime.S3LocationProperty(
                            bucket=agent_asset.s3_bucket_name,
                            prefix=agent_asset.s3_object_key,
                        )
                    ),
                    entry_point=["pt_agent.py"],
                    runtime="PYTHON_3_12",
                )
            ),
            environment_variables={
                "WORKOUT_TABLE_NAME": workout_table.table_name,
            },
        )

        # -------------------------------------------------------------------
        # SSM Parameter — Telegram bot token stored securely
        # -------------------------------------------------------------------
        # Creates the SSM path. You populate the actual token after creating
        # your Telegram bot via @BotFather.
        #
        # MANUAL STEP 2: store your bot token after deployment:
        #   aws ssm put-parameter \
        #     --name /pt-agent/telegram-bot-token \
        #     --value "<YOUR_BOT_TOKEN>" \
        #     --type SecureString \
        #     --overwrite

        # These parameters must be created manually before deploying:
        #
        #   aws ssm put-parameter \
        #     --name /pt-agent/telegram-bot-token \
        #     --value "<YOUR_BOT_TOKEN>" \
        #     --type SecureString
        #
        #   aws ssm put-parameter \
        #     --name /pt-agent/allowed-user-ids \
        #     --value "123456789,987654321" \
        #     --type String
        #
        # Get your bot token from @BotFather and your user ID from @userinfobot.

        bot_token_param = ssm.StringParameter.from_string_parameter_name(
            self,
            "TelegramBotToken",
            string_parameter_name="/pt-agent/telegram-bot-token",
        )

        allowed_users_param = ssm.StringParameter.from_string_parameter_name(
            self,
            "TelegramAllowedUserIds",
            string_parameter_name="/pt-agent/allowed-user-ids",
        )

        # -------------------------------------------------------------------
        # Telegram webhook Lambda
        # -------------------------------------------------------------------
        # Receives Telegram messages, checks the user whitelist, and forwards
        # to the AgentCore runtime.
        #

        telegram_lambda = lambda_.Function(
            self,
            "TelegramWebhookLambda",
            runtime=lambda_.Runtime.PYTHON_3_12,
            code=lambda_.Code.from_asset("lambdas/telegram"),
            handler="handler.handler",
            timeout=cdk.Duration.seconds(30),
            environment={
                "BOT_TOKEN_PARAM": bot_token_param.parameter_name,
                "WORKOUT_TABLE_NAME": workout_table.table_name,
                "ALLOWED_USER_IDS_PARAM": allowed_users_param.parameter_name,
                "AGENT_RUNTIME_ARN": agent_runtime.attr_agent_runtime_arn,
            },
        )

        # Allow Lambda to read bot token and allowed user IDs from SSM
        bot_token_param.grant_read(telegram_lambda)
        allowed_users_param.grant_read(telegram_lambda)

        # Allow Lambda to invoke the AgentCore runtime
        telegram_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[
                    agent_runtime.attr_agent_runtime_arn,
                    f"{agent_runtime.attr_agent_runtime_arn}/runtime-endpoint/DEFAULT",
                ],
            )
        )

        # -------------------------------------------------------------------
        # API Gateway — public HTTPS endpoint Telegram posts to
        # -------------------------------------------------------------------

        api = apigateway.RestApi(
            self,
            "TelegramWebhookApi",
            rest_api_name="pt-agent-telegram-webhook",
            description="Receives Telegram webhook events for the PT agent",
        )

        webhook_resource = api.root.add_resource("webhook")
        webhook_resource.add_method(
            "POST",
            apigateway.LambdaIntegration(telegram_lambda),
        )

        # -------------------------------------------------------------------
        # Stack outputs
        # -------------------------------------------------------------------

        CfnOutput(self, "WorkoutTableName", value=workout_table.table_name)
        CfnOutput(self, "AgentRoleArn", value=agent_role.role_arn)
        CfnOutput(
            self,
            "AgentRuntimeArn",
            value=agent_runtime.attr_agent_runtime_arn,
            description="Use this ARN in the Telegram Lambda AGENT_RUNTIME_ARN env var",
        )
        CfnOutput(
            self,
            "TelegramWebhookUrl",
            value=f"{api.url}webhook",
            description="Register with Telegram: curl https://api.telegram.org/bot<TOKEN>/setWebhook?url=<THIS_URL>",
        )
