from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
import uuid
import json
import os
import hashlib
from datetime import datetime
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "neuroclass-dev-secret-2024")

# ── In-memory data stores (replace with Supabase/DB later) ──────────────────────────────────
USERS = {}       # {user_id: {id, name, email, password_hash, role}}
CLASSES = {}     # {class_id: {id, code, name, subject, teacher_id, students: []}}
MEMBERSHIPS = {} # {user_id: [class_id, ...]}

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def generate_class_code() -> str:
    return uuid.uuid4().hex[:7].upper()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    uid = session.get("user_id")
    return USERS.get(uid)

# ── Auth routes ───────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "user_id" in session:
        user = get_current_user()
        if user and user["role"] == "instructor":
            return redirect(url_for("instructor_dashboard"))
        return redirect(url_for("student_dashboard"))
    return render_template("landing.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "student")
        pw_hash = hash_password(password)
        user = next(
            (u for u in USERS.values()
             if u["email"] == email and u["password_hash"] == pw_hash and u["role"] == role),
            None
        )
        if user:
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            flash("Welcome back, " + user["name"] + "!", "success")
            return redirect(url_for("instructor_dashboard" if role == "instructor" else "student_dashboard"))
        flash("Invalid credentials or role. Please check your details.", "error")
    return render_template("auth.html", mode="login")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name  = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role  = request.form.get("role", "student")
        if any(u["email"] == email for u in USERS.values()):
            flash("An account with that email already exists.", "error")
            return render_template("auth.html", mode="register")
        uid = str(uuid.uuid4())
        USERS[uid] = {
            "id": uid, "name": name, "email": email,
            "password_hash": hash_password(password),
            "role": role, "created_at": datetime.utcnow().isoformat()
        }
        MEMBERSHIPS[uid] = []
        session["user_id"] = uid
        session["role"] = role
        flash("Account created! Welcome to NeuroClass.", "success")
        return redirect(url_for("instructor_dashboard" if role == "instructor" else "student_dashboard"))
    return render_template("auth.html", mode="register")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# ── Instructor routes ────────────────────────────────────────────────────────────────────

@app.route("/instructor/dashboard")
@login_required
def instructor_dashboard():
    user = get_current_user()
    if user["role"] != "instructor":
        return redirect(url_for("student_dashboard"))
    my_classes = [c for c in CLASSES.values() if c["teacher_id"] == user["id"]]
    return render_template("instructor_dashboard.html", user=user, classes=my_classes)

@app.route("/instructor/create-class", methods=["POST"])
@login_required
def create_class():
    user = get_current_user()
    if user["role"] != "instructor":
        return jsonify({"error": "Unauthorized"}), 403
    name    = request.form.get("name", "").strip()
    subject = request.form.get("subject", "").strip()
    if not name:
        flash("Class name is required.", "error")
        return redirect(url_for("instructor_dashboard"))
    cid  = str(uuid.uuid4())
    code = generate_class_code()
    while any(c["code"] == code for c in CLASSES.values()):
        code = generate_class_code()
    CLASSES[cid] = {
        "id": cid, "code": code, "name": name, "subject": subject,
        "teacher_id": user["id"], "teacher_name": user["name"],
        "students": [], "created_at": datetime.utcnow().isoformat()
    }
    if cid not in MEMBERSHIPS:
        MEMBERSHIPS[cid] = []
    flash(f"Classroom '{name}' created! Share code: {code}", "success")
    return redirect(url_for("instructor_dashboard"))

@app.route("/instructor/class/<class_id>")
@login_required
def instructor_class_detail(class_id):
    user = get_current_user()
    cls  = CLASSES.get(class_id)
    if not cls or cls["teacher_id"] != user["id"]:
        flash("Classroom not found.", "error")
        return redirect(url_for("instructor_dashboard"))
    students = [USERS[sid] for sid in cls["students"] if sid in USERS]
    return render_template("instructor_class.html", user=user, cls=cls, students=students)

# ── Student routes ─────────────────────────────────────────────────────────────────────

@app.route("/student/dashboard")
@login_required
def student_dashboard():
    user = get_current_user()
    if user["role"] != "student":
        return redirect(url_for("instructor_dashboard"))
    my_class_ids = MEMBERSHIPS.get(user["id"], [])
    my_classes = [CLASSES[cid] for cid in my_class_ids if cid in CLASSES]
    return render_template("student_dashboard.html", user=user, classes=my_classes)

@app.route("/student/join-class", methods=["POST"])
@login_required
def join_class():
    user = get_current_user()
    if user["role"] != "student":
        return jsonify({"error": "Unauthorized"}), 403
    code = request.form.get("code", "").strip().upper()
    cls  = next((c for c in CLASSES.values() if c["code"] == code), None)
    if not cls:
        flash("Invalid classroom code. Please try again.", "error")
        return redirect(url_for("student_dashboard"))
    if user["id"] in cls["students"]:
        flash("You are already enrolled in this classroom.", "info")
        return redirect(url_for("student_dashboard"))
    cls["students"].append(user["id"])
    if user["id"] not in MEMBERSHIPS:
        MEMBERSHIPS[user["id"]] = []
    MEMBERSHIPS[user["id"]].append(cls["id"])
    flash(f"Successfully joined '{cls['name']}'!", "success")
    return redirect(url_for("student_dashboard"))

@app.route("/student/class/<class_id>")
@login_required
def student_class_detail(class_id):
    user = get_current_user()
    cls  = CLASSES.get(class_id)
    if not cls or user["id"] not in cls["students"]:
        flash("Classroom not found or you are not enrolled.", "error")
        return redirect(url_for("student_dashboard"))
    teacher = USERS.get(cls["teacher_id"])
    return render_template("student_class.html", user=user, cls=cls, teacher=teacher)

# ── API helpers ────────────────────────────────────────────────────────────────────────

@app.route("/api/class/<class_id>/students")
@login_required
def api_class_students(class_id):
    user = get_current_user()
    cls  = CLASSES.get(class_id)
    if not cls or cls["teacher_id"] != user["id"]:
        return jsonify({"error": "Unauthorized"}), 403
    students = [{"id": USERS[s]["id"], "name": USERS[s]["name"], "email": USERS[s]["email"]}
                for s in cls["students"] if s in USERS]
    return jsonify({"students": students, "count": len(students)})

if __name__ == "__main__":
    app.run(debug=True, port=5000)
