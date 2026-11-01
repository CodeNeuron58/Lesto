"""Meta's models behind one client: Muse Spark (Meta Model API) and Muse Spark Open (Baseten).

Both speak OpenAI-style Chat Completions with JSON-schema structured output,
so one class covers them; only the base URL, key and model id differ.

Facts this builds on (verified from the official docs, see context/08):
- Meta Model API: POST https://api.meta.ai/v1/chat/completions, Bearer key
- Baseten:        POST https://inference.baseten.co/v1/chat/completions, Bearer key
- structured output: response_format {"type": "json_schema", ...}; the JSON
  string arrives at choices[0].message.content
- Meta reports no logprobs, so confidence comes from the model's own field
  plus code checks — never from token probabilities
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..decide import Decision
from ..observe.table import ActionTable


def load_env(path: Path | None = None) -> None:
    """Tiny .env loader: KEY=value lines into os.environ, never overriding what's set."""
    env = path or Path(".env")
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class ModelError(RuntimeError):
    pass


@dataclass
class ModelReply:
    content: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    model: str


class ChatClient:
    """One keep-alive HTTP client for a chat-completions endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.meta.ai/v1",
        timeout: float = 60.0,
    ) -> None:
        self.model = model
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        schema: dict | None = None,
        schema_name: str = "reply",
        effort: str | None = None,
        max_tokens: int = 512,
    ) -> ModelReply:
        body: dict = {
            "model": self.model,
            "messages": ([{"role": "system", "content": system}] if system else [])
            + [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema},
            }
        if effort is not None:
            body["reasoning_effort"] = effort

        started = time.perf_counter()
        response = self._http.post("/chat/completions", json=body)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code == 401:
            raise ModelError("API key rejected (401): check META_API_KEY in .env")
        if response.status_code == 404:
            raise ModelError(f"model {self.model!r} not found (404): check the model id")
        if response.status_code == 429:
            raise ModelError("rate limited (429): slow down or check your TPM limit")
        if response.status_code != 200:
            raise ModelError(f"HTTP {response.status_code}: {response.text[:300]}")
        data = response.json()
        choice = data["choices"][0]
        content = (choice["message"].get("content") or "").strip()
        if not content and choice.get("finish_reason") == "length":
            reasoning = (data.get("usage", {}).get("completion_tokens_details", {})
                         .get("reasoning_tokens", 0))
            raise ModelError(
                f"hit max_tokens: the model spent {reasoning} tokens reasoning and never "
                f"wrote the answer — raise max_tokens"
            )
        usage = data.get("usage", {})
        return ModelReply(
            content=choice["message"]["content"] or "",
            latency_ms=latency_ms,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=data.get("model", self.model),
        )

    def structured(self, prompt: str, schema: dict, *, name: str = "reply",
                   effort: str | None = None, max_tokens: int = 2048) -> tuple[dict, ModelReply]:
        """Ask for JSON matching the schema and parse it; raises on invalid JSON."""
        reply = self.chat(prompt, schema=schema, schema_name=name, effort=effort,
                          max_tokens=max_tokens)
        try:
            return json.loads(reply.content), reply
        except json.JSONDecodeError as exc:
            raise ModelError(f"invalid JSON from {reply.model}: {reply.content[:200]}") from exc

    def close(self) -> None:
        self._http.close()


def muse_spark() -> ChatClient:
    """The Meta Model API decision/frontier client; key from .env (META_API_KEY)."""
    load_env()
    key = os.environ.get("META_API_KEY")
    if not key:
        raise ModelError("META_API_KEY is not set; put it in .env (never in code)")
    return ChatClient(api_key=key, model=os.environ.get("MUSE_MODEL", "muse-spark-1.3"))


def muse_spark_open() -> ChatClient:
    """The open-weights client on Baseten; key from .env (BASETEN_API_KEY)."""
    load_env()
    key = os.environ.get("BASETEN_API_KEY")
    if not key:
        raise ModelError("BASETEN_API_KEY is not set; request Baseten credits first")
    return ChatClient(
        api_key=key,
        model=os.environ.get("MUSE_OPEN_MODEL", "meta-models/muse-spark-open-1026"),
        base_url="https://inference.baseten.co/v1",
    )


# The decision schema: one request answers all four questions (and batches rows).
def decision_schema(allow_wait: bool = True, allow_navigate: bool = True) -> dict:
    """Desktop deciders get no 'wait' (timing is the driver's job) and no
    'navigate' (desktop apps have no address bar). Browser deciders get both."""
    actions = ["click", "type", "select", "press"] + (
        ["wait"] if allow_wait else []
    ) + (["navigate", "download"] if allow_navigate else []) + ["save_as", "done", "answer"]
    schema = json.loads(json.dumps(DECISION_SCHEMA))
    schema["properties"]["action"]["enum"] = actions
    return schema


DECISION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "action": {"type": "string",
                   "enum": ["click", "type", "select", "press", "save_as", "wait",
                            "done", "answer"]},
        "row": {"type": "integer"},
        "values": {"type": "array", "items": {"type": "string"}},
        "answer": {"type": "string"},
        "last_step_ok": {"type": "boolean"},
        "subgoal_done": {"type": "boolean"},
        "confidence": {"type": "number"},
        "note": {"type": "string"},
    },
    "required": ["action", "row", "values", "last_step_ok", "subgoal_done", "confidence"],
}


def decision_prompt(goal: str, subgoal: str, table: ActionTable,
                    history, last_result: str | None = None,
                    progress: str | None = None) -> str:
    """Only the goal, subgoal, action table and last two actions go to the model."""
    recent = "\n".join(
        f"- {d.action} row {d.row} {list(d.values)}" for d in list(history)[-2:]
    ) or "(none yet)"
    last = last_result or "(the last step has not happened yet)"
    progress_line = progress or "(counted from completed actions)"
    return (
        f"Goal: {goal}\n"
        f"Current subgoal: {subgoal}\n\n"
        f"Action table (numbered rows; act on rows by their number):\n"
        f"{table.render()}\n\n"
        f"Last two actions taken:\n{recent}\n"
        f"Result of the last action: {last}\n\n"
        "Reply with JSON only: pick ONE next action. 'click'/'type'/'select' need a row "
        "number from the table; 'type' fills 'values' with the text; 'press' puts the key "
        "in 'values' (e.g. [\"Enter\"]); 'navigate' puts the full URL in 'values' and goes "
        "there directly — IMPORTANT: you cannot reach the browser's address bar with "
        "keys like ctrl+l; it is not part of the page. To open a different URL, always "
        "use 'navigate'; 'download' fetches a URL to disk — 'values' is "
        "[url, destination-path] — use it when a file should be downloaded rather than "
        "viewed; 'save_as' is for saving the current document: put "
        "the complete destination path in 'values' (e.g. [\"C:\\folder\\file.txt\"]) and "
        "the driver opens the menu and dialog itself; 'wait' ONLY while an action is "
        "clearly still in progress; 'done' when the subgoal is complete; 'answer' puts "
        "the final answer in 'answer'. Judge whether the last step worked (last_step_ok) "
        "and whether the subgoal is done (subgoal_done). Set confidence between 0 and 1. "
        "If the table already shows what you need for the next action, act — do not wait."
    )


class MuseDecider:
    """The real decision model: Muse Spark picks the row, via one structured call.

    Implements the Decider protocol, so it drops into the same slot the
    scripted stand-ins used. The HTTP client is synchronous, so the call runs
    in a worker thread and the loop stays responsive.
    """

    def __init__(self, client: ChatClient | None = None, effort: str = "minimal",
                 max_tokens: int = 2048, allow_wait: bool = True,
                 allow_navigate: bool = True) -> None:
        self.client = client or muse_spark()
        self.effort = effort
        self.max_tokens = max_tokens
        self.allow_wait = allow_wait
        self.allow_navigate = allow_navigate

    async def decide(self, goal, subgoal, table: ActionTable, history,
                     last_result: str | None = None, progress: str | None = None):
        import asyncio

        prompt = decision_prompt(goal, subgoal, table, history, last_result, progress)
        parsed, reply = await asyncio.to_thread(
            self.client.structured, prompt, decision_schema(self.allow_wait, self.allow_navigate),
            name="decision", effort=self.effort, max_tokens=self.max_tokens,
        )
        action = parsed.get("action", "done")
        row = parsed.get("row")
        if action in ("click", "type", "select"):
            if not isinstance(row, int) or table.by_index(row) is None:
                return Decision(action="done", subgoal_done=False, confidence=0.0,
                                note=f"model picked missing row {row!r}")
        values = tuple(str(v) for v in parsed.get("values") or [])
        try:
            confidence = max(0.0, min(1.0, float(parsed.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        return Decision(
            action=action,
            row=row,
            values=values,
            answer=parsed.get("answer"),
            last_step_ok=parsed.get("last_step_ok"),
            subgoal_done=parsed.get("subgoal_done"),
            confidence=confidence,
            note=f"{parsed.get('note') or ''} · {reply.model} · {reply.latency_ms} ms".strip(" ·"),
        )
