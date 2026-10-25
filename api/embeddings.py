
import logging
import math

from django.conf import settings
from google.genai import types

from . import llm

logger = logging.getLogger(__name__)

BATCH_SIZE = 100          # Gemini accepts up to 100 texts per embed request


class EmbeddingError(Exception):
    """Embedding call failed or returned unusable vectors."""


def _normalize(vec):
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


def _embed_batch(batch, task_type):
    try:
        response = llm._get_client().models.embed_content(
            model=settings.EMBEDDING_MODEL,
            contents=batch,
            config=types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=settings.EMBEDDING_DIM,
            ),
        )
    except llm.LLMError as exc:                      # e.g. missing API key
        raise EmbeddingError(str(exc)) from exc
    except Exception as exc:                         # network, quota, auth, timeout ...
        raise EmbeddingError(f'Embedding call failed: {exc}') from exc

    vectors = [list(e.values) for e in (response.embeddings or [])]
    if len(vectors) != len(batch):
        raise EmbeddingError(f'Expected {len(batch)} embeddings, got {len(vectors)}')
    for vec in vectors:
        if len(vec) != settings.EMBEDDING_DIM:
            raise EmbeddingError(f'Expected {settings.EMBEDDING_DIM} dimensions, got {len(vec)}')
    return [_normalize(v) for v in vectors]


def embed_texts(texts, task_type='RETRIEVAL_DOCUMENT'):
    """Return one normalised vector per text (batched). Raises EmbeddingError."""
    texts = list(texts)
    vectors = []
    for i in range(0, len(texts), BATCH_SIZE):
        vectors.extend(_embed_batch(texts[i:i + BATCH_SIZE], task_type))
    return vectors