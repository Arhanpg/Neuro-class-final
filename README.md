# NeuroClass — AI-Powered Classroom Platform

Flask + MySQL web app for AI-enhanced classrooms.

## Quick Start

### 1. Install Python dependencies
```bash
pip install -r requirements.txt
```

### 2. Set up MySQL

**Fresh install (no existing DB):**
```bash
mysql -u root -p < setup_db.sql
```

**Already have the old DB and getting table errors?** Run the migration instead:
```bash
mysql -u root -p neuroclass < migrate.sql
```

### 3. Configure database password
Edit `config.py`:
```python
MYSQL_PASSWORD = 'your_mysql_root_password'
```

### 4. Run the app
```bash
python app.py
```
Open [http://localhost:5000](http://localhost:5000)

---

## Features (Phase 1)
- Role-based auth: Instructor / Student
- Instructor: Create classrooms, get unique join code
- Student: Join classroom via code
- Classroom dashboard with member list
- Light / Dark mode toggle

## Troubleshooting

### `Table 'neuroclass.lecture_materials' doesn't exist`
You have an old database. Run:
```bash
mysql -u root -p neuroclass < migrate.sql
```

### `No module named 'flask_mysqldb'`
```bash
pip install flask-mysqldb
```

### `1045 Access denied for user 'root'`
Check `MYSQL_PASSWORD` in `config.py`.
