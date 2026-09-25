import os

# ── Fallback single key before anything else imports it. Real key
#    rotation for marking happens through key_rotator.py / queue_manager.py;
#    this just satisfies any code path that reads GOOGLE_API_KEY directly. ──
os.environ.setdefault('GOOGLE_API_KEY', "AQ.Ab8RN6LoqvvLMsaIE9VmKGNHl-hMpQsh8Djk1cqT_Z-uOAGXpg")

import re
import time
import json
import uuid
import shutil
import tempfile
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file

from config import Config
from excel_generator import generate_exam_spreadsheet
from settings import SettingsManager
from queue_manager import get_queue_manager
from key_rotator import key_rotator
from utils.image_validator import ImageValidator
import batch_processor

app = Flask(__name__)
app.secret_key = Config.SECRET_KEY
settings_manager = SettingsManager(Config.BASE_DIR)

# ── Staging area for batch-ZIP uploads — cleared per-batch after confirm/discard ──
BATCH_STAGING_ROOT = Config.UPLOAD_FOLDER / "_batch_staging"
BATCH_ID_RE = re.compile(r"^[a-f0-9]{6,16}$")

# ── Start the background marking queue on boot ──────────────────────────────
get_queue_manager()   # instantiates & starts all worker threads once

_page_quality_checker = ImageValidator()

# ─────────────────────────────────────────────
# JSON helpers  (no database)
# ─────────────────────────────────────────────

def get_exam_folder(year, term, class_name, stream, subject, exam_type) -> Path:
    folder = (Config.UPLOAD_FOLDER / str(year) / term
              / f"{class_name}_{stream}" / subject / exam_type)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _meta_path(exam_folder: Path) -> Path:
    return exam_folder / "students_metadata.json"


def _load_meta(exam_folder: Path) -> dict:
    p = _meta_path(exam_folder)
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"exam_info": {}, "students": []}


