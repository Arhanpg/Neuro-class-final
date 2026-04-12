-- NeuroClass Database Setup
-- Run: mysql -u root -p < setup_db.sql

CREATE DATABASE IF NOT EXISTS neuroclass CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE neuroclass;

-- Users table (both students and instructors)
CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    full_name VARCHAR(120) NOT NULL,
    email VARCHAR(180) NOT NULL UNIQUE,
    password_hash VARCHAR(64) NOT NULL,
    role ENUM('student', 'instructor') NOT NULL DEFAULT 'student',
    avatar_url VARCHAR(255) DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- Classrooms table
CREATE TABLE IF NOT EXISTS classrooms (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    description TEXT,
    subject VARCHAR(80),
    code VARCHAR(12) NOT NULL UNIQUE,
    instructor_id INT NOT NULL,
    is_active TINYINT(1) DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (instructor_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Classroom members (students enrolled in classrooms)
CREATE TABLE IF NOT EXISTS classroom_members (
    id INT AUTO_INCREMENT PRIMARY KEY,
    classroom_id INT NOT NULL,
    user_id INT NOT NULL,
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY unique_member (classroom_id, user_id),
    FOREIGN KEY (classroom_id) REFERENCES classrooms(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Indexes for performance
CREATE INDEX idx_classrooms_instructor ON classrooms(instructor_id);
CREATE INDEX idx_members_classroom ON classroom_members(classroom_id);
CREATE INDEX idx_members_user ON classroom_members(user_id);

SELECT 'Database setup complete!' AS status;
