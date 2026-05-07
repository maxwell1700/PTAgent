#!/usr/bin/env python3
import aws_cdk as cdk
import os

from infra.pt_agent_stack import PtAgentStack


app = cdk.App()
PtAgentStack(
    app,
    "PtAgentStack",
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    ),
    
)

app.synth()
