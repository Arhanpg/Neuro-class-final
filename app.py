from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_mysqldb import MySQL
import MySQLdb.cursors
import hashlib
import os
import random
import string
from config import Config

app = Flask(__name__)
app.config.from_object(Config)

# MySQL config
app.config['MYSQL_HOST'] = Config.MYSQL_HOST
app.config['MYSQL_USER'] = Config.MYSQL_USER
app.config['MYSQL_PASSWORD'] = Config.MYSQL_PASSWORD
app.config['MYSQL_DB'] = Config.MYSQL_DB

mysql = MySQL(app)

os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)


def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()


def generate_classroom_code(length=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


def login_required(role=None):
    """Decorator to require login, optionally with a specific role."""
    from functools import wraps
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                flash('Please log in to continue.', 'warning')
                return redirect(url_for('login'))
            if role and session.get('role') != role:
                flash('Unauthorized access.', 'danger')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator


# ─── LANDING ────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')


# ─── AUTH ────────────────────────────────────────────────────────────────────

@app.route('/register', methods=['GET', 'POST'])
def register():
    role = request.args.get('role', 'student')  # 'student' or 'instructor'
    if role not in ('student', 'instructor'):
        role = 'student'

    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        role = request.form.get('role', 'student')

        # Validations
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

        hashed = hash_password(password)
        cursor.execute(
            'INSERT INTO users (full_name, email, password_hash, role) VALUES (%s, %s, %s, %s)',
            (full_name, email, hashed, role)
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
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        role = request.form.get('role', 'student')

        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT * FROM users WHERE email = %s AND role = %s', (email, role))
        user = cursor.fetchone()

        if user and user['password_hash'] == hash_password(password):
            session['user_id'] = user['id']
            session['full_name'] = user['full_name']
            session['email'] = user['email']
            session['role'] = user['role']
            flash(f'Welcome back, {user["full_name"]}!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password.', 'danger')

    return render_template('auth/login.html', role=role)


@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))


# ─── DASHBOARD ──────────────────────────────────────────────────────────────

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
        'SELECT * FROM classrooms WHERE instructor_id = %s ORDER BY created_at DESC',
        (session['user_id'],)
    )
    classrooms = cursor.fetchall()
    # Fetch student count per classroom
    for c in classrooms:
        cursor.execute(
            'SELECT COUNT(*) as cnt FROM classroom_members WHERE classroom_id = %s',
            (c['id'],)
        )
        c['student_count'] = cursor.fetchone()['cnt']
    return render_template('dashboard/teacher.html', classrooms=classrooms)


@app.route('/dashboard/student')
@login_required(role='student')
def student_dashboard():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        '''SELECT c.*, u.full_name as instructor_name 
           FROM classrooms c 
           JOIN classroom_members cm ON c.id = cm.classroom_id
           JOIN users u ON c.instructor_id = u.id
           WHERE cm.user_id = %s
           ORDER BY cm.joined_at DESC''',
        (session['user_id'],)
    )
    classrooms = cursor.fetchall()
    return render_template('dashboard/student.html', classrooms=classrooms)


# ─── CLASSROOM ───────────────────────────────────────────────────────────────

@app.route('/classroom/create', methods=['GET', 'POST'])
@login_required(role='instructor')
def create_classroom():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        subject = request.form.get('subject', '').strip()

        if not name:
            flash('Classroom name is required.', 'danger')
            return render_template('classroom/create.html')

        # Generate unique code
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        while True:
            code = generate_classroom_code()
            cursor.execute('SELECT id FROM classrooms WHERE code = %s', (code,))
            if not cursor.fetchone():
                break

        cursor.execute(
            'INSERT INTO classrooms (name, description, subject, code, instructor_id) VALUES (%s, %s, %s, %s, %s)',
            (name, description, subject, code, session['user_id'])
        )
        mysql.connection.commit()
        flash(f'Classroom "{name}" created! Share the code: {code}', 'success')
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
            'SELECT c.*, u.full_name as instructor_name FROM classrooms c JOIN users u ON c.instructor_id = u.id WHERE c.code = %s',
            (code,)
        )
        classroom = cursor.fetchone()

        if not classroom:
            flash('Invalid classroom code. Please check and try again.', 'danger')
            return render_template('classroom/join.html')

        # Check if already joined
        cursor.execute(
            'SELECT id FROM classroom_members WHERE classroom_id = %s AND user_id = %s',
            (classroom['id'], session['user_id'])
        )
        if cursor.fetchone():
            flash('You have already joined this classroom.', 'info')
            return redirect(url_for('student_dashboard'))

        cursor.execute(
            'INSERT INTO classroom_members (classroom_id, user_id) VALUES (%s, %s)',
            (classroom['id'], session['user_id'])
        )
        mysql.connection.commit()
        flash(f'Successfully joined "{classroom["name"]}"!', 'success')
        return redirect(url_for('student_dashboard'))

    return render_template('classroom/join.html')


@app.route('/classroom/<int:classroom_id>')
def view_classroom(classroom_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        'SELECT c.*, u.full_name as instructor_name FROM classrooms c JOIN users u ON c.instructor_id = u.id WHERE c.id = %s',
        (classroom_id,)
    )
    classroom = cursor.fetchone()
    if not classroom:
        flash('Classroom not found.', 'danger')
        return redirect(url_for('dashboard'))

    # Access check
    if session['role'] == 'instructor' and classroom['instructor_id'] != session['user_id']:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('dashboard'))
    if session['role'] == 'student':
        cursor.execute(
            'SELECT id FROM classroom_members WHERE classroom_id = %s AND user_id = %s',
            (classroom_id, session['user_id'])
        )
        if not cursor.fetchone():
            flash('You are not a member of this classroom.', 'danger')
            return redirect(url_for('student_dashboard'))

    # Fetch members
    cursor.execute(
        '''SELECT u.full_name, u.email, cm.joined_at 
           FROM classroom_members cm JOIN users u ON cm.user_id = u.id 
           WHERE cm.classroom_id = %s ORDER BY cm.joined_at''',
        (classroom_id,)
    )
    members = cursor.fetchall()

    return render_template('classroom/view.html', classroom=classroom, members=members)


# ─── API ─────────────────────────────────────────────────────────────────────

@app.route('/api/classroom/<int:classroom_id>/code', methods=['GET'])
@login_required(role='instructor')
def get_classroom_code(classroom_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute('SELECT code FROM classrooms WHERE id = %s AND instructor_id = %s', (classroom_id, session['user_id']))
    classroom = cursor.fetchone()
    if not classroom:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'code': classroom['code']})


if __name__ == '__main__':
    app.run(debug=True, port=5000)
