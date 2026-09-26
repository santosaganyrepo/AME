"""
Queue Manager v5 — Parallel Async AI Marking Queue
====================================================
v5 changes over v4:
- Jobs survive a restart (6.6): every waiting/running job is written to
  pending_jobs.json (atomic writes) and removed when it finishes. The
  `python app.py` entry point calls restore_pending_jobs() once on start.
  Old sessions are never scanned — only jobs that were actually waiting or
  running are picked up again.
- Error classification fix (6.4): the marker's parse-failure messages embed
  the first 300 characters of the AI's reply, and ordinary words in a
  student's answer ("network", "connection", "internal", …) made those look
  like network errors — the job then retried forever. Classification now
  ignores the quoted AI text, and every job has a hard attempt cap.
- Keys are leased per job (D2.2): a worker only ever marks with a key that
  is not cooling down; if every key is cooling down it waits exactly until
  the earliest one recovers instead of a fixed sleep. A quota error cools
  that key and moves the job straight to another key — no 10 s wait.
- One timing line per job in logs/marking.log (D1.1) and an ai_runs/ record
  per AI call (written by the marker, D1.2).
- Results are saved with the locked atomic helper (6.1), keep the previous
  mark in mark_history on a re-mark (D3.5), save "Needs review" with a
  review_reason when the marker flags a result (D3.1/D3.2), and a failed
  student keeps a plain failure_reason a teacher understands (D6.2).

v6 changes over v5 (speed + no needless failures):
- Transient errors back off exponentially with jitter (10 s, 20 s, 40 s …
  capped at 2 min) so workers stop retrying an overloaded model in lockstep.
- A quota hit no longer uses up the job's attempt budget: it has its own,
  larger cap. The key rests for as long as Google's reply says (retryDelay),
  or an hour for a per-day quota. If every key is out for more than 15 min
  the job fails straight away with a clear "daily limit" reason instead of
  hogging a worker.
- An invalid / expired key is parked and the job moves to another key —
  before, one bad key in .env failed every student that landed on it.
- MARKING_WORKERS_PER_KEY (default 1) runs more than one job per key for
  paid-tier keys with high rate limits.
- Re-uploading a student who is still WAITING in the queue updates that
  job instead of adding a second one, so a student is never marked twice.

Everything else — permanent-vs-transient errors, job cancel/retry, the
status API shape — is unchanged.
"""

import json
import os
import random
import threading
import uuid
import time
import traceback
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict
from enum import Enum

from key_rotator import key_rotator
from marking_log import get_marking_logger
from storage import read_json, update_json

log = get_marking_logger()

PENDING_JOBS_FILE = Path(__file__).parent / "pending_jobs.json"


# ─── Error classification ─────────────────────────────────────────────────────
TRANSIENT_KEYWORDS = [
    "ssl", "timeout", "503", "502", "429", "eof",
    "connection", "reset", "network", "dns", "unreachable",
    "temporary", "resource_exhausted", "resource exhausted",
    "unavailable", "deadline", "quota", "overloaded",
    "internal", "socket", "broken pipe", "getaddrinfo",
    "failed to connect", "connection refused", "name or service",
    "name resolution", "nodename nor servname", "errno",
    "remote end closed", "incomplete read",
]

RETRY_DELAY_SECS  = 10          # first back-off on a transient error (doubles each time)
MAX_RETRY_DELAY_SECS = 120      # back-off ceiling
MAX_JOB_ATTEMPTS  = 8           # hard cap per job (quota hits / waiting for a key don't count)
MIN_QUOTA_HITS    = 12          # quota hits allowed per job (at least 3 per key)
MAX_KEY_WAIT_SECS = 15 * 60     # all keys out longer than this → fail now, retry later
ALL_KEYS_EXHAUSTED = "all_keys_exhausted"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


PERMANENT_ERRORS = [
    "google_api_key not configured",
    "marking_failed_no_content",    # AI returned sentinel — blank pages
    "no student answer content",    # the marker's wording for the same sentinel
    "no answer sheets found",
    "student folder not found",
    "no scores list found",         # AI replied, but not in the required format
    "could not parse any scores",
    "ai reply incomplete",
    "cannot mark:",                 # question paper / rubric missing
    "no valid answer images",
]


