import os
import re
import time
import json
import uuid
import shutil
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file

from config import Config
from excel_generator import generate_exam_spreadsheet
from settings import SettingsManager
from queue_manager import get_queue_manager
from key_rotator import key_rotator
from image_validator import ImageValidator
from storage import read_json, update_json, write_json_atomic, load_metadata_cached
from page_prep import is_pdf_bytes, pdf_to_page_images, convert_pdf_pages_in_folder
from rubric_check import rubric_total_warning
import batch_processor
import users_store

app = Flask(__name__)
app.secret_key = Config.SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
settings_manager = SettingsManager(Config.BASE_DIR)

# ── Staging area for batch-ZIP uploads — cleared per-batch after confirm/discard ──
BATCH_STAGING_ROOT = Config.UPLOAD_FOLDER / "_batch_staging"
BATCH_ID_RE = re.compile(r"^[a-f0-9]{6,16}$")

# Student IDs become folder names (student_<id>) — letters, digits, space,
# dot, dash, underscore only, so an ID can never point outside the session.
STUDENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,63}$")

# ── Start the background marking queue on boot ──────────────────────────────
get_queue_manager()   # instantiates & starts all worker threads once

_page_quality_checker = ImageValidator()


# ─────────────────────────────────────────────
# Sign-in (section 7) — every page and API needs a signed-in teacher,
# except the sign-in page itself and static files.
# ─────────────────────────────────────────────

PUBLIC_ENDPOINTS = {"login", "static"}
_LOGIN_WINDOW_SECS = 300
_LOGIN_MAX_FAILURES = 8
_login_failures: dict = {}   # ip -> [timestamps] (in memory; one process)


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if session.get("user"):
        return None
    wants_json = (request.path.startswith("/api/") or request.method != "GET"
                  or request.accept_mimetypes.best == "application/json")
    if wants_json:
        return jsonify({"success": False, "login_required": True,
                        "message": "Your sign-in has expired. Please refresh the page and sign in again."}), 401
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


def _safe_next(target: str) -> str:
    target = (target or "").strip()
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("index")


