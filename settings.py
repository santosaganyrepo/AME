"""
Settings Management System
Handles auto-deletion of student answer sheets after 48 hours

(Restored from the compiled settings.cpython-*.pyc left in __pycache__ — the
source file was missing from the repository, which stopped app.py from
starting. Behaviour is identical to the compiled version; the only change is
that settings / deletion-log writes now go through the atomic helper in
storage.py.)
"""

import os
from pathlib import Path
from datetime import datetime, timedelta
import shutil

from storage import read_json, write_json_atomic


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
                    "confirmation_steps_completed": False,
                },
                "notifications": {
                    "show_deletion_warning": True,
                },
            }
            self.save_settings(default_settings)

        if not self.deletion_log_file.exists():
            write_json_atomic(self.deletion_log_file, [], indent=4)

    def load_settings(self) -> dict:
        """Loads settings from JSON file"""
        data = read_json(self.settings_file, None)
        if data is None:
            return self._get_default_settings()
        return data

    def save_settings(self, settings: dict):
        """Saves settings to JSON file"""
        write_json_atomic(self.settings_file, settings, indent=4)

    def _get_default_settings(self) -> dict:
        """Returns default settings"""
        return {
            "auto_deletion": {
                "enabled": False,
                "deletion_hours": 48,
                "last_run": None,
                "enabled_date": None,
                "confirmation_steps_completed": False,
            },
            "notifications": {
                "show_deletion_warning": True,
            },
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
                "message": "Auto-deletion requires two-step confirmation",
            }

        settings = self.load_settings()
        settings["auto_deletion"]["enabled"] = True
        settings["auto_deletion"]["enabled_date"] = datetime.now().isoformat()
        settings["auto_deletion"]["confirmation_steps_completed"] = True
        self.save_settings(settings)

        return {
            "success": True,
            "message": "Auto-deletion enabled successfully",
        }

    def disable_auto_deletion(self) -> dict:
        """Disables auto-deletion feature"""
        settings = self.load_settings()
        settings["auto_deletion"]["enabled"] = False
        settings["auto_deletion"]["confirmation_steps_completed"] = False
        self.save_settings(settings)

        return {
            "success": True,
            "message": "Auto-deletion disabled successfully",
        }

    def get_deletion_threshold_time(self) -> datetime:
        """Returns the datetime threshold for deletion (48 hours ago)"""
        settings = self.load_settings()
        hours = settings.get("auto_deletion", {}).get("deletion_hours", 48)
        return datetime.now() - timedelta(hours=hours)

    # Upload times are written as "%Y-%m-%d %H:%M" (app.add_or_update_student);
    # older records may carry seconds.
    _SYNC_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")

    @classmethod
    def _parse_sync_time(cls, value: str):
        for fmt in cls._SYNC_FORMATS:
            try:
                return datetime.strptime(value, fmt)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _metadata_files(uploads_folder: Path):
        """Every session's students_metadata.json, without walking into the
        (large) student page folders or the batch staging area."""
        for dirpath, dirnames, filenames in os.walk(uploads_folder):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith("student_") and d != "_batch_staging"]
            if "students_metadata.json" in filenames:
                yield Path(dirpath) / "students_metadata.json"

    def find_folders_for_deletion(self, uploads_folder: Path) -> list:
        """
        Finds all student folders older than the deletion threshold (48 h).
        Returns a list of folder paths and their metadata.

        (Fixed: this used to iterate the metadata file as a flat
        {student_id: info} dict and parse times with seconds, so it never
        found anything — metadata is {"exam_info": …, "students": [...]}.)
        """
        threshold_time = self.get_deletion_threshold_time()
        folders_to_delete = []
        if not Path(uploads_folder).exists():
            return folders_to_delete

        for metadata_file in self._metadata_files(Path(uploads_folder)):
            exam_path = metadata_file.parent
            data = read_json(metadata_file, None)
            students = data.get("students", []) if isinstance(data, dict) else []

            for student_info in students:
                if not isinstance(student_info, dict):
                    continue
                student_id = str(student_info.get("id") or student_info.get("student_id") or "")
                folder_name = student_info.get("folder") or f"student_{student_id}"
                if not student_id or "/" in folder_name or "\\" in folder_name or folder_name in (".", ".."):
                    continue
                student_folder = exam_path / folder_name
                if not student_folder.is_dir():
                    continue

                sync_time_str = student_info.get("sync_time")
                sync_time = self._parse_sync_time(sync_time_str)
                if sync_time is None or sync_time >= threshold_time:
                    continue

                try:
                    folder_size = sum(f.stat().st_size for f in student_folder.rglob("*") if f.is_file())
                except OSError:
                    folder_size = 0

                folders_to_delete.append({
                    "path": str(student_folder),
                    "student_id": student_id,
                    "student_name": student_info.get("student_name", "Unknown"),
                    "upload_time": sync_time_str,
                    "age_hours": (datetime.now() - sync_time).total_seconds() / 3600,
                    "size_mb": folder_size / (1024 * 1024),
                    "exam_session": str(exam_path.relative_to(uploads_folder)),
                })

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
                        "size_mb": folder_info["size_mb"],
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
            "deletion_records": deletion_records,
        }

    def _append_to_deletion_log(self, records: list):
        """Appends deletion records to log file"""
        log = read_json(self.deletion_log_file, [])
        if not isinstance(log, list):
            log = []

        log.extend(records)

        # Keep only last 1000 records
        if len(log) > 1000:
            log = log[-1000:]

        write_json_atomic(self.deletion_log_file, log, indent=4)

    def get_deletion_log(self, limit: int = 50) -> list:
        """Returns recent deletion records"""
        log = read_json(self.deletion_log_file, [])
        if not isinstance(log, list):
            return []
        return log[-limit:] if len(log) > limit else log

    def run_auto_deletion(self, uploads_folder: Path) -> dict:
        """
        Runs auto-deletion process if enabled
        Returns: dict with results
        """
        if not self.is_auto_deletion_enabled():
            return {
                "success": False,
                "message": "Auto-deletion is not enabled",
            }

        print("🗑️  Running auto-deletion process...")

        # Find folders to delete
        folders_to_delete = self.find_folders_for_deletion(uploads_folder)

        if len(folders_to_delete) == 0:
            print("✅ No folders found for deletion")
            return {
                "success": True,
                "message": "No folders found for deletion",
                "deleted_count": 0,
            }

        print(f"📋 Found {len(folders_to_delete)} folders to delete")

        # Delete folders
        result = self.delete_student_folders(folders_to_delete)

        print(f"✅ Deleted {result['deleted_count']} folders, freed {result['total_space_freed_mb']} MB")

        return result

    def get_deletion_preview(self, uploads_folder: Path) -> dict:
        """
        Preview what would be deleted without actually deleting
        """
        folders_to_delete = self.find_folders_for_deletion(uploads_folder)

        total_size = sum(f["size_mb"] for f in folders_to_delete)

        return {
            "total_folders": len(folders_to_delete),
            "total_size_mb": round(total_size, 2),
            "folders": folders_to_delete,
        }


def get_settings_manager(base_dir: Path) -> SettingsManager:
    """Get settings manager instance"""
    return SettingsManager(base_dir)
