"""
Fill the shared read-only demo workspace (user `demo_recruiter`) with realistic FICTIONAL data.

    python manage.py seed_demo                      # rebuild the demo workspace (safe to repeat)
    python manage.py seed_demo --score              # ...and run the AI pipeline so scores/ranks/interviews exist
    python manage.py seed_demo --wipe-user admin    # ALSO delete the candidates/jobs that user `admin` created
    python manage.py seed_demo --wipe-all           # ALSO delete every candidate/job of EVERY user (asks first)

All names, e-mails and phone numbers below are invented (example.com / 555-01xx).
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from api.agents import run_full_pipeline
from api.auth_views import DEMO_USERNAME
from api.models import Candidate, Job
from api.services import embed_candidate_chunks, rebuild_resume_chunks

JOBS = [
    ('Backend Developer', 'Python, Django, REST APIs, PostgreSQL', 2,
     'Design and build REST APIs, write automated tests and tune PostgreSQL queries. '
     'Docker and CI/CD experience is a plus.'),
    ('Data Analyst', 'SQL, Excel, Python, Power BI', 1,
     'Turn business data into dashboards and weekly reports. Strong SQL and Excel are essential.'),
    ('Frontend Developer', 'JavaScript, React, CSS, REST APIs', 2,
     'Build fast, accessible and responsive web interfaces in React and connect them to REST APIs.'),
]

# name, e-mail, phone, skills, years, resume
CANDIDATES = [
    ('Aarav Mehta', 'aarav.mehta@example.com', '+1-555-0101', 'Python, Django, REST APIs, PostgreSQL, Docker', 4, """Aarav Mehta
aarav.mehta@example.com | +1-555-0101

SUMMARY
Backend engineer with 4 years of experience building REST APIs and data-heavy services.

SKILLS
Languages: Python, SQL
Frameworks: Django, Django REST Framework, Celery
Tools: PostgreSQL, Docker, GitHub Actions, Redis

EXPERIENCE
Backend Developer, Northwind Payments (2022 - 2026)
- Built and maintained 30+ REST endpoints in Django REST Framework for a payments platform
- Cut slow API responses by 45 percent by tuning PostgreSQL queries and adding indexes
- Wrote 300+ automated tests and set up CI with GitHub Actions
Junior Developer, Bluepeak Software (2021 - 2022)
- Developed internal tools with Django and PostgreSQL

PROJECTS
Expense Tracker API: multi-user Django REST API with JWT login, Docker and 90 percent test coverage.

EDUCATION
B.Tech Computer Science, 2021"""),
    ('Diya Nair', 'diya.nair@example.com', '+1-555-0102', 'Python, FastAPI, Flask, SQL', 2, """Diya Nair
diya.nair@example.com | +1-555-0102

SUMMARY
Python developer with 2 years of experience in lightweight web services.

SKILLS
Python, FastAPI, Flask, SQL, MySQL, Git

EXPERIENCE
Software Developer, Greenfield Labs (2024 - 2026)
- Built internal REST services with FastAPI and Flask for an inventory system
- Wrote SQL reports in MySQL and automated them with Python scripts
Intern, Greenfield Labs (2023 - 2024)
- Fixed bugs and wrote unit tests

PROJECTS
Library Manager: Flask app with SQLite and a small admin dashboard.

EDUCATION
B.Sc Computer Science, 2023"""),
    ('Rohan Das', 'rohan.das@example.com', '+1-555-0103', 'Java, Spring Boot, MySQL', 1, """Rohan Das
rohan.das@example.com | +1-555-0103

SUMMARY
Junior Java developer looking for a backend role.

SKILLS
Java, Spring Boot, MySQL, Git

EXPERIENCE
Graduate Trainee, Metro Systems (2025 - 2026)
- Worked on a Spring Boot billing module and fixed production bugs

PROJECTS
Student Portal: Spring Boot and MySQL CRUD application.

