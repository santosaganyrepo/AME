import os

# ==========================================
# 🔑 GOOGLE GEMINI API KEY
# ==========================================
os.environ['GOOGLE_API_KEY'] = "AIzaSyBztrGrAfFTqZbR2lAks5zN671IkpiTKCM"
# ==========================================


from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
from pathlib import Path
import time
import json
import shutil
import tempfile
from PIL import Image
from config import Config
from utils import MetadataManager, ImageValidator
from excel_generator import generate_exam_spreadsheet
from settings import SettingsManager
import schedule

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = r"/Users/itadouble8/Desktop/ AGANY .M DOCUMENTS/AI MARKING ENGINE/uploads"
app.secret_key = Config.SECRET_KEY
# Initialize Settings Manager
settings_manager = SettingsManager(Config.BASE_DIR)

validator = ImageValidator(use_ai=True)

def compress_image(source_path, dest_path):
    """Optimizes the full page without cropping for low data usage."""
    with Image.open(source_path) as img:
        if img.mode != 'RGB':
            img = img.convert('RGB')
        
        max_dimension = 1800
        if img.width > max_dimension or img.height > max_dimension:
            ratio = max_dimension / float(max(img.width, img.height))
            new_size = (int(img.width * ratio), int(img.height * ratio))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
        
        img.save(dest_path, "JPEG", optimize=True, quality=35)

@app.route('/')
def index():
    metadata_path = Config.BASE_DIR / "uploads" / "metadata.json"
    stats = {"total_students": 0, "total_pages": 0}
    # ... rest of existing code ...
    
    # Check auto-deletion status
    auto_deletion_enabled = settings_manager.is_auto_deletion_enabled()
    
    return render_template('dashboard.html', 
                         stats=stats,
                         auto_deletion_enabled=auto_deletion_enabled)

@app.route('/upload-batch', methods=['POST'])
def upload_batch():
    if 'exam_session' not in session: 
        return jsonify({'success': False, 'message': 'No active session'}), 400
    
    info = session['exam_session']
    exam_path = Path(info['exam_path'])
    files = request.files.getlist('files[]')
    sid, sname = request.form.get('student_id'), request.form.get('student_name')
    
    if not sid or not sname:
        return jsonify({'success': False, 'message': 'Student ID and Name required'}), 400
    
    temp_folder = Config.BASE_DIR / "uploads" / f"temp_{sid}"
    temp_folder.mkdir(parents=True, exist_ok=True)
    
    report, failed, valid_paths = [], [], []

    try:
        # Step 1: Quality Validation
        for idx, file in enumerate(files):
            p_num = idx + 1
            p_path = temp_folder / f"p{p_num}.jpg"
            file.save(str(p_path))
            
            is_valid, reason, score = validator.validate_image(p_path)
            if not is_valid:
                failed.append(p_num)
                report.append(f"Page {p_num}: {reason} (score: {score:.1f}) – please retake this page")
            else:
                valid_paths.append(p_path)

        if len(failed) > 0:
            return jsonify({
                'success': False,
                'message': 'QUALITY CHECK FAILED',
                'details': report
            }), 422

        # Step 2: Compression & Permanent Storage
        student_folder = exam_path / f"student_{sid}"
        student_folder.mkdir(parents=True, exist_ok=True)
        
        for p_path in valid_paths:
            target_path = student_folder / p_path.name
            compress_image(p_path, target_path)

        # Step 3: Database Update (Initial entry)
        meta_file = exam_path / "students_metadata.json"
        
        with open(meta_file, 'r') as f:
            all_students = json.load(f)
            
        all_students[sid] = {
            "student_name": sname,
            "student_id": sid,
            "total_pages": len(valid_paths),
            "sync_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "Synced",
            "total_score": 0.0,
            "questions": {}
        }
        
        with open(meta_file, 'w') as f:
            json.dump(all_students, f, indent=4)
        
        # Step 4: AUTOMATIC AI MARKING WITH GEMINI
        print(f"🚀 Starting automatic AI marking for {sid}...")
        
        try:
            api_key = os.environ.get('GOOGLE_API_KEY')
            if api_key:
                from ai_marker_gemini_improved import AIMarker
                
                marker = AIMarker(api_key=api_key)
                
                marking_result = marker.mark_student(
                    student_id=sid,
                    student_name=sname,
                    exam_path=exam_path,
                    student_folder=student_folder
                )
                
                if marking_result.get('success'):
                    all_students[sid].update({
                        'status': 'Marked',
                        'total_score': marking_result['total_score'],
                        'raw_score': marking_result.get('raw_score', 0),
                        'max_score': marking_result.get('max_score', 0),
                        'questions': marking_result.get('questions', {}),
                        'overall_feedback': marking_result.get('overall_feedback', ''),
                        'strengths': marking_result.get('strengths', []),
                        'areas_for_improvement': marking_result.get('areas_for_improvement', [])
                    })
                    
                    with open(meta_file, 'w') as f:
                        json.dump(all_students, f, indent=4)
                    
                    print(f"✅ AI Marking Complete! Score: {marking_result['total_score']:.1f}%")
                    
                    return jsonify({
                        'success': True, 
                        'total_saved': len(valid_paths), 
                        'student_id': sid,
                        'marked': True,
                        'score': marking_result['total_score'],
                        'questions': marking_result.get('questions', {}),
                        'message': f'Uploaded and marked! Score: {marking_result["total_score"]:.1f}%'
                    })
                else:
                    print(f"⚠️ AI Marking failed: {marking_result.get('error')}")
                    return jsonify({
                        'success': True, 
                        'total_saved': len(valid_paths), 
                        'student_id': sid,
                        'marked': False,
                        'message': 'Uploaded successfully, but marking failed.'
                    })
            else:
                print("⚠️ GOOGLE_API_KEY not set")
                return jsonify({
                    'success': True, 
                    'total_saved': len(valid_paths), 
                    'student_id': sid,
                    'marked': False,
                    'message': 'Uploaded successfully (API key not configured)'
                })
                
        except Exception as marking_error:
            print(f"⚠️ Auto-marking error: {str(marking_error)}")
            import traceback
            traceback.print_exc()
            return jsonify({
                'success': True, 
                'total_saved': len(valid_paths), 
                'student_id': sid,
                'marked': False,
                'message': 'Uploaded successfully, but marking encountered an error'
            })

    finally:
        if temp_folder.exists():
            shutil.rmtree(temp_folder)

