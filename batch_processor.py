"""
Batch Upload Processor
=======================
Validates and extracts a teacher-uploaded ZIP of pre-scanned student exam
scripts into per-student folders, ready to be queued for AI marking.

Expected ZIP layout (one folder per student):

    class_batch.zip
    ├── 001_John Doe/
    │   ├── page1.jpg
    │   └── page2.jpg
    ├── 002_Mary Akello/
    │   └── script.pdf
    └── 003_Peter Ouma/
        └── p1.jpg

Folder names must be "ID_Name" (or "ID-Name" / "ID Name") — the ID is the
first token, everything after the separator is treated as the student's
name. Files sitting outside of a student folder are ignored, and one level
of wrapping folder (e.g. the zip was created from a single top folder) is
stripped automatically.

This module never trusts anything about the archive it is given — every
entry is checked for path-traversal, zip-bomb ratios, encryption, unusable
file types, and corrupted content before anything is written to disk.
Nothing here talks to Flask; `validate_and_extract_batch()` is a pure
function of (zip_path, staging_dir) -> manifest dict.
"""

import io
import os
import re
import shutil
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from image_validator import ImageValidator
from page_prep import natural_key, pdf_to_page_images

_quality_checker = ImageValidator()

# ── Limits & policy ─────────────────────────────────────────────────────────
MAX_ZIP_UNCOMPRESSED_BYTES = 500 * 1024 * 1024   # 500 MB total, uncompressed
MAX_COMPRESSION_RATIO      = 100                 # per-file zip-bomb guard
LARGE_BATCH_PAGE_THRESHOLD = 1000                # requires explicit confirm

ALLOWED_EXTS  = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
IMAGE_EXTS    = {".jpg", ".jpeg", ".png", ".webp"}
# Students are extracted in parallel: image decoding, the blur check and PDF
# rendering release the GIL, so a class ZIP is processed several times faster.
EXTRACT_WORKERS = max(2, min(8, os.cpu_count() or 2))
ARCHIVE_EXTS  = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}
JUNK_BASENAMES = {".ds_store", "thumbs.db", "desktop.ini"}

_ID_NAME_RE = re.compile(r"^([A-Za-z0-9]+)[\s_\-]+(.+)$")
_ID_ONLY_RE = re.compile(r"^[A-Za-z0-9]+$")

ISSUE_TEXT = {
    "no_valid_pages":  "No valid, readable pages were found for this student.",
    "name_missing":    "Couldn't detect this student's name from the folder name.",
    "duplicate_folder": "This student ID appeared in more than one folder — pages were combined.",
    "poor_image_quality": "One or more pages look blurry or poor quality and may be mis-marked.",
}


# ═══════════════════════════════════════════════════════════════════════════
# Small helpers
# ═══════════════════════════════════════════════════════════════════════════

def is_unsafe_path(name: str) -> bool:
    """Rejects absolute paths, drive letters, and '..' traversal segments."""
    if not name or name.startswith("/") or name.startswith("\\"):
        return True
    if re.match(r"^[A-Za-z]:", name):
        return True
    if ".." in name.split("/"):
        return True
    return False


def is_junk(posix_path: str) -> bool:
    """OS-generated clutter that should be silently discarded."""
    base = posix_path.rsplit("/", 1)[-1]
    if base.lower() in JUNK_BASENAMES:
        return True
    if base.startswith("."):
        return True
    if "__macosx" in posix_path.lower():
        return True
    return False


def parse_student_folder(folder_name: str):
    """'001_John Doe' -> ('001', 'John Doe'). Bare '005' -> ('005', '')."""
    name = folder_name.strip()
    if not name:
        return None, ""
    m = _ID_NAME_RE.match(name)
    if m:
        sid   = m.group(1).strip()
        sname = re.sub(r"[_\-]+", " ", m.group(2)).strip()
        return sid, sname
    if _ID_ONLY_RE.match(name):
        return name, ""
    return None, ""


def _strip_common_root(names):
    """
    Strips a shared wrapping folder (e.g. the whole zip was made from one
    top-level folder) while never eating into the student-folder level
    itself — stops as soon as the shallowest remaining path is exactly
    "student_folder/file".
    """
    parts_list = [tuple(n.split("/")) for n in names]
    while parts_list:
        firsts     = {p[0] for p in parts_list}
        min_depth  = min(len(p) for p in parts_list)
        if len(firsts) == 1 and min_depth > 2:
            parts_list = [p[1:] for p in parts_list]
        else:
            break
    return parts_list


