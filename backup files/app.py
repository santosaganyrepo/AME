"""
Flask Application - Enhanced Session-Based Upload
Implements: One-time setup + Continuous capture loop
"""
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from werkzeug.utils import secure_filename
import os
from pathlib import Path
from datetime import datetime

from config import Config
from utils import FileHandler, MetadataManager

app = Flask(__name__)
app.config.from_object(Config)
app.secret_key = Config.SECRET_KEY
Config.init_app(app)


@app.route('/')
def index():
    """Main page - Setup or Continue"""
    # Check if session exists
    if 'exam_session' in session:
        return redirect(url_for('upload_student'))
    return render_template('setup.html', 
                         subjects=Config.SUBJECTS,
                         classes=Config.CLASSES,
                         exam_types=Config.EXAM_TYPES,
                         terms=Config.TERMS)


@app.route('/setup', methods=['POST'])
def setup_session():
    """One-time exam setup - locks class, subject, term, year"""
    try:
        data = request.json
        
        # Validate required fields
        required = ['class', 'subject', 'exam_type', 'term', 'year']
        if not all(field in data for field in required):
            return jsonify({'success': False, 'message': 'All fields required'}), 400
        
        # Store in session
        session['exam_session'] = {
            'class': data['class'],
            'subject': data['subject'],
            'exam_type': data['exam_type'],
            'term': data['term'],
            'year': int(data['year']),
            'setup_time': datetime.now().isoformat()
        }
        
        return jsonify({
            'success': True,
            'message': 'Session created successfully',
            'session_data': session['exam_session']
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/upload-student')
def upload_student():
    """Continuous upload page - only student ID and photos"""
    if 'exam_session' not in session:
        return redirect(url_for('index'))
    
    exam_data = session['exam_session']
    return render_template('upload_student.html', exam_data=exam_data)


@app.route('/upload-batch', methods=['POST'])
def upload_batch():
    """Batch upload for one student - all pages at once"""
    try:
        if 'exam_session' not in session:
            return jsonify({'success': False, 'message': 'No active session'}), 400
        
        # Get student data
        student_id = request.form.get('student_id', '').strip()
        student_name = request.form.get('student_name', '').strip()
        
        if not student_id or not student_name:
            return jsonify({'success': False, 'message': 'Student ID and Name required'}), 400
        
        # Get files
        files = request.files.getlist('files[]')
        if not files or files[0].filename == '':
            return jsonify({'success': False, 'message': 'No files uploaded'}), 400
        
        # Get session data
        exam_data = session['exam_session']
        
        # Create student folder
        student_folder = FileHandler.create_student_folder(student_id)
        
        # Save all files
        saved_files = []
        for idx, file in enumerate(files, start=1):
            success, message, filepath = FileHandler.save_image(file, student_folder, idx)
            if success:
                saved_files.append(filepath.name)
        
        # Create metadata
        metadata = MetadataManager.create_metadata(
            student_id=student_id,
            student_name=student_name,
            class_name=exam_data['class'],
            subject=exam_data['subject'],
            exam_type=exam_data['exam_type'],
            term=exam_data['term'],
            year=exam_data['year'],
            total_pages=len(saved_files)
        )
        
        MetadataManager.save_metadata(student_id, metadata)
        
        return jsonify({
            'success': True,
            'message': f'Uploaded {len(saved_files)} pages for {student_name}',
            'student_id': student_id,
            'total_saved': len(saved_files),
            'clear_mobile': True  # Signal to clear mobile storage
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/end-session', methods=['POST'])
def end_session():
    """End current exam session"""
    session.pop('exam_session', None)
    return jsonify({'success': True, 'message': 'Session ended'})


@app.route('/students')
def view_students():
    """View all uploaded students"""
    metadata_list = MetadataManager.get_all_metadata()
    stats = MetadataManager.get_statistics()
    return render_template('view_students.html', students=metadata_list, stats=stats)


@app.route('/api/session')
def get_session():
    """Get current session info"""
    if 'exam_session' in session:
        return jsonify({'success': True, 'session': session['exam_session']})
    return jsonify({'success': False, 'message': 'No active session'})


if __name__ == '__main__':
    app.run(host=Config.HOST, port=Config.PORT, debug=Config.DEBUG)
