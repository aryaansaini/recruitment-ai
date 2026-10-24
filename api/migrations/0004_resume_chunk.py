import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0003_application_and_status_choices'),
    ]

    operations = [
        migrations.CreateModel(
            name='ResumeChunk',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('section', models.CharField(max_length=30)),
                ('chunk_index', models.PositiveIntegerField()),
                ('text', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('candidate', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='chunks', to='api.candidate')),
            ],
            options={
                'ordering': ['candidate_id', 'chunk_index'],
                'constraints': [models.UniqueConstraint(fields=('candidate', 'chunk_index'), name='unique_chunk_index_per_candidate')],
            },
        ),
    ]