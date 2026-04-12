-- Assignment Management System - Migration
-- Run this against your MySQL database

ALTER TABLE assignments
  ADD COLUMN IF NOT EXISTS rubric TEXT DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS assign_text LONGTEXT DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS source_label VARCHAR(50) DEFAULT 'text',
  ADD COLUMN IF NOT EXISTS visibility ENUM('draft','published','closed') DEFAULT 'published',
  ADD COLUMN IF NOT EXISTS max_attempts INT DEFAULT 1,
  ADD COLUMN IF NOT EXISTS ai_model VARCHAR(50) DEFAULT 'auto',
  ADD COLUMN IF NOT EXISTS strictness ENUM('lenient','balanced','strict') DEFAULT 'balanced',
  ADD COLUMN IF NOT EXISTS feedback_style ENUM('brief','detailed','with_suggestions') DEFAULT 'detailed';

ALTER TABLE assignment_submissions
  ADD COLUMN IF NOT EXISTS ai_feedback LONGTEXT DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS teacher_feedback TEXT DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS locked TINYINT(1) DEFAULT 0,
  ADD COLUMN IF NOT EXISTS attempts INT DEFAULT 1,
  ADD COLUMN IF NOT EXISTS relevance_flags TEXT DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS criterion_breakdown LONGTEXT DEFAULT NULL;

ALTER TABLE projects
  ADD COLUMN IF NOT EXISTS rubric TEXT DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS project_details LONGTEXT DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS visibility ENUM('draft','published','closed') DEFAULT 'published';

ALTER TABLE project_submissions
  ADD COLUMN IF NOT EXISTS ai_feedback LONGTEXT DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS teacher_feedback TEXT DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS locked TINYINT(1) DEFAULT 0,
  ADD COLUMN IF NOT EXISTS rejected TINYINT(1) DEFAULT 0,
  ADD COLUMN IF NOT EXISTS rejection_type VARCHAR(50) DEFAULT NULL;

CREATE TABLE IF NOT EXISTS query_history (
  id INT AUTO_INCREMENT PRIMARY KEY,
  student_id INT NOT NULL,
  classroom_id INT NOT NULL,
  question TEXT NOT NULL,
  answer LONGTEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY (classroom_id) REFERENCES classrooms(id) ON DELETE CASCADE
);
