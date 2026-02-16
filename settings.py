"""
Settings Management System
Handles auto-deletion of student answer sheets after 48 hours
"""

import json
from pathlib import Path
from datetime import datetime, timedelta
import shutil

class SettingsManager:
    """Manages application settings including auto-deletion"""
    
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.settings_file = base_dir / "settings.json"
        self.deletion_log_file = base_dir / "deletion_log.json"
        self._ensure_settings_exist()
    
    def _ensure_settings_exist(self):
        """Creates default settings file if it doesn't exist"""
        if not self.settings_file.exists():
            default_settings = {
                "auto_deletion": {
                    "enabled": False,
                    "deletion_hours": 48,
                    "last_run": None,
                    "enabled_date": None,
                    "confirmation_steps_completed": False
                },
                "notifications": {
                    "show_deletion_warning": True
                }
            }
            self.save_settings(default_settings)
        
        if not self.deletion_log_file.exists():
            with open(self.deletion_log_file, 'w') as f:
                json.dump([], f, indent=4)
    
    def load_settings(self) -> dict:
        """Loads settings from JSON file"""
        try:
            with open(self.settings_file, 'r') as f:
                return json.load(f)
        except:
            return self._get_default_settings()
    
    def save_settings(self, settings: dict):
        """Saves settings to JSON file"""
        with open(self.settings_file, 'w') as f:
            json.dump(settings, f, indent=4)
    
    def _get_default_settings(self) -> dict:
        """Returns default settings"""
        return {
            "auto_deletion": {
                "enabled": False,
                "deletion_hours": 48,
                "last_run": None,
                "enabled_date": None,
                "confirmation_steps_completed": False
            },
            "notifications": {
                "show_deletion_warning": True
            }
        }
    
    def is_auto_deletion_enabled(self) -> bool:
        """Checks if auto-deletion is enabled"""
        settings = self.load_settings()
        return settings.get("auto_deletion", {}).get("enabled", False)
    
    def enable_auto_deletion(self, confirmed: bool = False) -> dict:
        """
        Enables auto-deletion feature with confirmation
        Returns: dict with success status and message
        """
        if not confirmed:
            return {
                "success": False,
                "requires_confirmation": True,
                "message": "Auto-deletion requires two-step confirmation"
            }
        
        settings = self.load_settings()
        settings["auto_deletion"]["enabled"] = True
        settings["auto_deletion"]["enabled_date"] = datetime.now().isoformat()
        settings["auto_deletion"]["confirmation_steps_completed"] = True
        self.save_settings(settings)
        
        return {
            "success": True,
            "message": "Auto-deletion enabled successfully"
        }
    
    def disable_auto_deletion(self) -> dict:
        """Disables auto-deletion feature"""
        settings = self.load_settings()
        settings["auto_deletion"]["enabled"] = False
        settings["auto_deletion"]["confirmation_steps_completed"] = False
        self.save_settings(settings)
        
        return {
            "success": True,
            "message": "Auto-deletion disabled successfully"
        }
    
    def get_deletion_threshold_time(self) -> datetime:
        """Returns the datetime threshold for deletion (48 hours ago)"""
        settings = self.load_settings()
        hours = settings.get("auto_deletion", {}).get("deletion_hours", 48)
        return datetime.now() - timedelta(hours=hours)
    
    def find_folders_for_deletion(self, uploads_folder: Path) -> list:
        """
        Finds all student folders older than 48 hours
        Returns list of folder paths and their metadata
        """
        threshold_time = self.get_deletion_threshold_time()
        folders_to_delete = []
        
        # Find all students_metadata.json files
        for metadata_file in uploads_folder.rglob("students_metadata.json"):
            exam_path = metadata_file.parent
            
            try:
                with open(metadata_file, 'r') as f:
                    students_data = json.load(f)
                
                # Check each student folder
                for student_id, student_info in students_data.items():
                    student_folder = exam_path / f"student_{student_id}"
                    
                    if not student_folder.exists():
                        continue
                    
                    # Get upload time from metadata
                    sync_time_str = student_info.get("sync_time")
                    if not sync_time_str:
                        continue
                    
                    try:
                        # Parse sync time
                        sync_time = datetime.strptime(sync_time_str, "%Y-%m-%d %H:%M:%S")
                        
                        # Check if older than threshold
                        if sync_time < threshold_time:
                            # Calculate size
                            folder_size = sum(f.stat().st_size for f in student_folder.rglob('*') if f.is_file())
                            
                            folders_to_delete.append({
                                "path": str(student_folder),
                                "student_id": student_id,
                                "student_name": student_info.get("student_name", "Unknown"),
                                "upload_time": sync_time_str,
                                "age_hours": (datetime.now() - sync_time).total_seconds() / 3600,
                                "size_mb": folder_size / (1024 * 1024),
                                "exam_session": str(exam_path.relative_to(uploads_folder))
                            })
                    except ValueError:
                        continue
            
            except Exception as e:
                print(f"Error processing {metadata_file}: {e}")
                continue
        
        return folders_to_delete
    
    def delete_student_folders(self, folders_list: list) -> dict:
        """
        Deletes student folders and logs the action
        Returns: dict with deletion results
        """
        deleted_count = 0
        failed_count = 0
        total_space_freed = 0
        deletion_records = []
        
        for folder_info in folders_list:
            try:
                folder_path = Path(folder_info["path"])
                
                if folder_path.exists():
                    # Delete folder
                    shutil.rmtree(folder_path)
                    
                    deleted_count += 1
                    total_space_freed += folder_info["size_mb"]
                    
                    # Log deletion
                    deletion_records.append({
                        "student_id": folder_info["student_id"],
                        "student_name": folder_info["student_name"],
                        "exam_session": folder_info["exam_session"],
                        "upload_time": folder_info["upload_time"],
                        "deletion_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "size_mb": folder_info["size_mb"]
                    })
            
            except Exception as e:
                print(f"Failed to delete {folder_info['path']}: {e}")
                failed_count += 1
        
        # Save deletion log
        self._append_to_deletion_log(deletion_records)
        
        # Update last run time
        settings = self.load_settings()
        settings["auto_deletion"]["last_run"] = datetime.now().isoformat()
        self.save_settings(settings)
        
        return {
            "success": True,
            "deleted_count": deleted_count,
            "failed_count": failed_count,
            "total_space_freed_mb": round(total_space_freed, 2),
            "deletion_records": deletion_records
        }
    
    def _append_to_deletion_log(self, records: list):
        """Appends deletion records to log file"""
        try:
            with open(self.deletion_log_file, 'r') as f:
                log = json.load(f)
        except:
            log = []
        
        log.extend(records)
        
        # Keep only last 1000 records
        if len(log) > 1000:
            log = log[-1000:]
        
        with open(self.deletion_log_file, 'w') as f:
            json.dump(log, f, indent=4)
    
    def get_deletion_log(self, limit: int = 50) -> list:
        """Returns recent deletion records"""
        try:
            with open(self.deletion_log_file, 'r') as f:
                log = json.load(f)
            return log[-limit:] if len(log) > limit else log
        except:
            return []
    
    def run_auto_deletion(self, uploads_folder: Path) -> dict:
        """
        Runs auto-deletion process if enabled
        Returns: dict with results
        """
        if not self.is_auto_deletion_enabled():
            return {
                "success": False,
                "message": "Auto-deletion is not enabled"
            }
        
        print("🗑️  Running auto-deletion process...")
        
        # Find folders to delete
        folders_to_delete = self.find_folders_for_deletion(uploads_folder)
        
        if len(folders_to_delete) == 0:
            print("✅ No folders found for deletion")
            return {
                "success": True,
                "message": "No folders found for deletion",
                "deleted_count": 0
            }
        
        print(f"📋 Found {len(folders_to_delete)} folders to delete")
        
        # Delete folders
        result = self.delete_student_folders(folders_to_delete)
        
        print(f"✅ Deleted {result['deleted_count']} folders, freed {result['total_space_freed_mb']} MB")
        
        return result
    
    def get_deletion_preview(self, uploads_folder: Path) -> dict:
        """
        Preview what would be deleted without actually deleting
        Returns: dict with preview information
        """
        folders_to_delete = self.find_folders_for_deletion(uploads_folder)
        
        total_size = sum(f["size_mb"] for f in folders_to_delete)
        
        return {
            "total_folders": len(folders_to_delete),
            "total_size_mb": round(total_size, 2),
            "folders": folders_to_delete
        }


# Utility function for Flask integration
def get_settings_manager(base_dir: Path) -> SettingsManager:
    """Returns a SettingsManager instance"""
    return SettingsManager(base_dir)