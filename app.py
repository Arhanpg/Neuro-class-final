import os
import re
import random
import string
import hashlib
import uuid
from pathlib import Path
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify, abort
)
from flask_mysqldb import MySQL
import MySQLdb
import MySQLdb.cursors
from werkzeug.utils import secure_filename

from config import Config

app = Flask(__name__)
app.config.from_object(Config)

# ── Explicit utf8mb4 overrides ─────────────────────────────────────────────
# flask_mysqldb reads MYSQL_* keys from app.config; belt-and-braces
app.config['MYSQL_HOST']     = Config.MYSQL_HOST
app.config['MYSQL_USER']     = Config.MYSQL_USER
app.config['MYSQL_PASSWORD'] = Config.MYSQL_PASSWORD
app.config['MYSQL_DB']       = Config.MYSQL_DB
app.config['MYSQL_CHARSET']  = 'utf8mb4'
# init_command runs on every new connection to guarantee utf8mb4 session
app.config['MYSQL_CUSTOM_OPTIONS'] = {
    'charset': 'utf8mb4',
    'init_command': "SET NAMES 'utf8mb4' COLLATE 'utf8mb4_unicode_ci'",
}

mysql = MySQL(app)

# ── Register Blueprints ────────────────────────────────────────────────────
from routes_assignments import assignments_bp
app.register_blueprint(assignments_bp)

for d in [Config.UPLOAD_FOLDER, Config.LECTURES_BASE_DIR, Config.RAG_INDEX_DIR]:
    os.makedirs(d, exist_ok=True)

ALLOWED_EXT = {'pdf', 'doc', 'docx', 'txt'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT


def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()


def generate_classroom_code(length=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


def sanitize_for_mysql(text: str) -> str:
    """
    Remove characters outside the BMP (code points > U+FFFF) that MySQL's
    utf8 (3-byte) charset rejects with error 1366.
    With a true utf8mb4 connection this is a no-op, but we keep it as a
    safety net in case the DB table/column is still on legacy utf8.
    """
    # Encode to utf-8 bytes, decode back — strips surrogates
    text = text.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
    # As an extra safety net: strip lone surrogates (\uD800-\uDFFF)
    text = re.sub(r'[\uD800-\uDFFF]', '', text)
    return text


def login_required(role=None):
    from functools import wraps
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                flash('Please log in to continue.', 'warning')
                return redirect(url_for('login'))
            if role and session.get('role') != role:
                flash('Unauthorised access.', 'danger')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated
    return decorator


# ─── LANDING ─────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')


# ─── AUTH ──────────────────────────────────────────────────────────

@app.route('/register', methods=['GET', 'POST'])
def register():
    role = request.args.get('role', 'student')
    if role not in ('student', 'instructor'):
        role = 'student'

    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email     = request.form.get('email', '').strip().lower()
        password  = request.form.get('password', '')
        confirm   = request.form.get('confirm_password', '')
        role      = request.form.get('role', 'student')

        if not all([full_name, email, password, confirm]):
            flash('All fields are required.', 'danger')
            return render_template('auth/register.html', role=role)
        if password != confirm:
            flash('Passwords do not match.', 'danger')
            return render_template('auth/register.html', role=role)
        if len(password) < 6:
            flash('Password must be at least 6 characters.', 'danger')
            return render_template('auth/register.html', role=role)

        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT id FROM users WHERE email = %s', (email,))
        if cursor.fetchone():
            flash('An account with this email already exists.', 'danger')
            return render_template('auth/register.html', role=role)

        cursor.execute(
            'INSERT INTO users (full_name, email, password_hash, role) VALUES (%s,%s,%s,%s)',
            (full_name, email, hash_password(password), role)
        )
        mysql.connection.commit()
        flash('Account created! Please log in.', 'success')
        return redirect(url_for('login', role=role))

    return render_template('auth/register.html', role=role)


@app.route('/login', methods=['GET', 'POST'])
def login():
    role = request.args.get('role', 'student')
    if role not in ('student', 'instructor'):
        role = 'student'

    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        role     = request.form.get('role', 'student')

        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT * FROM users WHERE email=%s AND role=%s', (email, role))
        user = cursor.fetchone()

        if user and user['password_hash'] == hash_password(password):
            session['user_id']   = user['id']
            session['full_name'] = user['full_name']
            session['email']     = user['email']
            session['role']      = user['role']
            flash(f'Welcome back, {user["full_name"]}!', 'success')
            return redirect(url_for('dashboard'))
        flash('Invalid email or password.', 'danger')

    return render_template('auth/login.html', role=role)


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.', 'info')
    return redirect(url_for('index'))


# ─── DASHBOARD ─────────────────────────────────────────────────────────

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    if session['role'] == 'instructor':
        return redirect(url_for('teacher_dashboard'))
    return redirect(url_for('student_dashboard'))


@app.route('/dashboard/teacher')
@login_required(role='instructor')
def teacher_dashboard():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        'SELECT * FROM classrooms WHERE instructor_id=%s ORDER BY created_at DESC',
        (session['user_id'],)
    )
    classrooms = cursor.fetchall()
    for c in classrooms:
        cursor.execute(
            'SELECT COUNT(*) AS cnt FROM classroom_members WHERE classroom_id=%s',
            (c['id'],)
        )
        c['student_count'] = cursor.fetchone()['cnt']
        from ai_engine import get_training_status, is_indexed
        c['training_status'] = get_training_status(c['id'])
        c['is_indexed'] = bool(c.get('rag_indexed')) or is_indexed(c['id'])
    return render_template('dashboard/teacher.html', classrooms=classrooms)


@app.route('/dashboard/student')
@login_required(role='student')
def student_dashboard():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        '''SELECT c.*, u.full_name AS instructor_name
           FROM classrooms c
           JOIN classroom_members cm ON c.id = cm.classroom_id
           JOIN users u ON c.instructor_id = u.id
           WHERE cm.user_id = %s
           ORDER BY cm.joined_at DESC''',
        (session['user_id'],)
    )
    classrooms = cursor.fetchall()
    return render_template('dashboard/student.html', classrooms=classrooms)