def _strip_ai_text(error_str: str) -> str:
    """Drops the quoted AI reply the marker appends after 'Raw (first 300):'."""
    return error_str.split("Raw (first", 1)[0]


def _is_transient(error_str: str) -> bool:
    el = _strip_ai_text(error_str).lower()
    if any(p in el for p in PERMANENT_ERRORS):
        return False
    return any(k in el for k in TRANSIENT_KEYWORDS)


def friendly_failure(error_str: str) -> str:
    """Short, plain reason a teacher understands (D6.2)."""
    el = _strip_ai_text(error_str or "").lower()
    if "not configured" in el or "no gemini api keys" in el:
        return "AI service not set up — contact your administrator"
    if ALL_KEYS_EXHAUSTED in el:
        return "Daily AI limit reached on every key — retry later (or add more keys)"
    if "answer content" in el or "marking_failed_no_content" in el:
        return "Pages unreadable or blank — re-capture and upload again"
    if "no answer sheets" in el or "no valid answer images" in el or "student folder not found" in el:
        return "No readable pages found — upload this student's pages again"
    if "cannot mark:" in el:
        return "Question paper or marking scheme missing — re-upload them in setup"
    if "incomplete" in el:
        return "Report incomplete — please re-mark"
    if "no scores list" in el or "could not parse" in el:
        return "AI reply could not be read — please re-mark"
    if any(k in el for k in ("429", "quota", "resource_exhausted", "resource exhausted", "busy")):
        return "AI service busy — retry"
    if any(k in el for k in TRANSIENT_KEYWORDS):
        return "Could not reach the AI service — retry"
    return "Could not mark this student's script — retry, or check the uploaded pages"


# ─── Job model ────────────────────────────────────────────────────────────────
class JobStatus(str, Enum):
    PENDING    = "pending"
    PROCESSING = "processing"
    RETRYING   = "retrying"
    COMPLETED  = "completed"
    FAILED     = "failed"
    CANCELLED  = "cancelled"


class MarkingJob:
    def __init__(self, student_id, student_name, exam_folder, exam_info, page_count,
                 folder: str = None, job_id: str = None, enqueued_ts: float = None):
        self.job_id        = job_id or str(uuid.uuid4())[:8]
        self.student_id    = student_id
        self.student_name  = student_name
        self.exam_folder   = exam_folder          # str — survives pickling
        self.exam_info     = exam_info
        self.page_count    = page_count
        self.folder        = folder or f"student_{student_id}"   # survives an ID override
        self.status        = JobStatus.PENDING
        self.score         = None
        self.error         = None
        self.review_reason = None
        self.retry_count   = 0
        self.worker_id     = None
        self.enqueued_ts   = enqueued_ts or time.time()
        self.queued_at     = datetime.now().strftime("%H:%M:%S")
        self.started_at    = None
        self.completed_at  = None

    def to_dict(self):
        return {
            "job_id":       self.job_id,
            "student_id":   self.student_id,
            "student_name": self.student_name,
            "exam_folder":  self.exam_folder,
            "page_count":   self.page_count,
            "status":       self.status.value,
            "score":        self.score,
            "error":        self.error,
            "review_reason": self.review_reason,
            "retry_count":  self.retry_count,
            "worker_id":    self.worker_id,
            "queued_at":    self.queued_at,
            "started_at":   self.started_at,
            "completed_at": self.completed_at,
        }

    def to_pending_record(self) -> dict:
        return {
            "job_id":       self.job_id,
            "student_id":   self.student_id,
            "student_name": self.student_name,
            "exam_folder":  self.exam_folder,
            "exam_info":    self.exam_info,
            "page_count":   self.page_count,
            "folder":       self.folder,
            "enqueued_ts":  self.enqueued_ts,
        }


