"""
routes_assignments.py
All Assignment Management System routes for NeuroClass.
Register with: from routes_assignments import assignments_bp; app.register_blueprint(assignments_bp)
"""

import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, jsonify, abort
)
import MySQLdb
import MySQLdb.cursors
from werkzeug.utils import secure_filename

from config import Config

assignments_bp = Blueprint('assignments', __name__)

ALLOWED_EXT = {'pdf', 'doc', 'docx'}

# ─── cached set of known-missing columns so we only probe once per app start ──
_missing_columns = set()   # e.g. {'visibility', 'rubric', 'assign_text', ...}
_columns_probed  = False


def _db():
    """Open a fresh MySQL connection (used inside background threads)."""
    return MySQLdb.connect(
        host=Config.MYSQL_HOST,
        user=Config.MYSQL_USER,
        passwd=Config.MYSQL_PASSWORD,
        db=Config.MYSQL_DB,
        charset='utf8mb4',
        init_command="SET NAMES 'utf8mb4' COLLATE 'utf8mb4_unicode_ci'",
    )


def _cursor():
    """Cursor from the Flask-MySQLdb connection (used in request context)."""
    from app import mysql
    return mysql.connection.cursor(MySQLdb.cursors.DictCursor)


def _commit():
    from app import mysql
    mysql.connection.commit()


def _rollback_safe():
    try:
        from app import mysql
        mysql.connection.rollback()
    except Exception:
        pass