# ─── CLASSROOM CRUD ─────────────────────────────────────────────────────

@app.route('/classroom/create', methods=['GET', 'POST'])
@login_required(role='instructor')
def create_classroom():
    if request.method == 'POST':
        name        = request.form.get('name', '').strip()
        subject     = request.form.get('subject', '').strip()
        description = request.form.get('description', '').strip()

        if not name:
            flash('Classroom name is required.', 'danger')
            return render_template('classroom/create.html')

        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        for _ in range(10):
            code = generate_classroom_code()
            cursor.execute('SELECT id FROM classrooms WHERE code=%s', (code,))
            if not cursor.fetchone():
                break

        cursor.execute(
            'INSERT INTO classrooms (name, subject, description, code, instructor_id) VALUES (%s,%s,%s,%s,%s)',
            (name, subject, description, code, session['user_id'])
        )
        mysql.connection.commit()
        flash(f'Classroom "{name}" created! Share code: {code}', 'success')
        return redirect(url_for('teacher_dashboard'))

    return render_template('classroom/create.html')


@app.route('/classroom/join', methods=['GET', 'POST'])
@login_required(role='student')
def join_classroom():
    if request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        if not code:
            flash('Please enter a classroom code.', 'danger')
            return render_template('classroom/join.html')

        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute(
            '''SELECT c.*, u.full_name AS instructor_name
               FROM classrooms c JOIN users u ON c.instructor_id=u.id
               WHERE c.code=%s''',
            (code,)
        )
        classroom = cursor.fetchone()
        if not classroom:
            flash('Invalid classroom code. Please check and try again.', 'danger')
            return render_template('classroom/join.html')

        cursor.execute(
            'SELECT id FROM classroom_members WHERE classroom_id=%s AND user_id=%s',
            (classroom['id'], session['user_id'])
        )
        if cursor.fetchone():
            flash('You have already joined this classroom.', 'info')
            return redirect(url_for('view_classroom', classroom_id=classroom['id']))

        cursor.execute(
            'INSERT INTO classroom_members (classroom_id, user_id) VALUES (%s,%s)',
            (classroom['id'], session['user_id'])
        )
        mysql.connection.commit()
        flash(f'Successfully joined "{classroom["name"]}"!', 'success')
        return redirect(url_for('view_classroom', classroom_id=classroom['id']))

    return render_template('classroom/join.html')


