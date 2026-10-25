import pgvector.django.vector
from django.db import migrations


def create_hnsw_index(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(
            'CREATE INDEX IF NOT EXISTS chunk_embedding_hnsw ON api_resumechunk '
            'USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)'
        )


def drop_hnsw_index(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('DROP INDEX IF EXISTS chunk_embedding_hnsw')


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0004_resume_chunk'),
    ]

    operations = [
        migrations.AddField(
            model_name='resumechunk',
            name='embedding',
            field=pgvector.django.vector.VectorField(blank=True, dimensions=768, null=True),
        ),
        migrations.RunPython(create_hnsw_index, drop_hnsw_index),
    ]