# 🤖 AI-Powered Recruitment Assistant

> An AI-powered recruitment platform built with **Google ADK, Multi-Agent Systems, MCP Servers, and Django** to automate the complete hiring pipeline from resume screening to interview scheduling.

![Python](https://img.shields.io/badge/Python-3.x-blue)
![Django](https://img.shields.io/badge/Django-Framework-green)
![Google ADK](https://img.shields.io/badge/Google-ADK-red)
![MCP](https://img.shields.io/badge/MCP-Servers-orange)
![License](https://img.shields.io/badge/License-MIT-blue)

---

## 🚀 Live Demo

🌐 **Live Application:**  
https://aryaansaini.pythonanywhere.com/

💻 **GitHub Repository:**  
https://github.com/aryaansaini/recruitment-ai

---

# 📌 Overview

Recruitment teams spend significant time screening resumes, matching candidates with job descriptions, scheduling interviews, and ranking applicants.

This project automates the entire recruitment workflow using **Google ADK Agents**, **Model Context Protocol (MCP) Servers**, and **Agent-to-Agent (A2A) Communication**.

The system reduces manual effort, improves hiring efficiency, and provides recruiters with an intelligent dashboard for managing candidates.

---

# ✨ Key Features

- 📄 AI Resume Screening
- 🤖 Multi-Agent Architecture
- 📊 AI Candidate Ranking
- 📅 Automatic Interview Scheduling
- 📂 Resume Parser Integration
- 📈 Recruiter Dashboard
- 🔄 Real-time A2A Communication
- 🗂 ATS Management
- 📧 Candidate Tracking
- 💼 Production Deployment

---

# 🧠 Google ADK Agents

## 📄 Resume Screening Agent

- Parses uploaded resumes
- Extracts skills, education & experience
- Shortlists candidates
- Sends profiles to Job Matching Agent

---

## 🎯 Job Matching Agent

- Matches candidate skills with job requirements
- Calculates match percentage
- Forwards shortlisted candidates

---

## 🏆 Candidate Ranking Agent

- Scores candidates based on:
  - Skills
  - Experience
  - Education
  - Communication

- Generates AI Ranking Leaderboard

---

## 📅 Interview Coordination Agent

- Checks interviewer availability
- Creates interview schedule
- Updates ATS automatically
- Creates Calendar Events

---

# 🔗 MCP Servers

## ATS MCP

- Candidate Database
- Job Management
- Application Tracking
- CRUD Operations

---

## Resume Parser MCP

- PDF Resume Parsing
- DOCX Resume Parsing
- Skill Extraction
- Education Extraction
- Experience Extraction

---

## Calendar MCP

- Interview Scheduling
- Calendar Events
- Time Slot Management
- Interview Status

---

# 🔄 A2A Communication Flow

```
Resume Screening Agent
            │
            ▼
Job Matching Agent
            │
            ▼
Candidate Ranking Agent
            │
            ▼
Interview Coordination Agent
            │
            ▼
 ATS MCP + Calendar MCP
```

---

# 🖥 Application Modules

### 📊 Recruiter Dashboard

- Candidate Statistics
- Shortlisted Candidates
- Scheduled Interviews
- Rejected Candidates

---

### 📄 Candidate Screening Portal

- Upload Resume
- Add Candidate
- AI Resume Parsing
- Automated Screening

---

### 📅 Interview Scheduler

- Schedule Interviews
- Calendar Integration
- Interview Status

---

### 🏆 Candidate Ranking Dashboard

- AI Leaderboard
- Match Percentage
- Recommendation
- Final Score

---

# ⚙️ Tech Stack

| Category | Technologies |
|-----------|-------------|
| Frontend | HTML5, CSS3, JavaScript |
| Backend | Python, Django |
| Database | SQLite |
| AI Framework | Google ADK |
| Protocol | Agent-to-Agent (A2A) |
| MCP Servers | ATS MCP, Resume Parser MCP, Calendar MCP |
| Hosting | PythonAnywhere |
| Version Control | Git & GitHub |

---

# 📁 Project Structure

```
recruitment-ai/
│
├── agents/
├── mcp_servers/
├── recruitment/
├── templates/
├── static/
├── media/
├── database/
├── requirements.txt
├── manage.py
└── README.md
```

---

# 🚀 Installation

```bash
git clone https://github.com/aryaansaini/recruitment-ai.git

cd recruitment-ai

python -m venv venv

source venv/bin/activate
# Windows
venv\Scripts\activate

pip install -r requirements.txt

python manage.py migrate

python manage.py runserver
```

---

# 📸 Screenshots

Add screenshots here:

- Recruiter Dashboard
- Candidate Screening Portal
- Interview Scheduler
- Candidate Ranking Dashboard

---

# 🎯 Future Improvements

- Google Meet Integration
- Email Notifications
- LinkedIn Resume Import
- AI Interview Question Generator
- Voice Interview Assistant
- Analytics Dashboard
- Docker Deployment
- Kubernetes Support

---

# 👨‍💻 Author

**Aryan Saini**

📧 aryansaini2492004@gmail.com

🔗 LinkedIn  
https://linkedin.com/in/aryaansaini

💻 GitHub  
https://github.com/aryaansaini