def _save_meta(exam_folder: Path, data: dict):
    with open(_meta_path(exam_folder), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def init_exam_meta(exam_folder: Path, exam_info: dict):
    _save_meta(exam_folder, {"exam_info": exam_info, "students": []})


def get_students(exam_folder: Path) -> list:
    return _load_meta(exam_folder).get("students", [])


def add_or_update_student(exam_folder: Path, sid: str, sname: str, page_count: int) -> bool:
    try:
        data     = _load_meta(exam_folder)
        students = data.setdefault("students", [])
        now      = datetime.now().strftime("%Y-%m-%d %H:%M")
        existing = next((s for s in students if s.get("id") == sid), None)
        if existing:
            existing["total_pages"] = page_count
            existing["sync_time"]   = now
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
                "areas_for_improvement": []
            })
        _save_meta(exam_folder, data)
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
    """
    data = _load_meta(exam_folder)
    students = data.get("students", [])
    target = None
    for student in students:
        if str(student.get("id")) == str(student_id) or str(student.get("student_id")) == str(student_id):
            target = student
            break

    if target is None:
        return {"success": False, "message": "Student not found in this session."}

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

    # First-time override: snapshot the untouched AI values.
    if "ai_score" not in target:
        target["ai_score"] = target.get("total_score", 0)
    if "ai_name" not in target:
        target["ai_name"] = target.get("student_name", "")
    if "ai_id" not in target:
        target["ai_id"] = target.get("id", "")

    old_id = target.get("id")
    target["student_name"] = new_name
    target["id"]           = new_id
    target["total_score"]  = new_marks
    target["final_score"]  = new_marks
    target["overridden"]   = True
    target["overridden_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Keep the student_id/id in sync everywhere they're duplicated.
    if target.get("student_id") is not None:
        target["student_id"] = new_id

    _save_meta(exam_folder, data)
    return {"success": True, "student": target, "old_id": old_id}


def update_student_marks(exam_folder: Path, sid: str, questions: dict,
                         total_score: float, marking_result: dict):
    try:
        data = _load_meta(exam_folder)
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
        _save_meta(exam_folder, data)
    except Exception as e:
        print(f"update_student_marks error: {e}")


def get_statistics() -> dict:
    total_students = total_marked = total_sessions = 0
    if Config.UPLOAD_FOLDER.exists():
        for meta_file in Config.UPLOAD_FOLDER.rglob("students_metadata.json"):
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                students = data.get("students", [])
                if students:
                    total_sessions += 1
                    total_students += len(students)
                    total_marked   += sum(1 for s in students if s.get("marked"))
            except Exception:
                pass
    return {"total_students": total_students,
            "total_marked":   total_marked,
            "total_sessions": total_sessions}


def get_all_exams() -> list:
    exams = []
    if Config.UPLOAD_FOLDER.exists():
        for meta_file in Config.UPLOAD_FOLDER.rglob("students_metadata.json"):
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                info = data.get("exam_info", {})
                if info:
                    info["student_count"] = len(data.get("students", []))
                    info["marked_count"]  = sum(1 for s in data.get("students", []) if s.get("marked"))
                    exams.append(info)
            except Exception:
                pass
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

    # PDFs are exempt — Gemini reads them natively and blur detection needs
    # a raster image; a corrupted PDF is caught later by the AI marking step.
    if ext == "pdf":
        return {"idx": idx, "filename": orig_name, "ext": ext, "raw": raw, "ok": True, "reason": ""}

    is_ok, reason, _score = _page_quality_checker.validate_image_bytes(raw)
    return {"idx": idx, "filename": orig_name, "ext": ext, "raw": raw, "ok": is_ok, "reason": reason}


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
        })

        def save_files(file_list, prefix):
            """Saves uploaded files exactly as received — no compression."""
            saved = []
            for idx, f in enumerate(file_list):
                if not (f and f.filename):
                    continue
                ext      = f.filename.rsplit(".", 1)[-1].lower()
                filename = f"{prefix}_{idx + 1}.{ext}"
                fpath    = exam_folder / filename
                f.save(str(fpath))
                saved.append(filename)
            return saved

        with ThreadPoolExecutor(max_workers=2) as pool:
            rf = pool.submit(save_files, request.files.getlist("rubrics[]"),         "rubric")
            qf = pool.submit(save_files, request.files.getlist("question_papers[]"), "question_paper")
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
        with open(staging_dir / "manifest.json", "w", encoding="utf-8") as mf:
            json.dump(manifest, mf, indent=2, ensure_ascii=False)
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
    uploads use. Students with no valid pages ('error') are left out.
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

    try:
        with open(manifest_path, "r", encoding="utf-8") as mf:
            manifest = json.load(mf)
    except Exception:
        return jsonify({"success": False, "message": "Could not read the batch review data. Please re-upload."}), 500

    if manifest.get("severity") == "blocking":
        return jsonify({"success": False, "message": "This batch has unresolved blocking issues and cannot be marked yet."}), 400

    info        = session["exam_session"]
    exam_folder = Path(info["exam_folder"])
    qm          = get_queue_manager()
    queued      = 0
    skipped     = 0

    for stu in manifest.get("students", []):
        if stu.get("status") == "error":
            skipped += 1
            continue

        sid   = str(stu["id"])
        sname = (stu.get("name") or "").strip() or f"Student {sid}"
        src_folder = staging_dir / f"student_{sid}"
        if not src_folder.exists():
            skipped += 1
            continue

        dest_folder = exam_folder / f"student_{sid}"
        dest_folder.mkdir(parents=True, exist_ok=True)
        for page_file in sorted(src_folder.iterdir()):
            shutil.move(str(page_file), str(dest_folder / page_file.name))

        page_count = len(list(dest_folder.glob("page_*")))
        if page_count == 0:
            skipped += 1
            continue

        add_or_update_student(exam_folder, sid, sname, page_count)
        qm.enqueue(
            student_id=sid, student_name=sname,
            exam_folder=str(exam_folder), exam_info=info,
            page_count=page_count,
        )
        queued += 1

    shutil.rmtree(staging_dir, ignore_errors=True)

    return jsonify({
        "success": True,
        "queued":  queued,
        "skipped": skipped,
        "message": f"{queued} student(s) queued for AI marking." + (f" {skipped} skipped." if skipped else ""),
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
    if not files:
        return jsonify({"success": False, "message": "No pages were uploaded."}), 400

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

    student_folder.mkdir(parents=True, exist_ok=True)

    # ── All pages passed — save in parallel, original bytes, no compression ──
    valid_count = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(lambda r: (student_folder / f"page_{r['idx']+1}.{r['ext']}").write_bytes(r["raw"]), r)
                   for r in checked]
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

    # ── Enqueue for background AI marking ───────────────────────────────────
    qm  = get_queue_manager()
    job = qm.enqueue(
        student_id   = sid,
        student_name = sname,
        exam_folder  = str(exam_folder),
        exam_info    = info,
        page_count   = valid_count,
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

        exam_folder = Path(session_id)
        if not exam_folder.exists() or not _meta_path(exam_folder).exists():
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
    exam_sessions = []

    if Config.UPLOAD_FOLDER.exists():
        meta_files = sorted(
            Config.UPLOAD_FOLDER.rglob("students_metadata.json"),
            key=lambda p: p.stat().st_mtime, reverse=True
        )
        for meta_file in meta_files:
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                exam_info     = data.get("exam_info", {})
                students_list = data.get("students", [])

                if not exam_info:
                    parts = meta_file.parent.relative_to(Config.UPLOAD_FOLDER).parts
                    if len(parts) >= 5:
                        exam_info = {
                            "year":      parts[0],
                            "term":      parts[1],
                            "class":     parts[2].split("_")[0],
                            "stream":    parts[2].split("_")[1] if "_" in parts[2] else "",
                            "subject":   parts[3],
                            "exam_type": parts[4],
                        }

                if students_list:
                    exam_sessions.append({
                        **exam_info,
                        "students":       students_list,
                        "total_students": len(students_list),
                        "marked_count":   sum(1 for s in students_list if s.get("marked")),
                        "uploaded_count": sum(1 for s in students_list if not s.get("marked")),
                    })
            except Exception as e:
                print(f"Error reading {meta_file}: {e}")

    stats = {
        "total_students": sum(e["total_students"] for e in exam_sessions),
        "total_marked":   sum(e["marked_count"]   for e in exam_sessions),
        "total_sessions": len(exam_sessions),
    }
    return render_template("view_students.html", exam_sessions=exam_sessions, stats=stats)


@app.route("/all-exams")
def all_exams():
    """Retired — this pointed at a template that never existed (500 on
    every visit) and nothing in the app links to it. Route removed rather
    than patched since it was dead weight."""
    return redirect(url_for("view_students"))


@app.route("/generate-excel", methods=["POST"])
def generate_excel():
    try:
        data     = request.get_json()
        required = ["year", "term", "class", "stream", "subject", "exam_type", "format"]
        for field in required:
            if field not in data:
                return jsonify({"success": False, "message": f"Missing field: {field}"}), 400

        file_format = data["format"].lower()
        if file_format not in ("xlsx", "ods"):
            return jsonify({"success": False, "message": "Format must be xlsx or ods"}), 400

        temp_dir    = Path(tempfile.gettempdir())
        # `columns` is the {id, name, score, feedback, status} boolean map
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
    except Exception as e:
        print(f"Excel error: {e}")
        return jsonify({"success": False, "message": "Could not generate spreadsheet. Please try again."}), 500


@app.route("/api/sessions")
def api_sessions():
    """Return all exam sessions for the dashboard."""
    sessions_out = []

    if Config.UPLOAD_FOLDER.exists():
        for meta_file in Config.UPLOAD_FOLDER.rglob("students_metadata.json"):
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

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
                    "date":          datetime.fromtimestamp(meta_file.stat().st_mtime).isoformat(),
                    "created_at":    datetime.fromtimestamp(meta_file.stat().st_mtime).isoformat(),
                    "total_marks_possible": total_marks_for_paper,
                    "total_students": len(students),
                    "marked_count":  sum(1 for s in students if s.get("marked")),
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
                    })

                sessions_out.append(session_data)

            except Exception as e:
                print(f"Error loading session {meta_file}: {e}")

    sessions_out.sort(key=lambda x: x.get("date", ""), reverse=True)
    return jsonify({"sessions": sessions_out, "total": len(sessions_out)})


@app.route("/api/sessions/<path:session_id>")
def api_session_detail(session_id):
    target_path = Path(session_id)
    if not target_path.exists():
        return jsonify({"error": "Session not found"}), 404

    meta_file = target_path / "students_metadata.json"
    if not meta_file.exists():
        return jsonify({"error": "Session metadata not found"}), 404

    try:
        with open(meta_file, "r", encoding="utf-8") as f:
            data = json.load(f)

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
    target_path = Path(session_id)
    meta_file   = target_path / "students_metadata.json"

    if not meta_file.exists():
        return jsonify({"report": None})

    try:
        with open(meta_file, "r", encoding="utf-8") as f:
            data = json.load(f)

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
        return jsonify({"report": report})
    except Exception:
        return jsonify({"report": None})


# ─────────────────────────────────────────────
# Settings routes
# ─────────────────────────────────────────────

@app.route("/settings")
def settings_page():
    return redirect(url_for("index", _anchor="settings"))


@app.route("/settings/enable-auto-deletion", methods=["POST"])
def enable_auto_deletion():
    data = request.get_json()
    return jsonify(settings_manager.enable_auto_deletion(confirmed=data.get("confirmed", False)))


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

    exam_folder = Path(session_id)
    meta_file   = exam_folder / "students_metadata.json"

    if not exam_folder.exists() or not meta_file.exists():
        print(f"⚠️  resume_session: folder or metadata not found: {exam_folder}")
        return redirect(url_for("view_students"))

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
        with open(meta_file, "r", encoding="utf-8") as f:
            data = json.load(f)
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
# Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    Config.init_app(app)
    if settings_manager.is_auto_deletion_enabled():
        print("🗑️  Running auto-deletion…")
        settings_manager.run_auto_deletion(Config.UPLOAD_FOLDER)
    app.run(host="0.0.0.0", port=5000, debug=True,
            threaded=True, use_reloader=False)