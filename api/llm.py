"""
Thin wrapper around the Gemini API (google-genai SDK) that always returns
STRUCTURED output (validated pydantic objects) or raises LLMError.

No regex / string-splitting of model text, and no silent failures:
callers decide what to do on LLMError (e.g. use a fallback AND flag it).
"""
import logging

from django.conf import settings
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Gemini call failed or returned something unusable."""


def _clamp_0_100(value: int) -> int:
    return max(0, min(100, int(value)))


class ScreeningResult(BaseModel):
    score: int = Field(description='Overall fit score from 0 to 100')
    reason: str = Field(description='One or two sentences explaining the score, citing resume evidence')

    _clamp = field_validator('score')(_clamp_0_100)


class MatchResult(BaseModel):
    is_match: bool = Field(description='True if the candidate meets the job requirements')
    match_percent: int = Field(description='How well skills/experience match, 0 to 100')
    reason: str = Field(description='One sentence explaining the decision')

    _clamp = field_validator('match_percent')(_clamp_0_100)


_client = None


def _get_client():
    global _client
    if _client is None:
        if not settings.GEMINI_API_KEY:
            raise LLMError('GEMINI_API_KEY is not set')
        _client = genai.Client(
            api_key=settings.GEMINI_API_KEY,
            http_options=types.HttpOptions(timeout=30_000),  # 30 s, in milliseconds
        )
    return _client


def generate_structured(prompt: str, schema: type[BaseModel], *, temperature: float = 0.2):
    """Call Gemini and return an instance of `schema`, or raise LLMError."""
    try:
        response = _get_client().models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type='application/json',
                response_schema=schema,
                temperature=temperature,
            ),
        )
        parsed = getattr(response, 'parsed', None)
        if isinstance(parsed, schema):
            return parsed
        return schema.model_validate_json(response.text)
    except LLMError:
        raise
    except (ValidationError, ValueError) as exc:
        raise LLMError(f'Invalid structured output from Gemini: {exc}') from exc
    except Exception as exc:  # network, quota, auth, timeout ...
        raise LLMError(f'Gemini call failed: {exc}') from exc