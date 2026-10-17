from __future__ import annotations

import json
import re

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

_PII_PATTERNS = (
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{8,}\d)(?!\w)"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
)
_INJECTION_PATTERNS = (
    re.compile(r"\bignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|rules?)\b", re.I),
    re.compile(r"\b(?:reveal|print|show|repeat|expose)\s+(?:the\s+)?(?:system|developer|hidden)\s+(?:prompt|instructions?)\b", re.I),
    re.compile(r"\b(?:bypass|disable|override)\s+(?:all\s+)?(?:guardrails?|safety|instructions?)\b", re.I),
    re.compile(r"\bact\s+as\s+(?:an?\s+)?(?:unrestricted|unfiltered|different)\b", re.I),
    re.compile(r"<\|(?:system|assistant|developer)|\[INST\]", re.I),
)

SAFE_REFUSAL = (
    "Request blocked by FreightMesh input safety. Personal data and instruction-override "
    "attempts are not accepted. Submit a supported logistics request using approved fields."
)


def _refusal_response() -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part(text=SAFE_REFUSAL)],
        ),
        turnComplete=True,
    )


def inspect_untrusted_text(text: str) -> str | None:
    """Return a generic rejection category without retaining or returning the supplied text."""
    if any(pattern.search(text) for pattern in _PII_PATTERNS):
        return "personal_data"
    if any(pattern.search(text) for pattern in _INJECTION_PATTERNS):
        return "instruction_override"
    return None


def guard_untrusted_input(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> LlmResponse | None:
    del llm_request
    content = callback_context.user_content
    if content is None:
        return None

    text_parts: list[str] = []
    for part in content.parts or []:
        if part.inline_data is not None or part.file_data is not None:
            return _refusal_response()
        if part.text:
            text_parts.append(part.text)

    if inspect_untrusted_text("\n".join(text_parts)) is None:
        return None
    return _refusal_response()


def guard_model_output(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> LlmResponse | None:
    del callback_context
    content = llm_response.content
    if content is None:
        return None

    output_parts: list[str] = []
    for part in content.parts or []:
        if part.text:
            output_parts.append(part.text)
        if part.function_call is not None:
            output_parts.append(json.dumps(part.function_call.args or {}))

    if inspect_untrusted_text("\n".join(output_parts)) == "personal_data":
        return _refusal_response()
    return None
