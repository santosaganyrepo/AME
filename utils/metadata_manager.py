"""
Metadata Manager for storing exam information
"""
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

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