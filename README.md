# NeuroClass — AI-Powered Classroom Platform

Flask + MySQL + LangChain + LangGraph + FAISS RAG

## Quick Start

### 1. Clone & install
```bash
git clone https://github.com/Arhanpg/Neuro-class-final
cd Neuro-class-final
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env — add MySQL password and API keys
```

### 3. Setup MySQL database
```bash
mysql -u root -p < setup_db.sql
```
> ⚠️ If you already ran an older version of `setup_db.sql`, run it again — it uses `CREATE TABLE IF NOT EXISTS` so it's safe to re-run.

### 4. Run
```bash
python app.py
```
Open http://localhost:5000

---

## Features (Phase 1 + 2)

| Feature | Status |
|---|---|
| Login as Student / Instructor | ✅ |
| Instructor creates classroom + auto code | ✅ |
| Student joins via code | ✅ |
| Instructor uploads lecture PDFs | ✅ |
| One-click AI training (RAG / FAISS) | ✅ |
| AI Chatbot in every classroom | ✅ |
| Fallback LLM chain (Gemini → OpenRouter → Groq) | ✅ |
| Dark / Light mode toggle | ✅ |
| Assignment submission + AI grading | 🔜 Phase 3 |
| Project submission via GitHub link | 🔜 Phase 3 |
| Leaderboard | 🔜 Phase 3 |

## AI Keys
Get free API keys from:
- **Gemini**: https://aistudio.google.com/app/apikey
- **OpenRouter**: https://openrouter.ai (free Llama-3.3-70B)
- **Groq**: https://console.groq.com

Add to `.env`:
```
GEMINI_API_KEY=...
OPENROUTER_API_KEY=...
GROQ_API_KEY=...
```