def _build_manifest(students_out, warnings, blocking_errors):
    total_pages     = sum(s["pages"] for s in students_out)
    ready_or_check  = [s for s in students_out if s["status"] != "error"]

    if blocking_errors or (students_out and not ready_or_check):
        severity = "blocking"
    elif warnings:
        severity = "warning"
    else:
        severity = "ok"

    return {
        "severity":                          severity,
        "blocking_errors":                   blocking_errors,
        "warnings":                          warnings,
        "requires_large_batch_confirmation": total_pages > LARGE_BATCH_PAGE_THRESHOLD,
        "total_students":                    len(students_out),
        "total_pages":                       total_pages,
        "students":                          students_out,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════

def validate_and_extract_batch(zip_path: Path, staging_dir: Path) -> dict:
    """
    Validates `zip_path` and, if safe enough to touch disk, extracts every
    accepted student page into `staging_dir/student_<id>/page_N.<ext>`.

    Raises ValueError("corrupted_zip") only when the archive itself cannot
    be opened at all — every other problem is reported as a blocking or
    warning entry inside the returned manifest instead, so the teacher gets
    a friendly, actionable review screen rather than a dead end.
    """
    warnings, blocking_errors = [], []

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise ValueError("corrupted_zip")
    # Always closed — an open handle stops the ZIP being deleted on Windows.
    with zf:
        return _validate_open_zip(zf, staging_dir, warnings, blocking_errors)


def _validate_open_zip(zf, staging_dir: Path, warnings: list, blocking_errors: list) -> dict:
    infos = [i for i in zf.infolist() if not i.is_dir()]

    if not infos:
        blocking_errors.append({
            "code": "empty_zip",
            "message": "The ZIP file is empty — no files were found inside it. "
                       "Please re-zip your student folders and try again.",
        })
        return _build_manifest([], warnings, blocking_errors)

    # ── Password-protected archive ──────────────────────────────────────────
    if any(i.flag_bits & 0x1 for i in infos):
        blocking_errors.append({
            "code": "encrypted",
            "message": "This ZIP file is password-protected. Please remove the "
                       "password and re-upload.",
        })
        return _build_manifest([], warnings, blocking_errors)

    # ── Zip-bomb: total uncompressed size ───────────────────────────────────
    total_uncompressed = sum(i.file_size for i in infos)
    if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
        limit_mb = MAX_ZIP_UNCOMPRESSED_BYTES // (1024 * 1024)
        blocking_errors.append({
            "code": "too_large",
            "message": f"This archive would expand to more than {limit_mb} MB, "
                       "which is larger than allowed. Please split it into "
                       "smaller batches and upload separately.",
        })
        return _build_manifest([], warnings, blocking_errors)

    # ── Zip-bomb: suspicious per-file compression ratio ─────────────────────
    for i in infos:
        if i.compress_size > 0:
            ratio = i.file_size / i.compress_size
            if i.file_size > 50 * 1024 * 1024 and ratio > MAX_COMPRESSION_RATIO:
                blocking_errors.append({
                    "code": "suspicious_archive",
                    "message": "This archive contains a file with an abnormally "
                               "high compression ratio and was rejected as a "
                               "precaution. Please re-create the ZIP and try again.",
                })
                return _build_manifest([], warnings, blocking_errors)

    # ── Path safety + junk filtering ────────────────────────────────────────
    safe_names, unsafe_skipped = [], False
    for i in infos:
        name = i.filename.replace("\\", "/")
        if is_unsafe_path(name):
            unsafe_skipped = True
            continue
        if is_junk(name):
            continue
        safe_names.append(name)

    if unsafe_skipped:
        warnings.append({
            "code": "unsafe_paths",
            "message": "Some files had unsafe file paths and were skipped for security reasons.",
        })

    if not safe_names:
        blocking_errors.append({
            "code": "no_usable_files",
            "message": "No usable student files were found in this archive "
                       "after removing system/junk files.",
        })
        return _build_manifest([], warnings, blocking_errors)

    parts_list = _strip_common_root(safe_names)

    # ── Group files by student folder ───────────────────────────────────────
    students = {}
    for name, parts in zip(safe_names, parts_list):
        if len(parts) < 2:
            warnings.append({
                "code": "root_file",
                "message": f"Ignored file '{parts[-1] if parts else name}' — "
                           "it wasn't inside a student folder.",
            })
            continue

        folder_name, leaf_name = parts[0], parts[-1]
        ext = Path(leaf_name).suffix.lower()

        if ext in ARCHIVE_EXTS:
            warnings.append({
                "code": "nested_archive",
                "message": f"Nested archive '{leaf_name}' was ignored. Please "
                           "remove nested ZIP/RAR files and re-upload.",
            })
            continue
        if ext not in ALLOWED_EXTS:
            warnings.append({
                "code": "unsupported_type",
                "message": f"Unsupported file skipped: '{leaf_name}'. Only JPG, "
                           "PNG, WEBP and PDF answer sheets are supported.",
            })
            continue

        sid, sname = parse_student_folder(folder_name)
        if not sid:
            warnings.append({
                "code": "unparseable_folder",
                "message": f"Could not read a student ID from folder "
                           f"'{folder_name}'. It was skipped.",
            })
            continue

        entry = students.setdefault(sid, {"name": sname, "folders": set(), "files": [], "issues": set()})
        if entry["folders"] and folder_name not in entry["folders"]:
            entry["issues"].add("duplicate_folder")
        entry["folders"].add(folder_name)
        if sname and not entry["name"]:
            entry["name"] = sname
        entry["files"].append(name)

    if not students:
        blocking_errors.append({
            "code": "no_students",
            "message": "No student folders could be identified in this archive. "
                       "Make sure each student's scripts are inside their own "
                       "folder, named like '001_John Doe'.",
        })
        return _build_manifest([], warnings, blocking_errors)

    for sid, data in students.items():
        if "duplicate_folder" in data["issues"]:
            warnings.append({
                "code": "duplicate_student",
                "message": f"Student ID '{sid}' appears in more than one folder "
                           "— their pages were combined. Please verify this is correct.",
            })

    # ── Extract + validate content, per student (in parallel) ───────────────
    zip_lock = threading.Lock()   # one reader at a time on the shared archive
    with ThreadPoolExecutor(max_workers=EXTRACT_WORKERS) as pool:
        results = list(pool.map(
            lambda sid: _extract_student(zf, zip_lock, sid, students[sid], staging_dir),
            sorted(students.keys())))
    students_out = []
    for out, student_warnings in results:   # merged in ID order — deterministic
        students_out.append(out)
        warnings.extend(student_warnings)

    return _build_manifest(students_out, warnings, blocking_errors)


def _extract_student(zf, zip_lock, sid: str, data: dict, staging_dir: Path):
    """Extracts + checks one student's files. Returns (manifest entry, warnings)."""
    warnings = []
    dest_folder = staging_dir / f"student_{sid}"
    dest_folder.mkdir(parents=True, exist_ok=True)

    valid_pages = []
    flagged_pages = []   # [{"filename": saved page name, "reason": str}]
    # page2 before page10 — plain sorting sent long scripts out of order (D2.4)
    for zname in sorted(data["files"], key=natural_key):
        leaf = zname.rsplit("/", 1)[-1]
        ext  = Path(leaf).suffix.lower()
        try:
            with zip_lock:
                raw = zf.read(zname)
        except Exception:
            warnings.append({"code": "read_error", "message": f"Could not read '{leaf}' from the archive — skipped."})
            continue

        if len(raw) == 0:
            warnings.append({"code": "empty_file", "message": f"Skipped empty file: '{leaf}' (Student {sid})."})
            continue

        if ext in IMAGE_EXTS:
            try:
                Image.open(io.BytesIO(raw)).verify()
            except Exception:
                warnings.append({"code": "corrupted_image", "message": f"Skipped corrupted image: '{leaf}' (Student {sid})."})
                continue
        elif ext == ".pdf":
            if raw[:5] != b"%PDF-":
                warnings.append({"code": "corrupted_pdf", "message": f"Skipped corrupted PDF: '{leaf}' (Student {sid})."})
                continue

        valid_pages.append((ext, raw, leaf))

    # PDF answer scripts are rendered to one JPEG per page here (D5.1), so
    # the marker only ever sees page images; each PDF is kept alongside
    # as original.pdf / original_2.pdf.
    page_no, pdf_no = 0, 0
    for ext, raw, leaf in valid_pages:
        if ext == ".pdf":
            try:
                rendered = pdf_to_page_images(raw)
            except Exception:
                rendered = []
            if not rendered:
                warnings.append({"code": "corrupted_pdf", "message": f"Skipped unreadable PDF: '{leaf}' (Student {sid})."})
                continue
            pdf_no += 1
            (dest_folder / ("original.pdf" if pdf_no == 1 else f"original_{pdf_no}.pdf")).write_bytes(raw)
            for img in rendered:
                page_no += 1
                (dest_folder / f"page_{page_no}.jpg").write_bytes(img)
            continue

        page_no += 1
        page_name = f"page_{page_no}{ext}"
        (dest_folder / page_name).write_bytes(raw)

        # Quality check runs on photos/scans only (pages rendered from a
        # PDF above are exempt — a clean white PDF page trips the
        # "washed out" check). Never blocks the upload; this only flags the page so
        # the teacher can see and decide whether to fix or proceed.
        is_ok, reason, _score = _quality_checker.validate_image_bytes(raw)
        if not is_ok:
            flagged_pages.append({
                "filename": leaf,
                "saved_as": page_name,
                "reason":   reason,
            })

    issues = set(data["issues"])
    if not page_no:
        status = "error"
        issues.add("no_valid_pages")
        shutil.rmtree(dest_folder, ignore_errors=True)
    elif not data["name"]:
        status = "check"
        issues.add("name_missing")
    elif flagged_pages:
        status = "check"
        issues.add("poor_image_quality")
    elif issues:
        status = "check"
    else:
        status = "ready"

    if flagged_pages:
        names = ", ".join(f"'{p['filename']}' ({p['reason']})" for p in flagged_pages)
        warnings.append({
            "code": "poor_image_quality",
            "message": f"Student {sid}: possible image quality issue — {names}. "
                       f"You can still upload this batch; only this student's affected pages may be under-marked.",
        })

    return {
        "id":     sid,
        "name":   data["name"],
        "pages":  page_no,
        "status": status,
        "issues": [ISSUE_TEXT.get(code, code) for code in sorted(issues)],
        "flagged_pages": flagged_pages,
    }, warnings
