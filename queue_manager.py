"""
Queue Manager v4 — Parallel Async AI Marking Queue
====================================================
v4 changes over v3:
- Runs N worker threads concurrently instead of 1, each pulling a key
  from the shared key_rotator before marking a student. This is the
  single biggest throughput lever for mass testing: turns "40 students
  x ~20s each, strictly sequential" into N students marking in parallel.
- Uses the SAME key_rotator instance as app.py (imported from
  key_rotator.py) so key cooldown state is consistent everywhere —
  there is exactly one source of truth for which keys are healthy.
- Per-worker current-job tracking (was a single self._current_job) so
  the status API can report everything actively marking at once.

Everything else — retry classification, permanent-vs-transient errors,
metadata persistence, job cancel/retry — is unchanged from v3.
"""

import threading
import uuid
import json
import time
import os
import traceback
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict
from enum import Enum

from key_rotator import key_rotator


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

RETRY_DELAY_SECS  = 10          # seconds between retries on transient errors
PERMANENT_ERRORS = [
    "google_api_key not configured",
    "marking_failed_no_content",    # AI returned sentinel — blank pages
    "no answer sheets found",
    "student folder not found",
]


def _is_transient(error_str: str) -> bool:
    el = error_str.lower()
    if any(p in el for p in PERMANENT_ERRORS):
        return False
    return any(k in el for k in TRANSIENT_KEYWORDS)


# ─── Job model ────────────────────────────────────────────────────────────────
class JobStatus(str, Enum):
    PENDING    = "pending"
    PROCESSING = "processing"
    RETRYING   = "retrying"
    COMPLETED  = "completed"
    FAILED     = "failed"
    CANCELLED  = "cancelled"


class MarkingJob:
    def __init__(self, student_id, student_name, exam_folder, exam_info, page_count):
        self.job_id        = str(uuid.uuid4())[:8]
        self.student_id    = student_id
        self.student_name  = student_name
        self.exam_folder   = exam_folder          # str — survives pickling
        self.exam_info     = exam_info
        self.page_count    = page_count
        self.status        = JobStatus.PENDING
        self.score         = None
        self.error         = None
        self.retry_count   = 0
        self.worker_id     = None
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
            "retry_count":  self.retry_count,
            "worker_id":    self.worker_id,
            "queued_at":    self.queued_at,
            "started_at":   self.started_at,
            "completed_at": self.completed_at,
        }


