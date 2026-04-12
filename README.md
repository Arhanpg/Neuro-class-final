# NeuroClass — AI-Powered Classroom Platform

NeuroClass is a full-stack educational platform built with Flask + MySQL, featuring AI-graded assignments, RAG-powered chatbots trained on lecture notes, and classroom management.

## Tech Stack
- **Backend**: Python / Flask
- **Database**: MySQL (local)
- **Frontend**: Jinja2 templates + Vanilla CSS/JS
- **AI Layer**: LangChain + LangGraph (see `app.py` for agent code)

## Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Set Up MySQL Database
Make sure MySQL is running on your machine, then:
```bash
mysql -u root -p < setup_db.sql
```

### 3. Configure Environment (optional)
You can set these environment variables or edit `config.py`:
```bash
export MYSQL_HOST=localhost
export MYSQL_USER=root
export MYSQL_PASSWORD=your_password
export MYSQL_DB=neuroclass
export SECRET_KEY=your-secret-key
```

### 4. Run the App
```bash
python app.py
```
Open http://localhost:5000 in your browser.

## Features (Phase 1 — Current)
- [x] Landing page with role selection (Student / Instructor)
- [x] Registration & Login for both roles
- [x] Instructor: Create classroom + auto-generate 8-char join code
- [x] Student: Join classroom using code
- [x] Dashboard for both roles
- [x] View classroom members
- [x] Light/Dark mode

## Features (Phase 2 — Coming Soon)
- [ ] AI Chatbot (RAG on lecture notes)
- [ ] Assignment upload (PDF/text) & AI grading
- [ ] Project submission via GitHub repo link
- [ ] Leaderboard for assignments & projects
- [ ] Grade editing by instructor
- [ ] File upload (local storage)

## Project Structure
```
Neuro-class-final/
├── app.py              # Flask routes & logic
├── config.py           # Configuration
├── models.py           # DB helper
├── setup_db.sql        # MySQL schema
├── requirements.txt
├── templates/
│   ├── base.html
│   ├── index.html
│   ├── auth/
│   │   ├── login.html
│   │   └── register.html
│   ├── dashboard/
│   │   ├── teacher.html
│   │   └── student.html
│   └── classroom/
│       ├── create.html
│       ├── join.html
│       └── view.html
├── static/
│   ├── css/style.css
│   └── js/main.js
└── uploads/            # Local file storage (auto-created)
```
