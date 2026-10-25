
import math
from collections import OrderedDict

from django.db import connection
from pgvector.django import CosineDistance

from .embeddings import embed_texts
from .models import ResumeChunk


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def search_chunks(query, limit=20):
    """Return [(chunk, similarity 0..1)] best first. Raises EmbeddingError."""
    qvec = embed_texts([query], task_type='RETRIEVAL_QUERY')[0]
    chunks = ResumeChunk.objects.filter(embedding__isnull=False).select_related('candidate')

    if connection.vendor == 'postgresql':
        rows = chunks.annotate(distance=CosineDistance('embedding', qvec)).order_by('distance')[:limit]
        return [(c, 1.0 - float(c.distance)) for c in rows]

    scored = [(c, _cosine(list(c.embedding), qvec)) for c in chunks]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


def search_candidates(query, limit=5, evidence_per_candidate=2):
    """Best matching candidates for a free-text query, each with the resume lines that matched."""
    hits = search_chunks(query, limit=min(60, limit * 8))
    grouped = OrderedDict()
    for chunk, sim in hits:                          # hits are sorted: first chunk = best chunk
        entry = grouped.setdefault(chunk.candidate_id, {
            'candidate': chunk.candidate, 'similarity': sim, 'evidence': [],
        })
        if len(entry['evidence']) < evidence_per_candidate:
            entry['evidence'].append({'section': chunk.section, 'text': chunk.text, 'similarity': round(sim, 3)})
    return list(grouped.values())[:limit]