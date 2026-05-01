from aws_cdk import (
    # Duration,
    Stack,
    # aws_sqs as sqs,
)
from constructs import Construct

class PtAgentStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # The code that defines your stack goes here

        # example resource
        # queue = sqs.Queue(
        #     self, "PtAgentQueue",
        #     visibility_timeout=Duration.seconds(300),
        # )
        #TODO
        # dynamo to save gym split for the week 
            # json object for each day of the week with muscle group , weight , reps , sets 
        # using agent core build agent to automatically track and update  gym routine for users based on muscle growth and recovery time
        # system to send notifications about gym routine for the nextday


        # bedrokc agent core constructs 
        #bundle up /agent/pt_agent file
        # roles an permissions for agent to access dynamo and notification system cloudwatch logs
        #may need bedrock code interpreter construct to run agent core code in bedrock environment
        # memory ?


