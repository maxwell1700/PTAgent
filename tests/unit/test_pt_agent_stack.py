import importlib
import os
import sys
import types
import unittest
from unittest.mock import patch

os.environ.setdefault("WORKOUT_TABLE_NAME", "test")

# Stub heavy deps so classify_intent can be imported without AWS/Strands installed
_agentcore = types.ModuleType("bedrock_agentcore")
_agentcore.BedrockAgentCoreApp = type(  # noqa: E501
    "BedrockAgentCoreApp",
    (),
    {"entrypoint": lambda _s, f: f, "run": lambda _s: None},
)
sys.modules.setdefault("bedrock_agentcore", _agentcore)

_strands = types.ModuleType("strands")
_strands.Agent = type(
    "Agent",
    (),
    {"__init__": lambda _s, **_kw: None, "__call__": lambda _s, _p: "ok"},
)
_strands.tool = lambda f: f
sys.modules.setdefault("strands", _strands)

with patch("boto3.resource"):
    import runtime.agent.pt_agent as agent_mod

    importlib.reload(agent_mod)

classify = agent_mod.classify_intent


class TestClassifyIntent(unittest.TestCase):
    def test_today(self):
        self.assertEqual(classify("what's today"), "get_today")

    def test_week(self):
        self.assertEqual(classify("show full week"), "get_week")

    def test_agent(self):
        self.assertEqual(classify("log my bench press"), "agent")


if __name__ == "__main__":
    unittest.main()
