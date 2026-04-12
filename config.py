import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY     = os.getenv('SECRET_KEY', 'neuroclass-dev-secret-key')
    MYSQL_HOST     = os.getenv('MYSQL_HOST', 'localhost')
    MYSQL_USER     = os.getenv('MYSQL_USER', 'root')
    MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD', '')
    MYSQL_DB       = os.getenv('MYSQL_DB', 'neuroclass')
    # CRITICAL: force utf8mb4 so emoji / 4-byte unicode never cause error 1366
    MYSQL_CHARSET  = 'utf8mb4'
    # flask_mysqldb passes these kwargs to MySQLdb.connect()
    # init_command runs SET NAMES utf8mb4 on every new connection
    MYSQL_CUSTOM_OPTIONS = {
        'charset': 'utf8mb4',
        'init_command': "SET NAMES 'utf8mb4' COLLATE 'utf8mb4_unicode_ci'",
    }

    UPLOAD_FOLDER      = os.getenv('UPLOAD_FOLDER', 'uploads')
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50 MB

    # AI keys — loaded from .env
    GEMINI_API_KEY     = os.getenv('GEMINI_API_KEY', '')
    OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY', '')
    GROQ_API_KEY       = os.getenv('GROQ_API_KEY', '')

    # Local storage paths
    LECTURES_BASE_DIR = os.path.join(UPLOAD_FOLDER, 'lectures')
    RAG_INDEX_DIR     = os.path.join(UPLOAD_FOLDER, 'rag_indexes')
