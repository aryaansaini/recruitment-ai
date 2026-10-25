"""Day 6 tests: embeddings, pgvector storage, semantic search. Gemini is never called."""
import math
import re
import zlib
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import override_settings
from rest_framework.test import APITestCase

from .embeddings import BATCH_SIZE, EmbeddingError, embed_texts
from .models import ResumeChunk
from .search import search_candidates
from .services import embed_candidate_chunks, rebuild_resume_chunks
from .tests import SAMPLE_RESUME, make_candidate

DIM = 768


def fake_vector(text):
    """Deterministic bag-of-words embedding: texts sharing words get high cosine similarity."""
    vec = [0.0] * DIM
    for token in re.findall(r'[a-z]+', text.lower()):
        vec[zlib.crc32(token.encode()) % DIM] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def fake_embed_batch(batch, task_type):
    return [fake_vector(t) for t in batch]


class FakeResponse:
    def __init__(self, vectors):
        self.embeddings = [mock.Mock(values=v) for v in vectors]


def fake_client(dim=DIM, scale=2.0, count=None):
    """Fake google-genai client whose embed_content returns un-normalised vectors."""
    client = mock.Mock()

    def embed_content(model, contents, config):
        n = len(contents) if count is None else count
        return FakeResponse([[scale] * dim for _ in range(n)])

    client.models.embed_content.side_effect = embed_content
    return client


class Base(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('recruiter', password='pass12345')

    def login(self):
        self.client.force_authenticate(self.user)

    def patch_embedder(self):
        p = mock.patch('api.embeddings._embed_batch', side_effect=fake_embed_batch)
        p.start()
        self.addCleanup(p.stop)

    def candidate_with_chunks(self, **kw):
        c = make_candidate(**kw)
        rebuild_resume_chunks(c)
        return c


class EmbedTextsTests(Base):
    def test_batches_of_100(self):
        client = fake_client()
        with mock.patch('api.llm._get_client', return_value=client):
            vectors = embed_texts([f'text {i}' for i in range(250)])
        self.assertEqual(len(vectors), 250)
        sizes = [len(call.kwargs['contents']) for call in client.models.embed_content.call_args_list]
        self.assertEqual(sizes, [BATCH_SIZE, BATCH_SIZE, 50])

    def test_vectors_are_normalised_even_if_api_returns_raw_values(self):
        with mock.patch('api.llm._get_client', return_value=fake_client(scale=7.0)):
            vec = embed_texts(['hello'])[0]
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in vec)), 1.0, places=6)

    def test_task_type_and_dimension_are_sent(self):
        client = fake_client()
        with mock.patch('api.llm._get_client', return_value=client):
            embed_texts(['q'], task_type='RETRIEVAL_QUERY')
        config = client.models.embed_content.call_args.kwargs['config']
        self.assertEqual(config.task_type, 'RETRIEVAL_QUERY')
        self.assertEqual(config.output_dimensionality, DIM)

    def test_empty_input_makes_no_call(self):
        client = fake_client()
        with mock.patch('api.llm._get_client', return_value=client):
            self.assertEqual(embed_texts([]), [])
        client.models.embed_content.assert_not_called()

    def test_api_exception_becomes_embedding_error(self):
        client = mock.Mock()
        client.models.embed_content.side_effect = RuntimeError('429 quota')
        with mock.patch('api.llm._get_client', return_value=client):
            with self.assertRaises(EmbeddingError):
                embed_texts(['x'])

    def test_wrong_dimension_is_rejected(self):
        with mock.patch('api.llm._get_client', return_value=fake_client(dim=10)):
            with self.assertRaises(EmbeddingError):
                embed_texts(['x'])

    def test_wrong_vector_count_is_rejected(self):
        with mock.patch('api.llm._get_client', return_value=fake_client(count=1)):
            with self.assertRaises(EmbeddingError):
                embed_texts(['a', 'b'])

    def test_missing_api_key_becomes_embedding_error(self):
        import api.llm as llm
        with mock.patch.object(llm, '_client', None), override_settings(GEMINI_API_KEY=''):
            with self.assertRaises(EmbeddingError):
                embed_texts(['x'])


class StoreEmbeddingsTests(Base):
    def test_chunks_get_embeddings(self):
        self.patch_embedder()
        c = self.candidate_with_chunks(resume_text=SAMPLE_RESUME)
        n = embed_candidate_chunks(c)
        self.assertEqual(n, c.chunks.count())
        chunk = c.chunks.first()
        self.assertEqual(len(list(chunk.embedding)), DIM)

    def test_only_missing_chunks_are_embedded(self):
        self.patch_embedder()
        c = self.candidate_with_chunks(resume_text=SAMPLE_RESUME)
        embed_candidate_chunks(c)
        self.assertEqual(embed_candidate_chunks(c), 0)
        self.assertGreater(embed_candidate_chunks(c, only_missing=False), 0)

    def test_failure_does_not_raise_and_leaves_chunks_unembedded(self):
        with mock.patch('api.embeddings._embed_batch', side_effect=EmbeddingError('down')):
            c = self.candidate_with_chunks(resume_text=SAMPLE_RESUME)
            self.assertEqual(embed_candidate_chunks(c), 0)
        self.assertEqual(c.chunks.filter(embedding__isnull=True).count(), c.chunks.count())

    def test_embed_text_property_has_section_label(self):
        c = self.candidate_with_chunks(resume_text='Name\nSKILLS\nPython')
        self.assertEqual(c.chunks.first().embed_text, 'Skills: Python')

    def test_add_candidate_endpoint_embeds_chunks(self):
        self.login()
        self.patch_embedder()
        r = self.client.post('/api/add-candidate/', {
            'name': 'Rahul', 'email': 'r@x.com', 'phone': '123', 'skills': 'Python',
            'experience': 3, 'resume_text': SAMPLE_RESUME}, format='json')
        self.assertEqual(r.status_code, 201)
        self.assertGreater(r.data['embedded_chunks'], 0)
        self.assertEqual(r.data['embedded_chunks'], r.data['chunks'])

    def test_add_candidate_still_succeeds_when_embedding_fails(self):
        self.login()
        with mock.patch('api.embeddings._embed_batch', side_effect=EmbeddingError('down')):
            r = self.client.post('/api/add-candidate/', {
                'name': 'Rahul', 'email': 'r@x.com', 'phone': '123', 'skills': 'Python',
                'experience': 3, 'resume_text': SAMPLE_RESUME}, format='json')
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data['embedded_chunks'], 0)
        self.assertGreater(r.data['chunks'], 0)


