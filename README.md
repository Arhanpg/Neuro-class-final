# NeuroClass

AI-powered classroom management platform built with Flask.

## Features (Phase 1 — Auth & Classrooms)
- Register/Login as **Student** or **Instructor**
- Instructor: Create classrooms with auto-generated 7-character codes
- Student: Join classrooms using the classroom code
- Clean dashboard for both roles

## Coming Soon
- AI Chatbot (RAG on lecture notes via LangChain/LangGraph)
- Assignment upload (PDF/text) + AI grading
- Project submission via GitHub repo links
- Leaderboard for assignments & projects
- Grade editing by instructor

## Run Locally

```bash
pip install -r requirements.txt
python app.py
```
Then open http://localhost:5000

## Project Structure
```
app.py              # Flask app (routes, in-memory data store)
templates/          # Jinja2 HTML templates
  base.html         # Shared layout
  landing.html      # Public landing page
  auth.html         # Login / Register
  instructor_dashboard.html
  instructor_class.html
  student_dashboard.html
  student_class.html
static/
  css/style.css     # Full design system
  js/main.js        # Theme, toasts, helpers
requirements.txt
```
