import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0002_enable_pgvector'),
    ]

    operations = [
        migrations.AlterField(
            model_name='candidate',
            name='status',
            field=models.CharField(choices=[('pending', 'Pending'), ('shortlisted', 'Shortlisted'), ('matched', 'Matched'), ('ranked', 'Ranked'), ('interview_scheduled', 'Interview scheduled'), ('rejected', 'Rejected')], default='pending', max_length=50),
        ),
        migrations.CreateModel(
            name='Application',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('score', models.FloatField(default=0)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('shortlisted', 'Shortlisted'), ('matched', 'Matched'), ('ranked', 'Ranked'), ('interview_scheduled', 'Interview scheduled'), ('rejected', 'Rejected')], default='pending', max_length=50)),
                ('reason', models.TextField(blank=True)),
                ('fallback_used', models.BooleanField(default=False)),
                ('interview_date', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('candidate', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='applications', to='api.candidate')),
                ('job', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='applications', to='api.job')),
            ],
            options={
                'ordering': ['-score', '-created_at'],
                'constraints': [models.UniqueConstraint(fields=('candidate', 'job'), name='unique_candidate_job_application')],
            },
        ),
    ]