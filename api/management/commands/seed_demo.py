from django.core.management.base import BaseCommand

from api.models import Application, Candidate, Job

JOBS = [
    ('Backend Developer', 'Python, Django, SQL', 2, 'Build and maintain REST APIs.'),
    ('Data Analyst', 'SQL, Excel, Python', 1, 'Analyse hiring and sales data.'),
    ('Frontend Developer', 'JavaScript, React, CSS', 2, 'Build responsive web UIs.'),
]

CANDIDATES = [
    ('Rahul Sharma', 'rahul@example.com', '9876500001', 'Python, Django, SQL', 3,
     'Backend developer with 3 years of Django and PostgreSQL experience.'),
    ('Priya Verma', 'priya@example.com', '9876500002', 'SQL, Excel, Python', 2,
     'Data analyst experienced with dashboards and reporting.'),
    ('Aman Gupta', 'aman@example.com', '9876500003', 'JavaScript, React, CSS', 4,
     'Frontend engineer who built several React apps.'),
    ('Neha Singh', 'neha@example.com', '9876500004', 'Python, Flask, Docker', 1,
     'Junior developer with Flask projects and Docker basics.'),
    ('Karan Mehta', 'karan@example.com', '9876500005', 'Excel, Data Entry', 0,
     'Fresher with data entry experience.'),
]


class Command(BaseCommand):
    help = 'Create fake demo data (safe to run multiple times)'

    def handle(self, *args, **kwargs):
        jobs = {}
        for title, skills, exp, desc in JOBS:
            jobs[title], _ = Job.objects.get_or_create(
                title=title,
                defaults=dict(required_skills=skills, min_experience=exp, description=desc),
            )
        cands = {}
        for name, email, phone, skills, exp, text in CANDIDATES:
            cands[name], _ = Candidate.objects.get_or_create(
                email=email,
                defaults=dict(name=name, phone=phone, skills=skills,
                              experience=exp, resume_text=text),
            )

        Application.objects.get_or_create(
            candidate=cands['Rahul Sharma'], job=jobs['Backend Developer'],
            defaults={'status': 'interview_scheduled'})
        Application.objects.get_or_create(
            candidate=cands['Karan Mehta'], job=jobs['Backend Developer'],
            defaults={'status': 'rejected'})

        self.stdout.write(self.style.SUCCESS('Demo data ready'))