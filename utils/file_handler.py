"""
File Handler for managing exam script uploads
"""
import os
from pathlib import Path
from werkzeug.utils import secure_filename
from PIL import Image
import uuid
from datetime import datetime
from typing import List, Optional, Tuple

from config import Config


class FileHandler:
    """Handle file operations for exam scripts"""
    
    @staticmethod
    def allowed_file(filename: str) -> bool:
        """
        Check if file extension is allowed
        
        Args:
            filename: Name of the file
            
        Returns:
            True if allowed, False otherwise
        """
        return '.' in filename and \
               filename.rsplit('.', 1)[1].lower() in Config.ALLOWED_EXTENSIONS
    
    @staticmethod
    def create_student_folder(student_id: str) -> Path:
        """
        Create folder for student's exam scripts
        
        Args:
            student_id: Unique student identifier
            
        Returns:
            Path to created folder
        """
        student_folder = Config.UPLOAD_FOLDER / f"student_{student_id}"
        student_folder.mkdir(parents=True, exist_ok=True)
        return student_folder
    
    @staticmethod
    def generate_filename(original_filename: str, page_number: int) -> str:
        """
        Generate standardized filename
        
        Args:
            original_filename: Original uploaded filename
            page_number: Page number in the exam
            
        Returns:
            Standardized filename
        """
        ext = original_filename.rsplit('.', 1)[1].lower()
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        return f"page_{page_number:03d}_{timestamp}.{ext}"
    
    @staticmethod
    def save_image(file, student_folder: Path, page_number: int) -> Tuple[bool, str, Optional[Path]]:
        """
        Save uploaded image file
        
        Args:
            file: FileStorage object from Flask
            student_folder: Path to student's folder
            page_number: Page number
            
        Returns:
            Tuple of (success, message, filepath)
        """
        try:
            # Check file extension
            if not FileHandler.allowed_file(file.filename):
                return False, f"File type not allowed: {file.filename}", None
            
            # Generate filename
            filename = FileHandler.generate_filename(file.filename, page_number)
            filepath = student_folder / filename
            
            # Save file
            file.save(str(filepath))
            
            # Validate it's a valid image
            try:
                img = Image.open(filepath)
                img.verify()
                
                # Optional: Resize if too large (to save space)
                img = Image.open(filepath)  # Reopen after verify
                max_size = (2000, 2000)
                if img.size[0] > max_size[0] or img.size[1] > max_size[1]:
                    img.thumbnail(max_size, Image.Resampling.LANCZOS)
                    img.save(filepath, quality=95, optimize=True)
                
            except Exception as e:
                # Not a valid image, delete it
                filepath.unlink(missing_ok=True)
                return False, f"Invalid image file: {str(e)}", None
            
            return True, "File saved successfully", filepath
            
        except Exception as e:
            return False, f"Error saving file: {str(e)}", None
    
    @staticmethod
    def get_student_files(student_id: str) -> List[Path]:
        """
        Get all files for a student
        
        Args:
            student_id: Student identifier
            
        Returns:
            List of file paths
        """
        student_folder = Config.UPLOAD_FOLDER / f"student_{student_id}"
        
        if not student_folder.exists():
            return []
        
        # Get all image files, sorted by name
        files = []
        for ext in Config.ALLOWED_EXTENSIONS:
            files.extend(student_folder.glob(f"*.{ext}"))
        
        return sorted(files)
    
    @staticmethod
    def delete_student_folder(student_id: str) -> Tuple[bool, str]:
        """
        Delete a student's folder and all contents
        
        Args:
            student_id: Student identifier
            
        Returns:
            Tuple of (success, message)
        """
        try:
            student_folder = Config.UPLOAD_FOLDER / f"student_{student_id}"
            
            if not student_folder.exists():
                return False, "Student folder not found"
            
            # Delete all files
            for file in student_folder.iterdir():
                file.unlink()
            
            # Delete folder
            student_folder.rmdir()
            
            return True, "Folder deleted successfully"
            
        except Exception as e:
            return False, f"Error deleting folder: {str(e)}"
    
    @staticmethod
    def get_all_students() -> List[str]:
        """
        Get list of all student IDs that have uploaded files
        
        Returns:
            List of student IDs
        """
        if not Config.UPLOAD_FOLDER.exists():
            return []
        
        students = []
        for folder in Config.UPLOAD_FOLDER.iterdir():
            if folder.is_dir() and folder.name.startswith('student_'):
                student_id = folder.name.replace('student_', '')
                students.append(student_id)
        
        return sorted(students)
    
    @staticmethod
    def get_file_info(filepath: Path) -> dict:
        """
        Get information about a file
        
        Args:
            filepath: Path to file
            
        Returns:
            Dictionary with file information
        """
        try:
            stat = filepath.stat()
            
            # Get image dimensions if it's an image
            dimensions = None
            if filepath.suffix.lower() in ['.jpg', '.jpeg', '.png']:
                try:
                    img = Image.open(filepath)
                    dimensions = f"{img.width}x{img.height}"
                except:
                    pass
            
            return {
                'filename': filepath.name,
                'size': stat.st_size,
                'size_mb': round(stat.st_size / (1024 * 1024), 2),
                'created': datetime.fromtimestamp(stat.st_ctime).isoformat(),
                'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                'dimensions': dimensions
            }
        except Exception as e:
            return {'error': str(e)}