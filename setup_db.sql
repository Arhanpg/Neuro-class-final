-- NeuroClass Database Setup
-- Safe to run multiple times (uses IF NOT EXISTS everywhere)
-- Run: mysql -u root -p < setup_db.sql

CREATE DATABASE IF NOT EXISTS neuroclass CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE neuroclass;

-- Force the connection to use utf8mb4 so emoji / 4-byte characters are accepted
SET NAMES utf8mb4;
SET CHARACTER SET utf8mb4;
SET character_set_connection = utf8mb4;

-- Users
CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    full_name VARCHAR(120) NOT NULL,
    email VARCHAR(120) NOT NULL UNIQUE,
    password_hash VARCHAR(64) NOT NULL,
    role ENUM('student', 'instructor') NOT NULL DEFAULT 'student',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Classrooms
CREATE TABLE IF NOT EXISTS classrooms (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    subject VARCHAR(120),
    description TEXT,
    code VARCHAR(12) NOT NULL UNIQUE,
    instructor_id INT NOT NULL,
    rag_indexed TINYINT(1) DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (instructor_id) REFERENCES users(id) ON DELETE CASCADE
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Add rag_indexed if upgrading from old schema
SET @dbname = DATABASE();
SET @tname = 'classrooms';
SET @cname = 'rag_indexed';
SET @exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @dbname AND TABLE_NAME = @tname AND COLUMN_NAME = @cname
);
SET @query = IF(@exists = 0,
    'ALTER TABLE classrooms ADD COLUMN rag_indexed TINYINT(1) DEFAULT 0',
    'SELECT 1'
);
PREPARE stmt FROM @query;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Classroom membership
CREATE TABLE IF NOT EXISTS classroom_members (
    id INT AUTO_INCREMENT PRIMARY KEY,
    classroom_id INT NOT NULL,
    user_id INT NOT NULL,
    joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_member (classroom_id, user_id),
    FOREIGN KEY (classroom_id) REFERENCES classrooms(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Lecture materials (PDFs uploaded by instructor)
CREATE TABLE IF NOT EXISTS lecture_materials (
    id INT AUTO_INCREMENT PRIMARY KEY,
    classroom_id INT NOT NULL,
    filename VARCHAR(255) NOT NULL,
    original_name VARCHAR(255) NOT NULL,
    file_path VARCHAR(512) NOT NULL,
    uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (classroom_id) REFERENCES classrooms(id) ON DELETE CASCADE
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Chat history — MUST be utf8mb4 to store emoji in AI responses
CREATE TABLE IF NOT EXISTS chat_history (
    id INT AUTO_INCREMENT PRIMARY KEY,
    classroom_id INT NOT NULL,
    user_id INT NOT NULL,
    role ENUM('user', 'assistant') NOT NULL,
    message MEDIUMTEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (classroom_id) REFERENCES classrooms(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Fix existing chat_history column charset if table already existed with wrong charset
ALTER TABLE chat_history
    CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Assignments posted by instructor
CREATE TABLE IF NOT EXISTS assignments (
    id INT AUTO_INCREMENT PRIMARY KEY,
    classroom_id INT NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    due_date DATETIME,
    max_marks INT DEFAULT 100,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (classroom_id) REFERENCES classrooms(id) ON DELETE CASCADE
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Assignment submissions by students
CREATE TABLE IF NOT EXISTS assignment_submissions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    assignment_id INT NOT NULL,
    student_id INT NOT NULL,
    filename VARCHAR(255),
    file_path VARCHAR(512),
    submitted_text MEDIUMTEXT,
    ai_grade INT,
    ai_feedback MEDIUMTEXT,
    teacher_grade INT,
    submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_submission (assignment_id, student_id),
    FOREIGN KEY (assignment_id) REFERENCES assignments(id) ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE CASCADE
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Projects posted by instructor
CREATE TABLE IF NOT EXISTS projects (
    id INT AUTO_INCREMENT PRIMARY KEY,
    classroom_id INT NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    due_date DATETIME,
    max_marks INT DEFAULT 100,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (classroom_id) REFERENCES classrooms(id) ON DELETE CASCADE
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Project submissions (GitHub repo links) by students
CREATE TABLE IF NOT EXISTS project_submissions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    project_id INT NOT NULL,
    student_id INT NOT NULL,
    github_url VARCHAR(512) NOT NULL,
    ai_grade INT,
    ai_feedback MEDIUMTEXT,
    teacher_grade INT,
    submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_proj_submission (project_id, student_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE CASCADE
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- assignments: add rubric column
SET @dbname = DATABASE();
SET @exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @dbname AND TABLE_NAME = 'assignments' AND COLUMN_NAME = 'rubric'
);
SET @query = IF(@exists = 0,
    'ALTER TABLE assignments ADD COLUMN rubric TEXT',
    'SELECT "rubric already exists" AS info'
);
PREPARE stmt FROM @query;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- assignments: add visibility column
SET @dbname = DATABASE();
SET @exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @dbname AND TABLE_NAME = 'assignments' AND COLUMN_NAME = 'visibility'
);
SET @query = IF(@exists = 0,
    'ALTER TABLE assignments ADD COLUMN visibility ENUM(''draft'',''published'',''closed'') DEFAULT ''published''',
    'SELECT "visibility already exists" AS info'
);
PREPARE stmt FROM @query;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- assignments: add max_attempts column
SET @dbname = DATABASE();
SET @exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @dbname AND TABLE_NAME = 'assignments' AND COLUMN_NAME = 'max_attempts'
);
SET @query = IF(@exists = 0,
    'ALTER TABLE assignments ADD COLUMN max_attempts INT DEFAULT 1',
    'SELECT "max_attempts already exists" AS info'
);
PREPARE stmt FROM @query;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- assignment_submissions: add locked column
SET @dbname = DATABASE();
SET @exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @dbname AND TABLE_NAME = 'assignment_submissions' AND COLUMN_NAME = 'locked'
);
SET @query = IF(@exists = 0,
    'ALTER TABLE assignment_submissions ADD COLUMN locked TINYINT(1) DEFAULT 0',
    'SELECT "locked already exists" AS info'
);
PREPARE stmt FROM @query;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- assignment_submissions: add evaluation_detail column
SET @dbname = DATABASE();
SET @exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @dbname AND TABLE_NAME = 'assignment_submissions' AND COLUMN_NAME = 'evaluation_detail'
);
SET @query = IF(@exists = 0,
    'ALTER TABLE assignment_submissions ADD COLUMN evaluation_detail MEDIUMTEXT',
    'SELECT "evaluation_detail already exists" AS info'
);
PREPARE stmt FROM @query;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- project_submissions: add locked column
SET @dbname = DATABASE();
SET @exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @dbname AND TABLE_NAME = 'project_submissions' AND COLUMN_NAME = 'locked'
);
SET @query = IF(@exists = 0,
    'ALTER TABLE project_submissions ADD COLUMN locked TINYINT(1) DEFAULT 0',
    'SELECT "locked already exists" AS info'
);
PREPARE stmt FROM @query;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- project_submissions: add rejected column
SET @dbname = DATABASE();
SET @exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @dbname AND TABLE_NAME = 'project_submissions' AND COLUMN_NAME = 'rejected'
);
SET @query = IF(@exists = 0,
    'ALTER TABLE project_submissions ADD COLUMN rejected TINYINT(1) DEFAULT 0',
    'SELECT "rejected already exists" AS info'
);
PREPARE stmt FROM @query;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SELECT 'Database setup complete!' AS status;
