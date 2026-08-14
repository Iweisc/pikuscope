"""OpenAI-compatible chat completions client with retries and JSON extraction."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# Global cap on in-flight LLM requests across all threads/clients in this process.
# Oversubscribing the endpoint queues server-side and slows every request down.
_GLOBAL_SEMAPHORE: threading.Semaphore | None = None
_SEM_LOCK = threading.Lock()


def _global_semaphore() -> threading.Semaphore:
    global _GLOBAL_SEMAPHORE
    with _SEM_LOCK:
        if _GLOBAL_SEMAPHORE is None:
            n = int(os.environ.get("PIKUSCOPE_MAX_CONCURRENCY", "10"))
            _GLOBAL_SEMAPHORE = threading.Semaphore(max(1, n))
        return _GLOBAL_SEMAPHORE


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader; does not override existing env vars."""
    p = Path(path)
    if not p.exists():
        # Also try alongside the repo root (package parent).
        p = Path(__file__).resolve().parent.parent / ".env"
        if not p.exists():
            return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    calls: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.calls += other.calls


@dataclass
class LLMClient:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    reasoning_effort: str = "xhigh"
    timeout: float = 900.0
    max_retries: int = 5
    usage: Usage = field(default_factory=Usage)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def from_env(cls) -> "LLMClient":
        load_dotenv()
        base = os.environ.get("PIKUSCOPE_BASE_URL", "").rstrip("/")
        if base and not base.endswith("/v1"):
            base = base + "/v1"
        return cls(
            base_url=base,
            api_key=os.environ.get("PIKUSCOPE_API_KEY", ""),
            model=os.environ.get("PIKUSCOPE_MODEL", "gpt-5.6-sol"),
            reasoning_effort=os.environ.get("PIKUSCOPE_REASONING_EFFORT", "xhigh"),
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "reasoning_effort": reasoning_effort or self.reasoning_effort,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                with _global_semaphore():
                    with httpx.Client(timeout=self.timeout) as client:
                        resp = client.post(
                            f"{self.base_url}/chat/completions",
                            headers={"Authorization": f"Bearer {self.api_key}"},
                            json=payload,
                        )
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"retryable status {resp.status_code}: {resp.text[:300]}")
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage") or {}
                with self._lock:
                    self.usage.add(
                        Usage(
                            prompt_tokens=usage.get("prompt_tokens", 0),
                            completion_tokens=usage.get("completion_tokens", 0),
                            reasoning_tokens=(usage.get("completion_tokens_details") or {}).get(
                                "reasoning_tokens", 0
                            ),
                            calls=1,
                        )
                    )
                content = data["choices"][0]["message"].get("content") or ""
                if not content.strip():
                    raise RuntimeError("empty completion content")
                return content
            except Exception as e:  # noqa: BLE001 — retry loop
                last_err = e
                time.sleep(min(2**attempt * 2, 30))
        raise RuntimeError(f"LLM call failed after {self.max_retries} attempts: {last_err}")

    def chat_json(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        """Chat and parse a JSON object/array from the response, retrying on parse failure."""
        for attempt in range(3):
            text = self.chat(messages, **kwargs)
            try:
                return extract_json(text)
            except ValueError:
                if attempt == 2:
                    raise
                messages = messages + [
                    {"role": "assistant", "content": text[-4000:]},
                    {
                        "role": "user",
                        "content": "Your response could not be parsed as JSON. Respond again with ONLY the valid JSON, no prose, no markdown fences.",
                    },
                ]
        raise ValueError("unreachable")

    def chat_raw(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Single request with a fully custom payload; returns the parsed response body."""
        payload = {"model": self.model, "reasoning_effort": self.reasoning_effort, **payload}
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                with _global_semaphore():
                    with httpx.Client(timeout=self.timeout) as client:
                        resp = client.post(
                            f"{self.base_url}/chat/completions",
                            headers={"Authorization": f"Bearer {self.api_key}"},
                            json=payload,
                        )
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"retryable status {resp.status_code}: {resp.text[:300]}")
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage") or {}
                with self._lock:
                    self.usage.add(
                        Usage(
                            prompt_tokens=usage.get("prompt_tokens", 0),
                            completion_tokens=usage.get("completion_tokens", 0),
                            reasoning_tokens=(usage.get("completion_tokens_details") or {}).get(
                                "reasoning_tokens", 0
                            ),
                            calls=1,
                        )
                    )
                return data
            except Exception as e:  # noqa: BLE001 — retry loop
                last_err = e
                time.sleep(min(2**attempt * 2, 30))
        raise RuntimeError(f"LLM raw call failed after {self.max_retries} attempts: {last_err}")

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_handler: Any,
        *,
        max_rounds: int = 16,
        reasoning_effort: str | None = None,
    ) -> str:
        """Agentic loop: let the model call tools until it produces a final text answer.

        tool_handler(name: str, args: dict) -> str
        """
        msgs = list(messages)
        for _ in range(max_rounds):
            data = self.chat_raw(
                {
                    "messages": msgs,
                    "tools": tools,
                    "reasoning_effort": reasoning_effort or self.reasoning_effort,
                }
            )
            msg = data["choices"][0]["message"]
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                content = msg.get("content") or ""
                if content.strip():
                    return content
                # Model returned nothing — nudge once for a final answer.
                msgs.append({"role": "user", "content": "Provide your final answer now."})
                continue
            msgs.append(
                {"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls}
            )
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                try:
                    result = str(tool_handler(name, args))
                except Exception as e:  # noqa: BLE001 — surface tool errors to the model
                    result = f"tool error: {e}"
                if len(result) > 30_000:
                    result = result[:30_000] + "\n... (truncated)"
                msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": result})
        # Out of rounds: force a final answer without tools.
        msgs.append(
            {
                "role": "user",
                "content": "Tool budget exhausted. Provide your final answer NOW using what you already know.",
            }
        )
        return self.chat(msgs, reasoning_effort=reasoning_effort)


def extract_json(text: str) -> Any:
    """Extract the first JSON object or array from text (handles ```json fences)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # Fast path
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Scan for first balanced object/array
    for start_ch, end_ch in (("{", "}"), ("[", "]")):
        start = text.find(start_ch)
        if start == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == start_ch:
                depth += 1
            elif ch == end_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError(f"no parseable JSON in response: {text[:200]!r}")
