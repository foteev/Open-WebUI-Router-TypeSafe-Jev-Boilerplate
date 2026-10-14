# BUILD-STRIP-START — dev-only import; build_pipe.py inlines route.py in its place
from route import (  # noqa: F401
    Decision, RouterUnavailable, allowed_agents, build_questions, build_state,
    call_jev, clarify_text, decide, load_registry, parse_override,
    pending_clarification, resolve_pick,
)
# BUILD-STRIP-END

import asyncio
import json
import time
from typing import AsyncGenerator

from pydantic import BaseModel, Field

HOP_HEADER = "X-OWUI-Router"
DEFAULT_AGENTS = r'''__AGENTS_JSON__'''


def parse_sse_line(line: str):
    """Parse one line of an OpenAI/OWUI SSE stream → (text, sources, is_done)."""
    line = line.strip()
    if not line.startswith("data:"):
        return "", [], False
    data = line[5:].strip()
    if data == "[DONE]":
        return "", [], True
    try:
        obj = json.loads(data)
    except ValueError:
        return "", [], False

    text = ""
    for ch in obj.get("choices") or []:
        delta = ch.get("delta") or ch.get("message") or {}
        text += delta.get("content") or ""
    sources = obj.get("sources") or obj.get("citations") or []
    return text, sources if isinstance(sources, list) else [], False