EDUCATION
B.E Information Technology, 2025"""),
    ('Sneha Kulkarni', 'sneha.kulkarni@example.com', '+1-555-0104', 'SQL, Power BI, Excel, Python, Tableau', 3, """Sneha Kulkarni
sneha.kulkarni@example.com | +1-555-0104

SUMMARY
Data analyst with 3 years of experience turning sales and hiring data into dashboards.

SKILLS
SQL, Power BI, Excel, Python, Pandas, Tableau

EXPERIENCE
Data Analyst, Orbit Retail (2023 - 2026)
- Built 12 Power BI dashboards used weekly by the sales leadership team
- Wrote complex SQL queries over 40M rows and cut report time from 3 hours to 20 minutes
- Automated monthly Excel reports with Python and Pandas
Business Analyst Intern, Orbit Retail (2022 - 2023)
- Cleaned data and prepared weekly KPI reports

PROJECTS
Churn Analysis: Python and SQL study that found the top 3 reasons customers leave.

EDUCATION
B.Com with Data Analytics certification, 2022"""),
    ('Imran Qureshi', 'imran.qureshi@example.com', '+1-555-0105', 'Excel, Data Entry, Typing', 0, """Imran Qureshi
imran.qureshi@example.com | +1-555-0105

SUMMARY
Fresher with data entry experience, keen to grow into analytics.

SKILLS
Excel, Data Entry, Typing, MS Word

EXPERIENCE
Data Entry Operator, City Services (2025 - 2026)
- Entered and verified customer records in Excel

EDUCATION
B.A, 2025"""),
    ('Meera Iyer', 'meera.iyer@example.com', '+1-555-0106', 'JavaScript, TypeScript, React, CSS, REST APIs', 5, """Meera Iyer
meera.iyer@example.com | +1-555-0106

SUMMARY
Frontend engineer with 5 years of experience building responsive, accessible React applications.

SKILLS
JavaScript, TypeScript, React, Redux, CSS, HTML, REST APIs, Jest

EXPERIENCE
Senior Frontend Developer, Skyline Apps (2023 - 2026)
- Led the rebuild of a customer dashboard in React and TypeScript used by 50,000 people
- Improved page load time by 40 percent with code splitting and lazy loading
- Mentored 3 junior developers and introduced Jest tests
Frontend Developer, Pixel Studio (2021 - 2023)
- Built responsive landing pages and reusable CSS components

PROJECTS
Habit Tracker: React and REST API app with offline support.

EDUCATION
B.Tech Information Technology, 2021"""),
    ('Kabir Malhotra', 'kabir.malhotra@example.com', '+1-555-0107', 'JavaScript, Vue, HTML, CSS', 2, """Kabir Malhotra
kabir.malhotra@example.com | +1-555-0107

SUMMARY
Frontend developer with 2 years of experience, mostly in Vue.

SKILLS
JavaScript, Vue, HTML, CSS, Git

EXPERIENCE
Web Developer, Bright Agency (2024 - 2026)
- Built marketing websites and small Vue apps for clients
- Converted designs into responsive HTML and CSS
Intern, Bright Agency (2023 - 2024)
- Fixed UI bugs

PROJECTS
Recipe Finder: Vue app that calls a public recipe API.

EDUCATION
BCA, 2023"""),
    ('Ananya Bose', 'ananya.bose@example.com', '+1-555-0108', 'Python, Django, React, JavaScript, PostgreSQL', 3, """Ananya Bose
ananya.bose@example.com | +1-555-0108

SUMMARY
Full-stack developer with 3 years of experience in Django and React.

SKILLS
Python, Django, React, JavaScript, PostgreSQL, REST APIs, Docker

EXPERIENCE
Full-Stack Developer, Lumen Health (2023 - 2026)
- Built patient scheduling features end to end with Django REST APIs and a React front end
- Designed PostgreSQL tables and reduced duplicate bookings by 60 percent
- Added Docker and automated deployments
Intern, Lumen Health (2022 - 2023)
- Built small React components

PROJECTS
Clinic Booking App: Django, PostgreSQL and React with email reminders.

