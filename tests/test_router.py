"""test_router.py — fast unit tests for Universal Open WebUI Router."""
import json
import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from route import (
    CLARIFY_HEADER, Decision, allowed_agents, build_questions, build_state,
    clarify_text, decide, load_registry, parse_override,
    pending_clarification, resolve_pick,
)


class TestRouter(unittest.TestCase):
    def setUp(self):
        registry_path = PROJECT_ROOT / "src" / "agents.json"
        self.agents = load_registry(registry_path.read_text(encoding="utf-8"))

    def test_registry_loading(self):
        self.assertEqual(len(self.agents), 4)
        ids = [a["id"] for a in self.agents]
        self.assertIn("code", ids)
        self.assertIn("data", ids)
        self.assertIn("writing", ids)
        self.assertIn("general", ids)

    def test_allowed_agents_filtering(self):
        # Admin / full access: None returns all
        self.assertEqual(len(allowed_agents(self.agents, None)), 4)
        # Restricted access: only models in accessible set
        permitted = allowed_agents(self.agents, {"qwen2.5-coder:32b", "mistral-small:24b"})
        self.assertEqual({a["id"] for a in permitted}, {"code", "general"})

    def test_parse_override(self):
        # Explicit override with arguments
        d = parse_override("/code refactor this loop", self.agents)
        self.assertIsNotNone(d)
        self.assertEqual(d.agent_id, "code")
        self.assertEqual(d.message, "refactor this loop")

        # Explicit override without arguments
        d2 = parse_override("/write", self.agents)
        self.assertIsNotNone(d2)
        self.assertEqual(d2.agent_id, "writing")
        self.assertEqual(d2.message, "/write")

        # No override
        self.assertIsNone(parse_override("how do I calculate mean?", self.agents))

    def test_build_state(self):
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        state = build_state("next question", history, "Code & Engineering")
        self.assertEqual(state["message"], "next question")
        self.assertIn("user: hello", state["recent_context"])
        self.assertEqual(state["current_assistant"], "Code & Engineering")

    def test_build_questions(self):
        q = build_questions(self.agents)
        self.assertIn("agent", q)
        self.assertEqual(q["agent"]["type"], "choice")
        self.assertEqual(len(q["agent"]["criteria"]), 4)
        self.assertIn("follow_up", q)
        self.assertEqual(q["follow_up"]["type"], "noul")

    def test_decide_follow_up(self):
        answers = {
            "agent": {"choice": "data", "confidence": 0.65, "probabilities": {"data": 0.65, "code": 0.35}},
            "follow_up": {"noul": 0.88},
        }
        decision = decide(answers, self.agents, current_agent_id="code")
        self.assertEqual(decision.action, "dispatch")
        self.assertEqual(decision.agent_id, "code")
        self.assertEqual(decision.source, "follow_up")

    def test_decide_confident(self):
        answers = {
            "agent": {"choice": "code", "confidence": 0.94, "probabilities": {"code": 0.94, "general": 0.06}},
            "follow_up": {"noul": 0.10},
        }
        decision = decide(answers, self.agents, current_agent_id=None)
        self.assertEqual(decision.action, "dispatch")
        self.assertEqual(decision.agent_id, "code")
        self.assertEqual(decision.source, "confident")

    def test_decide_clarify_on_low_confidence(self):
        answers = {
            "agent": {"choice": "code", "confidence": 0.35, "probabilities": {"code": 0.35, "writing": 0.30, "data": 0.20, "general": 0.15}},
            "follow_up": {"noul": 0.10},
        }
        decision = decide(answers, self.agents, current_agent_id=None)
        self.assertEqual(decision.action, "clarify")
        self.assertIsNone(decision.agent_id)
        self.assertIn("code", decision.alternates)

    def test_clarification_flow(self):
        decision = Decision("clarify", None, "clarify", alternates=["code", "data"])
        text = clarify_text(decision, self.agents)
        self.assertTrue(text.startswith(CLARIFY_HEADER))
        self.assertIn("1. **Code & Engineering**", text)
        self.assertIn("(` /code `)".replace(" ", ""), text.replace(" ", ""))

        # Pending clarification detector
        mock_messages = [
            {"role": "user", "content": "how do I analyze this log file?"},
            {"role": "assistant", "content": text},
            {"role": "user", "content": "1"},
        ]
        pending = pending_clarification(mock_messages, self.agents)
        self.assertIsNotNone(pending)
        opts, orig = pending
        self.assertEqual(opts, ["code", "data"])
        self.assertEqual(orig, "how do I analyze this log file?")

        # Reply resolution
        self.assertEqual(resolve_pick("1", opts, self.agents), "code")
        self.assertEqual(resolve_pick("the second one", opts, self.agents), "data")
        self.assertEqual(resolve_pick("data", opts, self.agents), "data")
        self.assertIsNone(resolve_pick("something completely unrelated that has many words", opts, self.agents))

    def test_dist_built_pipe(self):
        sys.path.insert(0, str(PROJECT_ROOT / "dist"))
        import router_pipe
        pipe = router_pipe.Pipe()
        self.assertEqual(pipe.valves.OWUI_BASE_URL, "http://localhost:8080")
        self.assertTrue(len(pipe.valves.AGENTS) > 100)


if __name__ == "__main__":
    unittest.main()
