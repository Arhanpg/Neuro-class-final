import os
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
import MySQLdb.cursors
from werkzeug.utils import secure_filename

from config import Config

app = Flask(__name__)
app.config.from_object(Config)

app.config['MYSQL_HOST']     = Config.MYSQL_HOST
app.config['MYSQL_USER']     = Config.MYSQL_USER
app.config['MYSQL_PASSWORD'] = Config.MYSQL_PASSWORD
app.config['MYSQL_DB']       = Config.MYSQL_DB

mysql = MySQL(app)

for d in [Config.UPLOAD_FOLDER, Config.LECTURES_BASE_DIR, Config.RAG_INDEX_DIR]:
    os.makedirs(d, exist_ok=True)

ALLOWED_EXT = {'pdf', 'doc', 'docx', 'txt'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT


def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()


def generate_classroom_code(length=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


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


# ─── LANDING ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')


# ─── AUTH ──────────────────────────────────────────────────────────────────

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


# ─── DASHBOARD ─────────────────────────────────────────────────────────────

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


# ─── CLASSROOM CRUD ────────────────────────────────────────────────────────

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
        flash(f'Classroom "{name}" created! Share code with students: {code}', 'success')
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

    # ── FIX: Only load THIS user's chat history ──────────────────────────────
    cursor.execute(
        '''SELECT ch.role, ch.message, ch.created_at, u.full_name
           FROM chat_history ch
           JOIN users u ON ch.user_id = u.id
           WHERE ch.classroom_id = %s AND ch.user_id = %s
           ORDER BY ch.created_at ASC LIMIT 100''',
        (classroom_id, session['user_id'])
    )
    chat_history = cursor.fetchall()

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
    )


# ─── TEACHER: UPLOAD LECTURES ──────────────────────────────────────────────

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


# ─── TEACHER: DELETE LECTURE ───────────────────────────────────────────────

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
            'running': '⏳ Training in background…',
            'done':    '✅ Trained on your notes',
        }.get(status, '⚠️ Error — try re-training')
    })


# ─── CHATBOT API ───────────────────────────────────────────────────────────

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

    # Store user message — tagged with user_id for isolation
    cursor.execute(
        'INSERT INTO chat_history (classroom_id, user_id, role, message) VALUES (%s,%s,%s,%s)',
        (classroom_id, session['user_id'], 'user', question)
    )
    mysql.connection.commit()

    context_data = {'class_name': classroom['name']}
    from ai_engine import rag_query
    answer = rag_query(classroom_id, question, context_data)

    # Store assistant reply — also tagged with this user's id
    cursor.execute(
        'INSERT INTO chat_history (classroom_id, user_id, role, message) VALUES (%s,%s,%s,%s)',
        (classroom_id, session['user_id'], 'assistant', answer)
    )
    mysql.connection.commit()

    return jsonify({'answer': answer})


# ─── SERVE LECTURE FILES ────────────────────────────────────────────────────
# Handles both PDF (inline viewer) and other doc types (download)

@app.route('/uploads/lectures/<int:classroom_id>/<path:filename>')
def serve_lecture(classroom_id, filename):
    """Serve a lecture file.
    PDFs: served inline so the browser renders them directly.
    Other types: served as attachment (download).
    Access control: must be logged in AND either the instructor OR an enrolled student.
    """
    if 'user_id' not in session:
        return redirect(url_for('login'))

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    # Verify the file belongs to this classroom
    cursor.execute(
        'SELECT * FROM lecture_materials WHERE classroom_id=%s AND filename=%s',
        (classroom_id, filename)
    )
    material = cursor.fetchone()
    if not material:
        abort(404)

    # Verify access rights
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
        # Inline — browser opens its built-in PDF viewer
        response = make_response(send_from_directory(folder, filename))
        response.headers['Content-Disposition'] = f'inline; filename="{material["original_name"]}"'
        response.headers['Content-Type'] = 'application/pdf'
        return response
    else:
        # Force download for doc/docx/txt
        return send_from_directory(folder, filename, as_attachment=True,
                                   download_name=material['original_name'])


# ─── API HELPERS ────────────────────────────────────────────────────────────

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
