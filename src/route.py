"""route.py — pure TypeSafe Jev routing logic for Open WebUI router pipe.

Standard library only. Decisions use TypeSafe System One (Jev) primitives:
- Choice: identifies the best specialist agent
- Noul: determines if the turn is a follow-up to the ongoing conversation
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# ── Routing Thresholds ───────────────────────────────────────────────────────
CONFIDENT_THRESHOLD = 0.85     # agent.confidence >= this → dispatch directly
FOLLOW_UP_THRESHOLD = 0.70     # follow_up noul >= this → stay with current agent
CLARIFY_THRESHOLD   = 0.50     # agent.confidence < this → ask user to clarify

# ── State Budgets ────────────────────────────────────────────────────────────
CONTEXT_TURNS = 2              # Last 2 user/assistant pairs
CONTEXT_CHARS = 1500           # Max history characters passed to router
MESSAGE_CHARS = 3000           # Max characters of the incoming user turn

# ── TypeSafe System One API ──────────────────────────────────────────────────
JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"

CLARIFY_HEADER = "I want to route this to the right specialist. Which one fits best?"
CLARIFY_FOOTER = "Reply with the number or name — I will send your original question there."


@dataclass
class Decision:
    action: str                       # "dispatch" | "clarify"
    agent_id: str | None              # Target agent id when dispatching
    source: str                       # override | follow_up | confident | medium | clarified | clarify
    alternates: list[str] = field(default_factory=list)  # Runner-up agent ids
    message: str | None = None        # Cleaned message (override prefix stripped)


class RouterUnavailable(RuntimeError):
    """Raised when TypeSafe API is unreachable or returns an error."""
    pass


# ── Registry & Access ────────────────────────────────────────────────────────
def load_registry(text: str) -> list[dict]:
    """Parse and validate the agent registry JSON."""
    agents = json.loads(text)
    ids = [a["id"] for a in agents]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate agent id in registry")
    return agents


def allowed_agents(registry: list[dict], accessible_target_ids: set[str] | None = None) -> list[dict]:
    """Filter registry to specialist models the current user is permitted to use.
    If accessible_target_ids is None (admin or bypass), all agents are allowed."""
    if accessible_target_ids is None:
        return list(registry)
    return [a for a in registry if a["target"] in accessible_target_ids]


def parse_override(message: str, agents: list[dict]) -> Decision | None:
    """Check if message starts with an explicit override (e.g. '/code refactor this')."""
    head, _, rest = message.strip().partition(" ")
    head_lower = head.lower()
    for a in agents:
        if a.get("override") and head_lower == a["override"].lower():
            return Decision(
                action="dispatch",
                agent_id=a["id"],
                source="override",
                message=rest.strip() or message
            )
    return None


# ── TypeSafe Jev Request Building ────────────────────────────────────────────
def build_state(message: str, history: list[dict], current_agent_label: str | None = None) -> dict:
    """Extract trimmed conversation state for TypeSafe Jev."""
    turns = [m for m in history if m.get("role") in ("user", "assistant")][-2 * CONTEXT_TURNS:]
    ctx, budget = [], CONTEXT_CHARS
    for m in reversed(turns):
        text = str(m.get("content", ""))[:budget]
        if not text:
            break
        ctx.insert(0, f"{m['role']}: {text}")
        budget -= len(text)
    return {
        "message": message[:MESSAGE_CHARS],
        "recent_context": "\n".join(ctx),
        "current_assistant": current_agent_label or "none",
    }


def build_questions(agents: list[dict]) -> dict:
    """Build speculative Choice + Noul questions for TypeSafe Jev."""
    criteria = {a["id"]: a["route_criteria"] for a in agents}
    return {
        "agent": {
            "type": "choice",
            "instructions": (
                "Which assistant should answer `message`? Judge `message` itself; use "
                "`recent_context` only to clarify short or ambiguous references."
            ),
            "criteria": criteria,
        },
        "follow_up": {
            "type": "noul",
            "instructions": (
                "Is `message` a continuation of the ongoing conversation in `recent_context` "
                "(follow-up question, correction, extra detail) rather than a new, unrelated topic?"
            ),
            "criteria": {
                "true": "Continues the active topic or references earlier conversation.",
                "false": "Starts a new, independent topic or recent_context is empty.",
            },
        },
    }


def call_jev(api_key: str, state: dict, questions: dict, timeout: float = 2.5) -> dict:
    """Call TypeSafe System One API (/v1/systemone)."""
    payload = {"model": JEV_MODEL, "state": state, "questions": questions}
    req = urllib.request.Request(
        JEV_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("answers", {})
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
        raise RouterUnavailable(f"TypeSafe API error: {e}") from e


# ── Decision Logic ───────────────────────────────────────────────────────────
def decide(answers: dict, agents: list[dict], current_agent_id: str | None = None) -> Decision:
    """Apply thresholds to TypeSafe answers to produce a routing Decision."""
    allowed_ids = {a["id"] for a in agents}
    agent_ans = answers.get("agent") or {}
    probs = {k: float(v) for k, v in (agent_ans.get("probabilities") or {}).items() if k in allowed_ids}
    ranked = sorted(probs, key=probs.get, reverse=True)
    choice = agent_ans.get("choice") if agent_ans.get("choice") in allowed_ids else (ranked[0] if ranked else None)
    conf = float(agent_ans.get("confidence") or 0.0)

    follow_noul = float((answers.get("follow_up") or {}).get("noul") or 0.0)
    alternates = [x for x in ranked if x != choice][:2]

    # Rule 1: Active follow-up sticks with current assistant
    if current_agent_id in allowed_ids and follow_noul >= FOLLOW_UP_THRESHOLD:
        return Decision("dispatch", current_agent_id, "follow_up", [x for x in ranked if x != current_agent_id][:2])

    # Rule 2: High confidence picks the choice
    if choice and conf >= CONFIDENT_THRESHOLD:
        return Decision("dispatch", choice, "confident", alternates)

    # Rule 3: Low confidence or missing choice asks the user to clarify
    if choice is None or conf < CLARIFY_THRESHOLD:
        return Decision("clarify", None, "clarify", ranked[:2])

    # Rule 4: Medium confidence routes to top choice with runner-up alternate
    return Decision("dispatch", choice, "medium", alternates)


# ── Clarification Flow ───────────────────────────────────────────────────────
def clarify_text(decision: Decision, agents: list[dict]) -> str:
    """Format a numbered question offering candidate agents to the user."""
    by_id = {a["id"]: a for a in agents}
    candidates = [by_id[i] for i in decision.alternates if i in by_id][:2] or [by_id[i] for i in list(by_id)[:2]]
    lines = [
        f"{n}. **{a['label']}** — {a['route_criteria']['what'].split('.')[0]}. (`{a['override']}`)"
        for n, a in enumerate(candidates, 1)
    ]
    return f"{CLARIFY_HEADER}\n\n" + "\n".join(lines) + f"\n\n{CLARIFY_FOOTER}"


def pending_clarification(messages: list[dict], agents: list[dict]) -> tuple[list[str], str] | None:
    """Detect if the previous turn was our clarifying prompt and return (option_ids, original_question)."""
    if len(messages) < 3:
        return None
    ask, original = messages[-2], messages[-3]
    if ask.get("role") != "assistant" or original.get("role") != "user":
        return None
    content = str(ask.get("content") or "")
    if not content.startswith(CLARIFY_HEADER):
        return None

    by_override = {a["override"].lower(): a["id"] for a in agents if a.get("override")}
    found_ids = []
    for token in re.findall(r"\(`(/[^`]+)`\)", content):
        aid = by_override.get(token.lower())
        if aid and aid not in found_ids:
            found_ids.append(aid)
    return (found_ids, str(original.get("content") or "")) if found_ids else None


_ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2}


def resolve_pick(reply: str, option_ids: list[str], agents: list[dict]) -> str | None:
    """Resolve user's answer to a clarification prompt into an agent id."""
    text = " ".join(reply.lower().replace("*", " ").split()).strip(" .!?)#,:;")
    if not text or len(text.split()) > 6:
        return None

    # Number match: "1", "option 2", "#1"
    num_match = re.fullmatch(r"(?:option|number|no\.?)?\s*#?([1-9])", text)
    if num_match:
        idx = int(num_match.group(1)) - 1
        return option_ids[idx] if idx < len(option_ids) else None

    # Ordinal match: "the first", "second"
    for word in text.replace(",", " ").split():
        if word in _ORDINALS and _ORDINALS[word] < len(option_ids):
            return option_ids[_ORDINALS[word]]

    # Name match: "code", "data", "writing"
    by_id = {a["id"]: a for a in agents}
    for oid in option_ids:
        a = by_id.get(oid, {})
        aliases = {a.get("label", "").lower(), oid, oid.replace("-", " "), (a.get("override") or "").lstrip("/")}
        if any(alias and alias in text for alias in aliases):
            return oid
    return None