def _allowed(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT


def _sanitize(text: str) -> str:
    import re
    text = text.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
    return re.sub(r'[\uD800-\uDFFF]', '', text)


def _require_login(role=None):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if role and session.get('role') != role:
        abort(403)
    return None


def _grade_label(score):
    if score is None:
        return '?'
    if score >= 90: return 'A'
    if score >= 80: return 'B'
    if score >= 70: return 'C'
    if score >= 60: return 'D'
    return 'F'


# ─── Probe which columns actually exist in the assignments table ──────────────

def _probe_assignment_columns():
    """
    Check the DB schema once and cache which optional columns are missing.
    Optional columns: visibility, rubric, assign_text, source_label,
                      ai_model, strictness, feedback_style, max_attempts
    """
    global _columns_probed, _missing_columns
    if _columns_probed:
        return

    optional = ['visibility', 'rubric', 'assign_text', 'source_label',
                'ai_model', 'strictness', 'feedback_style', 'max_attempts']
    try:
        c = _cursor()
        c.execute("SHOW COLUMNS FROM assignments")
        existing = {row['Field'] for row in c.fetchall()}
        _missing_columns = set(optional) - existing
    except Exception:
        _missing_columns = set()   # if probe fails, try with all columns
    _columns_probed = True


def _has_col(col: str) -> bool:
    _probe_assignment_columns()
    return col not in _missing_columns


# ─── ADD missing columns automatically (best-effort, runs at first request) ──

def _ensure_assignment_columns():
    """
    Auto-ADD any missing optional columns so the app self-heals.
    Runs once per process startup.
    """
    _probe_assignment_columns()
    if not _missing_columns:
        return

    add_map = {
        'visibility':     "VARCHAR(20) NOT NULL DEFAULT 'published'",
        'rubric':         "TEXT NULL",
        'assign_text':    "TEXT NULL",
        'source_label':   "VARCHAR(20) NULL DEFAULT 'text'",
        'ai_model':       "VARCHAR(50) NULL DEFAULT 'auto'",
        'strictness':     "VARCHAR(20) NULL DEFAULT 'balanced'",
        'feedback_style': "VARCHAR(20) NULL DEFAULT 'detailed'",
        'max_attempts':   "INT NOT NULL DEFAULT 1",
    }
    try:
        c = _cursor()
        for col in list(_missing_columns):
            if col in add_map:
                try:
                    c.execute(f"ALTER TABLE assignments ADD COLUMN `{col}` {add_map[col]}")
                    _commit()
                    _missing_columns.discard(col)
                except MySQLdb.OperationalError as e:
                    if '1060' in str(e):   # Duplicate column — already exists
                        _missing_columns.discard(col)
    except Exception:
        pass
    # Re-probe after additions
    global _columns_probed
    _columns_probed = False
    _probe_assignment_columns()


# ─────────────────────────────────────────────────────────────────────────────
# AI FEEDBACK PARSERS  (match exact notebook output format)
# CRITERIONBREAKDOWN / IMPROVEMENTSUGGESTIONS / DETAILEDFEEDBACK
# ─────────────────────────────────────────────────────────────────────────────

def _parse_criteria(feedback: str) -> list:
    """
    Parse CRITERIONBREAKDOWN block from notebook AI output.
    Format:  CRITERIONBREAKDOWN\n- criterion name score/max  reason\n...
    """
    criteria = []
    in_block = False
    stop_keys = ('SCORE', 'GRADE', 'STRENGTHS', 'WEAKNESSES',
                 'IMPROVEMENTSUGGESTIONS', 'IMPROVEMENT_SUGGESTIONS',
                 'DETAILEDFEEDBACK', 'DETAILED_FEEDBACK')
    for line in feedback.splitlines():
        l = line.strip()
        upper = l.upper().replace(' ', '').replace('_', '')
        if 'CRITERIONBREAKDOWN' in upper:
            in_block = True
            continue
        if in_block:
            if any(upper.startswith(k.replace('_', '')) for k in stop_keys):
                break
            if l.startswith('-') or l.startswith('•'):
                criteria.append(l.lstrip('-•').strip())
    return criteria


def _parse_suggestions(feedback: str) -> list:
    """
    Parse IMPROVEMENTSUGGESTIONS block from notebook AI output.
    Format:  IMPROVEMENTSUGGESTIONS\n- suggestion\n- suggestion\n...
    """
    sugs = []
    in_block = False
    stop_keys = ('DETAILEDFEEDBACK', 'DETAILED_FEEDBACK', 'SCORE', 'GRADE')
    for line in feedback.splitlines():
        l = line.strip()
        upper = l.upper().replace(' ', '').replace('_', '')
        if 'IMPROVEMENTSUGGESTION' in upper:
            in_block = True
            continue
        if in_block:
            if any(upper.startswith(k.replace('_', '')) for k in stop_keys):
                break
            if l.startswith('-') or (l and l[0].isdigit()):
                sugs.append(l.lstrip('-0123456789.) ').strip())
    return sugs


def _extract_section(feedback: str, start_key: str, end_keys: list) -> str:
    """
    Extract a named section from notebook feedback text.
    Handles both STRENGTHS: text and STRENGTHS\ntext formats.
    """
    upper_fb = feedback.upper().replace(' ', '').replace('_', '')
    start_key_clean = start_key.upper().replace(' ', '').replace('_', '')
    pos = upper_fb.find(start_key_clean)
    if pos < 0:
        return ''
    end_pos = len(feedback)
    for ek in end_keys:
        ek_clean = ek.upper().replace(' ', '').replace('_', '')
        ep = upper_fb.find(ek_clean, pos + len(start_key_clean))
        if ep > pos and ep < end_pos:
            end_pos = ep
    chunk = feedback[pos:end_pos]
    lines = chunk.splitlines()
    result_lines = []
    for i, ln in enumerate(lines):
        if i == 0:
            colon_idx = ln.find(':')
            if colon_idx >= 0:
                result_lines.append(ln[colon_idx+1:].strip())
        else:
            result_lines.append(ln)
    return '\n'.join(result_lines).strip()


# ═══════════════════════════════════════════════════════════
#  TEACHER — CREATE ASSIGNMENT
# ═══════════════════════════════════════════════════════════

@assignments_bp.route('/classroom/<int:classroom_id>/assignment/create', methods=['GET', 'POST'])
def create_assignment(classroom_id):
    redir = _require_login('instructor')
    if redir: return redir

    # Self-heal: add missing columns if needed
    _ensure_assignment_columns()

    cursor = _cursor()
    cursor.execute('SELECT * FROM classrooms WHERE id=%s AND instructor_id=%s',
                   (classroom_id, session['user_id']))
    classroom = cursor.fetchone()
    if not classroom:
        flash('Classroom not found.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    if request.method == 'POST':
        title          = _sanitize(request.form.get('title', '').strip())
        due_date       = request.form.get('due_date') or None
        max_marks      = int(request.form.get('max_marks', 100))
        max_attempts   = int(request.form.get('max_attempts', 1))
        visibility     = request.form.get('visibility', 'published')
        rubric         = _sanitize(request.form.get('rubric', '').strip())
        assign_text    = _sanitize(request.form.get('assign_text', '').strip())
        source_label   = request.form.get('source_label', 'text')
        ai_model       = request.form.get('ai_model', 'auto')
        strictness     = request.form.get('strictness', 'balanced')
        feedback_style = request.form.get('feedback_style', 'detailed')

        if not title:
            flash('Title is required.', 'danger')
            return render_template('assignments/create.html', classroom=classroom)
        if not rubric:
            flash('Rubric is required — the AI needs it to grade submissions.', 'danger')
            return render_template('assignments/create.html', classroom=classroom)

        # Merge rubric into description so the AI can read it later
        description = f"RUBRIC:\n{rubric}\n\nASSIGNMENT:\n{assign_text}"

        # Handle file upload
        if source_label == 'file' and 'assign_file' in request.files:
            f = request.files['assign_file']
            if f and f.filename and _allowed(f.filename):
                save_dir = Path(Config.UPLOAD_FOLDER) / 'assignments' / str(classroom_id)
                save_dir.mkdir(parents=True, exist_ok=True)
                fn = f'{uuid.uuid4().hex}_{secure_filename(f.filename)}'
                f.save(str(save_dir / fn))
                assign_text = f'[FILE: {fn}]'
                description = f"RUBRIC:\n{rubric}\n\nASSIGNMENT:\n{assign_text}"

        # ── Build INSERT dynamically based on which columns actually exist ──
        cols   = ['classroom_id', 'title', 'description', 'due_date', 'max_marks']
        vals   = [classroom_id,   title,   description,   due_date,   max_marks]

        if _has_col('rubric'):
            cols.append('rubric');         vals.append(rubric)
        if _has_col('assign_text'):
            cols.append('assign_text');    vals.append(assign_text)
        if _has_col('source_label'):
            cols.append('source_label');   vals.append(source_label)
        if _has_col('ai_model'):
            cols.append('ai_model');       vals.append(ai_model)
        if _has_col('strictness'):
            cols.append('strictness');     vals.append(strictness)
        if _has_col('feedback_style'):
            cols.append('feedback_style'); vals.append(feedback_style)
        if _has_col('max_attempts'):
            cols.append('max_attempts');   vals.append(max_attempts)
        if _has_col('visibility'):
            cols.append('visibility');     vals.append(visibility)

        placeholders = ', '.join(['%s'] * len(cols))
        col_list     = ', '.join(f'`{c}`' for c in cols)
        sql = f"INSERT INTO assignments ({col_list}) VALUES ({placeholders})"

        cursor.execute(sql, vals)
        _commit()

        flash(f'Assignment "{title}" created! Students can now submit.', 'success')
        return redirect(url_for('view_classroom', classroom_id=classroom_id))

    return render_template('assignments/create.html', classroom=classroom)


# ═══════════════════════════════════════════════════════════
#  TEACHER — EDIT ASSIGNMENT
# ═══════════════════════════════════════════════════════════

@assignments_bp.route('/classroom/<int:classroom_id>/assignment/<int:assignment_id>/edit',
                      methods=['GET', 'POST'])
def edit_assignment(classroom_id, assignment_id):
    redir = _require_login('instructor')
    if redir: return redir

    _ensure_assignment_columns()

    cursor = _cursor()
    cursor.execute('SELECT * FROM classrooms WHERE id=%s AND instructor_id=%s',
                   (classroom_id, session['user_id']))
    classroom = cursor.fetchone()
    if not classroom:
        abort(403)

    cursor.execute('SELECT * FROM assignments WHERE id=%s AND classroom_id=%s',
                   (assignment_id, classroom_id))
    assignment = cursor.fetchone()
    if not assignment:
        abort(404)

    if request.method == 'POST':
        title        = _sanitize(request.form.get('title', assignment['title']))
        due_date     = request.form.get('due_date') or None
        max_marks    = int(request.form.get('max_marks', assignment['max_marks']))
        max_attempts = int(request.form.get('max_attempts', 1))
        visibility   = request.form.get('visibility', 'published')
        rubric       = _sanitize(request.form.get('rubric', ''))
        assign_text  = _sanitize(request.form.get('assign_text', ''))
        description  = f"RUBRIC:\n{rubric}\n\nASSIGNMENT:\n{assign_text}"

        # ── Build UPDATE dynamically ──
        set_parts = ['title=%s', 'description=%s', 'due_date=%s', 'max_marks=%s']
        set_vals  = [title,      description,       due_date,      max_marks]

        if _has_col('rubric'):
            set_parts.append('rubric=%s');         set_vals.append(rubric)
        if _has_col('assign_text'):
            set_parts.append('assign_text=%s');    set_vals.append(assign_text)
        if _has_col('max_attempts'):
            set_parts.append('max_attempts=%s');   set_vals.append(max_attempts)
        if _has_col('visibility'):
            set_parts.append('visibility=%s');     set_vals.append(visibility)

        set_vals.append(assignment_id)
        sql = f"UPDATE assignments SET {', '.join(set_parts)} WHERE id=%s"
        cursor.execute(sql, set_vals)
        _commit()

        flash('Assignment updated.', 'success')
        return redirect(url_for('view_classroom', classroom_id=classroom_id))

    return render_template('assignments/edit.html', classroom=classroom, assignment=assignment)


# ═══════════════════════════════════════════════════════════
#  TEACHER — DELETE ASSIGNMENT (soft delete / close)
# ═══════════════════════════════════════════════════════════

@assignments_bp.route('/classroom/<int:classroom_id>/assignment/<int:assignment_id>/delete',
                      methods=['POST'])
def delete_assignment(classroom_id, assignment_id):
    redir = _require_login('instructor')
    if redir: return redir

    cursor = _cursor()
    cursor.execute('SELECT id FROM classrooms WHERE id=%s AND instructor_id=%s',
                   (classroom_id, session['user_id']))
    if not cursor.fetchone():
        return jsonify({'ok': False}), 403

    if _has_col('visibility'):
        cursor.execute("UPDATE assignments SET visibility='closed' WHERE id=%s AND classroom_id=%s",
                       (assignment_id, classroom_id))
    else:
        # Fallback: hard delete if no visibility column
        cursor.execute("DELETE FROM assignments WHERE id=%s AND classroom_id=%s",
                       (assignment_id, classroom_id))
    _commit()
    return jsonify({'ok': True})


# ═══════════════════════════════════════════════════════════
#  TEACHER — SUBMISSION MONITORING DASHBOARD
# ═══════════════════════════════════════════════════════════

@assignments_bp.route('/classroom/<int:classroom_id>/assignment/<int:assignment_id>/submissions')
def assignment_submissions(classroom_id, assignment_id):
    redir = _require_login('instructor')
    if redir: return redir

    cursor = _cursor()
    cursor.execute('SELECT * FROM classrooms WHERE id=%s AND instructor_id=%s',
                   (classroom_id, session['user_id']))
    classroom = cursor.fetchone()
    if not classroom:
        abort(403)

    cursor.execute('SELECT * FROM assignments WHERE id=%s AND classroom_id=%s',
                   (assignment_id, classroom_id))
    assignment = cursor.fetchone()
    if not assignment:
        abort(404)

    cursor.execute(
        '''SELECT s.*, u.full_name AS student_name, u.email AS student_email
           FROM assignment_submissions s
           JOIN users u ON s.student_id = u.id
           WHERE s.assignment_id = %s
           ORDER BY s.submitted_at DESC''',
        (assignment_id,)
    )
    submissions = cursor.fetchall()

    for sub in submissions:
        sub['parsed_criteria'] = _parse_criteria(sub.get('ai_feedback', '') or '')

    return render_template('assignments/submissions.html',
                           classroom=classroom, assignment=assignment,
                           submissions=submissions)


# ═══════════════════════════════════════════════════════════
#  TEACHER — MANUAL GRADE OVERRIDE
# ═══════════════════════════════════════════════════════════

@assignments_bp.route('/assignment/submission/<int:sub_id>/override', methods=['POST'])
def override_submission_grade(sub_id):
    redir = _require_login('instructor')
    if redir: return redir

    data     = request.get_json() or {}
    grade    = data.get('grade')
    feedback = _sanitize(str(data.get('feedback', '')))

    if grade is None:
        return jsonify({'ok': False, 'error': 'grade is required'}), 400

    try:
        grade = float(grade)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'grade must be numeric'}), 400

    grade_label = _grade_label(grade)

    cursor = _cursor()
    cursor.execute(
        '''UPDATE assignment_submissions
           SET teacher_grade=%s, teacher_feedback=%s, teacher_grade_label=%s
           WHERE id=%s''',
        (grade, feedback, grade_label, sub_id)
    )
    _commit()
    return jsonify({'ok': True, 'grade': grade, 'label': grade_label})


# ═══════════════════════════════════════════════════════════
#  TEACHER — ANALYTICS
# ═══════════════════════════════════════════════════════════

@assignments_bp.route('/classroom/<int:classroom_id>/analytics')
def classroom_analytics(classroom_id):
    redir = _require_login('instructor')
    if redir: return redir

    cursor = _cursor()
    cursor.execute('SELECT * FROM classrooms WHERE id=%s AND instructor_id=%s',
                   (classroom_id, session['user_id']))
    classroom = cursor.fetchone()
    if not classroom:
        abort(403)

    cursor.execute(
        '''SELECT s.ai_grade, s.teacher_grade
           FROM assignment_submissions s
           JOIN assignments a ON s.assignment_id = a.id
           WHERE a.classroom_id = %s''',
        (classroom_id,)
    )
    all_subs = cursor.fetchall()
    scores = [row['teacher_grade'] if row['teacher_grade'] is not None else row['ai_grade']
              for row in all_subs if row['teacher_grade'] is not None or row['ai_grade'] is not None]

    grade_dist = {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'F': 0}
    for s in scores:
        grade_dist[_grade_label(s)] = grade_dist.get(_grade_label(s), 0) + 1

    stats = {
        'total_submissions': len(all_subs),
        'graded':            len(scores),
        'pending':           len(all_subs) - len(scores),
        'avg_score':         sum(scores) / len(scores) if scores else 0,
        'grade_dist':        grade_dist,
    }

    cursor.execute(
        '''SELECT a.title, a.id,
              COUNT(s.id) AS sub_count,
              AVG(COALESCE(s.teacher_grade, s.ai_grade)) AS avg_score,
              (SELECT COUNT(*) FROM classroom_members WHERE classroom_id=%s) AS total_students
           FROM assignments a
           LEFT JOIN assignment_submissions s ON a.id = s.assignment_id
           WHERE a.classroom_id = %s
           GROUP BY a.id, a.title''',
        (classroom_id, classroom_id)
    )
    raw_per = cursor.fetchall()
    per_assignment = []
    for r in raw_per:
        total_students = r['total_students'] or 1
        pct = round((r['sub_count'] / total_students) * 100, 1)
        per_assignment.append({
            'id':             r['id'],
            'title':          r['title'],
            'sub_count':      r['sub_count'],
            'avg_score':      r['avg_score'],
            'completion_pct': pct,
        })

    return render_template('assignments/analytics.html',
                           classroom=classroom, stats=stats,
                           per_assignment=per_assignment,
                           classroom_id=classroom_id)


