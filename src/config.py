"""Settings read from environment variables.

Locally they come from the .env file (loaded by python-dotenv). On Streamlit
Community Cloud, top-level entries in the app's Secrets are exposed as
environment variables too, so the same code works in both places.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()  # does nothing if there is no .env file; never overrides real environment variables

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_REASONING_EFFORT = "low"
PLACEHOLDER_KEYS = {"", "your-openai-api-key-here"}


@dataclass(frozen=True)
class LLMSettings:
    api_key: str
    model: str
    reasoning_effort: str

    def __repr__(self) -> str:  # never print the key, even by accident
        return f"LLMSettings(model={self.model!r}, reasoning_effort={self.reasoning_effort!r}, api_key=***)"


def get_llm_settings() -> LLMSettings:
    """Read LLM settings. A missing key is returned as an empty string (checked when the client is created)."""
    return LLMSettings(
        api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        model=os.getenv("OPENAI_MODEL", "").strip() or DEFAULT_MODEL,
        reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "").strip() or DEFAULT_REASONING_EFFORT,
    )