DJANGO_DEV = 'Name\nSKILLS\nPython, Django, REST APIs\nEXPERIENCE\nBuilt REST APIs with Django and PostgreSQL\nBuilt Django REST framework services'
ACCOUNTANT = 'Name\nSKILLS\nExcel, Tally, Accounting\nEXPERIENCE\nPrepared balance sheets and tax returns in Excel and Tally'


class SearchTests(Base):
    def setUp(self):
        super().setUp()
        self.patch_embedder()
        self.dev = self.candidate_with_chunks(name='Dev', email='d@x.com', resume_text=DJANGO_DEV)
        self.acc = self.candidate_with_chunks(name='Accountant', email='a@x.com', resume_text=ACCOUNTANT)
        for c in (self.dev, self.acc):
            embed_candidate_chunks(c)

    def test_relevant_candidate_ranks_first_with_evidence(self):
        results = search_candidates('Django REST APIs developer')
        self.assertEqual(results[0]['candidate'].id, self.dev.id)
        self.assertGreater(results[0]['similarity'], results[-1]['similarity'])
        self.assertTrue(any('Django' in e['text'] for e in results[0]['evidence']))

    def test_other_query_ranks_other_candidate_first(self):
        self.assertEqual(search_candidates('Excel Tally accounting')[0]['candidate'].id, self.acc.id)

    def test_one_entry_per_candidate_and_similarity_in_range(self):
        results = search_candidates('Django')
        ids = [r['candidate'].id for r in results]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(-1e-6 <= r['similarity'] <= 1 + 1e-6 for r in results))

    def test_limit_is_respected(self):
        self.assertEqual(len(search_candidates('Django', limit=1)), 1)

    def test_chunks_without_embedding_are_ignored(self):
        c = self.candidate_with_chunks(name='Fresh', email='f@x.com', resume_text=DJANGO_DEV)   # never embedded
        ids = [r['candidate'].id for r in search_candidates('Django', limit=10)]
        self.assertNotIn(c.id, ids)

    def test_endpoint_requires_login(self):
        self.assertEqual(self.client.get('/api/search/?q=django').status_code, 401)

    def test_endpoint_validates_input(self):
        self.login()
        self.assertEqual(self.client.get('/api/search/').status_code, 400)
        self.assertEqual(self.client.get('/api/search/?q=a').status_code, 400)
        self.assertEqual(self.client.get('/api/search/?q=django&limit=21').status_code, 400)
        self.assertEqual(self.client.get('/api/search/?q=' + 'x' * 301).status_code, 400)

    def test_endpoint_returns_ranked_candidates(self):
        self.login()
        r = self.client.get('/api/search/?q=Django REST APIs&limit=2')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data[0]['name'], 'Dev')
        self.assertIn('evidence', r.data[0])
        self.assertLessEqual(len(r.data), 2)

    def test_endpoint_returns_503_when_embedding_service_fails(self):
        self.login()
        with mock.patch('api.embeddings._embed_batch', side_effect=EmbeddingError('down')):
            r = self.client.get('/api/search/?q=django')
        self.assertEqual(r.status_code, 503)

    def test_search_is_rate_limited(self):
        self.login()
        codes = [self.client.get('/api/search/?q=django').status_code for _ in range(33)]
        self.assertIn(429, codes)


class BackfillCommandTests(Base):
    def test_embeds_only_missing_chunks(self):
        self.patch_embedder()
        done = self.candidate_with_chunks(name='Done', resume_text=SAMPLE_RESUME)
        embed_candidate_chunks(done)
        todo = self.candidate_with_chunks(name='Todo', email='t@x.com', resume_text=SAMPLE_RESUME)
        out = StringIO()
        call_command('embed_chunks', stdout=out)
        self.assertEqual(todo.chunks.filter(embedding__isnull=True).count(), 0)
        self.assertIn(f'{todo.chunks.count()} chunks embedded', out.getvalue())

    def test_nothing_to_do_message(self):
        self.patch_embedder()
        out = StringIO()
        call_command('embed_chunks', stdout=out)
        self.assertIn('Nothing to embed', out.getvalue())

    def test_failure_raises_command_error(self):
        self.candidate_with_chunks(resume_text=SAMPLE_RESUME)
        with mock.patch('api.embeddings._embed_batch', side_effect=EmbeddingError('quota')):
            with self.assertRaises(CommandError):
                call_command('embed_chunks', stdout=StringIO())

    def test_all_flag_reembeds_everything(self):
        self.patch_embedder()
        c = self.candidate_with_chunks(resume_text=SAMPLE_RESUME)
        embed_candidate_chunks(c)
        out = StringIO()
        call_command('embed_chunks', '--all', stdout=out)
        self.assertIn(f'{c.chunks.count()} chunks embedded', out.getvalue())