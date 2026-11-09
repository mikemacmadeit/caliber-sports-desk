from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeVar

from pydantic import BaseModel

from spe.config import settings

T = TypeVar("T", bound=BaseModel)


class IntelligenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int

    @property
    def cost_usd(self) -> float:
        return (
            self.input_tokens * settings.openai_input_usd_per_mtok
            + self.output_tokens * settings.openai_output_usd_per_mtok
        ) / 1_000_000


class LanguageModel(Protocol):
    def complete(self, *, instructions: str, user: str, schema: type[T]) -> tuple[T, Usage]: ...


class OpenAIResponses:
    """Official Responses API. The key stays in the process environment."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = settings.openai_api_key if api_key is None else api_key
        self.model = settings.openai_model if model is None else model

    def complete(self, *, instructions: str, user: str, schema: type[T]) -> tuple[T, Usage]:
        if not self.api_key:
            raise IntelligenceError("OPENAI_API_KEY is not configured")
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)
        response = client.responses.parse(
            model=self.model,
            instructions=instructions,
            input=user,
            text_format=schema,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise IntelligenceError("OpenAI returned no structured output")
        usage = response.usage
        return parsed, Usage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )
