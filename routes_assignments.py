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


# ═══════════════════════════════════════════════════════════
#  TEACHER — CREATE ASSIGNMENT
# ═══════════════════════════════════════════════════════════

@assignments_bp.route('/classroom/<int:classroom_id>/assignment/create', methods=['GET', 'POST'])
def create_assignment(classroom_id):
    redir = _require_login('instructor')
    if redir: return redir

    cursor = _cursor()
    cursor.execute('SELECT * FROM classrooms WHERE id=%s AND instructor_id=%s',
                   (classroom_id, session['user_id']))
    classroom = cursor.fetchone()
    if not classroom:
        flash('Classroom not found.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    if request.method == 'POST':
        title         = _sanitize(request.form.get('title', '').strip())
        due_date      = request.form.get('due_date') or None
        max_marks     = int(request.form.get('max_marks', 100))
        max_attempts  = int(request.form.get('max_attempts', 1))
        visibility    = request.form.get('visibility', 'published')
        rubric        = _sanitize(request.form.get('rubric', '').strip())
        assign_text   = _sanitize(request.form.get('assign_text', '').strip())
        source_label  = request.form.get('source_label', 'text')
        ai_model      = request.form.get('ai_model', 'auto')
        strictness    = request.form.get('strictness', 'balanced')
        feedback_style = request.form.get('feedback_style', 'detailed')

        if not title:
            flash('Title is required.', 'danger')
            return render_template('assignments/create.html', classroom=classroom)

        # Handle file upload
        if source_label == 'file' and 'assign_file' in request.files:
            f = request.files['assign_file']
            if f and f.filename and _allowed(f.filename):
                save_dir = Path(Config.UPLOAD_FOLDER) / 'assignments' / str(classroom_id)
                save_dir.mkdir(parents=True, exist_ok=True)
                fn = f'{uuid.uuid4().hex}_{secure_filename(f.filename)}'
                f.save(str(save_dir / fn))
                assign_text = f'[FILE: {fn}]'

        cursor.execute(
            '''INSERT INTO assignments
               (classroom_id, title, description, rubric, assign_text, source_label,
                due_date, max_marks, max_attempts, visibility, ai_model, strictness, feedback_style)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (classroom_id, title, assign_text, rubric, assign_text, source_label,
             due_date, max_marks, max_attempts, visibility, ai_model, strictness, feedback_style)
        )
        _commit()
        flash(f'Assignment "{title}" created!', 'success')
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

        cursor.execute(
            '''UPDATE assignments SET title=%s, description=%s, rubric=%s, assign_text=%s,
               due_date=%s, max_marks=%s, max_attempts=%s, visibility=%s WHERE id=%s''',
            (title, assign_text, rubric, assign_text, due_date,
             max_marks, max_attempts, visibility, assignment_id)
        )
        _commit()
        flash('Assignment updated.', 'success')
        return redirect(url_for('view_classroom', classroom_id=classroom_id))

    return render_template('assignments/edit.html', classroom=classroom, assignment=assignment)


# ═══════════════════════════════════════════════════════════
#  TEACHER — DELETE ASSIGNMENT (soft delete)
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

    cursor.execute("UPDATE assignments SET visibility='closed' WHERE id=%s AND classroom_id=%s",
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
        '''SELECT s.*, u.full_name AS student_name
           FROM assignment_submissions s
           JOIN users u ON s.student_id = u.id
           WHERE s.assignment_id = %s
           ORDER BY s.submitted_at DESC''',
        (assignment_id,)
    )
    submissions = cursor.fetchall()

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

    cursor = _cursor()
    cursor.execute(
        'UPDATE assignment_submissions SET teacher_grade=%s, teacher_feedback=%s WHERE id=%s',
        (grade, feedback, sub_id)
    )
    _commit()
    return jsonify({'ok': True})


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
            'title':          r['title'],
            'sub_count':      r['sub_count'],
            'avg_score':      r['avg_score'],
            'completion_pct': pct,
        })

    return render_template('assignments/analytics.html',
                           classroom=classroom, stats=stats, per_assignment=per_assignment)


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
    cursor.execute('SELECT * FROM assignments WHERE id=%s AND classroom_id=%s AND visibility != %s',
                   (assignment_id, classroom_id, 'draft'))
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
#  STUDENT — SUBMIT ASSIGNMENT (extended with lock + RAG eval)
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
        return jsonify({'ok': False, 'error': 'Not a member'}), 403

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
        return jsonify({'ok': False, 'error': 'Already submitted and locked'}), 400

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

    # Build rubric string
    rubric_parts = []
    if assignment.get('rubric'):
        rubric_parts.append(assignment['rubric'])
    rubric_parts.append(f"Title: {assignment['title']}")
    if assignment.get('description'):
        rubric_parts.append(f"Description: {assignment['description']}")
    rubric_parts.append(f"Max marks: {assignment['max_marks']}")
    rubric = _sanitize('\n'.join(rubric_parts))

    student_id_str = str(session['user_id'])
    course_id_str  = str(classroom_id)

    def _grade_thread():
        try:
            from ai_engine import evaluate_assignment, evaluate_project
            if file_path_str:
                result = evaluate_assignment(
                    submission_pdf=file_path_str,
                    rubric=rubric,
                    course_id=course_id_str,
                    student_id=student_id_str,
                )
                score    = int(result.get('score', 0))
                feedback = _sanitize(str(result.get('feedback', '')))
                flags    = _sanitize(str(result.get('relevance_flags', '')))
            else:
                result = evaluate_project(
                    repo_url=github_url,
                    project_rubric=rubric,
                    project_details=assignment.get('description', ''),
                    student_id=student_id_str,
                    classroom_id=classroom_id,
                )
                score    = int(result.get('score', 0))
                feedback = _sanitize(str(result.get('analysis', '')))
                flags    = ''

            conn = _db()
            c    = conn.cursor()
            c.execute(
                '''UPDATE assignment_submissions
                   SET ai_grade=%s, ai_feedback=%s, relevance_flags=%s, locked=1
                   WHERE id=%s''',
                (score, feedback, flags, sub_id)
            )
            conn.commit()
            conn.close()
            print(f'[Assign] Sub {sub_id} graded: {score}')
        except Exception as e:
            print(f'[Assign] Grading failed: {e}')
            import traceback; traceback.print_exc()

    threading.Thread(target=_grade_thread, daemon=True).start()
    return jsonify({'ok': True, 'message': 'Submitted! AI is evaluating. Check results in ~60 seconds.'})


# ═══════════════════════════════════════════════════════════
#  STUDENT — VIEW ASSIGNMENT RESULT
# ═══════════════════════════════════════════════════════════

@assignments_bp.route('/classroom/<int:classroom_id>/assignment/<int:assignment_id>/result')
def assignment_result(classroom_id, assignment_id):
    redir = _require_login('student')
    if redir: return redir

    cursor = _cursor()
    cursor.execute(
        '''SELECT s.*, a.title AS assignment_title
           FROM assignment_submissions s
           JOIN assignments a ON s.assignment_id = a.id
           WHERE s.assignment_id=%s AND s.student_id=%s''',
        (assignment_id, session['user_id'])
    )
    submission = cursor.fetchone()
    if not submission:
        flash('Submission not found.', 'danger')
        return redirect(url_for('view_classroom', classroom_id=classroom_id))

    teacher_override = submission.get('teacher_grade')
    return render_template('assignments/result.html',
                           submission=submission,
                           classroom_id=classroom_id,
                           teacher_override=teacher_override)


# ═══════════════════════════════════════════════════════════
#  STUDENT — GRADES DASHBOARD
# ═══════════════════════════════════════════════════════════

@assignments_bp.route('/student/grades')
def student_grades():
    redir = _require_login('student')
    if redir: return redir

    cursor = _cursor()
    cursor.execute(
        '''SELECT s.*, a.title AS assignment_title, c.name AS classroom_name,
                  c.id AS classroom_id, a.id AS assignment_id
           FROM assignment_submissions s
           JOIN assignments a ON s.assignment_id = a.id
           JOIN classrooms c  ON a.classroom_id  = c.id
           WHERE s.student_id = %s
           ORDER BY s.submitted_at DESC''',
        (session['user_id'],)
    )
    assignment_subs = cursor.fetchall()

    cursor.execute(
        '''SELECT s.*, p.title AS project_title, c.name AS classroom_name, c.id AS classroom_id
           FROM project_submissions s
           JOIN projects p ON s.project_id = p.id
           JOIN classrooms c ON p.classroom_id = c.id
           WHERE s.student_id = %s
           ORDER BY s.submitted_at DESC''',
        (session['user_id'],)
    )
    project_subs = cursor.fetchall()

    all_submissions = assignment_subs + project_subs
    return render_template('assignments/grades.html',
                           assignment_subs=assignment_subs,
                           project_subs=project_subs,
                           all_submissions=all_submissions)


# ═══════════════════════════════════════════════════════════
#  STUDENT — PROJECT ADVISORY (does NOT lock)
# ═══════════════════════════════════════════════════════════

@assignments_bp.route('/classroom/<int:classroom_id>/project/<int:project_id>/advisory',
                      methods=['GET', 'POST'])
def project_advisory(classroom_id, project_id):
    redir = _require_login('student')
    if redir: return redir

    cursor = _cursor()
    cursor.execute('SELECT id FROM classroom_members WHERE classroom_id=%s AND user_id=%s',
                   (classroom_id, session['user_id']))
    if not cursor.fetchone():
        abort(403)

    cursor.execute('SELECT * FROM classrooms WHERE id=%s', (classroom_id,))
    classroom = cursor.fetchone()
    cursor.execute('SELECT * FROM projects WHERE id=%s AND classroom_id=%s', (project_id, classroom_id))
    project = cursor.fetchone()
    if not project:
        abort(404)

    analysis = None
    if request.method == 'POST':
        repo_url = request.form.get('repo_url', '').strip()
        if repo_url:
            from ai_engine import analyze_project_advisory
            result   = analyze_project_advisory(
                repo_url=repo_url,
                project_rubric=project.get('rubric', '') or project.get('description', ''),
                student_id=str(session['user_id']),
                classroom_id=classroom_id,
                project_details=project.get('project_details', '') or project.get('description', ''),
            )
            analysis = result.get('analysis', 'Analysis failed. Try again.')

    return render_template('assignments/project_advisory.html',
                           classroom=classroom, project=project, analysis=analysis)


# ═══════════════════════════════════════════════════════════
#  POLL — submission grading status
# ═══════════════════════════════════════════════════════════

@assignments_bp.route('/assignment/submission/<int:sub_id>/status')
def submission_status(sub_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    cursor = _cursor()
    cursor.execute(
        'SELECT ai_grade, locked FROM assignment_submissions WHERE id=%s AND student_id=%s',
        (sub_id, session['user_id'])
    )
    row = cursor.fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'graded': row['ai_grade'] is not None, 'locked': bool(row['locked'])})
