-- NeuroClass MIGRATION SCRIPT
-- Run this if you already have the old database and just need the new tables
-- mysql -u root -p neuroclass < migrate.sql

USE neuroclass;

-- Add rag_indexed column to classrooms if missing
SET @dbname = DATABASE();
SET @exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @dbname AND TABLE_NAME = 'classrooms' AND COLUMN_NAME = 'rag_indexed'
);
SET @query = IF(@exists = 0,
    'ALTER TABLE classrooms ADD COLUMN rag_indexed TINYINT(1) DEFAULT 0',
    'SELECT "rag_indexed column already exists" AS info'
);
PREPARE stmt FROM @query;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- lecture_materials
CREATE TABLE IF NOT EXISTS lecture_materials (
    id INT AUTO_INCREMENT PRIMARY KEY,
    classroom_id INT NOT NULL,
    filename VARCHAR(255) NOT NULL,
    original_name VARCHAR(255) NOT NULL,
    file_path VARCHAR(512) NOT NULL,
    uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (classroom_id) REFERENCES classrooms(id) ON DELETE CASCADE
);

-- chat_history
CREATE TABLE IF NOT EXISTS chat_history (
    id INT AUTO_INCREMENT PRIMARY KEY,
    classroom_id INT NOT NULL,
    user_id INT NOT NULL,
    role ENUM('user', 'assistant') NOT NULL,
    message TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (classroom_id) REFERENCES classrooms(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- assignments
CREATE TABLE IF NOT EXISTS assignments (
    id INT AUTO_INCREMENT PRIMARY KEY,
    classroom_id INT NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    due_date DATETIME,
    max_marks INT DEFAULT 100,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (classroom_id) REFERENCES classrooms(id) ON DELETE CASCADE
);

-- assignment_submissions
CREATE TABLE IF NOT EXISTS assignment_submissions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    assignment_id INT NOT NULL,
    student_id INT NOT NULL,
    filename VARCHAR(255),
    file_path VARCHAR(512),
    submitted_text TEXT,
    ai_grade INT,
    ai_feedback TEXT,
    teacher_grade INT,
    submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_submission (assignment_id, student_id),
    FOREIGN KEY (assignment_id) REFERENCES assignments(id) ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE CASCADE
);

-- projects
CREATE TABLE IF NOT EXISTS projects (
    id INT AUTO_INCREMENT PRIMARY KEY,
    classroom_id INT NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    due_date DATETIME,
    max_marks INT DEFAULT 100,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (classroom_id) REFERENCES classrooms(id) ON DELETE CASCADE
);

-- project_submissions
CREATE TABLE IF NOT EXISTS project_submissions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    project_id INT NOT NULL,
    student_id INT NOT NULL,
    github_url VARCHAR(512) NOT NULL,
    ai_grade INT,
    ai_feedback TEXT,
    teacher_grade INT,
    submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_proj_submission (project_id, student_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE CASCADE
);

SELECT 'Migration complete! All tables are up to date.' AS status;
