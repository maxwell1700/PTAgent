import unittest


def classify_intent(prompt: str) -> str:
    p = prompt.lower()
    today_keywords = ["today", "today's", "this morning", "tonight", "what's on"]
    week_keywords = ["full week", "whole week", "weekly", "all week", "this week"]
    if any(w in p for w in today_keywords):
        return "get_today"
    if any(w in p for w in week_keywords):
        return "get_week"
    return "agent"


class TestClassifyIntent(unittest.TestCase):
    def test_today(self):
        self.assertEqual(classify_intent("what's today"), "get_today")

    def test_week(self):
        self.assertEqual(classify_intent("show full week"), "get_week")

    def test_agent(self):
        self.assertEqual(classify_intent("log my bench press"), "agent")