def _current_user() -> str:
    return (session.get("user") or {}).get("username", "")


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = _safe_next(request.values.get("next", ""))
    if request.method == "GET":
        if session.get("user"):
            return redirect(next_url)
        return render_template("login.html", next_url=next_url, username="", error=None,
                               no_users=not users_store.has_users())

    ip = request.remote_addr or "?"
    now = time.time()
    recent = [t for t in _login_failures.get(ip, []) if now - t < _LOGIN_WINDOW_SECS]
    username = (request.form.get("username") or "").strip()
    if len(recent) >= _LOGIN_MAX_FAILURES:
        return render_template("login.html", next_url=next_url, username=username,
                               error="Too many attempts. Please wait a few minutes and try again.",
                               no_users=False), 429

    user = users_store.verify(username, request.form.get("password") or "")
    if not user:
        recent.append(now)
        _login_failures[ip] = recent
        return render_template("login.html", next_url=next_url, username=username,
                               error="Wrong username or password.",
                               no_users=not users_store.has_users()), 401

    _login_failures.pop(ip, None)
    exam_session = session.get("exam_session")
    session.clear()
    session.permanent = True
    session["user"] = user
    if exam_session:
        session["exam_session"] = exam_session
    return redirect(next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─────────────────────────────────────────────
# JSON helpers  (no database) — every write is locked + atomic (storage.py)
# ─────────────────────────────────────────────

def _safe_component(value: str) -> str:
    """A folder-name component from a form field; rejects path tricks."""
    v = (value or "").strip()
    if not v or v in (".", "..") or "/" in v or "\\" in v or "\x00" in v:
        raise ValueError(f"Invalid value: {value!r}")
    return v


def get_exam_folder(year, term, class_name, stream, subject, exam_type) -> Path:
    folder = (Config.UPLOAD_FOLDER / _safe_component(str(year)) / _safe_component(term)
              / f"{_safe_component(class_name)}_{_safe_component(stream)}"
              / _safe_component(subject) / _safe_component(exam_type))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _meta_path(exam_folder: Path) -> Path:
    return exam_folder / "students_metadata.json"


def _session_folder(session_id: str):
    """
    The exam folder for a session_id sent by the browser, or None. It must
    exist, contain students_metadata.json, and sit inside the uploads
    folder — a session_id can never be used to read or write elsewhere.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return None
    candidates = [Path(session_id)]
    if not Path(session_id).is_absolute():
        candidates.append(Path("/" + session_id))   # <path:> routes drop the leading slash
    root = Config.UPLOAD_FOLDER.resolve()
    for p in candidates:
        try:
            rp = p.resolve()
        except (OSError, RuntimeError):
            continue
        if root in rp.parents and _meta_path(rp).exists():
            return p
    return None


def _load_meta(exam_folder: Path) -> dict:
    data = read_json(_meta_path(exam_folder), None)
    if not isinstance(data, dict):
        return {"exam_info": {}, "students": []}
    return data


def _save_meta(exam_folder: Path, data: dict):
    write_json_atomic(_meta_path(exam_folder), data)


def _update_meta(exam_folder: Path, mutate):
    return update_json(_meta_path(exam_folder), mutate, default={"exam_info": {}, "students": []})


def init_exam_meta(exam_folder: Path, exam_info: dict):
    _save_meta(exam_folder, {"exam_info": exam_info, "students": []})


def get_students(exam_folder: Path) -> list:
    return _load_meta(exam_folder).get("students", [])


def _student_folder_name(student: dict) -> str:
    return student.get("folder") or f"student_{student.get('id')}"


def _same_name(a: str, b: str) -> bool:
    return " ".join((a or "").split()).casefold() == " ".join((b or "").split()).casefold()


def _id_conflict(exam_folder: Path, sid: str, sname: str):
    """The existing student using this ID under a DIFFERENT name, if any."""
    for s in get_students(exam_folder):
        if str(s.get("id")) == str(sid) and not _same_name(s.get("student_name", ""), sname):
            return s
    return None


def _has_teacher_final(exam_folder: Path, sid: str) -> bool:
    """A teacher's override is the final mark — such a student is never sent to the AI again."""
    return any(str(s.get("id")) == str(sid) and s.get("overridden") for s in get_students(exam_folder))


def add_or_update_student(exam_folder: Path, sid: str, sname: str, page_count: int) -> bool:
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        user = _current_user()

        def mutate(data):
            students = data.setdefault("students", [])
            existing = next((s for s in students if s.get("id") == sid), None)
            if existing:
                existing["total_pages"] = page_count
                existing["sync_time"]   = now
                existing.setdefault("folder", f"student_{sid}")
                if not existing.get("marked"):
                    existing["status"] = "Uploaded"
                existing.pop("failure_reason", None)
            else:
                students.append({
                    "id":                    sid,
                    "student_id":            sid,
                    "student_name":          sname,
                    "total_pages":           page_count,
                    "status":                "Uploaded",
                    "marked":                False,
                    "total_score":           0,
                    "sync_time":             now,
                    "questions":             {},
                    "overall_feedback":      "",
                    "strengths":             [],
                    "areas_for_improvement": [],
                    "folder":                f"student_{sid}",
                    "created_by":            user,
                })

        _update_meta(exam_folder, mutate)
        return True
    except Exception as e:
        print(f"add_or_update_student error: {e}")
        return False


def apply_manual_override(exam_folder: Path, student_id: str, new_name: str,
                          new_id: str, new_marks: float) -> dict:
    """
    Applies a teacher's manual override of a student's Name / ID / final
    mark on top of the AI-generated result.

    The AI-generated mark is NEVER overwritten — the first time a student
    is overridden, their current total_score (whatever the AI produced) is
    copied into `ai_score` (and the original name/id into `ai_name` /
    `ai_id`) so that value is preserved permanently, even across repeated
    edits. `total_score` itself always holds the CURRENT final value used
    everywhere downstream (student report, session report, spreadsheet
    export) so nothing else needs to know an override happened.

    The student's folder on disk is never renamed; the record keeps pointing
    at it through `folder`, so re-marking still finds the pages after an ID
    change. An ID already used by another student in the session is refused.
    """
    max_marks = 100.0  # scores are stored as percentages (0-100)
    try:
        new_marks = float(new_marks)
    except (TypeError, ValueError):
        return {"success": False, "message": "Marks must be a number."}
    if new_marks < 0 or new_marks > max_marks:
        return {"success": False, "message": f"Marks must be between 0 and {max_marks}."}

    new_name = (new_name or "").strip()
    new_id   = (new_id or "").strip()
    if not new_name or not new_id:
        return {"success": False, "message": "Name and ID cannot be empty."}
    if not STUDENT_ID_RE.match(new_id):
        return {"success": False, "message": "ID can only use letters, numbers, spaces, dots, dashes and underscores."}

    outcome = {}

    def mutate(data):
        students = data.get("students", [])
        target = None
        for student in students:
            if str(student.get("id")) == str(student_id) or str(student.get("student_id")) == str(student_id):
                target = student
                break

        if target is None:
            outcome.update({"success": False, "message": "Student not found in this session."})
            return

        if any(s is not target and str(s.get("id")) == new_id for s in students):
            outcome.update({"success": False,
                            "message": f"Another student in this session already has ID {new_id}."})
            return

        # First-time override: snapshot the untouched AI values.
        if "ai_score" not in target:
            target["ai_score"] = target.get("total_score", 0)
        if "ai_name" not in target:
            target["ai_name"] = target.get("student_name", "")
        if "ai_id" not in target:
            target["ai_id"] = target.get("id", "")

        target.setdefault("folder", f"student_{target.get('id')}")
        old_id = target.get("id")
        target["student_name"] = new_name
        target["id"]           = new_id
        target["total_score"]  = new_marks
        target["final_score"]  = new_marks
        target["overridden"]   = True
        target["overridden_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        target["overridden_by"] = _current_user()

        # A teacher who sets the final mark has reviewed the result.
        if target.get("status") == "Needs review":
            target["status"] = "Marked"
            target["reviewed"] = True

        # Keep the student_id/id in sync everywhere they're duplicated.
        if target.get("student_id") is not None:
            target["student_id"] = new_id

        outcome.update({"success": True, "student": dict(target), "old_id": old_id})

    _update_meta(exam_folder, mutate)
    return outcome


def update_student_marks(exam_folder: Path, sid: str, questions: dict,
                         total_score: float, marking_result: dict):
    try:
        def mutate(data):
            for student in data.get("students", []):
                if student.get("id") == sid:
                    student.update({
                        "status":                "Marked",
                        "marked":                True,
                        "total_score":           total_score,
                        "raw_score":             marking_result.get("raw_score", 0),
                        "max_score":             marking_result.get("max_score", 100),
                        "questions":             questions,
                        "overall_feedback":      marking_result.get("overall_feedback", ""),
                        "strengths":             marking_result.get("strengths", []),
                        "areas_for_improvement": marking_result.get("areas_for_improvement", []),
                    })
                    break
        _update_meta(exam_folder, mutate)
    except Exception as e:
        print(f"update_student_marks error: {e}")


_BACKUP_DIR_RE = re.compile(r"_backup_\d+$")


def _all_meta_files() -> list:
    """
    Every session's students_metadata.json. Same result as rglob(), but it
    never descends into student_* folders (thousands of page images, and
    never a metadata file) or the batch staging area, so it stays fast as
    uploads grow (D2.6).
    """
    root = Config.UPLOAD_FOLDER
    if not root.exists():
        return []
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        # "<exam>_backup_<time>" is the copy kept when a session is
        # overwritten at setup — it is not a live session, so it must not
        # show up as a duplicate on the dashboard or in results.
        dirnames[:] = [d for d in dirnames
                       if not d.startswith("student_") and not _BACKUP_DIR_RE.search(d)
                       and d not in ("_batch_staging", "ai_runs", "previous_uploads")]
        if "students_metadata.json" in filenames:
            found.append(Path(dirpath) / "students_metadata.json")
    return found


def get_statistics() -> dict:
    total_students = total_marked = total_sessions = 0
    for meta_file in _all_meta_files():
        data = load_metadata_cached(meta_file, {}, copy_result=False)
        students = data.get("students", []) if isinstance(data, dict) else []
        if students:
            total_sessions += 1
            total_students += len(students)
            total_marked   += sum(1 for s in students if s.get("marked"))
    return {"total_students": total_students,
            "total_marked":   total_marked,
            "total_sessions": total_sessions}


def get_all_exams() -> list:
    exams = []
    for meta_file in _all_meta_files():
        data = load_metadata_cached(meta_file, {})
        info = data.get("exam_info", {}) if isinstance(data, dict) else {}
        if info:
            info["student_count"] = len(data.get("students", []))
            info["marked_count"]  = sum(1 for s in data.get("students", []) if s.get("marked"))
            exams.append(info)
    return exams


# ─────────────────────────────────────────────
# File saving helpers — NO COMPRESSION.
# Uploaded files are written to disk exactly as received.
# ─────────────────────────────────────────────

def read_and_check_quality(args):
    """
    Reads an uploaded page fully into memory and runs the blur/brightness
    quality check on it before anything touches disk. Individual upload
    blocks outright on a bad page (no "upload anyway" — see /upload-batch),
    so we need the verdict before committing any bytes.
    """
    file_obj, idx = args
    orig_name = getattr(file_obj, "filename", "") or f"page_{idx + 1}"
    ext = orig_name.rsplit(".", 1)[-1].lower() if "." in orig_name else "jpg"
    if ext not in ("jpg", "jpeg", "png", "webp", "pdf"):
        ext = "jpg"
    try:
        raw = file_obj.read()
    except Exception as e:
        return {"idx": idx, "filename": orig_name, "ext": ext, "raw": b"",
                "ok": False, "reason": "Could not read uploaded file."}

    if not raw:
        return {"idx": idx, "filename": orig_name, "ext": ext, "raw": raw,
                "ok": False, "reason": "Empty file."}

    # PDFs are recognised by their content (the upload page names every file
    # page_N.jpg). They are exempt from the blur check and are converted to
    # page images before saving (D5.1).
    if ext == "pdf" or is_pdf_bytes(raw):
        return {"idx": idx, "filename": orig_name, "ext": "pdf", "raw": raw, "ok": True, "reason": ""}

    is_ok, reason, _score = _page_quality_checker.validate_image_bytes(raw)
    return {"idx": idx, "filename": orig_name, "ext": ext, "raw": raw, "ok": is_ok, "reason": reason}


def _set_aside_previous_upload(student_folder: Path) -> int:
    """
    On a re-upload, the student's previous page files are moved (never
    deleted) into previous_uploads/<timestamp>/ so a shorter new script can't
    leave stale pages behind for the AI to read.
    """
    if not student_folder.exists():
        return 0
    old = [p for p in student_folder.iterdir()
           if p.is_file() and (p.name.startswith("page_") or p.name.startswith("original"))]
    if not old:
        return 0
    dest = student_folder / "previous_uploads" / datetime.now().strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    for p in old:
        shutil.move(str(p), str(dest / p.name))
    return len(old)


def _expand_pages(checked: list):
    """
    Turns the uploaded files (in order) into (page_bytes, ext) entries,
    rendering each PDF into one JPEG per page (≈200 DPI). Returns
    (pages, pdf_originals). Raises ValueError for an unreadable PDF.
    """
    pages, pdfs = [], []
    for r in checked:
        if r["ext"] == "pdf":
            try:
                rendered = pdf_to_page_images(r["raw"])
            except Exception:
                raise ValueError(f"'{r['filename']}' is not a readable PDF.")
            if not rendered:
                raise ValueError(f"'{r['filename']}' has no pages.")
            pages += [(img, "jpg") for img in rendered]
            pdfs.append(r["raw"])
        else:
            pages.append((r["raw"], r["ext"]))
    return pages, pdfs


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.route("/")
def index():
    stats = get_statistics()
    auto_deletion_enabled = settings_manager.is_auto_deletion_enabled()
    return render_template("dashboard.html", stats=stats,
                           auto_deletion_enabled=auto_deletion_enabled)


@app.route("/setup-start")
def setup_start():
    session.pop("exam_session", None)
    return render_template("setup.html",
                           subjects=Config.SUBJECTS,
                           classes=Config.CLASSES,
                           terms=Config.TERMS)


@app.route("/setup", methods=["POST"])
def setup_session():
    try:
        data     = request.form
        required = ["year", "term", "class", "stream", "subject", "exam_type"]
        for field in required:
            if not data.get(field, "").strip():
                return jsonify({"success": False, "message": f"Missing field: {field}"}), 400

        year, term, class_name, stream, subject, exam_type = [
            data.get(f).strip() for f in required
        ]
        for label, value in zip(required, (year, term, class_name, stream, subject, exam_type)):
            try:
                _safe_component(value)
            except ValueError:
                return jsonify({"success": False, "message": f"Invalid {label}: it can't contain / or \\."}), 400
        rubric_text = data.get("rubric_text", "")

        # ── Total marks for this paper — entered once at setup ─────────────
        # Stored with the session and used later by the system (never the AI)
        # to convert each student's raw marks obtained into a percentage.
        total_marks_raw = data.get("total_marks", "").strip()
        try:
            total_marks = float(total_marks_raw) if total_marks_raw else 100.0
        except ValueError:
            total_marks = 100.0
        if total_marks <= 0:
            total_marks = 100.0
        # Keep whole numbers clean (e.g. 100 instead of 100.0) in stored JSON
        if total_marks == int(total_marks):
            total_marks = int(total_marks)

        exam_folder       = get_exam_folder(year, term, class_name, stream, subject, exam_type)
        existing_students = get_students(exam_folder)

        if existing_students and data.get("force_overwrite") != "true":
            return jsonify({
                "success":           False,
                "duplicate_session": True,
                "student_count":     len(existing_students),
                "message":           f"{len(existing_students)} student(s) already exist in this session.",
                "session_info": {
                    "year": year, "term": term, "class": class_name,
                    "stream": stream, "subject": subject, "exam_type": exam_type,
                    "total_marks": total_marks,
                }
            }), 409

        if data.get("force_overwrite") == "true" and exam_folder.exists():
            backup = exam_folder.parent / f"{exam_folder.name}_backup_{int(time.time())}"
            shutil.move(str(exam_folder), str(backup))
            exam_folder = get_exam_folder(year, term, class_name, stream, subject, exam_type)

        init_exam_meta(exam_folder, {
            "year": year, "term": term, "class": class_name,
            "stream": stream, "subject": subject, "exam_type": exam_type,
            "total_marks": total_marks,
            "created_by": _current_user(),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

        rubric_uploads = [f for f in request.files.getlist("rubrics[]") if f and f.filename]
        qp_uploads     = [f for f in request.files.getlist("question_papers[]") if f and f.filename]

        # Re-running setup on an existing session replaces its materials. Old
        # files must go first: a 2-page question paper uploaded over an old
        # 5-page one used to leave pages 3–5 behind, and the AI read them all.
        # A group is only cleared when new material for it was provided.
        stale = []
        if qp_uploads:
            stale += list(exam_folder.glob("question_paper_*"))
        if rubric_uploads or rubric_text.strip():
            stale += list(exam_folder.glob("rubric_*"))
        for old_file in stale:
            if old_file.is_file():
                old_file.unlink()

        def save_files(file_list, prefix):
            """Saves uploaded files exactly as received — no compression."""
            saved = []
            for f in file_list:
                ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
                if ext not in ("pdf", "jpg", "jpeg", "png", "webp"):
                    print(f"⚠️  Skipped {prefix} upload with unsupported type: {f.filename!r}")
                    continue
                filename = f"{prefix}_{len(saved) + 1}.{ext}"
                f.save(str(exam_folder / filename))
                saved.append(filename)
            return saved

        with ThreadPoolExecutor(max_workers=2) as pool:
            rf = pool.submit(save_files, rubric_uploads, "rubric")
            qf = pool.submit(save_files, qp_uploads,     "question_paper")
            saved_rubrics = rf.result()
            saved_qps     = qf.result()

        if rubric_text.strip():
            with open(exam_folder / "rubric_text.txt", "w", encoding="utf-8") as f:
                f.write(rubric_text)
            saved_rubrics.append("rubric_text.txt")

        session["exam_session"] = {
            "year":        year,
            "term":        term,
            "class":       class_name,
            "stream":      stream,
            "subject":     subject,
            "exam_type":   exam_type,
            "total_marks": total_marks,
            "exam_folder": str(exam_folder),
        }

        # ── D3.3 — do the readable rubric stage marks add up to the total
        #    entered? A warning only; setup has already completed. ────────
        rubric_warning = None
        try:
            rubric_warning = rubric_total_warning(exam_folder, rubric_text, total_marks)
        except Exception as e:
            print(f"rubric check skipped: {e}")

        print(f"✅ Session ready — QP:{len(saved_qps)} Rubrics:{len(saved_rubrics)} Total marks:{total_marks}")

        return jsonify({
            "success":     True,
            "overwritten": data.get("force_overwrite") == "true",
            "message":     (
                f"Setup complete: {len(saved_qps)} paper(s), "
                f"{len(saved_rubrics)} rubric(s) saved. Total marks for paper: {total_marks}. Ready to mark."
            ),
            "files_saved": {"question_papers": len(saved_qps), "rubrics": len(saved_rubrics)},
            "total_marks": total_marks,
            "rubric_warning": rubric_warning,
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"success": False, "message": "Setup failed. Please try again."}), 500


@app.route("/upload-student")
def upload_student():
    if "exam_session" not in session:
        return redirect(url_for("setup_start"))
    return render_template("upload_student.html", exam_data=session["exam_session"])


# ─────────────────────────────────────────────
# Upload method choice (individual vs batch ZIP)
# ─────────────────────────────────────────────

@app.route("/choose-method")
def choose_method():
    """Shown right after /setup succeeds — lets the teacher pick between
    marking students one at a time or uploading a whole class as a ZIP."""
    if "exam_session" not in session:
        return redirect(url_for("setup_start"))
    return render_template("choose_method.html", exam_data=session["exam_session"])


@app.route("/batch-upload")
def batch_upload_page():
    if "exam_session" not in session:
        return redirect(url_for("setup_start"))
    return render_template("batch_upload.html", exam_data=session["exam_session"])


# ─────────────────────────────────────────────
# Batch ZIP upload API — validate/extract, review, confirm
# ─────────────────────────────────────────────

@app.route("/api/batch/upload", methods=["POST"])
def api_batch_upload():
    """
    Accepts a ZIP of student folders, runs the full security/quality audit
    in batch_processor, and extracts everything that passed into a staging
    folder. Nothing is queued for marking yet — the teacher reviews the
    manifest first and explicitly confirms via /api/batch/confirm.
    """
    if "exam_session" not in session:
        return jsonify({"success": False, "message": "Session expired. Please set up a new marking session."}), 400

    f = request.files.get("batch_zip")
    if not f or not f.filename:
        return jsonify({"success": False, "message": "Please choose a ZIP file to upload."}), 400
    if not f.filename.lower().endswith(".zip"):
        return jsonify({"success": False, "message": "Only .zip files are supported for batch upload."}), 400

    batch_id = uuid.uuid4().hex[:12]
    staging_dir = BATCH_STAGING_ROOT / batch_id
    staging_dir.mkdir(parents=True, exist_ok=True)
    zip_path = staging_dir / "upload.zip"

    try:
        f.save(str(zip_path))
        manifest = batch_processor.validate_and_extract_batch(zip_path, staging_dir)
    except ValueError:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return jsonify({
            "success": False,
            "message": "The uploaded file is corrupted or not a valid ZIP archive.",
        }), 400
    except Exception as e:
        print(f"Batch upload error: {e}")
        import traceback; traceback.print_exc()
        shutil.rmtree(staging_dir, ignore_errors=True)
        return jsonify({"success": False, "message": "Could not process this ZIP file. Please try again."}), 500
    finally:
        zip_path.unlink(missing_ok=True)

    manifest["batch_id"] = batch_id
    try:
        write_json_atomic(staging_dir / "manifest.json", manifest)
    except Exception as e:
        print(f"Could not persist batch manifest: {e}")

    return jsonify({"success": True, "manifest": manifest})


@app.route("/api/batch/discard", methods=["POST"])
def api_batch_discard():
    """Cleans up a batch's staging folder — called on 'Back' or page unload."""
    data = request.get_json(force=True, silent=True) or {}
    batch_id = (data.get("batch_id") or "").strip()
    if batch_id and BATCH_ID_RE.match(batch_id):
        shutil.rmtree(BATCH_STAGING_ROOT / batch_id, ignore_errors=True)
    return jsonify({"success": True})


@app.route("/api/batch/confirm", methods=["POST"])
def api_batch_confirm():
    """
    Moves every 'ready' / 'check' student from the batch staging folder into
    the real exam session folder, writes them into students_metadata.json,
    and enqueues each one on the same background marking queue individual
    uploads use. Students with no valid pages ('error') are left out, and so
    is a student whose ID already belongs to a different student here.
    """
    if "exam_session" not in session:
        return jsonify({"success": False, "message": "Session expired. Please set up a new marking session."}), 400

    data = request.get_json(force=True, silent=True) or {}
    batch_id = (data.get("batch_id") or "").strip()
    if not batch_id or not BATCH_ID_RE.match(batch_id):
        return jsonify({"success": False, "message": "Invalid batch reference."}), 400

    staging_dir = BATCH_STAGING_ROOT / batch_id
    manifest_path = staging_dir / "manifest.json"
    if not staging_dir.exists() or not manifest_path.exists():
        return jsonify({"success": False, "message": "This batch was not found — it may have already been processed or discarded."}), 404

    manifest = read_json(manifest_path, None)
    if not isinstance(manifest, dict):
        return jsonify({"success": False, "message": "Could not read the batch review data. Please re-upload."}), 500

    if manifest.get("severity") == "blocking":
        return jsonify({"success": False, "message": "This batch has unresolved blocking issues and cannot be marked yet."}), 400

    info        = session["exam_session"]
    exam_folder = Path(info["exam_folder"])
    qm          = get_queue_manager()
    queued      = 0
    skipped     = 0
    conflicts   = []
    teacher_kept = []

    for stu in manifest.get("students", []):
        if stu.get("status") == "error":
            skipped += 1
            continue

        sid   = str(stu["id"])
        sname = (stu.get("name") or "").strip() or f"Student {sid}"
        src_folder = staging_dir / f"student_{sid}"
        if not src_folder.exists() or not STUDENT_ID_RE.match(sid):
            skipped += 1
            continue

        other = _id_conflict(exam_folder, sid, sname)
        if other:
            conflicts.append(f"ID {sid} already belongs to {other.get('student_name', 'another student')}")
            skipped += 1
            continue

        dest_folder = exam_folder / f"student_{sid}"
        dest_folder.mkdir(parents=True, exist_ok=True)
        _set_aside_previous_upload(dest_folder)
        for page_file in sorted(src_folder.iterdir()):
            shutil.move(str(page_file), str(dest_folder / page_file.name))

        page_count = len([p for p in dest_folder.glob("page_*") if p.is_file()])
        if page_count == 0:
            skipped += 1
            continue

        add_or_update_student(exam_folder, sid, sname, page_count)
        if _has_teacher_final(exam_folder, sid):
            teacher_kept.append(sname)
            continue
        qm.enqueue(
            student_id=sid, student_name=sname,
            exam_folder=str(exam_folder), exam_info=info,
            page_count=page_count, folder=f"student_{sid}",
        )
        queued += 1

    shutil.rmtree(staging_dir, ignore_errors=True)

    message = f"{queued} student(s) queued for AI marking." + (f" {skipped} skipped." if skipped else "")
    if conflicts:
        message += " Not added: " + "; ".join(conflicts) + "."
    if teacher_kept:
        message += (" Pages saved but not re-marked (teacher's final mark kept): "
                    + ", ".join(teacher_kept) + ".")
    return jsonify({
        "success": True,
        "queued":  queued,
        "skipped": skipped,
        "conflicts": conflicts,
        "message": message,
    })


@app.route("/upload-batch", methods=["POST"])
def upload_batch():
    """
    Saves student pages to disk (no compression) then immediately enqueues
    the student for AI marking via the background queue. Returns instantly —
    the browser resets right away and the teacher can process the next student.
    """
    if "exam_session" not in session:
        return jsonify({"success": False, "message": "Session expired. Please set up a new marking session."}), 400

    info        = session["exam_session"]
    exam_folder = Path(info["exam_folder"])
    files       = request.files.getlist("files[]")
    sid         = request.form.get("student_id", "").strip()
    sname       = request.form.get("student_name", "").strip()

    if not sid or not sname:
        return jsonify({"success": False, "message": "Student ID and Name are required."}), 400
    if not STUDENT_ID_RE.match(sid):
        return jsonify({"success": False, "message": "Student ID can only use letters, numbers, spaces, dots, dashes and underscores."}), 400
    if not files:
        return jsonify({"success": False, "message": "No pages were uploaded."}), 400

    # Same ID + same name = normal re-upload. Same ID + different name would
    # overwrite another student's pages, so it's refused.
    other = _id_conflict(exam_folder, sid, sname)
    if other:
        return jsonify({
            "success": False,
            "message": (f"Another student in this session already has ID {sid} "
                        f"({other.get('student_name', 'unknown')}). Check the ID, or use the same name to re-upload."),
        }), 400

    student_folder = exam_folder / f"student_{sid}"

    # ── Quality gate — read + check every page BEFORE anything is written
    #    to disk. Individual upload blocks outright on a bad page (unlike
    #    batch, which flags-and-continues); nothing is saved unless every
    #    page clears the check, so there's never a half-saved student folder
    #    left behind by a rejected upload. ──────────────────────────────────
    with ThreadPoolExecutor(max_workers=6) as pool:
        checked = list(pool.map(read_and_check_quality, [(files[i], i) for i in range(len(files))]))
    checked.sort(key=lambda r: r["idx"])

    bad_pages = [r for r in checked if not r["ok"]]
    if bad_pages:
        return jsonify({
            "success": False,
            "message": (
                f"{len(bad_pages)} page(s) are too blurry or poor quality to mark accurately. "
                "Retake or replace them and try again."
            ),
            "bad_pages": [
                {"index": r["idx"], "filename": r["filename"], "reason": r["reason"]}
                for r in bad_pages
            ],
        }), 422

    # ── PDF answer scripts become page images here (D5.1) — the marker only
    #    ever sees page images. The PDF itself is kept as original.pdf. ──────
    try:
        pages, pdf_originals = _expand_pages(checked)
    except ValueError as e:
        return jsonify({"success": False, "message": f"{e} Please upload it again or send photos of the pages."}), 400

    student_folder.mkdir(parents=True, exist_ok=True)
    _set_aside_previous_upload(student_folder)

    for i, raw in enumerate(pdf_originals, start=1):
        (student_folder / ("original.pdf" if i == 1 else f"original_{i}.pdf")).write_bytes(raw)

    # ── All pages passed — save in parallel, original bytes, no compression ──
    valid_count = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(lambda n, raw, ext: (student_folder / f"page_{n}.{ext}").write_bytes(raw), n, raw, ext)
                   for n, (raw, ext) in enumerate(pages, start=1)]
        for fut in as_completed(futures):
            try:
                fut.result()
                valid_count += 1
            except Exception as e:
                print(f"page save error: {e}")

    if valid_count == 0:
        return jsonify({"success": False, "message": "No valid images could be saved. Please re-capture the pages."}), 400

    if not add_or_update_student(exam_folder, sid, sname, valid_count):
        return jsonify({"success": False, "message": "Could not save the student record. Please try again."}), 500

    if _has_teacher_final(exam_folder, sid):
        return jsonify({
            "success":      True,
            "queued":       False,
            "total_saved":  valid_count,
            "student_id":   sid,
            "student_name": sname,
            "message":      (f"{sname}'s pages were saved. They already have a final mark set by a teacher, "
                             "so the AI will not re-mark them."),
        })

    # ── Enqueue for background AI marking ───────────────────────────────────
    qm  = get_queue_manager()
    job = qm.enqueue(
        student_id   = sid,
        student_name = sname,
        exam_folder  = str(exam_folder),
        exam_info    = info,
        page_count   = valid_count,
        folder       = f"student_{sid}",
    )
    print(f"📥 {sid} ({sname}) enqueued as job {job.job_id} — {valid_count} pages saved")

    return jsonify({
        "success":     True,
        "queued":      True,
        "job_id":      job.job_id,
        "total_saved": valid_count,
        "student_id":  sid,
        "student_name": sname,
        "message":     f"{sname} uploaded successfully. AI marking will begin shortly.",
    })


# ─────────────────────────────────────────────
# Queue API routes
# ─────────────────────────────────────────────

@app.route("/api/queue/status")
def api_queue_status():
    """Poll-able endpoint for the front-end queue panel."""
    try:
        return jsonify(get_queue_manager().get_status())
    except Exception as e:
        return jsonify({"error": "Could not retrieve queue status."}), 500


@app.route("/api/queue/pause", methods=["POST"])
def api_queue_pause():
    try:
        get_queue_manager().pause()
        return jsonify({"success": True, "message": "Queue paused."})
    except Exception:
        return jsonify({"success": False, "message": "Could not pause queue."}), 500


@app.route("/api/queue/resume", methods=["POST"])
def api_queue_resume():
    try:
        get_queue_manager().resume()
        return jsonify({"success": True, "message": "Queue resumed."})
    except Exception:
        return jsonify({"success": False, "message": "Could not resume queue."}), 500


@app.route("/api/queue/cancel/<job_id>", methods=["POST"])
def api_queue_cancel(job_id):
    try:
        ok = get_queue_manager().cancel_job(job_id)
        if ok:
            return jsonify({"success": True, "message": "Job cancelled."})
        return jsonify({"success": False, "message": "Job not found or already running."}), 404
    except Exception:
        return jsonify({"success": False, "message": "Could not cancel job."}), 500


@app.route("/api/queue/retry/<job_id>", methods=["POST"])
def api_queue_retry(job_id):
    try:
        ok = get_queue_manager().retry_job(job_id)
        if ok:
            return jsonify({"success": True, "message": "Job re-queued."})
        return jsonify({"success": False, "message": "Job not found or not in a retryable state."}), 404
    except Exception:
        return jsonify({"success": False, "message": "Could not retry job."}), 500


@app.route("/api/sessions/retry-failed", methods=["POST"])
def api_retry_failed():
    """
    D6.1 — re-queues every student in one session whose marking failed.
    Students already waiting in the queue are left alone. A student
    uploaded as a PDF before PDFs were converted gets its pages rendered now.
    """
    payload = request.get_json(force=True, silent=True) or {}
    exam_folder = _session_folder(payload.get("session_id"))
    if exam_folder is None:
        return jsonify({"success": False, "message": "Session not found."}), 404

    data = _load_meta(exam_folder)
    exam_info = data.get("exam_info", {})
    info = {
        "year": exam_info.get("year", ""), "term": exam_info.get("term", ""),
        "class": exam_info.get("class", ""), "stream": exam_info.get("stream", ""),
        "subject": exam_info.get("subject", ""), "exam_type": exam_info.get("exam_type", ""),
        "total_marks": exam_info.get("total_marks", 100), "exam_folder": str(exam_folder),
    }
    qm = get_queue_manager()
    queued, missing = [], []
    for s in data.get("students", []):
        if s.get("status") != "Failed" or s.get("overridden"):
            continue
        sid = str(s.get("id"))
        if qm.is_queued(str(exam_folder), sid):
            continue
        folder = _student_folder_name(s)
        sf = exam_folder / folder
        if not sf.exists():
            missing.append(s.get("student_name", sid))
            continue
        try:
            convert_pdf_pages_in_folder(sf)
        except Exception as e:
            print(f"PDF conversion on retry failed for {sid}: {e}")
        pages = len([p for p in sf.glob("page_*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")])
        qm.enqueue(student_id=sid, student_name=s.get("student_name", ""),
                   exam_folder=str(exam_folder), exam_info=info,
                   page_count=pages or s.get("total_pages", 0), folder=folder)
        queued.append(sid)

    if queued:
        def mutate(d):
            for s in d.get("students", []):
                if str(s.get("id")) in queued and s.get("status") == "Failed":
                    s["status"] = "Uploaded"
                    s.pop("failure_reason", None)
        _update_meta(exam_folder, mutate)

    msg = f"{len(queued)} student(s) re-queued for marking." if queued else "No failed students to retry."
    if missing:
        msg += f" {len(missing)} could not be retried because their pages are missing — upload them again."
    return jsonify({"success": True, "queued": len(queued), "message": msg})


# ─────────────────────────────────────────────
# Manual override (teacher corrections after AI marking)
# ─────────────────────────────────────────────

@app.route("/api/student/override", methods=["POST"])
def api_student_override():
    """
    Persists a teacher's manual correction of a student's Name, ID, and/or
    final mark. session_id is the exam folder path (same convention used by
    /resume-session and /api/sessions — see the docstring on resume_session
    for why a query/JSON field is used instead of a URL path segment).

    The AI-generated mark/name/id are preserved (see apply_manual_override)
    so nothing downstream — student report, session report, spreadsheet
    export — ever loses track of what the AI originally produced.
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        session_id = (payload.get("session_id") or "").strip()
        student_id = (payload.get("student_id") or "").strip()
        new_name   = payload.get("name", "")
        new_id     = payload.get("id", "")
        new_marks  = payload.get("marks")

        if not session_id or not student_id:
            return jsonify({"success": False, "message": "session_id and student_id are required."}), 400
        if new_marks is None:
            return jsonify({"success": False, "message": "marks is required."}), 400

        exam_folder = _session_folder(session_id)
        if exam_folder is None:
            return jsonify({"success": False, "message": "Session not found."}), 404

        result = apply_manual_override(exam_folder, student_id, new_name, new_id, new_marks)
        if not result.get("success"):
            return jsonify(result), 400

        return jsonify({
            "success": True,
            "message": "Override saved.",
            "student": {
                "id":            result["student"].get("id"),
                "name":          result["student"].get("student_name"),
                "total_score":   result["student"].get("total_score"),
                "ai_score":      result["student"].get("ai_score"),
                "overridden":    True,
            },
        })
    except Exception as e:
        print(f"override error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"success": False, "message": "Could not save override. Please try again."}), 500


# ─────────────────────────────────────────────
# API key rotation status (monitoring / demo visibility)
# ─────────────────────────────────────────────

@app.route("/api/system/key-status")
def api_key_status():
    """Which Gemini API keys are healthy vs cooling down, for demo/debugging."""
    try:
        return jsonify({"success": True, **key_rotator.status()})
    except Exception:
        return jsonify({"success": False, "message": "Could not retrieve key status."}), 500


# ─────────────────────────────────────────────
# Sessions / Results API
# ─────────────────────────────────────────────

@app.route("/students")
def view_students():
    """Results page — it loads its data from /api/sessions (cached), so the
    route itself does no work."""
    return render_template("view_students.html")


@app.route("/all-exams")
def all_exams():
    """Retired — this pointed at a template that never existed (500 on
    every visit) and nothing in the app links to it. Route removed rather
    than patched since it was dead weight."""
    return redirect(url_for("view_students"))


@app.route("/generate-excel", methods=["POST"])
def generate_excel():
    try:
        data     = request.get_json(silent=True) or {}
        required = ["year", "term", "class", "stream", "subject", "exam_type", "format"]
        for field in required:
            if field not in data:
                return jsonify({"success": False, "message": f"Missing field: {field}"}), 400

        file_format = data["format"].lower()
        if file_format not in ("xlsx", "ods"):
            return jsonify({"success": False, "message": "Format must be xlsx or ods"}), 400
        for field in ("year", "term", "class", "stream", "subject", "exam_type"):
            try:
                _safe_component(str(data[field]))
            except ValueError:
                return jsonify({"success": False, "message": "No data found for this session. Check that all fields match exactly."}), 404

        temp_dir    = Path(tempfile.gettempdir())
        # `columns` is the {id, name, score, feedback, status, …} boolean map
        # sent by the "Columns to Include" checklist in the Generate
        # Spreadsheet modal. Passing it straight through means the exported
        # file only contains the columns the teacher selected — 'pages' is
        # always kept as useful context (see excel_generator._resolve_columns).
        columns = data.get("columns")
        output_file = generate_exam_spreadsheet(
            base_folder=Config.UPLOAD_FOLDER,
            year=data["year"], term=data["term"],
            class_name=data["class"], stream=data["stream"],
            subject=data["subject"], exam_type=data["exam_type"],
            file_format=file_format, output_folder=temp_dir,
            columns=columns,
        )
        mime = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                if file_format == "xlsx"
                else "application/vnd.oasis.opendocument.spreadsheet")
        return send_file(output_file, as_attachment=True,
                         download_name=output_file.name, mimetype=mime)
    except FileNotFoundError:
        return jsonify({"success": False, "message": "No data found for this session. Check that all fields match exactly."}), 404
    except ValueError:
        return jsonify({"success": False, "message": "There are no students in this session yet."}), 404
    except Exception as e:
        print(f"Excel error: {e}")
        return jsonify({"success": False, "message": "Could not generate spreadsheet. Please try again."}), 500


def _review_flags(student: dict) -> dict:
    """
    Additive fields the results page uses for Needs review / Failed badges
    and the stage breakdown — only sent when they apply, so an ordinary
    student's entry (and the response size) is unchanged.
    """
    out = {}
    if student.get("status") == "Needs review":
        out.update(needs_review=True, review_reason=student.get("review_reason", ""))
    elif student.get("status") == "Failed":
        out.update(failed=True, failure_reason=student.get("failure_reason", ""))
    if student.get("stage_scores"):
        out["stage_scores"] = student["stage_scores"]
    return out


# Built session summaries, keyed on the metadata file's mtime + size, so an
# unchanged session costs nothing on the next /api/sessions call (D2.6).
_session_summary_cache: dict = {}


def _session_summary(meta_file: Path) -> dict:
    st = meta_file.stat()
    key = str(meta_file)
    hit = _session_summary_cache.get(key)
    if hit and hit[0] == (st.st_mtime_ns, st.st_size):
        return hit[1]

    data = load_metadata_cached(meta_file, None, copy_result=False)
    if not isinstance(data, dict):
        raise ValueError("unreadable metadata")

    exam_info = data.get("exam_info", {})
    students  = data.get("students", [])
    total_marks_for_paper = exam_info.get("total_marks", 100)

    session_data = {
        "id":            str(meta_file.parent),
        "session_id":    str(meta_file.parent),
        "name":          f"{exam_info.get('subject','Unknown')} - {exam_info.get('exam_type','Exam')}",
        "subject":       exam_info.get("subject", ""),
        "class":         exam_info.get("class", ""),
        "grade":         exam_info.get("class", ""),
        "stream":        exam_info.get("stream", ""),
        "term":          exam_info.get("term", ""),
        "year":          exam_info.get("year", ""),
        "exam_type":     exam_info.get("exam_type", ""),
        "total_marks":   total_marks_for_paper,
        "date":          datetime.fromtimestamp(st.st_mtime).isoformat(),
        "created_at":    datetime.fromtimestamp(st.st_mtime).isoformat(),
        "total_marks_possible": total_marks_for_paper,
        "total_students": len(students),
        "marked_count":  sum(1 for s in students if s.get("marked")),
        "failed_count":  sum(1 for s in students if s.get("status") == "Failed"),
        "review_count":  sum(1 for s in students if s.get("status") == "Needs review"),
        "students":      [],
        "report":        None,
        "report_generating": False,
    }

    for student in students:
        overridden = bool(student.get("overridden"))
        session_data["students"].append({
            "id":                    student.get("id", ""),
            "student_id":            student.get("id", ""),
            "name":                  student.get("student_name", "Unknown"),
            "pages":                 student.get("total_pages", 1),
            "num_pages":             student.get("total_pages", 1),
            "status":                "marked" if student.get("marked") else "pending",
            "score":                 student.get("total_score", 0),
            "total_score":           student.get("total_score", 0),
            "raw_score":             student.get("raw_score", 0),
            "max_score":             student.get("max_score", total_marks_for_paper),
            "total":                 100,
            "diagnostic":            student.get("overall_feedback", ""),
            "comment":               student.get("overall_feedback", ""),
            "feedback":              student.get("overall_feedback", ""),
            "questions":             student.get("questions", {}),
            "strengths":             student.get("strengths", []),
            "areas_for_improvement": student.get("areas_for_improvement", []),
            "_overridden":           overridden,
            "_ai_score":             student.get("ai_score") if overridden else None,
            "_ai_name":              student.get("ai_name") if overridden else None,
            "_ai_id":                student.get("ai_id") if overridden else None,
            **_review_flags(student),
        })

    _session_summary_cache[key] = ((st.st_mtime_ns, st.st_size), session_data)
    return session_data


_sessions_response_cache = {"key": None, "body": None}


@app.route("/api/sessions")
def api_sessions():
    """Return all exam sessions for the dashboard."""
    meta_files = _all_meta_files()
    stats = []
    for meta_file in meta_files:
        try:
            st = meta_file.stat()
            stats.append((str(meta_file), st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    key = tuple(sorted(stats))
    if _sessions_response_cache["key"] == key:     # nothing changed since the last call
        return app.response_class(_sessions_response_cache["body"], mimetype="application/json")

    sessions_out = []
    for meta_file in meta_files:
        try:
            sessions_out.append(_session_summary(meta_file))
        except Exception as e:
            print(f"Error loading session {meta_file}: {e}")

    sessions_out.sort(key=lambda x: x.get("date", ""), reverse=True)
    body = app.json.dumps({"sessions": sessions_out, "total": len(sessions_out)}, separators=(",", ":"))
    _sessions_response_cache.update(key=key, body=body)
    return app.response_class(body, mimetype="application/json")


@app.route("/api/sessions/<path:session_id>")
def api_session_detail(session_id):
    target_path = _session_folder(session_id)
    if target_path is None:
        return jsonify({"error": "Session not found"}), 404

    try:
        data = load_metadata_cached(_meta_path(target_path), {}, copy_result=False)

        exam_info = data.get("exam_info", {})
        students  = data.get("students", [])
        total_marks_for_paper = exam_info.get("total_marks", 100)

        students_data = []
        for student in students:
            overridden = bool(student.get("overridden"))
            students_data.append({
                "id":           student.get("id", ""),
                "student_id":   student.get("id", ""),
                "name":         student.get("student_name", "Unknown"),
                "pages":        student.get("total_pages", 1),
                "status":       "marked" if student.get("marked") else "pending",
                "score":        student.get("total_score", 0),
                "total_score":  student.get("total_score", 0),
                "raw_score":    student.get("raw_score", 0),
                "max_score":    student.get("max_score", total_marks_for_paper),
                "total":        100,
                "diagnostic":   student.get("overall_feedback", ""),
                "questions":    student.get("questions", {}),
                "_overridden":  overridden,
                "_ai_score":    student.get("ai_score") if overridden else None,
                **_review_flags(student),
            })

        return jsonify({
            "id":             session_id,
            "name":           f"{exam_info.get('subject','Exam')} - {exam_info.get('exam_type','')}",
            "subject":        exam_info.get("subject", ""),
            "class":          exam_info.get("class", ""),
            "total_marks":    total_marks_for_paper,
            "students":       students_data,
            "total_students": len(students),
            "marked_count":   sum(1 for s in students if s.get("marked")),
        })
    except Exception as e:
        return jsonify({"error": "Could not load session details."}), 500


@app.route("/api/sessions/<path:session_id>/report")
def api_session_report(session_id):
    """
    The session summary is plain arithmetic over the saved scores — it does
    not call the AI, so there is nothing to cache (checked for D5.3).
    """
    target_path = _session_folder(session_id)
    if target_path is None:
        return jsonify({"report": None})

    try:
        data = load_metadata_cached(_meta_path(target_path), {}, copy_result=False)

        students        = data.get("students", [])
        marked_students = [s for s in students if s.get("marked")]

        if not marked_students:
            return jsonify({"report": None})

        scores    = [s.get("total_score", 0) for s in marked_students]
        avg_score = sum(scores) / len(scores) if scores else 0
        above70   = len([s for s in scores if s >= 70])
        mid       = len([s for s in scores if 50 <= s < 70])
        below50   = len([s for s in scores if s < 50])

        report = (
            f"This session has {len(marked_students)} marked student(s) "
            f"with an average score of {avg_score:.1f}%. "
            f"{above70} scored 70% or above (excellent), "
            f"{mid} scored between 50–69% (satisfactory), and "
            f"{below50} scored below 50% (needs improvement). "
            f"Consider revisiting weaker topics with the lower-scoring group."
        )
        review = [s.get("student_name", s.get("id")) for s in students if s.get("status") == "Needs review"]
        if review:
            report += (f" {len(review)} result(s) are flagged NEEDS REVIEW and should be checked "
                       f"by a teacher before being shared: {', '.join(review)}.")
        return jsonify({"report": report})
    except Exception:
        return jsonify({"report": None})


# ─────────────────────────────────────────────
# Settings routes
# ─────────────────────────────────────────────

@app.route("/settings")
def settings_page():
    from ai_marker_gemini_improved import PRIMARY_MODEL, FALLBACK_MODEL
    auto = settings_manager.load_settings().get("auto_deletion", {})
    return render_template(
        "settings.html", active="settings",
        auto_deletion=auto,
        system={
            "model": PRIMARY_MODEL,
            "fallback_model": FALLBACK_MODEL,
            "keys": key_rotator.status(),
            "workers": get_queue_manager().get_status().get("worker_count"),
        },
    )


@app.route("/help")
def help_page():
    return render_template("help.html", active="help")


@app.route("/settings/enable-auto-deletion", methods=["POST"])
def enable_auto_deletion():
    data = request.get_json(silent=True) or {}
    return jsonify(settings_manager.enable_auto_deletion(confirmed=bool(data.get("confirmed", False))))


@app.route("/settings/disable-auto-deletion", methods=["POST"])
def disable_auto_deletion():
    return jsonify(settings_manager.disable_auto_deletion())


@app.route("/settings/deletion-preview")
def deletion_preview():
    return jsonify(settings_manager.get_deletion_preview(Config.UPLOAD_FOLDER))


@app.route("/settings/deletion-log")
def deletion_log():
    return jsonify({"log": settings_manager.get_deletion_log(limit=50)})


@app.route("/resume-session")
def resume_session():
    """
    Restores an already-set-up (possibly ended) exam session as the active
    session, so the teacher can upload additional students into it — e.g.
    if the session was ended before every student was captured.

    session_id is passed as a query parameter (?session_id=...) rather than
    a path segment, because exam folders are absolute filesystem paths
    (e.g. "/Users/name/.../Term 2/S3_C/Biology/Tests 4") containing slashes
    and spaces — encoding those into a URL *path* segment causes Flask's
    <path:...> converter to 404 on the encoded slashes. Query strings don't
    have that problem.

    Marking relies on the question paper + rubric images that were saved to
    this exact exam_folder at /setup time. AIMarker re-reads them fresh from
    disk on every single AI call (no caching), so simply pointing the active
    session back at this folder is enough for materials to be resent
    correctly for any newly-added student too.
    """
    session_id = request.args.get("session_id", "").strip()
    next_page  = request.args.get("next", "").strip()
    if not session_id:
        return redirect(url_for("view_students"))

    exam_folder = _session_folder(session_id)
    if exam_folder is None:
        print(f"⚠️  resume_session: folder or metadata not found: {session_id}")
        return redirect(url_for("view_students"))
    meta_file = _meta_path(exam_folder)

    # Sanity check: warn loudly (but don't block) if the stored question
    # paper / rubric files are missing from this folder — marking would
    # otherwise silently proceed with no exam materials.
    has_qp     = any(exam_folder.glob("question_paper_*"))
    has_rubric = any(exam_folder.glob("rubric_*")) or (exam_folder / "rubric_text.txt").exists()
    if not has_qp or not has_rubric:
        print(
            f"⚠️  Resuming session at {exam_folder} but "
            f"{'question paper' if not has_qp else ''}"
            f"{' and ' if not has_qp and not has_rubric else ''}"
            f"{'rubric' if not has_rubric else ''} file(s) are missing on disk. "
            f"New students added to this session will be marked without that context."
        )

    try:
        data = read_json(meta_file, {})
        exam_info = data.get("exam_info", {})

        session["exam_session"] = {
            "year":        exam_info.get("year", ""),
            "term":        exam_info.get("term", ""),
            "class":       exam_info.get("class", ""),
            "stream":      exam_info.get("stream", ""),
            "subject":     exam_info.get("subject", ""),
            "exam_type":   exam_info.get("exam_type", ""),
            "total_marks": exam_info.get("total_marks", 100),
            "exam_folder": str(exam_folder),
        }
    except Exception as e:
        print(f"resume_session error: {e}")
        return redirect(url_for("view_students"))

    # The "Add Students" modal in View Results already asked individual-vs-
    # batch once — go straight to the chosen page instead of asking again
    # via /choose-method.
    if next_page == "batch":
        return redirect(url_for("batch_upload_page"))
    return redirect(url_for("upload_student"))


@app.route("/end-session")
def end_session():
    session.pop("exam_session", None)
    return redirect(url_for("index"))


# ─────────────────────────────────────────────
# Startup housekeeping (entry point only)
# ─────────────────────────────────────────────

def cleanup_batch_staging(max_age_hours: float = 24) -> list:
    """
    D4.4 — removes unconfirmed batch uploads older than 24 h. Only folders
    directly inside _batch_staging/ are touched; exam folders never are.
    """
    removed = []
    if not BATCH_STAGING_ROOT.exists():
        return removed
    cutoff = time.time() - max_age_hours * 3600
    for d in BATCH_STAGING_ROOT.iterdir():
        try:
            if d.is_dir() and BATCH_ID_RE.match(d.name) and d.stat().st_mtime < cutoff:
                shutil.rmtree(d)
                removed.append(d.name)
        except OSError as e:
            print(f"⚠️  Could not remove staging folder {d.name}: {e}")
    if removed:
        print(f"🧹 Removed {len(removed)} unconfirmed batch upload(s) older than {max_age_hours:g}h: {', '.join(removed)}")
    return removed


def _lan_addresses() -> list:
    import socket
    addrs = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith("127."))


# ─────────────────────────────────────────────
# Entry point — `python app.py` (section 5): one process, waitress, 8 threads.
# ─────────────────────────────────────────────

if __name__ == "__main__":
    Config.init_app(app)
    if Config.SECRET_KEY == "dev-secret-key-change-in-production":
        print("⚠️  SECRET_KEY is not set in .env — sign-ins are not secure. See README.")
    if not users_store.has_users():
        print("⚠️  No teacher accounts yet — create one with: python manage_users.py add <username>")
    if len(key_rotator) == 0:
        print("⚠️  No Gemini API keys in .env (GEMINI_API_KEY_1 … GEMINI_API_KEY_5) — marking is disabled.")

    cleanup_batch_staging()
    if settings_manager.is_auto_deletion_enabled():
        print("🗑️  Running auto-deletion…")
        settings_manager.run_auto_deletion(Config.UPLOAD_FOLDER)

    get_queue_manager().restore_pending_jobs()

    from waitress import serve
    host, port = Config.HOST, Config.PORT
    print(f"🌐 ExamManager running on http://localhost:{port}")
    for addr in _lan_addresses():
        print(f"   On other devices on this network: http://{addr}:{port}")
    serve(app, host=host, port=port, threads=Config.SERVER_THREADS,
          max_request_body_size=1024 * 1024 * 1024, channel_timeout=300)