# ─── Queue Manager ────────────────────────────────────────────────────────────
class QueueManager:
    MAX_LOG_ENTRIES = 120
    MAX_COMPLETED   = 300
    MAX_WORKERS     = 16  # ceiling regardless of how many keys are configured

    def __init__(self, pending_file: Path = None):
        self._lock         = threading.Lock()
        self._condition     = threading.Condition(self._lock)
        self._pending:      List[MarkingJob] = []
        self._completed:    List[MarkingJob] = []
        self._current_jobs: Dict[int, MarkingJob] = {}   # worker_id -> job
        self._paused       = False
        self._stop_evt     = threading.Event()
        self._log: List[dict] = []
        self._pending_file = Path(pending_file or PENDING_JOBS_FILE)
        self._restored     = False

        # Shared with app.py — one source of truth for key cooldown state.
        self._key_pool = key_rotator
        num_keys        = len(self._key_pool) or 1
        per_key         = max(1, _env_int("MARKING_WORKERS_PER_KEY", 1))
        self._worker_count = max(1, min(self.MAX_WORKERS, num_keys * per_key))

        self._workers: List[threading.Thread] = [
            threading.Thread(
                target=self._worker_loop, args=(i,),
                name=f"AIMarkingWorker-{i}", daemon=True
            )
            for i in range(self._worker_count)
        ]
        self._started = False

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self):
        if self._started:
            return
        self._started = True
        for w in self._workers:
            w.start()
        self._log_event(
            f"🚀 Marking queue started — {self._worker_count} parallel worker(s), "
            f"{len(self._key_pool)} API key(s) in rotation"
        )
        if len(self._key_pool) == 0:
            self._log_event("⚠  No Gemini API keys in .env (GEMINI_API_KEY_1 …) — marking is disabled")

    def enqueue(self, student_id, student_name, exam_folder, exam_info, page_count,
                folder: str = None) -> "MarkingJob":
        folder = folder or f"student_{student_id}"
        with self._condition:
            # Still waiting (not started)? Its pages were just replaced on disk,
            # so the waiting job will mark the new ones — don't add a second.
            for waiting in self._pending:
                if waiting.exam_folder == str(exam_folder) and waiting.folder == folder:
                    waiting.student_name = student_name
                    waiting.page_count   = page_count
                    waiting.exam_info    = exam_info
                    self._log_event(f"📥 Already queued: {student_name} — will mark the new pages")
                    self._persist_add(waiting)
                    return waiting
            job = MarkingJob(student_id, student_name, str(exam_folder), exam_info, page_count, folder=folder)
            # Recorded before a worker can see it (pending_jobs.json has its own lock).
            self._persist_add(job)
            self._pending.append(job)
            self._log_event(
                f"📥 Queued: {student_name} (ID: {student_id}, {page_count} page{'s' if page_count!=1 else ''})")
            self._condition.notify_all()
        return job

    def restore_pending_jobs(self) -> int:
        """
        Called once by the `python app.py` entry point (6.6): re-queues every
        job that was waiting or running when the app last stopped. Nothing
        else is scanned — finished jobs were removed from the file already.
        """
        if self._restored:
            return 0
        self._restored = True
        if len(self._key_pool) == 0:
            n = len(read_json(self._pending_file, {}) or {})
            if n:
                self._log_event(f"⚠  {n} unfinished job(s) kept in pending_jobs.json — add API keys to .env and restart to mark them")
            return 0

        records = read_json(self._pending_file, {}) or {}
        restored = 0
        with self._condition:
            queued_ids = {j.job_id for j in self._pending} | {j.job_id for j in self._current_jobs.values()}
            for rec in sorted(records.values(), key=lambda r: r.get("enqueued_ts", 0)):
                if rec.get("job_id") in queued_ids:
                    continue
                if not Path(rec.get("exam_folder", "")).exists():
                    continue
                job = MarkingJob(rec["student_id"], rec.get("student_name", ""), rec["exam_folder"],
                                 rec.get("exam_info") or {}, rec.get("page_count", 0),
                                 folder=rec.get("folder"), job_id=rec.get("job_id"),
                                 enqueued_ts=rec.get("enqueued_ts"))
                self._pending.append(job)
                restored += 1
            if restored:
                self._log_event(f"♻  Restored {restored} unfinished marking job(s) from before the restart")
                self._condition.notify_all()
        return restored

    def is_queued(self, exam_folder: str, student_id: str) -> bool:
        """True if this student is already waiting or being marked."""
        ef = str(exam_folder)
        with self._lock:
            active = list(self._pending) + list(self._current_jobs.values())
            return any(j.exam_folder == ef and str(j.student_id) == str(student_id) for j in active)

    def pause(self):
        with self._lock:
            self._paused = True
            self._log_event("⏸  Queue paused by user")

    def resume(self):
        with self._condition:
            self._paused = False
            self._log_event("▶  Queue resumed")
            self._condition.notify_all()

    def cancel_job(self, job_id: str) -> bool:
        with self._lock:
            for i, job in enumerate(self._pending):
                if job.job_id == job_id:
                    job.status = JobStatus.CANCELLED
                    job.completed_at = datetime.now().strftime("%H:%M:%S")
                    self._pending.pop(i)
                    self._completed.append(job)
                    self._log_event(f"❌ Cancelled: {job.student_name}")
                    break
            else:
                return False
        self._persist_remove(job)
        return True

    def retry_job(self, job_id: str) -> bool:
        with self._condition:
            for job in self._completed:
                if job.job_id == job_id and job.status in (JobStatus.FAILED, JobStatus.CANCELLED):
                    job.status       = JobStatus.PENDING
                    job.error        = None
                    job.score        = None
                    job.retry_count  = 0
                    job.worker_id    = None
                    job.started_at   = None
                    job.completed_at = None
                    job.enqueued_ts  = time.time()
                    job.queued_at    = datetime.now().strftime("%H:%M:%S")
                    self._completed.remove(job)
                    self._pending.append(job)
                    self._log_event(f"🔄 Re-queued: {job.student_name}")
                    self._condition.notify_all()
                    break
            else:
                return False
        self._persist_add(job)
        return True

    def get_status(self) -> dict:
        with self._lock:
            pending_list   = [j.to_dict() for j in self._pending]
            completed_list = [j.to_dict() for j in self._completed]
            current_jobs   = [j.to_dict() for j in self._current_jobs.values()]

            total     = len(pending_list) + len(completed_list) + len(current_jobs)
            marked    = sum(1 for j in self._completed if j.status == JobStatus.COMPLETED)
            failed    = [j.to_dict() for j in self._completed if j.status == JobStatus.FAILED]
            cancelled = [j.to_dict() for j in self._completed if j.status == JobStatus.CANCELLED]
            remaining = len(pending_list) + len(current_jobs)
            pct       = round((marked / total * 100) if total > 0 else 0)

            active_jobs = list(self._current_jobs.values())
            if any(j.retry_count > 0 for j in active_jobs):
                status_str = "retrying"
            elif active_jobs:
                status_str = "processing"
            elif self._paused:
                status_str = "paused"
            elif pending_list:
                status_str = "waiting"
            else:
                status_str = "idle"

            return {
                "status":       status_str,
                "paused":       self._paused,
                # Backward-compatible single-job field (first active job, if any)
                "current_job":  current_jobs[0] if current_jobs else None,
                # Full picture — every worker's current job
                "current_jobs": current_jobs,
                "worker_count": self._worker_count,
                "key_pool":     self._key_pool.status(),
                "queue":        pending_list,
                "completed":    [j.to_dict() for j in self._completed
                                 if j.status == JobStatus.COMPLETED],
                "failed":       failed,
                "cancelled":    cancelled,
                "total":        total,
                "marked":       marked,
                "remaining":    remaining,
                "percentage":   pct,
                "log":          list(self._log[-30:]),
            }

    # ── pending_jobs.json (6.6) ───────────────────────────────────────────────

    def _persist_add(self, job: MarkingJob):
        try:
            update_json(self._pending_file,
                        lambda d: d.__setitem__(job.job_id, job.to_pending_record()), default={})
        except Exception as e:
            log.warning(f"[Queue] Could not record pending job {job.job_id}: {e}")

    def _persist_remove(self, job: MarkingJob):
        try:
            update_json(self._pending_file, lambda d: d.pop(job.job_id, None) and None, default={})
        except Exception as e:
            log.warning(f"[Queue] Could not clear pending job {job.job_id}: {e}")

    # ── Internal worker ────────────────────────────────────────────────────────

    def _log_event(self, message: str):
        entry = {"time": datetime.now().strftime("%H:%M:%S"), "message": message}
        self._log.append(entry)
        if len(self._log) > self.MAX_LOG_ENTRIES:
            self._log = self._log[-self.MAX_LOG_ENTRIES:]
        log.info(f"[Queue {entry['time']}] {message}")

    def _worker_loop(self, worker_id: int):
        """Daemon loop for one worker — never exits while app is running."""
        while not self._stop_evt.is_set():
            job = self._pick_next_job(worker_id)
            if job is None:
                with self._condition:
                    self._condition.wait(timeout=2.0)
                continue
            try:
                self._process_job(job, worker_id)
            except Exception as exc:   # never let one bad job kill a worker thread
                log.error(f"[Queue W{worker_id}] Worker error on {job.student_id}: {exc}")
                traceback.print_exc()
                try:
                    self._fail_job(job, "An unexpected error occurred. Try re-queuing this student.", str(exc))
                    self._persist_remove(job)
                finally:
                    self._finish(job, worker_id)

    def _finish(self, job: MarkingJob, worker_id: int):
        """Moves a job off this worker into the completed list."""
        with self._lock:
            if self._current_jobs.get(worker_id) is job:
                self._current_jobs.pop(worker_id, None)
            self._completed.append(job)
            if len(self._completed) > self.MAX_COMPLETED:
                self._completed = self._completed[-self.MAX_COMPLETED:]

    def _pick_next_job(self, worker_id: int) -> Optional[MarkingJob]:
        with self._condition:
            if self._paused or not self._pending:
                return None
            job              = self._pending.pop(0)
            job.status       = JobStatus.PROCESSING
            job.worker_id    = worker_id
            job.started_at   = datetime.now().strftime("%H:%M:%S")
            self._current_jobs[worker_id] = job
            self._log_event(f"🤖 [W{worker_id}] Marking: {job.student_name} (ID: {job.student_id})")
            return job

    def _acquire_key(self, job: MarkingJob, worker_id: int) -> Optional[str]:
        """
        A key that is not cooling down; waits for the earliest one if all are
        (D2.2). Returns ALL_KEYS_EXHAUSTED instead of waiting when every key
        is out for longer than MAX_KEY_WAIT_SECS (e.g. daily quota used up).
        """
        announced = False
        while not self._stop_evt.is_set():
            key, wait = self._key_pool.acquire()
            if key:
                return key
            if wait > MAX_KEY_WAIT_SECS:
                return ALL_KEYS_EXHAUSTED
            if not announced:
                self._log_event(f"⏳ [W{worker_id}] All API keys cooling down — "
                                f"{job.student_name} continues in {wait:.0f}s")
                announced = True
            self._stop_evt.wait(min(wait, 60))
        return None

    def _process_job(self, job: MarkingJob, worker_id: int):
        """
        Run the AI marking. Transient errors retry (capped at MAX_JOB_ATTEMPTS);
        permanent errors (blank pages, missing QP/rubric, unreadable AI reply)
        fail straight away. A quota error cools only that key and the job moves
        to another key immediately — other workers are unaffected.
        """
        exam_folder    = Path(job.exam_folder)
        student_folder = exam_folder / job.folder
        t_start        = time.time()
        queue_wait     = t_start - job.enqueued_ts
        run_info: dict = {}
        key_slots: List[int] = []
        final_error    = None
        attempts       = 0      # real attempts: transient errors count, quota hits don't
        quota_hits     = 0
        max_quota_hits = max(MIN_QUOTA_HITS, 3 * len(self._key_pool))

        while True:  # ← retry loop
            api_key = None
            try:
                if len(self._key_pool) == 0:
                    final_error = "AI service not configured"
                    self._fail_job(job, "AI service not configured. Contact your administrator.",
                                   "google_api_key not configured")
                    break

                attempts += 1
                if attempts > MAX_JOB_ATTEMPTS or quota_hits > max_quota_hits:
                    self._fail_job(job, "AI service busy — please retry this student.",
                                   final_error or "busy: attempt limit reached")
                    break

                teacher = self.teacher_final_mark(exam_folder, job)
                if teacher is not None:
                    self._keep_teacher_mark(job, worker_id, teacher.get("total_score"))
                    break

                api_key = self._acquire_key(job, worker_id)
                if api_key is None:     # shutting down — job stays in pending_jobs.json
                    with self._lock:
                        if self._current_jobs.get(worker_id) is job:
                            self._current_jobs.pop(worker_id, None)
                    return
                if api_key == ALL_KEYS_EXHAUSTED:
                    api_key = None
                    self._fail_job(job, "Every API key has reached its limit — retry later.",
                                   f"{ALL_KEYS_EXHAUSTED}: {final_error or ''}")
                    break
                key_slots.append(self._key_pool.slot_of(api_key))

                from ai_marker_gemini_improved import AIMarker
                marker = AIMarker(api_key=api_key)
                result = marker.mark_student(
                    student_id     = job.student_id,
                    student_name   = job.student_name,
                    exam_path      = exam_folder,
                    student_folder = student_folder,
                    exam_info      = job.exam_info,
                    runs_dir       = student_folder / "ai_runs",
                )
                run_info = result.get("_run") or run_info

                if result.get("success"):
                    score = result.get("total_score", 0)
                    reasons = (result.get("_run") or {}).get("review_reasons") or []
                    if self._save_marks(exam_folder, job, result) == "teacher_final":
                        teacher = self.teacher_final_mark(exam_folder, job) or {}
                        self._keep_teacher_mark(job, worker_id, teacher.get("total_score"))
                        break
                    with self._lock:
                        job.status        = JobStatus.COMPLETED
                        job.score         = score
                        job.error         = None
                        job.review_reason = "; ".join(reasons) or None
                        job.completed_at  = datetime.now().strftime("%H:%M:%S")
                        retry_note = f" (after {job.retry_count} retries)" if job.retry_count else ""
                        flag_note  = " — needs review" if reasons else ""
                        self._log_event(f"✅ [W{worker_id}] Marked: {job.student_name} — {score:.1f}%{retry_note}{flag_note}")
                    break  # ← success

                else:
                    err = result.get("error", "Marking returned no result")
                    final_error = err
                    if self._key_pool.is_quota_error(_strip_ai_text(err)):
                        quota_hits += 1
                        attempts -= 1
                        self._key_pool.report_quota_error(api_key, reason=_strip_ai_text(err))
                        self._note_retry(job, worker_id, "Key hit quota — moving to another key")
                        continue
                    if _is_transient(err):
                        self._schedule_retry(job, worker_id, attempts, "AI temporarily unavailable — will retry automatically")
                        continue
                    else:
                        self._fail_job(job, "Could not mark this student's script. Please check the uploaded pages.", err)
                        break

            except Exception as exc:
                err_str = f"{type(exc).__name__}: {str(exc)}"
                final_error = err_str
                if api_key and self._key_pool.is_bad_key_error(err_str):
                    quota_hits += 1
                    attempts -= 1
                    self._key_pool.report_bad_key(api_key, reason=err_str)
                    self._note_retry(job, worker_id, f"Key slot {key_slots[-1]} was rejected — moving to another key")
                    continue
                if self._key_pool.is_quota_error(err_str) and api_key:
                    quota_hits += 1
                    attempts -= 1
                    self._key_pool.report_quota_error(api_key, reason=err_str)
                    self._note_retry(job, worker_id, "Key hit quota — moving to another key")
                    continue
                if _is_transient(err_str):
                    self._schedule_retry(job, worker_id, attempts, "Network or service issue — retrying automatically")
                    continue
                else:
                    log.error(f"[Queue W{worker_id}] Permanent error for {job.student_id}: {err_str}")
                    traceback.print_exc()
                    self._fail_job(job, "An unexpected error occurred. Try re-queuing this student.", err_str)
                    break
            finally:
                self._key_pool.release(api_key)

        self._log_metrics(job, worker_id, queue_wait, time.time() - t_start, key_slots, run_info)
        self._persist_remove(job)
        self._finish(job, worker_id)

    def _log_metrics(self, job, worker_id, queue_wait, processing, key_slots, run):
        """One machine-readable line per job in logs/marking.log (D1.1)."""
        usage = run.get("usage") or {}
        metrics = {
            "job_id":          job.job_id,
            "student_id":      job.student_id,
            "exam_folder":     job.exam_folder,
            "outcome":         job.status.value,
            "worker":          worker_id,
            "queue_wait_s":    round(queue_wait, 2),
            "upload_s":        run.get("prepare_secs"),
            "ai_call_s":       run.get("ai_secs"),
            "parse_s":         run.get("parse_secs"),
            "total_s":         round(processing, 2),
            "pages":           run.get("pages", job.page_count),
            "payload_bytes":   run.get("payload_bytes"),
            "transport":       run.get("transport"),
            "key_slots":       key_slots,
            "job_retries":     job.retry_count,
            "network_retries": run.get("network_retries"),
            "ai_attempts":     len(run.get("ai_attempts") or []),
            "finish_reason":   run.get("finish_reason"),
            "tokens_prompt":   usage.get("prompt"),
            "tokens_output":   usage.get("output"),
            "tokens_thinking": usage.get("thinking"),
            "tokens_total":    usage.get("total"),
            "file_uploads":    run.get("file_uploads"),
            "file_cache_hits": run.get("file_cache_hits"),
            "needs_review":    bool(run.get("review_reasons")),
        }
        log.info("📈 job_metrics " + json.dumps(metrics, ensure_ascii=False))

    def _keep_teacher_mark(self, job: MarkingJob, worker_id: int, final_score):
        """A teacher overrode this student: their mark is final, the AI doesn't re-mark."""
        with self._lock:
            job.status        = JobStatus.COMPLETED
            job.score         = final_score
            job.error         = None
            job.review_reason = None
            job.completed_at  = datetime.now().strftime("%H:%M:%S")
        self._log_event(f"🔒 [W{worker_id}] {job.student_name} has a teacher's final mark — kept, not re-marked by the AI")

    def _note_retry(self, job: MarkingJob, worker_id: int, user_message: str):
        """Quota rotation — no sleep, the next attempt takes a different key."""
        with self._lock:
            job.retry_count += 1
            self._log_event(f"🔁 [W{worker_id}] Retry #{job.retry_count} for {job.student_name} — {user_message}")

    def _schedule_retry(self, job: MarkingJob, worker_id: int, attempt: int, user_message: str):
        """
        Exponential back-off with jitter (10 s, 20 s, 40 s … ≤ 2 min), then the
        while-loop retries. Jitter keeps workers from hitting an overloaded
        model at the same instant.
        """
        with self._lock:
            job.retry_count += 1
            job.status = JobStatus.RETRYING
            job.error  = f"Retrying (attempt #{job.retry_count})…"
            n = job.retry_count
        delay = min(RETRY_DELAY_SECS * (2 ** max(0, attempt - 1)), MAX_RETRY_DELAY_SECS)
        delay = delay * random.uniform(0.8, 1.3)
        self._log_event(
            f"⏳ [W{worker_id}] Retry #{n} for {job.student_name} "
            f"in {delay:.0f}s — {user_message}"
        )
        self._stop_evt.wait(delay)
        with self._lock:
            job.status = JobStatus.PROCESSING
            job.error  = None

    def _fail_job(self, job: MarkingJob, user_message: str, raw_error: str = ""):
        """Mark a job as permanently failed with a friendly message, and record why."""
        reason = friendly_failure(raw_error or user_message)
        with self._lock:
            job.status       = JobStatus.FAILED
            job.error        = reason
            job.completed_at = datetime.now().strftime("%H:%M:%S")
            self._log_event(f"⚠  Failed: {job.student_name} — {reason[:80]}")
        if raw_error:
            log.info(f"[Queue] Failure detail for {job.student_id}: {_strip_ai_text(raw_error)[:300]}")
        self._save_failure(Path(job.exam_folder), job, reason)

    # ── Metadata persistence (locked + atomic, 6.1) ────────────────────────────

    @staticmethod
    def _find_student(data: dict, job: MarkingJob) -> Optional[dict]:
        students = data.get("students", [])
        for student in students:   # the folder never changes, even after an ID override
            if student.get("folder") == job.folder:
                return student
        for student in students:
            if student.get("id") == job.student_id or student.get("student_id") == job.student_id:
                return student
        return None

    @staticmethod
    def _save_marks(exam_folder: Path, job: MarkingJob, marking_result: dict) -> str:
        """
        Write AI results back to students_metadata.json (locked, atomic).
        Returns "saved", or "teacher_final" when a teacher overrode this
        student while the AI was marking — the teacher's mark is final and
        is never overwritten (the AI's reply stays in ai_runs/ only).
        """
        run     = marking_result.get("_run") or {}
        reasons = run.get("review_reasons") or []
        now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        outcome = {"value": "saved"}

        def mutate(data):
            student = QueueManager._find_student(data, job)
            if student is None:
                log.warning(f"[Queue] {job.student_id} is no longer in {exam_folder.name}/students_metadata.json — result kept in ai_runs/ only")
                return
            if student.get("overridden"):
                outcome["value"] = "teacher_final"
                return
            # D3.5 — keep the previous mark before overwriting it
            if student.get("marked"):
                prev = {
                    "total_score": student.get("total_score"),
                    "raw_score":   student.get("raw_score"),
                    "marked_at":   student.get("marked_at"),
                    "model":       student.get("model"),
                    "prompt_version": student.get("prompt_version"),
                    "status":      student.get("status"),
                }
                student.setdefault("mark_history", []).append(prev)

            student.update({
                "status":                "Needs review" if reasons else "Marked",
                "marked":                True,
                "total_score":           marking_result.get("total_score", 0),
                "raw_score":             marking_result.get("raw_score", 0),
                "max_score":             marking_result.get("max_score", 100),
                "questions":             marking_result.get("questions", {}),
                "overall_feedback":      marking_result.get("overall_feedback", ""),
                "strengths":             marking_result.get("strengths", []),
                "areas_for_improvement": marking_result.get("areas_for_improvement", []),
                "stage_scores":          marking_result.get("stage_scores", []),
                "marked_at":             now,
                "model":                 run.get("model"),
                "prompt_version":        run.get("prompt_version"),
                "folder":                job.folder,
            })
            if reasons:
                student["review_reason"] = "; ".join(reasons)
            else:
                student.pop("review_reason", None)
            student.pop("failure_reason", None)

        try:
            update_json(exam_folder / "students_metadata.json", mutate,
                        default={"exam_info": {}, "students": []})
        except Exception as e:
            log.error(f"[Queue] Metadata save failed for {job.student_id}: {e}")
        return outcome["value"]

    @staticmethod
    def teacher_final_mark(exam_folder: Path, job: MarkingJob) -> Optional[dict]:
        """The student's record if a teacher has overridden their mark, else None."""
        data = read_json(Path(exam_folder) / "students_metadata.json", {}) or {}
        student = QueueManager._find_student(data, job)
        return student if student and student.get("overridden") else None

    @staticmethod
    def _save_failure(exam_folder: Path, job: MarkingJob, reason: str):
        """
        D6.2 — a student with no mark yet is saved as "Failed" with a plain
        reason. A student who already HAS a mark keeps it (and its status);
        only the reason is noted, so a failed re-mark never hides a result.
        """
        def mutate(data):
            student = QueueManager._find_student(data, job)
            if student is None:
                return
            student["failure_reason"] = reason
            student["failed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            student.setdefault("folder", job.folder)
            if not student.get("marked"):
                student["status"] = "Failed"

        meta = exam_folder / "students_metadata.json"
        if not meta.exists():
            return
        try:
            update_json(meta, mutate, default={"exam_info": {}, "students": []})
        except Exception as e:
            log.error(f"[Queue] Could not record failure for {job.student_id}: {e}")


# ─── Singleton accessor ───────────────────────────────────────────────────────
_queue_manager: Optional[QueueManager] = None
_qm_lock = threading.Lock()


def get_queue_manager() -> QueueManager:
    global _queue_manager
    if _queue_manager is None:
        with _qm_lock:
            if _queue_manager is None:
                _queue_manager = QueueManager()
                _queue_manager.start()
    return _queue_manager