# ═══════════════════════════════════════════════════════════
#  STUDENT — VIEW ASSIGNMENT
# ═══════════════════════════════════════════════════════════

@assignments_bp.route('/classroom/<int:classroom_id>/assignment/<int:assignment_id>')
def view_assignment(classroom_id, assignment_id):
    redir = _require_login('student')
    if redir: return redir

    cursor = _cursor()
    cursor.execute('SELECT id FROM classroom_members WHERE classroom_id=%s AND user_id=%s',
                   (classroom_id, session['user_id']))
    if not cursor.fetchone():
        abort(403)

    cursor.execute('SELECT * FROM classrooms WHERE id=%s', (classroom_id,))
    classroom = cursor.fetchone()

    # Use visibility filter only if column exists
    if _has_col('visibility'):
        cursor.execute(
            "SELECT * FROM assignments WHERE id=%s AND classroom_id=%s AND visibility != 'draft'",
            (assignment_id, classroom_id)
        )
    else:
        cursor.execute(
            "SELECT * FROM assignments WHERE id=%s AND classroom_id=%s",
            (assignment_id, classroom_id)
        )
    assignment = cursor.fetchone()
    if not assignment:
        abort(404)

    cursor.execute(
        'SELECT * FROM assignment_submissions WHERE assignment_id=%s AND student_id=%s',
        (assignment_id, session['user_id'])
    )
    existing = cursor.fetchone()

    return render_template('assignments/student_view.html',
                           classroom=classroom, assignment=assignment,
                           existing_submission=existing,
                           now=datetime.now())