@app.route('/students')
def view_students():
    exam_sessions = []
    
    if Config.UPLOAD_FOLDER.exists():
        metadata_files = []
        for metadata_file in Config.UPLOAD_FOLDER.rglob("students_metadata.json"):
            mod_time = metadata_file.stat().st_mtime
            metadata_files.append((mod_time, metadata_file))
        
        metadata_files.sort(reverse=True, key=lambda x: x[0])
        
        for mod_time, metadata_file in metadata_files:
            exam_path = metadata_file.parent
            path_parts = exam_path.relative_to(Config.UPLOAD_FOLDER).parts
            
            if len(path_parts) >= 6:
                try:
                    with open(metadata_file, 'r') as f:
                        students_dict = json.load(f)
                        students_list = list(students_dict.values())
                        
                        if students_list:
                            exam_sessions.append({
                                'year': path_parts[0],
                                'term': path_parts[1],
                                'class': path_parts[2],
                                'stream': path_parts[3],
                                'subject': path_parts[4],
                                'exam_type': path_parts[5],
                                'students': students_list,
                                'total_students': len(students_list),
                                'marked_count': sum(1 for s in students_list if s.get('status') == 'Marked'),
                                'uploaded_count': sum(1 for s in students_list if s.get('status') != 'Marked')
                            })
                except:
                    pass
    
    total_students = sum(exam['total_students'] for exam in exam_sessions)
    total_marked = sum(exam['marked_count'] for exam in exam_sessions)
    
    stats = {
        "total_students": total_students,
        "total_marked": total_marked,
        "total_sessions": len(exam_sessions)
    }
    
    return render_template('view_students.html', 
                           exam_sessions=exam_sessions,
                           stats=stats)

@app.route('/setup-start')
def setup_start():
    session.pop('exam_session', None)
    return render_template('setup.html', subjects=Config.SUBJECTS, classes=Config.CLASSES, terms=Config.TERMS)