@app.route('/classroom/<int:classroom_id>')
def view_classroom(classroom_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        '''SELECT c.*, u.full_name AS instructor_name
           FROM classrooms c JOIN users u ON c.instructor_id=u.id
           WHERE c.id=%s''',
        (classroom_id,)
    )
    classroom = cursor.fetchone()
    if not classroom:
        flash('Classroom not found.', 'danger')
        return redirect(url_for('dashboard'))

    if session['role'] == 'instructor' and classroom['instructor_id'] != session['user_id']:
        flash('Unauthorised.', 'danger')
        return redirect(url_for('dashboard'))
    if session['role'] == 'student':
        cursor.execute(
            'SELECT id FROM classroom_members WHERE classroom_id=%s AND user_id=%s',
            (classroom_id, session['user_id'])
        )
        if not cursor.fetchone():
            flash('You are not a member of this classroom.', 'danger')
            return redirect(url_for('student_dashboard'))

    cursor.execute(
        '''SELECT u.full_name, u.email, cm.joined_at
           FROM classroom_members cm
           JOIN users u ON cm.user_id = u.id
           WHERE cm.classroom_id = %s ORDER BY cm.joined_at''',
        (classroom_id,)
    )
    members = cursor.fetchall()

    cursor.execute(
        'SELECT * FROM lecture_materials WHERE classroom_id=%s ORDER BY uploaded_at DESC',
        (classroom_id,)
    )
    materials = cursor.fetchall()

    cursor.execute(
        '''SELECT ch.role, ch.message, ch.created_at, u.full_name
           FROM chat_history ch
           JOIN users u ON ch.user_id = u.id
           WHERE ch.classroom_id = %s AND ch.user_id = %s
           ORDER BY ch.created_at ASC LIMIT 100''',
        (classroom_id, session['user_id'])
    )
    chat_history = cursor.fetchall()

    # ── Load assignments & projects for classroom view ──
    cursor.execute(
        'SELECT * FROM assignments WHERE classroom_id=%s ORDER BY created_at DESC',
        (classroom_id,)
    )
    assignments = cursor.fetchall()

    cursor.execute(
        'SELECT * FROM projects WHERE classroom_id=%s ORDER BY created_at DESC',
        (classroom_id,)
    )
    projects = cursor.fetchall()

    # For each assignment, load submission if student
    my_assignment_submissions = {}
    my_project_submissions = {}
    if session['role'] == 'student':
        for a in assignments:
            cursor.execute(
                'SELECT * FROM assignment_submissions WHERE assignment_id=%s AND student_id=%s',
                (a['id'], session['user_id'])
            )
            sub = cursor.fetchone()
            if sub:
                my_assignment_submissions[a['id']] = sub
        for p in projects:
            cursor.execute(
                'SELECT * FROM project_submissions WHERE project_id=%s AND student_id=%s',
                (p['id'], session['user_id'])
            )
            sub = cursor.fetchone()
            if sub:
                my_project_submissions[p['id']] = sub

    # For teacher: load all submissions with student info
    assignment_submissions_all = {}
    project_submissions_all = {}
    if session['role'] == 'instructor':
        for a in assignments:
            cursor.execute(
                '''SELECT s.*, u.full_name AS student_name
                   FROM assignment_submissions s
                   JOIN users u ON s.student_id = u.id
                   WHERE s.assignment_id = %s
                   ORDER BY s.submitted_at DESC''',
                (a['id'],)
            )
            assignment_submissions_all[a['id']] = cursor.fetchall()
        for p in projects:
            cursor.execute(
                '''SELECT s.*, u.full_name AS student_name
                   FROM project_submissions s
                   JOIN users u ON s.student_id = u.id
                   WHERE s.project_id = %s
                   ORDER BY s.submitted_at DESC''',
                (p['id'],)
            )
            project_submissions_all[p['id']] = cursor.fetchall()

    from ai_engine import is_indexed, get_training_status
    indexed = bool(classroom.get('rag_indexed')) or is_indexed(classroom_id)
    training_status = get_training_status(classroom_id)

    return render_template(
        'classroom/view.html',
        classroom=classroom,
        members=members,
        materials=materials,
        chat_history=chat_history,
        indexed=indexed,
        training_status=training_status,
        assignments=assignments,
        projects=projects,
        my_assignment_submissions=my_assignment_submissions,
        my_project_submissions=my_project_submissions,
        assignment_submissions_all=assignment_submissions_all,
        project_submissions_all=project_submissions_all,
    )


# ─── TEACHER: UPLOAD LECTURES ──────────────────────────────────────────────────

