-- migrate_assignments.sql
-- Run once to add/update assignment tables.
-- Safe to re-run (uses IF NOT EXISTS and ALTER IGNORE).

CREATE TABLE IF NOT EXISTS assignments (
    id             INT AUTO_INCREMENT PRIMARY KEY,
    classroom_id   INT NOT NULL,
    title          VARCHAR(255) NOT NULL,
    description    TEXT,
    rubric         TEXT NOT NULL,
    assign_text    TEXT,
    source_label   VARCHAR(50)  DEFAULT 'text',
    due_date       DATE,
    max_marks      INT          DEFAULT 100,
    max_attempts   INT          DEFAULT 1,
    visibility     ENUM('draft','published','closed') DEFAULT 'published',
    ai_model       VARCHAR(50)  DEFAULT 'auto',
    strictness     VARCHAR(20)  DEFAULT 'balanced',
    feedback_style VARCHAR(20)  DEFAULT 'detailed',
    created_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (classroom_id) REFERENCES classrooms(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS assignment_submissions (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    assignment_id       INT NOT NULL,
    student_id          INT NOT NULL,
    filename            VARCHAR(255),
    file_path           VARCHAR(600),
    submitted_text      TEXT,
    -- AI grading output (LangGraph pipeline: extract→relevance→evaluate→lock)
    ai_grade            FLOAT,
    ai_grade_label      VARCHAR(2),
    -- ai_feedback stores the RAW notebook output:
    --   CRITERIONBREAKDOWN / SCORE / GRADE / STRENGTHS / WEAKNESSES /
    --   IMPROVEMENTSUGGESTIONS / DETAILEDFEEDBACK
    ai_feedback         MEDIUMTEXT,
    -- Teacher override
    teacher_grade       FLOAT,
    teacher_grade_label VARCHAR(2),
    teacher_feedback    TEXT,
    -- Lock flag: 1 after LangGraph node_lock fires (no resubmission)
    locked              TINYINT(1) DEFAULT 0,
    submitted_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_student_assignment (student_id, assignment_id),
    FOREIGN KEY (assignment_id) REFERENCES assignments(id) ON DELETE CASCADE,
    FOREIGN KEY (student_id)    REFERENCES users(id)
);
