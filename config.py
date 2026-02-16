"""
Configuration for Phase 1 - Image Upload System
Enhanced with Session-Based Workflow & Rubric Support
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

class Config:
    """Base configuration"""
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
    BASE_DIR = Path(__file__).parent
    UPLOAD_FOLDER = BASE_DIR / os.getenv('UPLOAD_FOLDER', 'uploads/exams')
    RUBRIC_FOLDER = BASE_DIR / 'uploads/rubrics'
    MAX_FILE_SIZE = int(os.getenv('MAX_FILE_SIZE', 10485760))
    ALLOWED_EXTENSIONS = set(os.getenv('ALLOWED_EXTENSIONS', 'jpg,jpeg,png,pdf').split(','))
    HOST = os.getenv('HOST', '0.0.0.0')
    PORT = int(os.getenv('PORT', 5000))
    DEBUG = os.getenv('DEBUG', 'True').lower() == 'true'
    
    SUBJECTS = [
        'English Language', 'Literature in English', 'Mathematics',
        'Biology', 'Chemistry', 'Physics', 'Geography',
        'History and Political Education', 'Kiswahili',
        'Christian Religious Education (CRE)',
        'Islamic Religious Education (IRE)',
        'Art and Design', 'Performing Arts', 'Entrepreneurship',
        'Information and Communication Technology (ICT)',
        'Skills Technology and Design', 'Nutrition and Food Technology',
        'Agriculture', 'Physical Education', 'General Science (Special Needs)',
        'French', 'German', 'Arabic', 'Latin', 'Chinese (Mandarin)',
        'Luganda', 'Jophadola (Dhopadhola)', 'Leb-Acholi', 'Leb-Lango',
        'Lugbarati (Lugbara)', 'Lunyankole-Rukiga', 'Runyoro-Rutoro',
        'Lumasaba', 'Lusoga'
    ]
    
    CLASSES = ['S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7']
    TERMS = ['Term 1', 'Term 2', 'Term 3']
    
    @staticmethod
    def init_app(app):
        Config.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
        Config.RUBRIC_FOLDER.mkdir(parents=True, exist_ok=True)