class Pipe:
    class Valves(BaseModel):
        TYPESAFE_API_KEY: str = Field(default="", description="TypeSafe API key for Jev router")
        AGENTS: str = Field(default=DEFAULT_AGENTS, description="Agent registry (JSON)")
        OWUI_BASE_URL: str = Field(default="http://localhost:8080", description="Open WebUI URL inside its container")
        ROUTER_TIMEOUT_S: float = Field(default=2.5, description="Timeout for TypeSafe Jev API call")
        DISPATCH_TIMEOUT_S: float = Field(default=300.0, description="Max seconds for specialist model response")
        STICKY_TTL_S: int = Field(default=86400, description="Seconds a chat remembers its active specialist")
        SHOW_LABEL: bool = Field(default=True, description='Show "Answered by: ..." status above responses')

    def __init__(self):
        self.valves = self.Valves()
        self._current: dict[str, tuple[str, float]] = {}  # chat_id → (agent_id, timestamp)
        self._access_cache: dict[str, tuple[float, set[str]]] = {}  # user_id → (timestamp, model_ids)

    async def pipe(self, body: dict, __user__: dict = None, __request__=None,
                   __event_emitter__=None, __metadata__: dict = None, __task__: str = None):
        """Open WebUI pipe entrypoint."""
        user = __user__ or {}
        meta = __metadata__ or {}

        # 1. Prevent recursion
        if __request__ is not None and __request__.headers.get(HOP_HEADER):
            return "Error: router loopback recursion detected. Check specialist target IDs in the AGENTS valve."

        token = self._extract_token(__request__)
        if not token:
            return "Error: No session token found. Please sign in again."

        # 2. Load agent registry
        try:
            registry = load_registry(self.valves.AGENTS)
        except Exception as e:
            return f"Error: AGENTS valve contains invalid JSON ({e})."

        # 3. Filter agents by user's access
        accessible_targets = await self._get_accessible_model_ids(user, token)
        agents = allowed_agents(registry, accessible_targets)
        if not agents:
            return "No specialist assistant is available for your account. Please contact your administrator."

        # 4. Background tasks (title/tags generation) bypass router
        if __task__:
            general = next((a for a in agents if a["id"] == "general"), agents[0])
            return await self._complete_task(general["target"], body.get("messages", []), token)

        return self._dispatch_turn(body, user, meta, token, agents, __event_emitter__)

    async def _dispatch_turn(self, body: dict, user: dict, meta: dict, token: str,
                             agents: list[dict], emit) -> AsyncGenerator[str, None]:
        messages = list(body.get("messages") or [])
        if not messages:
            yield "Error: Conversation history is empty."
            return

        last_content = self._text_of(messages[-1].get("content"))
        history = [
            {"role": m["role"], "content": self._text_of(m.get("content"))}
            for m in messages[:-1] if m.get("role") in ("user", "assistant")
        ]
        chat_id = meta.get("chat_id")
        by_id = {a["id"]: a for a in agents}
        current_agent_id = self._get_sticky_agent(chat_id, by_id)

        # 5. Check pending clarification reply
        pending = pending_clarification(messages, agents)
        decision = parse_override(last_content, agents)

        if decision is not None and pending:
            # User replied to clarification with an override (e.g. "/code")
            if (decision.message or "").strip() == last_content.strip():
                messages = messages[:-2]  # bare "/code": send original question
            else:
                messages = messages[:-3] + [{**messages[-1], "content": decision.message}]
        elif decision is not None:
            if decision.message and isinstance(messages[-1].get("content"), str):
                messages[-1] = {**messages[-1], "content": decision.message}
        elif pending:
            option_ids, original_content = pending
            picked = resolve_pick(last_content, option_ids, agents)
            if picked:
                decision = Decision("dispatch", picked, "clarified")
                messages = messages[:-2]  # drop clarifying prompt and user's pick; send original
                last_content = self._text_of(messages[-1].get("content"))

        # 6. Call TypeSafe Jev router if no override / clarification resolved
        if decision is None:
            if not self.valves.TYPESAFE_API_KEY:
                general = next((a for a in agents if a["id"] == "general"), agents[0])
                decision = Decision("dispatch", general["id"], "default_no_key")
            else:
                current_label = by_id[current_agent_id]["label"] if current_agent_id else None
                state = build_state(last_content, history, current_label)
                questions = build_questions(agents)
                try:
                    answers = await asyncio.to_thread(
                        call_jev, self.valves.TYPESAFE_API_KEY, state, questions, self.valves.ROUTER_TIMEOUT_S
                    )
                    decision = decide(answers, agents, current_agent_id)
                except RouterUnavailable:
                    # Fallback on network/API failure to general or clarify
                    decision = Decision("clarify", None, "api_fallback", [a["id"] for a in agents[:2]])

        # 7. Handle Clarification
        if decision.action == "clarify":
            yield clarify_text(decision, agents)
            return

        # 8. Dispatch to Chosen Specialist Model
        chosen_agent = by_id[decision.agent_id]
        self._set_sticky_agent(chat_id, chosen_agent["id"])

        if emit and self.valves.SHOW_LABEL:
            label_text = f"Answered by: {chosen_agent['label']}"
            if decision.source == "medium" and decision.alternates:
                runner_up = by_id.get(decision.alternates[0])
                if runner_up and runner_up.get("override"):
                    label_text += f" · switch: {runner_up['override']}"
            await emit({"type": "status", "data": {"description": label_text, "done": True}})

        async for chunk in self._stream_response(chosen_agent, messages, token, emit):
            yield chunk

    async def _stream_response(self, agent: dict, messages: list, token: str, emit) -> AsyncGenerator[str, None]:
        """Stream completion from specialist model through Open WebUI."""
        import aiohttp

        url = f"{self.valves.OWUI_BASE_URL.rstrip('/')}/api/chat/completions"
        payload = {"model": agent["target"], "messages": messages, "stream": True}
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", HOP_HEADER: "1"}
        timeout = aiohttp.ClientTimeout(total=self.valves.DISPATCH_TIMEOUT_S)

        seen_sources = set()
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        detail = (await resp.text())[:300]
                        yield f"Error from {agent['label']} (HTTP {resp.status}): {detail}"
                        return
                    async for raw_line in resp.content:
                        line = raw_line.decode("utf-8", "ignore")
                        text, sources, done = parse_sse_line(line)
                        if emit and sources:
                            for src in sources:
                                key = json.dumps(src, sort_keys=True, default=str)[:300]
                                if key not in seen_sources:
                                    seen_sources.add(key)
                                    await emit({"type": "source", "data": src})
                        if text:
                            yield text
                        if done:
                            break
        except asyncio.TimeoutError:
            yield f"\n\nError: {agent['label']} did not respond within {int(self.valves.DISPATCH_TIMEOUT_S)}s."
        except Exception as e:
            yield f"\n\nError communicating with {agent['label']}: {e}"

    async def _complete_task(self, model: str, messages: list, token: str) -> str:
        """Non-streaming completion for background tasks (e.g. title generation)."""
        import aiohttp
        url = f"{self.valves.OWUI_BASE_URL.rstrip('/')}/api/chat/completions"
        payload = {"model": model, "messages": messages, "stream": False}
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", HOP_HEADER: "1"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return data["choices"][0]["message"]["content"]

    async def _get_accessible_model_ids(self, user: dict, token: str) -> set[str] | None:
        """Fetch models permitted for the current user from /api/models."""
        user_key = user.get("id") or token[-16:]
        now = time.monotonic()
        cached = self._access_cache.get(user_key)
        if cached and (now - cached[0]) < 60:
            return cached[1]

        try:
            import aiohttp
            url = f"{self.valves.OWUI_BASE_URL.rstrip('/')}/api/models"
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", HOP_HEADER: "1"}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        ids = {m.get("id") for m in (data.get("data") or []) if isinstance(m, dict)}
                        self._access_cache[user_key] = (now, ids)
                        return ids
        except Exception:
            pass
        return None

    def _get_sticky_agent(self, chat_id: str | None, by_id: dict) -> str | None:
        if not chat_id:
            return None
        cached = self._current.get(chat_id)
        if not cached:
            return None
        agent_id, ts = cached
        if time.monotonic() - ts > self.valves.STICKY_TTL_S or agent_id not in by_id:
            return None
        return agent_id

    def _set_sticky_agent(self, chat_id: str | None, agent_id: str):
        if chat_id:
            self._current[chat_id] = (agent_id, time.monotonic())

    @staticmethod
    def _text_of(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        return ""

    @staticmethod
    def _extract_token(request) -> str:
        if request is None:
            return ""
        auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        try:
            return request.cookies.get("token") or ""
        except Exception:
            return ""
