"""Real OpenAI client. Imported lazily so the rest of the package
runs offline."""

from __future__ import annotations

from typing import Any


class OpenAIChatClient:
    def __init__(self, model: str = "gpt-4o-mini") -> None:
        from openai import OpenAI
        self._client = OpenAI()
        self.model = model

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        choice = resp.choices[0]
        m = choice.message
        msg: dict[str, Any] = {"role": m.role, "content": m.content}
        tcs = getattr(m, "tool_calls", None) or []
        if tcs:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tcs
            ]
        return {"choices": [{"message": msg}]}
