# Add this to your app.py

from flask import send_file
from excel_generator import generate_exam_spreadsheet
import tempfile
from pathlib import Path
import os
import requests
from flask import Flask, request, jsonify
from config import Config
app = Flask(__name__)

@app.route('/generate-excel', methods=['POST'])
def generate_excel():
    """
    Generates Excel/ODS spreadsheet for specified exam session.
    """
    try:
        data = request.get_json()
        
        # Validate required fields
        required = ['year', 'term', 'class', 'stream', 'subject', 'exam_type', 'format']
        for field in required:
            if field not in data:
                return jsonify({
                    'success': False,
                    'message': f'Missing required field: {field}'
                }), 400
        
        # Extract parameters
        year = data['year']
        term = data['term']
        class_name = data['class']
        stream = data['stream']
        subject = data['subject']
        exam_type = data['exam_type']
        file_format = data['format'].lower()
        
        # Validate format
        if file_format not in ['xlsx', 'ods']:
            return jsonify({
                'success': False,
                'message': 'Format must be xlsx or ods'
            }), 400
        
        # Create temp folder for output
        temp_dir = Path(tempfile.gettempdir())
        
        # Generate spreadsheet
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
        
        # Send file to user
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