EDUCATION
B.Tech Computer Science, 2022"""),
]

# which candidates the AI pipeline is run for when --score is used: (candidate name, job title)
PIPELINE_RUNS = [
    ('Rohan Das', 'Backend Developer'), ('Diya Nair', 'Backend Developer'),
    ('Aarav Mehta', 'Backend Developer'), ('Imran Qureshi', 'Data Analyst'),
    ('Sneha Kulkarni', 'Data Analyst'), ('Kabir Malhotra', 'Frontend Developer'),
    ('Ananya Bose', 'Frontend Developer'), ('Meera Iyer', 'Frontend Developer'),
]


class Command(BaseCommand):
    help = 'Rebuild the read-only demo workspace with realistic fictional jobs and candidates.'

    def add_arguments(self, parser):
        parser.add_argument('--wipe-user', action='append', default=[], metavar='USERNAME',
                            help="Also delete this user's candidates and jobs (repeatable)")
        parser.add_argument('--wipe-all', action='store_true',
                            help='Also delete the candidates and jobs of EVERY user (asks first)')
        parser.add_argument('--noinput', action='store_true', help='Do not ask for confirmation')
        parser.add_argument('--no-embed', action='store_true', help='Skip Gemini embeddings (no API calls)')
        parser.add_argument('--score', action='store_true',
                            help='Run the AI pipeline for the demo candidates (uses Gemini)')

    def handle(self, *args, **options):
        User = get_user_model()

        if options['wipe_all']:
            self.confirm(options, f'This deletes ALL {Candidate.objects.count()} candidates and '
                                  f'{Job.objects.count()} jobs of EVERY user.')
            Candidate.objects.all().delete()          # resume chunks, applications and logs cascade
            Job.objects.all().delete()
            self.stdout.write('Deleted all candidates and jobs.')
        for username in options['wipe_user']:
            user = User.objects.filter(username=username).first()
            if user is None:
                raise CommandError(f'User "{username}" does not exist.')
            self.confirm(options, f'This deletes {Candidate.objects.filter(owner=user).count()} candidates and '
                                  f'{Job.objects.filter(owner=user).count()} jobs of "{username}".')
            Candidate.objects.filter(owner=user).delete()
            Job.objects.filter(owner=user).delete()
            self.stdout.write(f'Deleted the data of "{username}".')

        demo, created = User.objects.get_or_create(username=DEMO_USERNAME)
        if created:
            demo.set_unusable_password()
            demo.save()
        Candidate.objects.filter(owner=demo).delete()       # rebuilding = safe to run again
        Job.objects.filter(owner=demo).delete()

        jobs = {title: Job.objects.create(owner=demo, title=title, required_skills=skills,
                                          min_experience=exp, description=desc)
                for title, skills, exp, desc in JOBS}
        cands, embedded = {}, 0
        for name, email, phone, skills, years, resume in CANDIDATES:
            c = Candidate.objects.create(owner=demo, name=name, email=email, phone=phone,
                                         skills=skills, experience=years, resume_text=resume)
            rebuild_resume_chunks(c)
            if not options['no_embed']:
                embedded += embed_candidate_chunks(c)
            cands[name] = c
        self.stdout.write(f'Created {len(jobs)} jobs and {len(cands)} candidates '
                          f'({embedded} resume chunks embedded).')

        if options['score']:
            for name, title in PIPELINE_RUNS:
                c, _slot = run_full_pipeline(cands[name].id, jobs[title].id)
                self.stdout.write(f'  {name} for {title}: {c.status}, score {c.score}'
                                  f'{" (AI fallback)" if getattr(c, "fallback_used", False) else ""}')
        self.stdout.write(self.style.SUCCESS('Demo workspace ready.'))

    def confirm(self, options, message):
        if options['noinput']:
            return
        if input(f'{message}\nType "yes" to continue: ').strip().lower() != 'yes':
            raise CommandError('Cancelled. Nothing was deleted.')