@app.route('/setup', methods=['POST'])
def setup_session():
    try:
        data = request.form
        required_fields = ['year', 'term', 'class', 'stream', 'subject', 'exam_type']
        
        for field in required_fields:
            value = data.get(field)
            if not value or not value.strip():
                return jsonify({
                    'success': False, 
                    'message': f'Missing required field: {field}'
                }), 400
        
        path_parts = [data.get(f).strip() for f in required_fields]
        exam_path = Config.UPLOAD_FOLDER.joinpath(*path_parts)
        
        # Duplicate Session Guard
        session_exists = exam_path.exists()
        has_students = False
        student_count = 0
        
        if session_exists:
            meta_file = exam_path / "students_metadata.json"
            if meta_file.exists():
                with open(meta_file, 'r') as f:
                    students_data = json.load(f)
                    student_count = len(students_data)
                    has_students = student_count > 0
        
        force_overwrite = data.get('force_overwrite') == 'true'
        
        if has_students and not force_overwrite:
            return jsonify({
                'success': False,
                'duplicate_session': True,
                'student_count': student_count,
                'message': f'There are {student_count} student(s) marked under this same session. Do you want to overwrite these students?',
                'session_info': {
                    'year': path_parts[0],
                    'term': path_parts[1],
                    'class': path_parts[2],
                    'stream': path_parts[3],
                    'subject': path_parts[4],
                    'exam_type': path_parts[5]
                }
            }), 409
        
        if force_overwrite and session_exists:
            if exam_path.exists():
                backup_path = exam_path.parent / f"{exam_path.name}_backup_{int(time.time())}"
                shutil.move(str(exam_path), str(backup_path))
        
        exam_path.mkdir(parents=True, exist_ok=True)

        if 'rubrics[]' in request.files:
            rubric_files = request.files.getlist('rubrics[]')
            for idx, f in enumerate(rubric_files):
                if f and f.filename:
                    ext = f.filename.rsplit('.', 1)[-1]
                    f.save(exam_path / f"rubric_{idx+1}.{ext}")

        if data.get('rubric_text'):
            with open(exam_path / "rubric.txt", "w") as f:
                f.write(data.get('rubric_text'))

        if 'question_papers[]' in request.files:
            qp_files = request.files.getlist('question_papers[]')
            for idx, f in enumerate(qp_files):
                if f and f.filename:
                    ext = f.filename.rsplit('.', 1)[-1]
                    f.save(exam_path / f"question_paper_{idx+1}.{ext}")

        meta_file = exam_path / "students_metadata.json"
        with open(meta_file, "w") as f:
            json.dump({}, f)

        session['exam_session'] = {
            'year': path_parts[0], 
            'term': path_parts[1], 
            'class': path_parts[2],
            'stream': path_parts[3], 
            'subject': path_parts[4], 
            'exam_type': path_parts[5],
            'exam_path': str(exam_path)
        }
        
        return jsonify({
            'success': True,
            'overwritten': force_overwrite,
            'message': 'Session created successfully' if not force_overwrite else 'Previous session overwritten successfully'
        })

    except Exception as e: 
        print(f"CRITICAL SETUP ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/upload-student')
def upload_student():
    if 'exam_session' not in session: 
        return redirect(url_for('setup_start'))
    return render_template('upload_student.html', exam_data=session['exam_session'])

@app.route('/end-session')
def end_session():
    session.pop('exam_session', None)
    return redirect(url_for('index'))

@app.route('/generate-excel', methods=['POST'])
def generate_excel():
    """Generates Excel/ODS spreadsheet for specified exam session."""
    try:
        data = request.get_json()
        
        required = ['year', 'term', 'class', 'stream', 'subject', 'exam_type', 'format']
        for field in required:
            if field not in data:
                return jsonify({
                    'success': False,
                    'message': f'Missing required field: {field}'
                }), 400
        
        year = data['year']
        term = data['term']
        class_name = data['class']
        stream = data['stream']
        subject = data['subject']
        exam_type = data['exam_type']
        file_format = data['format'].lower()
        
        if file_format not in ['xlsx', 'ods']:
            return jsonify({
                'success': False,
                'message': 'Format must be xlsx or ods'
            }), 400
        
        temp_dir = Path(tempfile.gettempdir())
        
        output_file = generate_exam_spreadsheet(
            base_folder=Config.UPLOAD_FOLDER,
            year=year,
            term=term,
            class_name=class_name,
            stream=stream,
            subject=subject,
            exam_type=exam_type,
            file_format=file_format,
            output_folder=temp_dir
        )
        
        return send_file(
            output_file,
            as_attachment=True,
            download_name=output_file.name,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if file_format == 'xlsx' else 'application/vnd.oasis.opendocument.spreadsheet'
        )
        
    except FileNotFoundError as e:
        return jsonify({
            'success': False,
            'message': str(e)
        }), 404
    except ValueError as e:
        return jsonify({
            'success': False,
            'message': str(e)
        }), 400
    except Exception as e:
        print(f"Excel generation error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'message': f'Error generating spreadsheet: {str(e)}'
        }), 500
    
@app.route('/settings')
def settings_page():
    """Settings page"""
    settings = settings_manager.load_settings()
    return render_template('settings.html', 
                         settings=settings,
                         auto_deletion_enabled=settings_manager.is_auto_deletion_enabled())

@app.route('/settings/enable-auto-deletion', methods=['POST'])
def enable_auto_deletion():
    """Enable auto-deletion"""
    data = request.get_json()
    result = settings_manager.enable_auto_deletion(confirmed=data.get('confirmed', False))
    return jsonify(result)

@app.route('/settings/disable-auto-deletion', methods=['POST'])
def disable_auto_deletion():
    """Disable auto-deletion"""
    result = settings_manager.disable_auto_deletion()
    return jsonify(result)

@app.route('/settings/deletion-preview')
def deletion_preview():
    """Preview what will be deleted"""
    preview = settings_manager.get_deletion_preview(Config.UPLOAD_FOLDER)
    return jsonify(preview)

@app.route('/settings/deletion-log')
def deletion_log():
    """Get deletion history"""
    log = settings_manager.get_deletion_log(limit=50)
    return jsonify({"log": log})    

if __name__ == '__main__':
    Config.init_app(app)
    if settings_manager.is_auto_deletion_enabled():
        print("🗑️  Auto-deletion is enabled, running cleanup...")
        settings_manager.run_auto_deletion(Config.UPLOAD_FOLDER)
    app.run(host='0.0.0.0', port=5000, debug=True)