@app.route('/classroom/<int:classroom_id>/upload_lectures', methods=['GET', 'POST'])
@login_required(role='instructor')
def upload_lectures(classroom_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        'SELECT * FROM classrooms WHERE id=%s AND instructor_id=%s',
        (classroom_id, session['user_id'])
    )
    classroom = cursor.fetchone()
    if not classroom:
        flash('Classroom not found.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    if request.method == 'POST':
        files = request.files.getlist('lecture_files')
        if not files or all(f.filename == '' for f in files):
            flash('Please select at least one file.', 'danger')
            return render_template('classroom/upload_lectures.html', classroom=classroom, materials=[])

        save_dir = Path(Config.LECTURES_BASE_DIR) / str(classroom_id)
        save_dir.mkdir(parents=True, exist_ok=True)

        saved = 0
        for f in files:
            if f and f.filename and allowed_file(f.filename):
                orig        = secure_filename(f.filename)
                unique_name = f'{uuid.uuid4().hex}_{orig}'
                fpath       = save_dir / unique_name
                f.save(str(fpath))
                cursor.execute(
                    'INSERT INTO lecture_materials (classroom_id, filename, original_name, file_path) VALUES (%s,%s,%s,%s)',
                    (classroom_id, unique_name, orig, str(fpath))
                )
                saved += 1

        mysql.connection.commit()
        if saved == 0:
            flash('No valid files uploaded. Accepted: PDF, DOC, DOCX, TXT.', 'danger')
        else:
            flash(f'{saved} file(s) uploaded. Click "Train AI" to index them.', 'success')
        return redirect(url_for('view_classroom', classroom_id=classroom_id))

    cursor.execute(
        'SELECT * FROM lecture_materials WHERE classroom_id=%s ORDER BY uploaded_at DESC',
        (classroom_id,)
    )
    materials = cursor.fetchall()
    return render_template('classroom/upload_lectures.html', classroom=classroom, materials=materials)


# ─── TEACHER: DELETE LECTURE ──────────────────────────────────────────────────

@app.route('/classroom/<int:classroom_id>/delete_lecture/<int:material_id>', methods=['POST'])
@login_required(role='instructor')
def delete_lecture(classroom_id, material_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cursor.execute(
        'SELECT id FROM classrooms WHERE id=%s AND instructor_id=%s',
        (classroom_id, session['user_id'])
    )
    if not cursor.fetchone():
        return jsonify({'ok': False, 'error': 'Unauthorised'}), 403

    cursor.execute(
        'SELECT * FROM lecture_materials WHERE id=%s AND classroom_id=%s',
        (material_id, classroom_id)
    )
    material = cursor.fetchone()
    if not material:
        return jsonify({'ok': False, 'error': 'File not found'}), 404

    try:
        file_path = Path(material['file_path'])
        if file_path.exists():
            file_path.unlink()
    except Exception as e:
        print(f'[Delete] Could not remove file: {e}')

    cursor.execute('DELETE FROM lecture_materials WHERE id=%s', (material_id,))
    mysql.connection.commit()

    cursor.execute(
        'SELECT COUNT(*) AS cnt FROM lecture_materials WHERE classroom_id=%s',
        (classroom_id,)
    )
    remaining = cursor.fetchone()['cnt']
    if remaining == 0:
        cursor.execute(
            'UPDATE classrooms SET rag_indexed=0 WHERE id=%s', (classroom_id,)
        )
        mysql.connection.commit()
        from ai_engine import _vector_stores
        _vector_stores.pop(classroom_id, None)

    return jsonify({'ok': True, 'message': f'"{material["original_name"]}" deleted.', 'remaining': remaining})


# ─── TEACHER: TRAIN AI ─────────────────────────────────────────────────────

@app.route('/classroom/<int:classroom_id>/train_ai', methods=['POST'])
@login_required(role='instructor')
def train_ai(classroom_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        'SELECT * FROM classrooms WHERE id=%s AND instructor_id=%s',
        (classroom_id, session['user_id'])
    )
    classroom = cursor.fetchone()
    if not classroom:
        return jsonify({'ok': False, 'error': 'Classroom not found'}), 404

    from ai_engine import build_rag_index
    result = build_rag_index(classroom_id)
    return jsonify(result)


# ─── TRAINING STATUS POLL ──────────────────────────────────────────────────

@app.route('/classroom/<int:classroom_id>/train_status')
def train_status(classroom_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    from ai_engine import get_training_status, is_indexed
    status  = get_training_status(classroom_id)
    indexed = is_indexed(classroom_id)

    if status == 'done' or indexed:
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute(
            'UPDATE classrooms SET rag_indexed=1 WHERE id=%s', (classroom_id,)
        )
        mysql.connection.commit()

    return jsonify({
        'status': status,
        'indexed': indexed,
        'label': {
            'idle':    'Not trained',
            'running': 'Training in background...',
            'done':    'Trained on your notes',
        }.get(status, 'Error - try re-training')
    })


# ─── CHATBOT API ───────────────────────────────────────────────────────

@app.route('/classroom/<int:classroom_id>/chat', methods=['POST'])
def classroom_chat(classroom_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute('SELECT * FROM classrooms WHERE id=%s', (classroom_id,))
    classroom = cursor.fetchone()
    if not classroom:
        return jsonify({'error': 'Classroom not found'}), 404

    if session['role'] == 'student':
        cursor.execute(
            'SELECT id FROM classroom_members WHERE classroom_id=%s AND user_id=%s',
            (classroom_id, session['user_id'])
        )
        if not cursor.fetchone():
            return jsonify({'error': 'Not a member of this classroom'}), 403

    data     = request.get_json()
    question = (data or {}).get('message', '').strip()
    if not question:
        return jsonify({'error': 'Message cannot be empty'}), 400

    safe_question = sanitize_for_mysql(question)

    # Store user message
    cursor.execute(
        'INSERT INTO chat_history (classroom_id, user_id, role, message) VALUES (%s,%s,%s,%s)',
        (classroom_id, session['user_id'], 'user', safe_question)
    )
    mysql.connection.commit()

    # Fetch upcoming assignments so the AI can answer deadline queries
    cursor.execute(
        '''SELECT title, due_date, max_marks
           FROM assignments
           WHERE classroom_id=%s AND due_date >= CURDATE()
           ORDER BY due_date ASC LIMIT 10''',
        (classroom_id,)
    )
    assignments = cursor.fetchall() or []

    context_data = {
        'class_name':   classroom.get('name', ''),
        'subject':      classroom.get('subject', ''),
        'student_name': session.get('full_name', ''),
        'assignments':  [
            {
                'title':     a.get('title'),
                'due_date':  str(a.get('due_date', 'TBD')),
                'max_marks': a.get('max_marks'),
            }
            for a in assignments
        ],
    }

    sess_key = f"{classroom_id}_{session['user_id']}"

    from ai_engine import rag_query
    try:
        answer = rag_query(
            classroom_id,
            question,
            context_data=context_data,
            session_key=sess_key,
        )
    except Exception as e:
        print(f'[Chat] AI error: {e}')
        answer = 'Sorry, I encountered an error. Please try again in a moment.'

    # Sanitize AI reply before storing (removes surrogate chars / bad unicode)
    safe_answer = sanitize_for_mysql(answer)

    cursor.execute(
        'INSERT INTO chat_history (classroom_id, user_id, role, message) VALUES (%s,%s,%s,%s)',
        (classroom_id, session['user_id'], 'assistant', safe_answer)
    )
    mysql.connection.commit()

    return jsonify({'answer': answer})  # return original (with emoji) to frontend


# ─── TEACHER: POST ASSIGNMENT ─────────────────────────────────────────────

@app.route('/classroom/<int:classroom_id>/post_assignment', methods=['POST'])
@login_required(role='instructor')
def post_assignment(classroom_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        'SELECT id FROM classrooms WHERE id=%s AND instructor_id=%s',
        (classroom_id, session['user_id'])
    )
    if not cursor.fetchone():
        return jsonify({'ok': False, 'error': 'Unauthorised'}), 403

    data        = request.get_json() or {}
    title       = sanitize_for_mysql(data.get('title', '').strip())
    description = sanitize_for_mysql(data.get('description', '').strip())
    due_date    = data.get('due_date')   # ISO string or None
    max_marks   = int(data.get('max_marks', 100))

    if not title:
        return jsonify({'ok': False, 'error': 'Title is required'}), 400

    cursor.execute(
        'INSERT INTO assignments (classroom_id, title, description, due_date, max_marks) VALUES (%s,%s,%s,%s,%s)',
        (classroom_id, title, description, due_date or None, max_marks)
    )
    mysql.connection.commit()
    return jsonify({'ok': True, 'id': cursor.lastrowid, 'title': title})


# ─── TEACHER: POST PROJECT ─────────────────────────────────────────────

@app.route('/classroom/<int:classroom_id>/post_project', methods=['POST'])
@login_required(role='instructor')
def post_project(classroom_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        'SELECT id FROM classrooms WHERE id=%s AND instructor_id=%s',
        (classroom_id, session['user_id'])
    )
    if not cursor.fetchone():
        return jsonify({'ok': False, 'error': 'Unauthorised'}), 403

    data        = request.get_json() or {}
    title       = sanitize_for_mysql(data.get('title', '').strip())
    description = sanitize_for_mysql(data.get('description', '').strip())
    due_date    = data.get('due_date')
    max_marks   = int(data.get('max_marks', 100))

    if not title:
        return jsonify({'ok': False, 'error': 'Title is required'}), 400

    cursor.execute(
        'INSERT INTO projects (classroom_id, title, description, due_date, max_marks) VALUES (%s,%s,%s,%s,%s)',
        (classroom_id, title, description, due_date or None, max_marks)
    )
    mysql.connection.commit()
    return jsonify({'ok': True, 'id': cursor.lastrowid, 'title': title})


# ─── STUDENT: SUBMIT ASSIGNMENT ───────────────────────────────────────────

@app.route('/classroom/<int:classroom_id>/submit_assignment/<int:assignment_id>', methods=['POST'])
@login_required(role='student')
def submit_assignment(classroom_id, assignment_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        'SELECT id FROM classroom_members WHERE classroom_id=%s AND user_id=%s',
        (classroom_id, session['user_id'])
    )
    if not cursor.fetchone():
        return jsonify({'ok': False, 'error': 'Not a member'}), 403

    cursor.execute(
        'SELECT * FROM assignments WHERE id=%s AND classroom_id=%s',
        (assignment_id, classroom_id)
    )
    assignment = cursor.fetchone()
    if not assignment:
        return jsonify({'ok': False, 'error': 'Assignment not found'}), 404

    # Check if already submitted
    cursor.execute(
        'SELECT id FROM assignment_submissions WHERE assignment_id=%s AND student_id=%s',
        (assignment_id, session['user_id'])
    )
    if cursor.fetchone():
        return jsonify({'ok': False, 'error': 'Already submitted'}), 400

    submitted_text = ''
    filename = None
    file_path_str = None

    # Handle file upload
    if 'submission_file' in request.files:
        f = request.files['submission_file']
        if f and f.filename and allowed_file(f.filename):
            save_dir = Path(Config.UPLOAD_FOLDER) / 'assignments' / str(assignment_id)
            save_dir.mkdir(parents=True, exist_ok=True)
            orig        = secure_filename(f.filename)
            unique_name = f'{uuid.uuid4().hex}_{orig}'
            fpath       = save_dir / unique_name
            f.save(str(fpath))
            filename       = unique_name
            file_path_str  = str(fpath)
    else:
        # Text submission
        data = request.get_json() or {}
        submitted_text = sanitize_for_mysql(data.get('text', '').strip())

    cursor.execute(
        '''INSERT INTO assignment_submissions
               (assignment_id, student_id, filename, file_path, submitted_text)
           VALUES (%s,%s,%s,%s,%s)''',
        (assignment_id, session['user_id'], filename, file_path_str, submitted_text)
    )
    sub_id = cursor.lastrowid
    mysql.connection.commit()

    # ── Grade asynchronously with LangGraph ──
    rubric = sanitize_for_mysql(
        f"Title: {assignment['title']}\nDescription: {assignment['description'] or ''}\nMax marks: {assignment['max_marks']}"
    )

    def _grade():
        try:
            from ai_engine import evaluate_assignment
            result = evaluate_assignment(
                submission_pdf=file_path_str or '',
                rubric=rubric,
                course_id=str(classroom_id),
                student_id=str(session['user_id']),
            )
            score    = int(result.get('score', 0))
            feedback = sanitize_for_mysql(str(result.get('feedback', '')))

            import MySQLdb
            from config import Config as C
            conn = MySQLdb.connect(
                host=C.MYSQL_HOST, user=C.MYSQL_USER, passwd=C.MYSQL_PASSWORD,
                db=C.MYSQL_DB, charset='utf8mb4',
                init_command="SET NAMES 'utf8mb4' COLLATE 'utf8mb4_unicode_ci'"
            )
            c = conn.cursor()
            c.execute(
                'UPDATE assignment_submissions SET ai_grade=%s, ai_feedback=%s WHERE id=%s',
                (score, feedback, sub_id)
            )
            conn.commit()
            conn.close()
            print(f'[Grade] Assignment sub {sub_id} scored {score}')
        except Exception as e:
            print(f'[Grade] Assignment grading failed: {e}')

    import threading
    threading.Thread(target=_grade, daemon=True).start()

    return jsonify({'ok': True, 'message': 'Submitted! AI is grading, check back soon.'})


# ─── STUDENT: SUBMIT PROJECT ──────────────────────────────────────────────

@app.route('/classroom/<int:classroom_id>/submit_project/<int:project_id>', methods=['POST'])
@login_required(role='student')
def submit_project(classroom_id, project_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        'SELECT id FROM classroom_members WHERE classroom_id=%s AND user_id=%s',
        (classroom_id, session['user_id'])
    )
    if not cursor.fetchone():
        return jsonify({'ok': False, 'error': 'Not a member'}), 403

    cursor.execute(
        'SELECT * FROM projects WHERE id=%s AND classroom_id=%s',
        (project_id, classroom_id)
    )
    project = cursor.fetchone()
    if not project:
        return jsonify({'ok': False, 'error': 'Project not found'}), 404

    cursor.execute(
        'SELECT id FROM project_submissions WHERE project_id=%s AND student_id=%s',
        (project_id, session['user_id'])
    )
    if cursor.fetchone():
        return jsonify({'ok': False, 'error': 'Already submitted'}), 400

    data       = request.get_json() or {}
    github_url = data.get('github_url', '').strip()
    if not github_url or not github_url.startswith('http'):
        return jsonify({'ok': False, 'error': 'Valid GitHub URL required'}), 400

    cursor.execute(
        'INSERT INTO project_submissions (project_id, student_id, github_url) VALUES (%s,%s,%s)',
        (project_id, session['user_id'], github_url)
    )
    sub_id = cursor.lastrowid
    mysql.connection.commit()

    # ── Grade asynchronously ──
    rubric = sanitize_for_mysql(
        f"Title: {project['title']}\nDescription: {project['description'] or ''}\nMax marks: {project['max_marks']}"
    )

    def _grade():
        try:
            from ai_engine import evaluate_project
            result = evaluate_project(
                repo_url=github_url,
                project_rubric=rubric,
                project_details=str(project.get('description', '')),
                student_id=str(session['user_id']),
                classroom_id=classroom_id,
            )
            score    = int(result.get('score', 0))
            feedback = sanitize_for_mysql(str(result.get('analysis', '')))

            import MySQLdb
            from config import Config as C
            conn = MySQLdb.connect(
                host=C.MYSQL_HOST, user=C.MYSQL_USER, passwd=C.MYSQL_PASSWORD,
                db=C.MYSQL_DB, charset='utf8mb4',
                init_command="SET NAMES 'utf8mb4' COLLATE 'utf8mb4_unicode_ci'"
            )
            c = conn.cursor()
            c.execute(
                'UPDATE project_submissions SET ai_grade=%s, ai_feedback=%s WHERE id=%s',
                (score, feedback, sub_id)
            )
            conn.commit()
            conn.close()
            print(f'[Grade] Project sub {sub_id} scored {score}')
        except Exception as e:
            print(f'[Grade] Project grading failed: {e}')

    import threading
    threading.Thread(target=_grade, daemon=True).start()

    return jsonify({'ok': True, 'message': 'Submitted! AI is evaluating your repo, check back soon.'})


# ─── TEACHER: OVERRIDE GRADE ──────────────────────────────────────────────

@app.route('/classroom/<int:classroom_id>/grade_assignment/<int:sub_id>', methods=['POST'])
@login_required(role='instructor')
def override_assignment_grade(classroom_id, sub_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        'SELECT id FROM classrooms WHERE id=%s AND instructor_id=%s',
        (classroom_id, session['user_id'])
    )
    if not cursor.fetchone():
        return jsonify({'ok': False, 'error': 'Unauthorised'}), 403

    data  = request.get_json() or {}
    grade = data.get('grade')
    if grade is None:
        return jsonify({'ok': False, 'error': 'grade required'}), 400

    cursor.execute(
        'UPDATE assignment_submissions SET teacher_grade=%s WHERE id=%s',
        (int(grade), sub_id)
    )
    mysql.connection.commit()
    return jsonify({'ok': True})


@app.route('/classroom/<int:classroom_id>/grade_project/<int:sub_id>', methods=['POST'])
@login_required(role='instructor')
def override_project_grade(classroom_id, sub_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        'SELECT id FROM classrooms WHERE id=%s AND instructor_id=%s',
        (classroom_id, session['user_id'])
    )
    if not cursor.fetchone():
        return jsonify({'ok': False, 'error': 'Unauthorised'}), 403

    data  = request.get_json() or {}
    grade = data.get('grade')
    if grade is None:
        return jsonify({'ok': False, 'error': 'grade required'}), 400

    cursor.execute(
        'UPDATE project_submissions SET teacher_grade=%s WHERE id=%s',
        (int(grade), sub_id)
    )
    mysql.connection.commit()
    return jsonify({'ok': True})


# ─── LEADERBOARD ───────────────────────────────────────────────────────────

@app.route('/classroom/<int:classroom_id>/leaderboard')
def leaderboard(classroom_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        'SELECT name, subject FROM classrooms WHERE id=%s', (classroom_id,)
    )
    classroom = cursor.fetchone()
    if not classroom:
        abort(404)

    # Assignment leaderboard: use teacher_grade if set, else ai_grade
    cursor.execute(
        '''SELECT u.full_name AS student_name,
                  a.title AS assignment_title,
                  COALESCE(s.teacher_grade, s.ai_grade) AS final_grade,
                  a.max_marks,
                  s.submitted_at
           FROM assignment_submissions s
           JOIN users u ON s.student_id = u.id
           JOIN assignments a ON s.assignment_id = a.id
           WHERE a.classroom_id = %s
           ORDER BY final_grade DESC''',
        (classroom_id,)
    )
    assignment_lb = cursor.fetchall()

    # Project leaderboard
    cursor.execute(
        '''SELECT u.full_name AS student_name,
                  p.title AS project_title,
                  COALESCE(s.teacher_grade, s.ai_grade) AS final_grade,
                  p.max_marks,
                  s.submitted_at
           FROM project_submissions s
           JOIN users u ON s.student_id = u.id
           JOIN projects p ON s.project_id = p.id
           WHERE p.classroom_id = %s
           ORDER BY final_grade DESC''',
        (classroom_id,)
    )
    project_lb = cursor.fetchall()

    return render_template(
        'classroom/leaderboard.html',
        classroom=classroom,
        classroom_id=classroom_id,
        assignment_lb=assignment_lb,
        project_lb=project_lb,
    )


# ─── SERVE LECTURE FILES ────────────────────────────────────────────────────

@app.route('/uploads/lectures/<int:classroom_id>/<path:filename>')
def serve_lecture(classroom_id, filename):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        'SELECT * FROM lecture_materials WHERE classroom_id=%s AND filename=%s',
        (classroom_id, filename)
    )
    material = cursor.fetchone()
    if not material:
        abort(404)

    cursor.execute('SELECT instructor_id FROM classrooms WHERE id=%s', (classroom_id,))
    classroom = cursor.fetchone()
    if not classroom:
        abort(404)

    if session['role'] == 'instructor':
        if classroom['instructor_id'] != session['user_id']:
            abort(403)
    else:
        cursor.execute(
            'SELECT id FROM classroom_members WHERE classroom_id=%s AND user_id=%s',
            (classroom_id, session['user_id'])
        )
        if not cursor.fetchone():
            abort(403)

    from flask import send_from_directory, make_response
    folder = os.path.join(Config.LECTURES_BASE_DIR, str(classroom_id))
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

    if ext == 'pdf':
        response = make_response(send_from_directory(folder, filename))
        response.headers['Content-Disposition'] = f'inline; filename="{material["original_name"]}"'
        response.headers['Content-Type'] = 'application/pdf'
        return response
    else:
        return send_from_directory(folder, filename, as_attachment=True,
                                   download_name=material['original_name'])


# ─── API HELPERS ────────────────────────────────────────────────────────

@app.route('/api/classroom/<int:classroom_id>/code')
@login_required(role='instructor')
def get_classroom_code(classroom_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        'SELECT code FROM classrooms WHERE id=%s AND instructor_id=%s',
        (classroom_id, session['user_id'])
    )
    c = cursor.fetchone()
    if not c:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'code': c['code']})


if __name__ == '__main__':
    app.run(debug=True, port=5000)
