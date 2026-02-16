"""
Utility functions for AI Marking Engine
"""
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from PIL import Image
import cv2
import numpy as np

from config import Config


class MetadataManager:
    """Manage student metadata"""
    METADATA_FILENAME = "metadata.json"
    
    @staticmethod
    def save_metadata(student_id, data):
        """Save student metadata to global metadata file"""
        metadata_file = Path("uploads/metadata.json")
        metadata_file.parent.mkdir(parents=True, exist_ok=True)
        
        all_data = []
        if metadata_file.exists():
            try:
                with open(metadata_file, 'r') as f:
                    all_data = json.load(f)
                    if isinstance(all_data, dict):
                        all_data = list(all_data.values())
            except:
                all_data = []

        all_data.append(data)
        
        with open(metadata_file, 'w') as f:
            json.dump(all_data, f, indent=4)
        
        return True
    
    @staticmethod
    def load_metadata(student_id: str) -> Optional[Dict]:
        """Load metadata from JSON file"""
        try:
            student_folder = Config.UPLOAD_FOLDER / f"student_{student_id}"
            metadata_path = student_folder / MetadataManager.METADATA_FILENAME
            if not metadata_path.exists():
                return None
            with open(metadata_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading metadata: {str(e)}")
            return None
    
    @staticmethod
    def update_metadata(student_id: str, updates: Dict) -> bool:
        """Update existing metadata"""
        try:
            metadata = MetadataManager.load_metadata(student_id)
            if not metadata:
                return False
            metadata.update(updates)
            metadata['last_updated'] = datetime.now().isoformat()
            return MetadataManager.save_metadata(student_id, metadata)
        except Exception as e:
            print(f"Error updating metadata: {str(e)}")
            return False
    
    @staticmethod
    def get_all_metadata():
        """Get all metadata records"""
        metadata_file = Path("uploads/metadata.json")
        if not metadata_file.exists():
            return []
        with open(metadata_file, 'r') as f:
            try:
                return json.load(f)
            except:
                return [] 

    @staticmethod
    def get_statistics():
        """Get statistics from metadata"""
        metadata_file = Path("uploads/metadata.json")
        
        if not metadata_file.exists():
            return {"total_students": 0, "total_pages": 0}
        
        with open(metadata_file, 'r') as f:
            try:
                data = json.load(f)
                total_students = len(data)
                total_pages = sum(int(s.get('total_pages', 0)) for s in data)
                
                return {
                    "total_students": total_students,
                    "total_pages": total_pages
                }
            except:
                return {"total_students": 0, "total_pages": 0}


class ImageValidator:
    """Validate image quality for exam papers"""
    
    def __init__(self, use_ai=False):
        self.use_ai = use_ai
    
    def validate_image(self, image_path: Path) -> Tuple[bool, str, Optional[float]]:
        """
        Validate an image for quality
        
        Args:
            image_path: Path to the image file
            
        Returns:
            Tuple of (is_valid, reason, quality_score)
        """
        try:
            # Check if file exists
            if not image_path.exists():
                return False, "File does not exist", None
            
            # Try to open with PIL
            try:
                img = Image.open(image_path)
                img.verify()
                img = Image.open(image_path)  # Reopen after verify
            except Exception as e:
                return False, f"Invalid image file: {str(e)}", None
            
            # Check minimum dimensions
            min_width, min_height = 400, 400
            if img.width < min_width or img.height < min_height:
                return False, f"Image too small: {img.width}x{img.height} (minimum {min_width}x{min_height})", None
            
            # Check if image is too blurry using Laplacian variance
            if self.use_ai:
                blur_score = self._check_blur(image_path)
                if blur_score is not None and blur_score < 100:  # Threshold for blur
                    return False, f"Too blurry - focus camera properly (score: {blur_score:.1f})", blur_score
            
            return True, "Image is valid", None
            
        except Exception as e:
            return False, f"Validation error: {str(e)}", None
    
    def _check_blur(self, image_path: Path) -> Optional[float]:
        """
        Check if image is blurry using Laplacian variance
        
        Args:
            image_path: Path to image
            
        Returns:
            Blur score (higher is sharper)
        """
        try:
            # Read image with OpenCV
            image = cv2.imread(str(image_path))
            if image is None:
                return None
            
            # Convert to grayscale
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            
            # Calculate Laplacian variance
            laplacian = cv2.Laplacian(gray, cv2.CV_64F)
            variance = laplacian.var()
            
            return variance
            
        except Exception as e:
            print(f"Blur detection error: {str(e)}")
            return None