# ═══════════════════════════════════════════════════════════
#  STUDENT — SUBMIT ASSIGNMENT  (notebook LangGraph pipeline hooked in)
#  Pipeline: node_extract → node_relevance_check → node_evaluate → node_lock
# ═══════════════════════════════════════════════════════════

@assignments_bp.route('/classroom/<int:classroom_id>/assignment/<int:assignment_id>/submit',
                      methods=['POST'])
def submit_assignment_v2(classroom_id, assignment_id):
    redir = _require_login('student')
    if redir: return redir

    cursor = _cursor()
    cursor.execute('SELECT id FROM classroom_members WHERE classroom_id=%s AND user_id=%s',
                   (classroom_id, session['user_id']))
    if not cursor.fetchone():
        return jsonify({'ok': False, 'error': 'Not a member of this classroom'}), 403

    cursor.execute('SELECT * FROM assignments WHERE id=%s AND classroom_id=%s',
                   (assignment_id, classroom_id))
    assignment = cursor.fetchone()
    if not assignment:
        return jsonify({'ok': False, 'error': 'Assignment not found'}), 404

    cursor.execute(
        'SELECT id, locked FROM assignment_submissions WHERE assignment_id=%s AND student_id=%s',
        (assignment_id, session['user_id'])
    )
    existing = cursor.fetchone()
    if existing and existing['locked']:
        return jsonify({'ok': False, 'error': 'Already submitted and locked. No resubmission allowed.'}), 400

    file_path_str = None
    github_url    = None

    if 'submission_file' in request.files:
        f = request.files['submission_file']
        if f and f.filename and _allowed(f.filename):
            save_dir = Path(Config.UPLOAD_FOLDER) / 'submissions' / str(classroom_id) / str(session['user_id'])
            save_dir.mkdir(parents=True, exist_ok=True)
            fn = f'{uuid.uuid4().hex}_{secure_filename(f.filename)}'
            fpath = save_dir / fn
            f.save(str(fpath))
            file_path_str = str(fpath)
        else:
            return jsonify({'ok': False, 'error': 'Invalid file type. Upload PDF, DOC, or DOCX.'}), 400
    else:
        data = request.get_json() or {}
        github_url = data.get('github_url', '').strip()
        if not github_url:
            return jsonify({'ok': False, 'error': 'No file or GitHub URL provided'}), 400

    cursor.execute(
        '''INSERT INTO assignment_submissions
           (assignment_id, student_id, filename, file_path, submitted_text, locked)
           VALUES (%s,%s,%s,%s,%s,%s)''',
        (assignment_id, session['user_id'],
         Path(file_path_str).name if file_path_str else github_url,
         file_path_str, github_url or '', 0)
    )
    sub_id = cursor.lastrowid
    _commit()

    # Extract rubric — try dedicated column, fall back to description field
    raw_desc = assignment.get('description', '') or ''
    if 'RUBRIC:' in raw_desc.upper():
        rubric_extracted = raw_desc.split('ASSIGNMENT:')[0].replace('RUBRIC:', '').strip()
    else:
        rubric_extracted = assignment.get('rubric', '') or raw_desc

    rubric_parts = [rubric_extracted] if rubric_extracted else []
    rubric_parts.append(f"Title: {assignment['title']}")
    rubric_parts.append(f"Max marks: {assignment['max_marks']}")
    rubric = _sanitize('\n'.join(rubric_parts))

    student_id_str = str(session['user_id'])
    course_id_str  = str(classroom_id)

    def _grade_thread():
        """
        Background thread — runs notebook LangGraph pipeline:
        node_extract → node_relevance_check → node_evaluate → node_lock
        """
        db = None
        try:
            from ai_engine import evaluate_assignment, evaluate_project

            if file_path_str:
                result = evaluate_assignment(
                    submission_pdf=file_path_str,
                    rubric=rubric,
                    course_id=course_id_str,
                    student_id=student_id_str
                )
                score    = float(result.get('score', 0))
                feedback = _sanitize(str(result.get('feedback', '') or result.get('evaluation', '')))
            else:
                result = evaluate_project(
                    repo_url=github_url,
                    project_rubric=rubric,
                    project_details=assignment.get('description', ''),
                    student_id=student_id_str,
                    course_id=course_id_str
                )
                score    = float(result.get('score', 0))
                feedback = _sanitize(str(result.get('analysis', '')))

            grade = _grade_label(score)
            db = _db()
            c  = db.cursor()
            c.execute(
                '''UPDATE assignment_submissions
                   SET ai_grade=%s, ai_grade_label=%s, ai_feedback=%s, locked=1
                   WHERE id=%s''',
                (score, grade, feedback[:65000], sub_id)
            )
            db.commit()
        except Exception as exc:
            try:
                if db is None:
                    db = _db()
                c2 = db.cursor()
                err_msg = _sanitize(str(exc))[:2000]
                c2.execute(
                    "UPDATE assignment_submissions SET ai_feedback=%s WHERE id=%s",
                    (f'GRADING ERROR: {err_msg}', sub_id)
                )
                db.commit()
            except Exception:
                pass
        finally:
            if db:
                try: db.close()
                except Exception: pass

    t = threading.Thread(target=_grade_thread, daemon=True)
    t.start()

    return jsonify({
        'ok':     True,
        'sub_id': sub_id,
        'message': 'Submission received! AI is now evaluating your work (30–90 seconds).'
    })


