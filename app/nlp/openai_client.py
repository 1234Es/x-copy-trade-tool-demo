"""Thin wrapper over the OpenAI API's structured-outputs contract.

Verified against developers.openai.com/api/docs/guides/structured-outputs
(fetched 2026-07-13): Chat Completions uses
`response_format={"type": "json_schema", "json_schema": {...}}` with
`strict: true` inside the json_schema block. Low temperature is used
throughout since this is an extraction/classification task, not a
creative one.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from openai import APIError, APITimeoutError, OpenAI, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


class MalformedOpenAIResponseError(Exception):
    """Raised when the API returns something that isn't valid JSON matching
    the requested schema shape -- the caller must treat this as a rejection,
    never fall back to guessing."""


@dataclass(frozen=True)
class StructuredCompletionResult:
    request_id: str | None
    parsed: dict[str, Any]
    raw_content: str


class OpenAIClient:
    def __init__(self, api_key: str, model: str, temperature: float = 0.1):
        self._client = OpenAI(api_key=api_key)
        self.model = model
        self.temperature = temperature

    @retry(
        retry=retry_if_exception_type((RateLimitError, APITimeoutError)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def structured_completion(
        self,
        system_prompt: str,
        user_content: str,
        json_schema: dict[str, Any],
    ) -> StructuredCompletionResult:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_schema", "json_schema": json_schema},
            )
        except APIError as exc:
            raise MalformedOpenAIResponseError(f"OpenAI API error: {exc}") from exc

        choice = response.choices[0] if response.choices else None
        if choice is None or choice.message is None or choice.message.content is None:
            raise MalformedOpenAIResponseError("OpenAI response had no message content.")

        if getattr(choice, "finish_reason", None) == "length":
            raise MalformedOpenAIResponseError("OpenAI response was truncated (finish_reason=length).")

        raw_content = choice.message.content
        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            raise MalformedOpenAIResponseError(f"OpenAI response was not valid JSON: {exc}") from exc

        if not isinstance(parsed, dict):
            raise MalformedOpenAIResponseError("OpenAI response JSON was not an object.")

        return StructuredCompletionResult(request_id=response.id, parsed=parsed, raw_content=raw_content)