# ─── Queue Manager ────────────────────────────────────────────────────────────
class QueueManager:
    MAX_LOG_ENTRIES = 120
    MAX_COMPLETED   = 300
    MAX_WORKERS     = 7   # ceiling regardless of how many keys are configured

    def __init__(self):
        self._lock         = threading.Lock()
        self._condition     = threading.Condition(self._lock)
        self._pending:      List[MarkingJob] = []
        self._completed:    List[MarkingJob] = []
        self._current_jobs: Dict[int, MarkingJob] = {}   # worker_id -> job
        self._paused       = False
        self._stop_evt     = threading.Event()
        self._log: List[dict] = []

        # Shared with app.py — one source of truth for key cooldown state.
        self._key_pool = key_rotator
        num_keys        = len(self._key_pool) or 1
        self._worker_count = max(1, min(self.MAX_WORKERS, num_keys))

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

    def enqueue(self, student_id, student_name, exam_folder, exam_info, page_count) -> "MarkingJob":
        job = MarkingJob(student_id, student_name, str(exam_folder), exam_info, page_count)
        with self._condition:
            self._pending.append(job)
            self._log_event(
                f"📥 Queued: {student_name} (ID: {student_id}, {page_count} page{'s' if page_count!=1 else ''})")
            self._condition.notify_all()
        return job

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
                    return True
        return False

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
                    job.queued_at    = datetime.now().strftime("%H:%M:%S")
                    self._completed.remove(job)
                    self._pending.append(job)
                    self._log_event(f"🔄 Re-queued: {job.student_name}")
                    self._condition.notify_all()
                    return True
        return False

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

    # ── Internal worker ────────────────────────────────────────────────────────

    def _log_event(self, message: str):
        entry = {"time": datetime.now().strftime("%H:%M:%S"), "message": message}
        self._log.append(entry)
        if len(self._log) > self.MAX_LOG_ENTRIES:
            self._log = self._log[-self.MAX_LOG_ENTRIES:]
        print(f"[Queue {entry['time']}] {message}")

    def _worker_loop(self, worker_id: int):
        """Daemon loop for one worker — never exits while app is running."""
        while not self._stop_evt.is_set():
            job = self._pick_next_job(worker_id)
            if job is None:
                with self._condition:
                    self._condition.wait(timeout=2.0)
                continue
            self._process_job(job, worker_id)

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

    def _process_job(self, job: MarkingJob, worker_id: int):
        """
        Run the AI marking with unlimited retries on transient errors.
        Permanent errors (blank pages, missing API key) fail immediately.
        Quota errors put that specific key in cooldown and grab a fresh
        one from the shared pool on the next attempt — other workers are
        unaffected since they're on different keys.
        """
        exam_folder    = Path(job.exam_folder)
        student_folder = exam_folder / f"student_{job.student_id}"

        while True:  # ← retry loop
            api_key = None
            try:
                if len(self._key_pool) == 0:
                    self._fail_job(job, "AI service not configured. Contact your administrator.")
                    break

                api_key = self._key_pool.get_key()

                from ai_marker_gemini_improved import AIMarker
                marker = AIMarker(api_key=api_key)
                result = marker.mark_student(
                    student_id     = job.student_id,
                    student_name   = job.student_name,
                    exam_path      = exam_folder,
                    student_folder = student_folder,
                    exam_info      = job.exam_info,
                )

                if result.get("success"):
                    score = result.get("total_score", 0)
                    self._save_marks(exam_folder, job.student_id, result)
                    with self._lock:
                        job.status       = JobStatus.COMPLETED
                        job.score        = score
                        job.error        = None
                        job.completed_at = datetime.now().strftime("%H:%M:%S")
                        retry_note = f" (after {job.retry_count} retries)" if job.retry_count else ""
                        self._log_event(f"✅ [W{worker_id}] Marked: {job.student_name} — {score:.1f}%{retry_note}")
                    break  # ← success

                else:
                    err = result.get("error", "Marking returned no result")
                    if self._key_pool.is_quota_error(err) and api_key:
                        self._key_pool.report_quota_error(api_key, reason=err[:150])
                        self._schedule_retry(job, worker_id, "Key hit quota — rotating to next key")
                        continue
                    if _is_transient(err):
                        self._schedule_retry(job, worker_id, "AI temporarily unavailable — will retry automatically")
                        continue
                    else:
                        self._fail_job(job, "Could not mark this student's script. Please check the uploaded pages.")
                        break

            except Exception as exc:
                err_str = f"{type(exc).__name__}: {str(exc)}"
                if self._key_pool.is_quota_error(err_str) and api_key:
                    self._key_pool.report_quota_error(api_key, reason=err_str[:150])
                    self._schedule_retry(job, worker_id, "Key hit quota — rotating to next key")
                    continue
                if _is_transient(err_str):
                    self._schedule_retry(job, worker_id, "Network or service issue — retrying automatically")
                    continue
                else:
                    print(f"[Queue W{worker_id}] Permanent error for {job.student_id}: {err_str}")
                    traceback.print_exc()
                    self._fail_job(job, "An unexpected error occurred. Try re-queuing this student.")
                    break

        # Always clean up this worker's current job pointer
        with self._lock:
            self._current_jobs.pop(worker_id, None)
            self._completed.append(job)
            if len(self._completed) > self.MAX_COMPLETED:
                self._completed = self._completed[-self.MAX_COMPLETED:]

    def _schedule_retry(self, job: MarkingJob, worker_id: int, user_message: str):
        """Sleep RETRY_DELAY_SECS then the while-loop will retry the AI call."""
        job.retry_count += 1
        with self._lock:
            job.status = JobStatus.RETRYING
            job.error  = f"Retrying (attempt #{job.retry_count})…"
        self._log_event(
            f"⏳ [W{worker_id}] Retry #{job.retry_count} for {job.student_name} "
            f"in {RETRY_DELAY_SECS}s — {user_message}"
        )
        time.sleep(RETRY_DELAY_SECS)
        with self._lock:
            job.status = JobStatus.PROCESSING
            job.error  = None

    def _fail_job(self, job: MarkingJob, user_message: str):
        """Mark a job as permanently failed with a friendly message."""
        with self._lock:
            job.status       = JobStatus.FAILED
            job.error        = user_message
            job.completed_at = datetime.now().strftime("%H:%M:%S")
            self._log_event(f"⚠  Failed: {job.student_name} — {user_message[:60]}")

    # ── Metadata persistence ───────────────────────────────────────────────────

    @staticmethod
    def _save_marks(exam_folder: Path, sid: str, marking_result: dict):
        """Write AI results back to students_metadata.json with retry."""
        meta_path = exam_folder / "students_metadata.json"
        for attempt in range(5):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for student in data.get("students", []):
                    if student.get("id") == sid or student.get("student_id") == sid:
                        student.update({
                            "status":                "Marked",
                            "marked":                True,
                            "total_score":           marking_result.get("total_score", 0),
                            "raw_score":             marking_result.get("raw_score", 0),
                            "max_score":             marking_result.get("max_score", 100),
                            "questions":             marking_result.get("questions", {}),
                            "overall_feedback":      marking_result.get("overall_feedback", ""),
                            "strengths":             marking_result.get("strengths", []),
                            "areas_for_improvement": marking_result.get("areas_for_improvement", []),
                        })
                        break
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                return
            except Exception as e:
                if attempt < 4:
                    time.sleep(0.4 * (attempt + 1))
                else:
                    print(f"[Queue] Metadata save failed for {sid}: {e}")


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