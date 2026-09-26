"""
Backup (D4.3) — zips uploads/, users.json, settings and the pending-job list
into backups/exammanager-backup-YYYYMMDD-HHMMSS.zip and keeps the newest 7.

Run by hand or from a scheduled task (it never runs inside the app):

    python tools/backup.py                 # default: keep 7
    python tools/backup.py --keep 14

Unconfirmed batch uploads (uploads/exams/_batch_staging) are skipped.
Photos and PDFs are stored without re-compression (they are already
compressed), so a backup is fast and the originals are byte-for-byte intact.
"""

import argparse
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = ROOT / "backups"
EXTRA_FILES = ["users.json", "settings.json", "deletion_log.json", "pending_jobs.json"]
STORED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".zip", ".gz"}
PREFIX = "exammanager-backup-"
OLD_PREFIXES = ("edumark-backup-",)   # backups made before the rename still count toward --keep


def make_backup(keep: int = 7) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"{PREFIX}{datetime.now():%Y%m%d-%H%M%S}.zip"
    partial = target.with_suffix(".zip.partial")
    files = 0
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        uploads = ROOT / "uploads"
        if uploads.exists():
            for p in sorted(uploads.rglob("*")):
                if not p.is_file() or "_batch_staging" in p.parts or p.name == ".DS_Store":
                    continue
                method = zipfile.ZIP_STORED if p.suffix.lower() in STORED_EXTS else zipfile.ZIP_DEFLATED
                zf.write(p, p.relative_to(ROOT).as_posix(), compress_type=method)
                files += 1
        for name in EXTRA_FILES:
            p = ROOT / name
            if p.exists():
                zf.write(p, name)
                files += 1
    partial.replace(target)   # only a finished zip ever gets the .zip name

    backups = [p for pre in (PREFIX, *OLD_PREFIXES) for p in BACKUP_DIR.glob(f"{pre}*.zip")]
    backups.sort(key=lambda p: p.name.split("-backup-", 1)[1])   # by timestamp, whatever the prefix
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink()
    size_mb = target.stat().st_size / 1024 / 1024
    print(f"✅ Backup written: {target} ({files} files, {size_mb:.1f} MB). Keeping the newest {keep}.")
    return target


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Back up ExamManager data (uploads, accounts, settings).")
    p.add_argument("--keep", type=int, default=7, help="how many backups to keep (default 7)")
    args = p.parse_args(argv)
    try:
        make_backup(args.keep)
    except OSError as e:
        print(f"❌ Backup failed: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