# ═══════════════════════════════════════════════════════════
#  AJAX — Poll grading status
# ═══════════════════════════════════════════════════════════

@assignments_bp.route('/submission/<int:sub_id>/status')
def submission_status(sub_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    cursor = _cursor()
    cursor.execute(
        'SELECT ai_grade, ai_grade_label, locked, ai_feedback FROM assignment_submissions WHERE id=%s',
        (sub_id,)
    )
    row = cursor.fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404

    graded = row['locked'] == 1
    error  = bool(row.get('ai_feedback', '') and
                  str(row.get('ai_feedback', '')).startswith('GRADING ERROR'))
    return jsonify({
        'graded': graded,
        'error':  error,
        'score':  row['ai_grade'],
        'label':  row['ai_grade_label'],
    })


# ═══════════════════════════════════════════════════════════
#  STUDENT — SUBMISSION RESULT PAGE
# ═══════════════════════════════════════════════════════════

@assignments_bp.route('/submission/<int:sub_id>/result')
def submission_result(sub_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    cursor = _cursor()
    cursor.execute(
        '''SELECT s.*, a.title AS assignment_title, a.description AS assignment_desc,
                  a.classroom_id, a.max_marks, u.full_name
           FROM assignment_submissions s
           JOIN assignments a ON s.assignment_id = a.id
           JOIN users u ON s.student_id = u.id
           WHERE s.id=%s''',
        (sub_id,)
    )
    sub = cursor.fetchone()
    if not sub:
        flash('Submission not found.', 'danger')
        return redirect(url_for('dashboard'))

    if sub['student_id'] != session['user_id'] and session.get('role') != 'instructor':
        abort(403)

    feedback    = sub.get('ai_feedback', '') or ''
    criteria    = _parse_criteria(feedback)
    suggestions = _parse_suggestions(feedback)
    strengths   = _extract_section(feedback, 'STRENGTHS',
                                   ['WEAKNESSES', 'IMPROVEMENTSUGGESTIONS',
                                    'IMPROVEMENT_SUGGESTIONS', 'DETAILEDFEEDBACK', 'DETAILED_FEEDBACK'])
    weaknesses  = _extract_section(feedback, 'WEAKNESSES',
                                   ['IMPROVEMENTSUGGESTIONS', 'IMPROVEMENT_SUGGESTIONS',
                                    'DETAILEDFEEDBACK', 'DETAILED_FEEDBACK'])
    detailed    = _extract_section(feedback, 'DETAILEDFEEDBACK', [])
    if not detailed:
        detailed = _extract_section(feedback, 'DETAILED_FEEDBACK', [])

    return render_template('assignments/result.html',
                           sub=sub,
                           feedback=feedback,
                           criteria=criteria,
                           suggestions=suggestions,
                           strengths=strengths,
                           weaknesses=weaknesses,
                           detailed=detailed,
                           classroom_id=sub['classroom_id'])


# ═══════════════════════════════════════════════════════════
#  LEADERBOARD  (per-assignment, visible to both students and teachers)
# ═══════════════════════════════════════════════════════════

@assignments_bp.route('/classroom/<int:classroom_id>/assignment/<int:assignment_id>/leaderboard')
def assignment_leaderboard(classroom_id, assignment_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    is_teacher = session.get('role') == 'instructor'
    cursor = _cursor()

    cursor.execute('SELECT * FROM classrooms WHERE id=%s', (classroom_id,))
    classroom = cursor.fetchone()
    if not classroom:
        abort(404)

    cursor.execute('SELECT * FROM assignments WHERE id=%s AND classroom_id=%s',
                   (assignment_id, classroom_id))
    assignment = cursor.fetchone()
    if not assignment:
        abort(404)

    cursor.execute(
        '''SELECT s.student_id, u.full_name,
              COALESCE(s.teacher_grade, s.ai_grade) AS final_score,
              COALESCE(s.teacher_grade_label, s.ai_grade_label) AS grade_label
           FROM assignment_submissions s
           JOIN users u ON s.student_id = u.id
           WHERE s.assignment_id=%s AND s.locked=1
           ORDER BY final_score DESC''',
        (assignment_id,)
    )
    rows = cursor.fetchall()

    leaderboard = []
    for rank, r in enumerate(rows, 1):
        is_you = r['student_id'] == session['user_id']
        score  = round(float(r['final_score'] or 0), 1)
        if is_teacher or is_you:
            display_name = r['full_name']
        else:
            display_name = f'Student #{rank:03d}'
        medal = {1: '🥇', 2: '🥈', 3: '🥉'}.get(rank, str(rank))
        leaderboard.append({
            'rank':         rank,
            'medal':        medal,
            'display_name': display_name,
            'score':        score,
            'grade':        r['grade_label'] or _grade_label(score),
            'is_you':       is_you,
        })

    return render_template('assignments/leaderboard.html',
                           classroom=classroom,
                           assignment=assignment,
                           leaderboard=leaderboard,
                           classroom_id=classroom_id,
                           is_teacher=is_teacher)
