import json
from pathlib import Path
from config import Config

class MetadataManager:
    @staticmethod
    def save_metadata(student_id, data):
        path = Config.BASE_DIR / "uploads" / "metadata.json"
        existing_data = {}
        if path.exists():
            with open(path, 'r') as f:
                try:
                    existing_data = json.load(f)
                except: existing_data = {}
        
        existing_data[student_id] = data
        with open(path, 'w') as f:
            json.dump(existing_data, f, indent=4)
