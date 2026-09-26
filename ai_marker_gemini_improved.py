"""
AI Marking Engine — v12
========================
Changes from v11
────────────────
* Every document in the request is labelled ("QUESTION PAPER — part 1 of 2",
  "MARKING SCHEME / RUBRIC — …", "STUDENT ANSWER SCRIPT — …"); before, the
  model received one unlabelled run of images and had to guess where each
  document began. PROMPT_VERSION is now "v12" — run tools/benchmark.py.
* One Gemini client per API key is reused across jobs (connection reuse),
  and the question paper / rubric bytes are read once per session instead
  of once per student. Content sent to the model is unchanged.
* A 503 "model overloaded" reply is retried 6 s / 15 s / 30 s apart (with
  jitter) instead of 2 s / 5 s / 10 s, and — only if GEMINI_FALLBACK_MODEL
  is set — handed to a fallback model. The model that actually marked is
  stored with every result.
* Optional GEMINI_THINKING_LEVEL in .env (blank = model default).
* A stray "Q1: 4" entry in the SCORES line is read as 4, not 1.

Changes from v10
────────────────
0. RESULT CACHING REMOVED ENTIRELY: v10 kept a per-student fingerprint
   result cache (result_cache.json) so a resubmission of an identical
   script wouldn't be re-marked. In practice this caused a real failure
   mode: if a teacher wanted to RE-MARK a student (fixed a blurry page,
   swapped in the correct pages under the same page count/size, or just
   wanted a fresh AI pass) the cache could still match and silently hand
   back the OLD score instead of actually calling the AI again. No
   student score is cached anywhere in this pipeline any more — every
   single call to mark_student() always re-marks fresh from whatever
   pages currently sit in the student's folder and always calls the AI.
   There is no result_cache.json file, no fingerprinting, and no
   "from_cache" short-circuit left in the code.

1. SESSION MATERIALS (question paper + rubric) ARE NOT CHANGED BY THIS —
   they were already, and remain, re-read fresh from disk on every call
   (see point 5 below) and are NEVER cached/skipped the way student
   results used to be. Removing the result cache only removes the
   student-score shortcut; it has no effect on how the question paper or
   rubric are supplied to the AI.

Changes from v9 (all preserved in v11)
────────────────
1. GEMINI FILE API FOR LARGE PAYLOADS (NEW): Every image/PDF was previously
   sent inline as base64 bytes on every single AI call. That works fine for
   the 1–3 page scripts tested so far, but a real full exam — an 8–10+ page
   essay-style answer script plus a multi-page question paper and rubric —
   can push the combined inline payload toward Gemini's inline request size
   ceiling. When that happens it doesn't fail cleanly; it just looks like a
   random AI/network error to the queue.

   Fix: before every marking call, the combined raw byte size of the
   question paper + rubric + student answer pages is measured. If it stays
   under a safe inline threshold (15 MB raw — inline requests are typically
   capped around 20 MB once base64 overhead is added, so this leaves
   headroom), everything is sent inline exactly as before — fastest path,
   zero extra round trips, unchanged behaviour for the vast majority of
   scripts tested so far.

   If the combined payload is larger than that, the pipeline automatically
   switches to the Gemini File API instead: every document is uploaded
   once via `client.files.upload(...)`, the marking call then references
   each file by its returned handle/URI rather than embedding raw bytes.
   This removes the inline size ceiling entirely (File API documents are
   supported up to a much larger per-file limit) without needing any local
   image compression or resizing that could degrade legibility of student
   handwriting.

2. FILE-HANDLE CACHE FOR SHARED EXAM MATERIALS: The question paper and
   rubric are identical for every student in a session. When the File API
   path is used, uploaded file handles for these documents are cached
   in-process (keyed by absolute path + size + mtime) so the same QP/rubric
   isn't re-uploaded to Gemini on every single student — only once per
   session per worker process, then reused. Student answer pages are
   always unique per student, so they're uploaded fresh each time and not
   cached. This is a plain upload-handle cache — NOT Gemini context/prompt
   caching, and it doesn't change how each student's answers are marked;
   every marking call still reads the full rubric and question paper fresh
   as before (see point 3 from v9, preserved below).

Everything else is unchanged from v9:

3. STAGE-LEVEL SCORING, PLAIN-NUMBER OUTPUT: The AI still first reads the
   entire rubric front-to-back and identifies every distinct scoring stage
   (e.g. Part A Q1(a), Part B Q3, Section II Q5(ii)) along with that
   stage's own rubric-defined maximum — this full-rubric read is now
   emphasised even more strongly so no stage is skipped or missed. It then
   marks the student ONLY against stages actually attempted — stages the
   student did not attempt are left out entirely (no marks, and explicitly
   NOT zero). The prompt now walks the rubric stage list in strict order
   (start to finish) to reduce the chance of the AI drifting or stopping
   partway through a long rubric.

4. PLAIN NUMBER LIST — NO LABELS, NO AI-SIDE ARITHMETIC: The AI returns a
   single comma-separated "SCORES:" list containing ONLY plain numbers —
   one number per attempted stage, in rubric order, with no stage names,
   colons, or other text attached to each number. This is faster for the
   model to produce and far more reliable to parse than the old
   "<stage name>: <marks>" format. It never sums these, never states an
   overall total, and never calculates a percentage. The SYSTEM (not the
   AI) sums the numbers, then computes:
       total_score (%) = sum(stage_marks_obtained) / total_marks_for_paper * 100

5. NO GEMINI CONTEXT/PROMPT CACHING: The question paper and marking scheme
   content is read fresh and sent with EVERY single AI call, for EVERY
   student — nothing is cached on Gemini's side to shortcut marking. (The
   File API upload-handle cache described in point 2 above only avoids a
   redundant *upload*; the full document content is still supplied fresh
   to the model on every call, same as v9.)

6. BIGGER REPORT: The AI still writes one continuous, professional,
   narrative examiner's report (no headings, no bullet points, no
   markdown) — it just no longer states a total/percentage inside that
   report. The target length has been increased from ~100–150 words to
   ~220–280 words so the report gives a fuller picture of the student's
   performance.

7. (v10 point 7 — the per-student result cache — has been REMOVED in v11.
   See point 0 above.)
"""

from google import genai
from google.genai import types
import hashlib
import json
import random
import threading
from pathlib import Path
from typing import Dict, List, Optional
import os
import time
import re
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from marking_log import get_marking_logger
from page_prep import list_answer_pages, list_session_docs, sorted_natural, prepare_page_bytes, prep_settings
from storage import write_json_atomic

log = get_marking_logger()

FAILURE_SENTINEL = "MARKING_FAILED_NO_CONTENT"

# ── Model ──────────────────────────────────────────────────────────────────────
# Set GEMINI_MODEL in .env to change it — any model switch must go through
# tools/benchmark.py first (D3.4).
PRIMARY_MODEL = os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.6-flash"
# Optional: used ONLY when the primary model is overloaded (503 / "high
# demand") after its own retries. Blank = never switch models. A different
# model can mark differently, so benchmark it before setting this; every
# result records which model actually marked it.
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "").strip()
# Optional thinking level ("low" / "medium" / "high"). Blank = model default.
# Less thinking is faster and cheaper — benchmark before changing.
THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "").strip().lower()
if THINKING_LEVEL and THINKING_LEVEL not in ("minimal", "low", "medium", "high"):
    log.warning(f"GEMINI_THINKING_LEVEL={THINKING_LEVEL!r} is not minimal/low/medium/high — ignored")
    THINKING_LEVEL = ""

# Bumped whenever _build_report_prompt's wording changes, so every stored AI
# run can be traced back to the exact prompt that produced it (D1.2).
# v12: every document in the request is preceded by a text label
# ("QUESTION PAPER — page 1 of 2", "MARKING SCHEME …", "STUDENT ANSWER
# SCRIPT …"), so the model no longer has to guess where the question paper
# ends and the marking scheme / answers begin.
PROMPT_VERSION = "v12"

# ── Output budget (6.7) ────────────────────────────────────────────────────────
# Thinking tokens count against max_output_tokens, so 8192 could run out
# before the report was finished. MAX_OUTPUT_TOKENS is the normal budget;
# RETRY_OUTPUT_TOKENS is used for the single automatic re-try when a reply
# comes back cut off (D3.1).
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default

MAX_OUTPUT_TOKENS   = _env_int("GEMINI_MAX_OUTPUT_TOKENS", 32768)
RETRY_OUTPUT_TOKENS = _env_int("GEMINI_RETRY_OUTPUT_TOKENS", 65536)

# ── Network retries inside one AI call (D2.1) ─────────────────────────────────
# Quota/429 errors are NOT retried here — they are raised straight away so
# the queue can cool that key down and move the job to a free key.
NETWORK_RETRY_DELAYS = (2.0, 5.0, 10.0)
# A 503 "model overloaded" clears on the order of tens of seconds, not two:
# hammering it every 2 s wastes calls (which can count against quota) and,
# with several workers retrying in lockstep, makes the overload worse.
OVERLOAD_RETRY_DELAYS = (6.0, 15.0, 30.0)
_sleep = time.sleep   # indirection so tests can skip the waits
_QUOTA = ("429", "resource_exhausted", "resource exhausted", "quota")
_OVERLOADED = ("503", "unavailable", "overloaded", "high demand")


def _is_overloaded(msg: str) -> bool:
    m = msg.lower()
    return any(k in m for k in _OVERLOADED) and not any(k in m for k in _QUOTA)

# ── Transient error detection (mirrors queue_manager) ──────────────────────────
_TRANSIENT = [
    "ssl", "timeout", "503", "502", "429", "eof",
    "connection", "reset", "network", "dns", "unreachable",
    "temporary", "resource_exhausted", "resource exhausted",
    "unavailable", "deadline", "quota", "overloaded",
    "internal", "socket", "broken pipe", "getaddrinfo",
    "failed to connect", "connection refused",
]

_VALID_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
# Question papers and rubrics may be uploaded as PDFs (setup.html allows .pdf) —
# Gemini can read PDFs natively (it processes each page internally, same as an
# image), so these are sent through as-is rather than being silently dropped.
_VALID_DOC_EXTS = _VALID_IMAGE_EXTS | {".pdf"}

# ── Gemini File API — shared upload-handle cache (D2.3) ────────────────────────
# Keyed on "key_fingerprint::abs_path::size::mtime_ns":
#   * path + size + mtime — a modified/replaced file on disk is never served
#     a stale handle;
#   * key fingerprint — an uploaded file belongs to the Google project of the
#     key that uploaded it, so a handle is only reused by workers holding the
#     same key (sharing it across keys made every other worker's call fail
#     and re-upload).
# Shared by every worker thread in this process. A per-entry lock makes sure
# that when several workers need the same question paper at the same moment,
# only one uploads it and the others reuse that upload.
_file_handle_cache: Dict[str, "types.File"] = {}
_file_handle_cache_lock = threading.Lock()
_file_upload_locks: Dict[str, threading.Lock] = {}
_MAX_FILE_HANDLE_CACHE = 500
_FILE_HANDLE_SAFETY_SECS = 3600   # re-upload if a handle expires within the hour

# ── One Gemini client per API key, shared by every job on that key ────────────
# Creating a client per job threw away the HTTP connection pool, so every
# student paid a fresh TLS handshake. The client is thread-safe.
_clients: Dict[str, "genai.Client"] = {}
_clients_lock = threading.Lock()


def _client_for(api_key: str) -> "genai.Client":
    with _clients_lock:
        client = _clients.get(api_key)
        if client is None:
            client = _clients[api_key] = genai.Client(
                api_key=api_key, http_options={"api_version": "v1beta"})
        return client


# ── Prepared question-paper / rubric parts (inline path) ──────────────────────
# Every student in a session shares the same question paper and rubric, so
# their bytes are read (and, with D2.5 on, re-encoded) once and reused. The
# key includes size + mtime + the D2.5 settings: a replaced file, or a
# changed setting, is always read fresh. The full content is still sent to
# the model on every call — this only skips re-reading the disk.
_doc_part_cache: Dict[str, tuple] = {}   # key -> (Part, prepared byte count)
_doc_part_cache_lock = threading.Lock()
_MAX_DOC_PART_CACHE = 200


def _finish_reason(response) -> Optional[str]:
    try:
        fr = response.candidates[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        return None
    if fr is None:
        return None
    return getattr(fr, "name", None) or str(fr).split(".")[-1]


def _usage(response) -> Dict[str, int]:
    um = getattr(response, "usage_metadata", None)
    if um is None:
        return {}
    out = {}
    for field, label in (("prompt_token_count", "prompt"),
                         ("candidates_token_count", "output"),
                         ("thoughts_token_count", "thinking"),
                         ("total_token_count", "total")):
        val = getattr(um, field, None)
        if val is not None:
            out[label] = int(val)
    return out


def _labelled(title: str, parts: list) -> list:
    """Puts a short text label before each document so the model always knows
    which image is question paper, marking scheme or student answer (v12)."""
    n = len(parts)
    out = []
    for i, part in enumerate(parts, start=1):
        out.append(types.Part.from_text(text=f"[{title} — part {i} of {n}]"))
        out.append(part)
    return out


def _looks_like_rejected_file(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "file" in msg and any(k in msg for k in ("403", "404", "permission", "not found", "expired", "not exist"))


class AIMarker:

    # Combined raw byte size (QP + rubric + all answer pages) above which the
    # pipeline switches from inline base64 to the Gemini File API. Inline
    # requests are typically capped around ~20MB once base64 (~33% overhead)
    # is added, so 15MB of raw file bytes leaves comfortable headroom for the
    # prompt text itself.
    INLINE_SIZE_THRESHOLD_BYTES = 15 * 1024 * 1024  # 15 MB

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not self.api_key:
            raise ValueError("GOOGLE_API_KEY not configured — add GEMINI_API_KEY_1… to .env")
        self._key_fp = hashlib.sha256(self.api_key.encode()).hexdigest()[:12]
        self.client = _client_for(self.api_key)

        self._gen_cfg = dict(
            temperature=0.0,
            top_p=0,
            top_k=1,
            # Thinking tokens share this budget with the ~220–280 word report,
            # so it is set well above what the report itself needs (6.7).
            max_output_tokens=MAX_OUTPUT_TOKENS,
            candidate_count=1,
        )
        if THINKING_LEVEL:
            self._gen_cfg["thinking_config"] = types.ThinkingConfig(thinking_level=THINKING_LEVEL)
        self._model_used = PRIMARY_MODEL
        self._payload_bytes = 0
        self._network_retries = 0
        self._file_uploads = 0
        self._file_cache_hits = 0

    # ═══════════════════════════════════════════════════════════════════════════
    # Prompt builder — the school's required marking procedure + report format
    # ═══════════════════════════════════════════════════════════════════════════
    def _build_report_prompt(self, student_name: str, student_id: str,
                              total_marks: float, page_count: int,
                              rubric_notes: str) -> str:

        page_note = ""
        if page_count is not None:
            page_note = (
                f"\nThe student submitted {page_count} page(s) of answers. "
                "Base your marking strictly on what is visible in these pages. "
                "Do not assume or invent answers for content that is not shown.\n"
            )

        rubric_note = (
            f"\nADDITIONAL TEACHER MARKING NOTES:\n{rubric_notes}\n"
            if rubric_notes.strip() else ""
        )

        return f"""You are an experienced, highly accurate, and consistent examiner responsible for marking secondary school examinations.

You will be provided with:

1. The official Question Paper.
2. The official Marking Scheme / Rubric.
3. The student's answer script.
4. (Optional) Additional teacher marking notes.

Your responsibility is to assess the student's work exactly as a qualified human examiner would.

========================================
MARKING PROCEDURE
========================================

Follow these steps in the exact order.

STEP 1 — Read the Question Paper completely.

Before marking anything, carefully read the entire question paper and determine:

• The examination instructions.
• The number of questions/items the student is required to answer.
• Any compulsory questions.
• Section rules (for example: "Answer any three questions from Section B").
• The maximum marks allocated to each question.

Always follow the examination instructions throughout the marking process.

========================================

STEP 2 — Read the Official Marking Scheme.

Carefully study the entire rubric before looking at the student's answers.

Use the rubric as the primary authority for awarding marks.

Before you look at the student's answers, read the marking scheme from its very first stage to its very last stage, without skipping ahead or stopping early, and identify every distinct scoring STAGE defined in the rubric — for example Part A Question 1(a), Part A Question 1(b), Part B Question 3, Section II Question 5(ii), etc. — together with the exact maximum marks the rubric allocates to each individual stage. Build this as a complete, ordered list covering the ENTIRE rubric — a rubric with many stages must be read just as thoroughly as a short one, and stages appearing later in the document (e.g. the final questions or the last page of the marking scheme) matter exactly as much as the first ones and must never be rushed, glossed over, or omitted. Treat each of these as a separate, independently scored unit. Do not merge several rubric stages into one combined figure, and do not invent a stage that the rubric does not define.

Award marks only when the student's answer demonstrates knowledge or reasoning that matches the marking scheme, and only up to the maximum the rubric allocates to that specific stage — never more.

If additional teacher notes are provided, treat them as part of the official marking guidance.

========================================

STEP 3 — Read the Student's Answers.

Carefully examine every page of the student's script.

Do not skip pages.

Read all handwriting carefully before deciding that something is incorrect or missing.

Identify every relevant answer before awarding marks.

========================================

STEP 4 — Mark Each Stage.

Go through the complete stage list you identified in STEP 2, one at a time, IN THE EXACT SAME ORDER the stages appear in the rubric from start to finish — do not reorder them, do not jump around, and do not stop before reaching the final stage in the rubric. For every stage:

• Check whether the student actually attempted that stage at all (wrote something addressing it).
• If the student did NOT attempt that stage — it is blank, skipped, or nothing on the script addresses it — then do NOT award any mark for it, and do NOT award zero either. Simply leave that stage out of your final list of scores entirely, as if it were never mentioned. Do not guess, assume, or invent an attempt that is not present in the script.
• If the student DID attempt that stage, compare the answer directly with the marking scheme and award marks strictly for that stage only, up to (and never exceeding) that stage's own rubric-defined maximum.
• Award partial marks for a stage whenever the rubric allows it for that stage.
• Do not deduct marks simply because wording differs if the required idea is clearly communicated.
• Where diagrams, calculations, tables, or labelled illustrations are part of a stage's answer, assess them according to the rubric for that stage.
• If a student answers more questions/stages than permitted, follow the examination instructions when deciding which responses should be marked.
• Never lump two or more rubric stages together into one combined score — report each attempted stage on its own.

Always base marks on evidence found in the student's work.

Never assume knowledge that is not shown.

Never invent missing answers.

Never award a mark, including zero, for a stage the student did not attempt.

========================================

CONSISTENCY

The marking process must remain completely consistent throughout the entire examination.

The same answer must always receive the same score regardless of the student.

Do not become more generous or more severe as marking progresses.

Apply exactly the same marking standard to every question.

========================================

MARKS FOR THIS PAPER

The total marks available for this entire paper is {total_marks}, spread across all the individual stages defined in the marking scheme. You do NOT add these up yourself — the school system totals your reported stage marks automatically.

CRITICAL — PER-STAGE CEILING ONLY: For each individual stage, never award more marks than that specific stage's own rubric-defined maximum. Do not think about the paper-wide {total_marks} ceiling while marking a stage — only that stage's own allocation matters at that moment.

Do NOT scale, convert, or re-express your marks on any other scale (such as out of 100, or out of 20 per stage, or any scale other than the rubric's own points for that stage). Think and calculate only in terms of the raw marks the rubric allocates to each individual stage.

Do NOT calculate or state a percentage anywhere in your response, and do NOT think in percentage terms at any point while marking. Do NOT add up an overall total anywhere in your response, and do NOT state one — that arithmetic is performed separately by the school system from your individual stage scores, and it is not your job.
{page_note}{rubric_note}
========================================

BLANK OR UNREADABLE SCRIPTS

If every page of the student's answer script is completely blank, unreadable, or clearly contains no attempted answers, respond with exactly this text and nothing else: {FAILURE_SENTINEL}

========================================

OUTPUT REQUIREMENTS

Produce ONE continuous professional report of approximately 220 to 280 words. Do NOT exceed 300 words under any circumstances, and do not fall noticeably below 220 words.

Do NOT separate the report into multiple sections.

Do NOT create headings such as "Item Analysis", "Strengths", or "Weaknesses".

The report should flow naturally as one coherent paragraph (or at most two short paragraphs).

Because the report must stay within 300 words, you do not need to narrate every single question line by line — instead, within that fuller paragraph:

• Do NOT state a total score, a sum, or a percentage anywhere in the report — describe performance qualitatively only.
• Describe the overall pattern of performance across the paper in more depth (which sections/parts were handled well, which were not, and why).
• Mention the student's strongest area with at least one concrete example drawn from their actual answer.
• Mention the student's weakest area or biggest gap, again with a concrete example.
• Note any secondary observations worth flagging (e.g. a recurring misunderstanding, inconsistent performance between sections, presentation/working-shown issues).
• Give one or two clear, practical recommendations for improvement.

The report should read like a fuller, considered professional examiner's remark written for a teacher — informative and detailed, but never padded, repetitive, or waffling.

Do not use bullet points.

Do not use numbered lists.

Do not use markdown.

Do not mention your marking process.

Do not explain how you reached the marks.

Do not repeat the same point twice.

Simply provide the final concise report.

Ensure the report is internally consistent, factually accurate, and fully aligned with the official marking scheme and examination instructions.

========================================
REQUIRED RESPONSE FORMAT — FOLLOW EXACTLY
========================================

Line 1 must begin exactly with the label "SCORES:" followed by a single comma-separated list of PLAIN NUMBERS ONLY — nothing else on that line. Do NOT write stage names, question numbers, colons, words, or any other text next to the numbers — just the numbers themselves, separated by commas. The list must contain ONE number for every rubric stage the student actually attempted, listed strictly in the exact same order those stages appear in the rubric from start to finish — and NOTHING for stages the student did not attempt (no zeros, no placeholders, no entry of any kind for a skipped stage). Each number is simply the marks you awarded for that stage (this may legitimately be 0 if the stage was attempted but answered entirely incorrectly), and must never exceed that stage's own rubric-defined maximum. Never combine two or more stages into one number — one number per attempted stage, and never omit a stage that was attempted.

SCORES: [marks], [marks], [marks], [marks]

For example, if the student attempted four stages and scored 4, 2, 5 and 3 marks on them respectively, line 1 must read exactly: SCORES: 4, 2, 5, 3

Leave one blank line after that line, then write the full continuous professional report described above (approximately 220–280 words) as ordinary prose. Do not repeat the scores, any stage names, or any total inside the report.

Student being marked: {student_name} (ID: {student_id})
"""

    # ═══════════════════════════════════════════════════════════════════════════
    # Image helpers
    # ═══════════════════════════════════════════════════════════════════════════

    def load_rubric_context(self, exam_path: Path) -> str:
        for name in ("rubric_text.txt", "rubric.txt"):
            p = exam_path / name
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    text = f.read()
                log.info(f"📄 Rubric text: {name} ({len(text)} chars)")
                return text
        return ""

    @staticmethod
    def _mime_for(path: Path) -> str:
        ext = path.suffix.lower()
        if ext == ".pdf":
            return "application/pdf"
        if ext in {".jpg", ".jpeg"}:
            return "image/jpeg"
        return f"image/{ext[1:]}"

    def prepare_images_for_gemini(self, image_paths: List[Path], shared: bool = False) -> List[types.Part]:
        """
        Loads images AND PDFs INLINE as base64 Part objects. PDFs are sent
        with mime_type "application/pdf" — Gemini reads PDF documents
        natively (each page is processed internally, the same way it
        processes a standalone image), so no local PDF-to-image conversion
        is needed.

        This is the FAST PATH, used when the combined payload for a call is
        comfortably under the inline size ceiling (see
        `INLINE_SIZE_THRESHOLD_BYTES`). For larger payloads,
        `prepare_files_via_file_api` is used instead — see that method's
        docstring for why.

        Files are taken in page-number order (page_2 before page_10). Images
        may be sent as an upright/downscaled in-memory copy when D2.5 is
        switched on in .env (see page_prep.prepare_page_bytes); the file on
        disk is never changed.

        `shared=True` (question paper / rubric) reuses the prepared bytes of
        an unchanged file across students — see `_doc_part_cache`.
        """
        parts: List[types.Part] = []
        for img_path in sorted_natural(image_paths):
            ext = img_path.suffix.lower()
            if ext not in _VALID_DOC_EXTS:
                continue
            try:
                st = img_path.stat()
                size = st.st_size
                if size < 100:
                    log.info(f"   ⚠  Tiny file ({size}B) skipped: {img_path.name}")
                    continue
                cache_key = (f"{img_path.resolve()}::{size}::{st.st_mtime_ns}::{prep_settings()}"
                             if shared else None)
                hit = None
                if cache_key:
                    with _doc_part_cache_lock:
                        hit = _doc_part_cache.get(cache_key)
                if hit:
                    part, n_bytes, where = hit[0], hit[1], "inline, reused"
                else:
                    with open(img_path, "rb") as f:
                        data = f.read()
                    mime = self._mime_for(img_path)
                    data, mime, note = prepare_page_bytes(img_path, data, mime)
                    part, n_bytes = types.Part.from_bytes(data=data, mime_type=mime), len(data)
                    where = "inline" + (f" → {n_bytes/1024:.1f} KB, {note}" if note else "")
                    if cache_key:
                        with _doc_part_cache_lock:
                            if len(_doc_part_cache) >= _MAX_DOC_PART_CACHE:
                                _doc_part_cache.pop(next(iter(_doc_part_cache)))
                            _doc_part_cache[cache_key] = (part, n_bytes)
                self._payload_bytes += n_bytes
                kind = "PDF" if ext == ".pdf" else "image"
                parts.append(part)
                log.info(f"   ✅ {img_path.name} ({size/1024:.1f} KB) [{kind} · {where}]")
            except Exception as e:
                log.info(f"   ⚠  Could not load {img_path.name}: {e}")
        return parts

    # ═══════════════════════════════════════════════════════════════════════════
    # Gemini File API — used automatically for large combined payloads so a
    # long, multi-page essay-style exam never hits the inline request size
    # ceiling. Documents are uploaded once and referenced by handle/URI on
    # the marking call rather than embedding raw base64 bytes.
    # ═══════════════════════════════════════════════════════════════════════════

    def _wait_for_file_active(self, file_obj, timeout: float = 60.0, poll: float = 1.0):
        """Polls a just-uploaded Gemini file until it leaves PROCESSING state."""
        start = time.time()
        while True:
            state = getattr(file_obj, "state", None)
            state_name = getattr(state, "name", str(state)) if state is not None else ""
            if state_name == "ACTIVE":
                return file_obj
            if state_name == "FAILED":
                raise RuntimeError(f"Gemini file processing failed for {getattr(file_obj, 'name', '?')}")
            if time.time() - start > timeout:
                raise TimeoutError(
                    f"Timed out waiting for Gemini file to become ACTIVE: "
                    f"{getattr(file_obj, 'name', '?')}"
                )
            time.sleep(poll)
            file_obj = self.client.files.get(name=file_obj.name)

    @staticmethod
    def _handle_is_fresh(handle) -> bool:
        exp = getattr(handle, "expiration_time", None)
        if exp is None:
            return True
        try:
            return exp.timestamp() - time.time() > _FILE_HANDLE_SAFETY_SECS
        except Exception:
            return True

    def _upload_file_to_gemini(self, path: Path):
        """
        Uploads a single file via the Gemini File API, reusing a cached
        handle if this exact file (by path + size + mtime) was already
        uploaded earlier in this process with the same key — which happens
        constantly for the question paper and rubric, since every student in
        a session shares the same ones. A handle close to its 48h expiry is
        replaced; one the API rejects is dropped by `_forget_file_handles`
        and re-uploaded on the next attempt.
        """
        stat = path.stat()
        cache_key = f"{self._key_fp}::{path.resolve()}::{stat.st_size}::{stat.st_mtime_ns}"

        with _file_handle_cache_lock:
            upload_lock = _file_upload_locks.setdefault(cache_key, threading.Lock())

        with upload_lock:
            with _file_handle_cache_lock:
                cached = _file_handle_cache.get(cache_key)
            if cached is not None and self._handle_is_fresh(cached):
                self._file_cache_hits += 1
                return cached

            mime = self._mime_for(path)

            def _do_upload():
                return self.client.files.upload(file=str(path), config={"mime_type": mime})

            uploaded = self._call_with_retry(_do_upload)
            uploaded = self._wait_for_file_active(uploaded)
            self._file_uploads += 1

            with _file_handle_cache_lock:
                _file_handle_cache[cache_key] = uploaded
                if len(_file_handle_cache) > _MAX_FILE_HANDLE_CACHE:
                    # Drop oldest-inserted entries (dict preserves insertion order)
                    for stale_key in list(_file_handle_cache.keys())[:100]:
                        _file_handle_cache.pop(stale_key, None)
                        _file_upload_locks.pop(stale_key, None)

            return uploaded

    def _forget_file_handles(self):
        """Drops every cached handle for this key (called when the API rejects one)."""
        with _file_handle_cache_lock:
            for k in [k for k in _file_handle_cache if k.startswith(f"{self._key_fp}::")]:
                _file_handle_cache.pop(k, None)

    def prepare_files_via_file_api(self, paths: List[Path]) -> List[types.Part]:
        """
        Uploads each document via the Gemini File API and returns a list of
        `Part` objects referencing the uploaded file by URI — functionally
        equivalent to `prepare_images_for_gemini` from the model's point of
        view, but the raw bytes never travel inline inside the
        generateContent request body. This is what removes the inline
        request-size ceiling for long, multi-page exams.
        """
        parts: List[types.Part] = []
        for path in sorted_natural(paths):
            ext = path.suffix.lower()
            if ext not in _VALID_DOC_EXTS:
                continue
            try:
                size = path.stat().st_size
                if size < 100:
                    log.info(f"   ⚠  Tiny file ({size}B) skipped: {path.name}")
                    continue
                uploaded = self._upload_file_to_gemini(path)
                part = types.Part.from_uri(file_uri=uploaded.uri, mime_type=uploaded.mime_type)
                parts.append(part)
                self._payload_bytes += size
                kind = "PDF" if ext == ".pdf" else "image"
                log.info(f"   ✅ {path.name} ({size/1024:.1f} KB) [{kind} · File API: {uploaded.name}]")
            except Exception as e:
                if any(k in str(e).lower() for k in _QUOTA):
                    raise   # let the queue move this job to another key
                log.info(f"   ⚠  Could not upload {path.name} via File API: {e}")
        return parts

    @staticmethod
    def _combined_size_bytes(*path_lists: List[Path]) -> int:
        total = 0
        for paths in path_lists:
            for p in paths:
                try:
                    total += p.stat().st_size
                except Exception:
                    pass
        return total

    def _prepare_documents(self, answer_pages: List[Path], qp_files: List[Path],
                            rubric_files: List[Path]):
        """
        Decides — per marking call — whether to send documents inline
        (fast path, used for the vast majority of scripts) or via the
        Gemini File API (used automatically once the combined raw payload
        crosses `INLINE_SIZE_THRESHOLD_BYTES`, e.g. a long multi-page
        essay exam). Returns (answer_parts, qp_parts, rubric_parts,
        used_file_api: bool).
        """
        self._payload_bytes = 0
        combined_bytes = self._combined_size_bytes(answer_pages, qp_files, rubric_files)

        if combined_bytes > self.INLINE_SIZE_THRESHOLD_BYTES:
            log.info(
                f"📦  Combined payload {combined_bytes/1024/1024:.1f} MB exceeds the "
                f"{self.INLINE_SIZE_THRESHOLD_BYTES/1024/1024:.0f} MB inline threshold — "
                f"switching to the Gemini File API for this call."
            )
            answer_parts = self.prepare_files_via_file_api(answer_pages)
            qp_parts     = self.prepare_files_via_file_api(qp_files)
            rubric_parts = self.prepare_files_via_file_api(rubric_files)
            return answer_parts, qp_parts, rubric_parts, True

        answer_parts = self.prepare_images_for_gemini(answer_pages)
        qp_parts     = self.prepare_images_for_gemini(qp_files, shared=True)
        rubric_parts = self.prepare_images_for_gemini(rubric_files, shared=True)
        return answer_parts, qp_parts, rubric_parts, False

    # ═══════════════════════════════════════════════════════════════════════════
    # Retry wrapper (D2.1) — also reused for Gemini File API upload calls.
    #   * quota / 429 → raised immediately, never retried on the same key: the
    #     queue cools that key down and moves the job to a free key.
    #   * network / temporary service errors → up to 3 retries, 2s / 5s / 10s
    #     apart plus a little random jitter so workers don't retry in lockstep.
    #   * 503 "model overloaded" → up to 3 retries, 6s / 15s / 30s apart (+ up
    #     to 50% jitter): the overload needs tens of seconds to clear.
    # ═══════════════════════════════════════════════════════════════════════════

    def _call_with_retry(self, fn, delays=NETWORK_RETRY_DELAYS):
        attempts = len(delays) + 1
        for attempt in range(attempts):
            try:
                return fn()
            except Exception as e:
                msg = str(e).lower()
                if any(k in msg for k in _QUOTA):
                    raise
                if any(k in msg for k in _TRANSIENT) and attempt < len(delays):
                    if _is_overloaded(msg):
                        base = OVERLOAD_RETRY_DELAYS[min(attempt, len(OVERLOAD_RETRY_DELAYS) - 1)]
                        delay = base + random.uniform(0, base * 0.5)
                    else:
                        delay = delays[attempt] + random.uniform(0, delays[attempt] * 0.3)
                    self._network_retries += 1
                    log.info(f"   ⚠  Transient error (attempt {attempt+1}/{attempts}): "
                             f"{type(e).__name__}: {str(e)[:80]}")
                    log.info(f"   ⏳ Retrying in {delay:.0f}s…")
                    _sleep(delay)
                    continue
                raise

    # ═══════════════════════════════════════════════════════════════════════════
    # AI call — ALWAYS fresh content. QP + rubric + answers are sent together
    # on every single call, for every student — either inline or via File API
    # references, depending on `_prepare_documents`'s decision above. No
    # Gemini context caching whatsoever.
    # ═══════════════════════════════════════════════════════════════════════════

    def _call_ai(self, prompt: str, qp_parts, rubric_parts, answer_parts,
                 max_output_tokens: int = None) -> dict:
        """
        Returns {"text", "finish_reason", "usage"} — finish_reason and token
        usage (including thinking tokens) are logged and stored with every
        run so cut-off replies can be seen and counted (6.7 / D1).
        """
        all_parts = (
            [types.Part.from_text(text=prompt)]
            + _labelled("QUESTION PAPER", qp_parts)
            + _labelled("MARKING SCHEME / RUBRIC", rubric_parts)
            + _labelled("STUDENT ANSWER SCRIPT", answer_parts)
            + [types.Part.from_text(text=(
                "[END OF STUDENT ANSWER SCRIPT] Now mark this script following the "
                "MARKING PROCEDURE above and reply in the REQUIRED RESPONSE FORMAT."))]
        )
        contents = [types.Content(role="user", parts=all_parts)]
        cfg = dict(self._gen_cfg)
        if max_output_tokens:
            cfg["max_output_tokens"] = max_output_tokens
        config = types.GenerateContentConfig(**cfg)

        def _call(model):
            return lambda: self.client.models.generate_content(
                model=model, contents=contents, config=config)

        try:
            response = self._call_with_retry(_call(PRIMARY_MODEL))
            self._model_used = PRIMARY_MODEL
        except Exception as e:
            if not (FALLBACK_MODEL and FALLBACK_MODEL != PRIMARY_MODEL and _is_overloaded(str(e))):
                raise
            log.info(f"   ↪  {PRIMARY_MODEL} is overloaded — using fallback model {FALLBACK_MODEL}")
            response = self._call_with_retry(_call(FALLBACK_MODEL))
            self._model_used = FALLBACK_MODEL
        return {
            "text":          (response.text or "").strip(),
            "finish_reason": _finish_reason(response),
            "usage":         _usage(response),
            "max_output_tokens": cfg["max_output_tokens"],
        }

    # ═══════════════════════════════════════════════════════════════════════════
    # Response parser — extracts the per-stage marks obtained; the SYSTEM sums
    # them and computes the percentage. The AI never adds anything up itself.
    # ═══════════════════════════════════════════════════════════════════════════

    def _parse_stage_scores(self, scores_line: str) -> list:
        """
        Parses a "SCORES:" line into a list of {"stage": str, "marks": float}.
        The line is now expected to be PLAIN NUMBERS ONLY, comma-separated,
        one per attempted rubric stage, in rubric order — e.g. "4, 2, 5, 3".
        No stage names/labels are sent by the AI any more; generic
        positional labels ("Stage 1", "Stage 2", …) are generated here
        purely for the audit trail. Any entry that isn't a plain number is
        skipped rather than guessed at (defensive against stray AI text).
        """
        stage_scores = []
        entries = [e.strip() for e in scores_line.split(",") if e.strip()]
        for entry in entries:
            num_match = re.fullmatch(r"-?\d+(?:\.\d+)?", entry)
            if not num_match:
                # Defensive fallback for stray text around a number. A label
                # before a colon/equals ("Q1(a): 4") is dropped first — taking
                # the first number there would read the question number, not
                # the mark. "4/5" or "4 marks" still give 4.
                tail = re.split(r"[:=]", entry)[-1]
                num_match = re.search(r"-?\d+(?:\.\d+)?", tail)
                if not num_match:
                    continue
            marks = float(num_match.group(0))
            stage_scores.append({
                "stage": f"Stage {len(stage_scores) + 1}",
                "marks": marks,
            })
        return stage_scores

    def _parse_response(self, res_text: str, student_id: str, student_name: str,
                         total_marks: float) -> dict:

        if FAILURE_SENTINEL in res_text:
            log.info("❌ AI: no valid answer content in images")
            return {"success": False,
                    "error":   "Images appear to contain no student answer content."}

        m = re.search(r"SCORES\s*:\s*(.+)", res_text, re.IGNORECASE)
        if not m:
            return {
                "success": False,
                "error":   f"No scores list found in AI response. Raw (first 300): {res_text[:300]}",
            }

        scores_line  = m.group(1).strip()
        stage_scores = self._parse_stage_scores(scores_line)

        if not stage_scores:
            return {
                "success": False,
                "error":   f"Could not parse any scores from AI response. Raw (first 300): {res_text[:300]}",
            }

        # ── The SYSTEM sums the plain numbers — the AI never adds these up ──
        raw_obtained = sum(s["marks"] for s in stage_scores)
        if raw_obtained > total_marks:
            log.info(f"⚠  Sum of stage scores {raw_obtained} exceeds paper max {total_marks} — saved as-is and flagged for review")
        # Flag, don't cap: an impossible total is saved exactly as summed so a
        # teacher sees the real problem; _score_flags marks it "Needs review".

        numbers_str = ", ".join(str(s["marks"]) for s in stage_scores)
        log.info(f"🧮  Parsed {len(stage_scores)} attempted stage score(s): [{numbers_str}]")
        log.info(f"🧮  System sum: {raw_obtained}/{total_marks}")

        # Everything after the SCORES line is the continuous narrative report
        report_text = res_text[m.end():].strip()
        report_text = report_text.lstrip(":\n\r ").strip()
        if not report_text:
            report_text = "AI marking complete."

        # ── Percentage is calculated HERE by the system, never by the AI ──
        percentage = (raw_obtained / total_marks * 100) if total_marks else 0.0
        percentage = round(percentage, 1)

        return {
            "success":               True,
            "student_id":            student_id,
            "student_name":          student_name,
            "total_score":           percentage,           # % — computed by the system
            "raw_score":             round(raw_obtained, 1),  # sum of attempted-stage marks
            "max_score":             total_marks,           # total marks for the paper
            "status":                "Marked",
            "questions":             {},
            "stage_scores":          stage_scores,          # audit trail: per-stage breakdown, attempted stages only
            "overall_feedback":      report_text,
            "strengths":             [],
            "areas_for_improvement": [],
        }

    # ═══════════════════════════════════════════════════════════════════════════
    # Result checks — run AFTER parsing and never change the score. They only
    # decide whether a result is trustworthy or must go to a teacher as
    # "Needs review" (D3.1 incomplete output, D3.2 flag-don't-cap).
    # ═══════════════════════════════════════════════════════════════════════════

    _REPORT_END_CHARS = ('.', '!', '?', '"', "'", ')', '”', '’')

    @classmethod
    def _incomplete_reason(cls, call: dict, parsed: dict) -> Optional[str]:
        """Why this reply looks cut off, or None if it is complete."""
        fr = (call.get("finish_reason") or "").upper()
        if fr and fr != "STOP":
            return f"AI reply stopped early (finish_reason={fr})"
        if not parsed.get("success"):
            return None   # a clean failure (e.g. blank script) is not a truncation
        report = (parsed.get("overall_feedback") or "").strip()
        if not report or report == "AI marking complete.":
            return "AI reply contained scores but no report"
        if not report.endswith(cls._REPORT_END_CHARS):
            return "AI report ends mid-sentence"
        return None

    @staticmethod
    def _score_flags(res_text: str, parsed: dict, total_marks: float) -> List[str]:
        """
        D3.2 — problems with the numbers themselves. Nothing is capped: the
        score is saved exactly as the stage marks add up, and these reasons
        mark the result "Needs review" so a teacher decides.
        (The AI returns plain numbers without stage names, so a per-stage
        maximum can't be checked here — only what the system can know for
        certain: unreadable entries, negatives, a single stage above the whole
        paper, and a total above the paper total.)
        """
        flags = []
        stages = parsed.get("stage_scores") or []
        m = re.search(r"SCORES\s*:\s*(.+)", res_text or "", re.IGNORECASE)
        if m:
            entries = [e.strip() for e in m.group(1).split(",") if e.strip()]
            unreadable = len(entries) - len(stages)
            if unreadable > 0:
                flags.append(f"{unreadable} stage score(s) could not be read")
        negatives = [s["marks"] for s in stages if s["marks"] < 0]
        if negatives:
            flags.append(f"negative stage score(s): {', '.join(f'{n:g}' for n in negatives)}")
        too_big = [s["marks"] for s in stages if s["marks"] > total_marks]
        if too_big:
            flags.append(f"a stage score is larger than the whole paper ({', '.join(f'{n:g}' for n in too_big)} > {total_marks:g})")
        raw_sum = sum(s["marks"] for s in stages)
        if raw_sum > total_marks:
            flags.append(f"stage scores add up to {raw_sum:g}, more than the paper total of {total_marks:g}")
        if raw_sum < 0:
            flags.append(f"stage scores add up to {raw_sum:g}, below zero")
        return flags

    def _save_run_record(self, runs_dir: Optional[Path], record: dict):
        if not runs_dir:
            return
        try:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            write_json_atomic(Path(runs_dir) / f"{stamp}.json", record)
        except Exception as e:
            log.warning(f"   ⚠  Could not save AI run record: {e}")

    # ═══════════════════════════════════════════════════════════════════════════
    # Public: mark_student
    # ═══════════════════════════════════════════════════════════════════════════

    def mark_student(self, student_id: str, student_name: str,
                     exam_path: Path, student_folder: Path,
                     exam_info: dict = None, runs_dir: Path = None) -> dict:
        """
        Marks one student. The returned dict is unchanged from v11, plus a
        "_run" entry with timings, token usage, finish_reason, transport and
        `review_reasons` (empty when the result is trustworthy). When
        `runs_dir` is given, a JSON record of every AI call is written there
        (the queue passes student_<id>/ai_runs; the benchmark passes its own
        output folder so it never writes into a session).
        """
        self._payload_bytes = 0
        self._network_retries = 0
        self._file_uploads = 0
        self._file_cache_hits = 0
        self._model_used = PRIMARY_MODEL

        if not student_folder.exists():
            return {"success": False, "error": f"Student folder not found: {student_folder}"}

        # page_2 before page_10 — alphabetical order sent long scripts to the AI out of order (D2.4)
        answer_pages = list_answer_pages(student_folder)
        if not answer_pages:
            return {"success": False, "error": "No answer sheets found in student folder"}

        page_count  = len(answer_pages)
        total_marks = float((exam_info or {}).get("total_marks", 100) or 100)

        log.info(
            f"\n{'━'*62}\n"
            f"🎓  {student_name}  (ID: {student_id})\n"
            f"    Folder      : {student_folder}\n"
            f"    Pages       : {page_count}\n"
            f"    Total marks : {total_marks}\n"
            f"{'━'*62}"
        )

        # ── No result caching: every call to mark_student re-marks the
        #    student fresh from whatever pages currently sit in their
        #    folder. This is intentional — a cached score would otherwise
        #    silently persist and be returned again if a teacher re-marks
        #    the same student (e.g. after fixing a blurry page or
        #    correcting the wrong pages), which is exactly the failure
        #    mode this pipeline must avoid. ─────────────────────────────
        rubric_text = self.load_rubric_context(exam_path)

        # ── Question paper + rubric — read fresh from disk EVERY time. No
        #    Gemini context/prompt caching is used anywhere in this pipeline;
        #    only the underlying File API *upload handle* may be reused
        #    across students in the same session (see _upload_file_to_gemini),
        #    which is a pure transport-layer optimisation. ────────────────────
        qp_files     = list_session_docs(exam_path, "question_paper")
        rubric_files = list_session_docs(exam_path, "rubric")

        # ── Decide inline vs. Gemini File API based on the combined payload
        #    size for THIS call (QP + rubric + this student's answer pages),
        #    then load everything accordingly. ─────────────────────────────
        t_prep = time.time()
        answer_parts, qp_parts, rubric_parts, used_file_api = self._prepare_documents(
            answer_pages, qp_files, rubric_files
        )
        prep_secs = time.time() - t_prep

        if not answer_parts:
            return {"success": False, "error": "No valid answer images could be loaded"}

        # ── Loud diagnostics: silently marking without the rubric defeats the
        #    entire point of stage-level scoring, so this must never happen
        #    quietly. Two distinct failure modes are possible: (a) no files
        #    matching the naming pattern exist on disk at all, or (b) files
        #    exist but none were loadable (unsupported format, corrupted
        #    file, or a File API upload failure). ─────────────────────────────
        if not qp_files:
            log.info(f"⚠️  No question_paper_* files found on disk in {exam_path}")
        elif not qp_parts:
            log.info(f"⚠️  {len(qp_files)} question paper file(s) found but none were loadable "
                  f"(unsupported format or corrupted file): {[f.name for f in qp_files]}")

        if not rubric_files and not rubric_text:
            log.info(f"⚠️  No rubric_* files (and no rubric_text.txt) found on disk in {exam_path}")
        elif rubric_files and not rubric_parts:
            log.info(f"⚠️  {len(rubric_files)} rubric file(s) found but none were loadable "
                  f"(unsupported format or corrupted file): {[f.name for f in rubric_files]}")

        if not qp_parts or (not rubric_parts and not rubric_text.strip()):
            missing = []
            if not qp_parts:
                missing.append("question paper")
            if not rubric_parts and not rubric_text.strip():
                missing.append("marking scheme/rubric")
            missing_str = " and ".join(missing)
            log.info(f"🛑  Refusing to mark {student_id} — no {missing_str} available. "
                  f"The AI must always read the rubric before marking a student.")
            return {
                "success": False,
                "error": (
                    f"Cannot mark: {missing_str} not found, or the uploaded file was corrupted "
                    f"or in an unsupported format (supported: JPG, PNG, WEBP, PDF). "
                    f"Please re-upload it in the exam setup."
                ),
            }

        prompt = self._build_report_prompt(
            student_name=student_name,
            student_id=student_id,
            total_marks=total_marks,
            page_count=page_count,
            rubric_notes=rubric_text,
        )
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]

        total_imgs = len(qp_parts) + len(rubric_parts) + len(answer_parts)
        transport  = "File API" if used_file_api else "inline"

        # ── AI call, with ONE automatic re-try at a larger output budget if the
        #    reply comes back cut off (D3.1). Each call is stateless and
        #    re-sends the full question paper + rubric + answers. ─────────────
        ai_secs = parse_secs = 0.0
        usage_total: Dict[str, int] = {}
        attempts = []
        result, call, incomplete = None, None, None
        budgets = [None, max(RETRY_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS)]
        for attempt_no, budget in enumerate(budgets, start=1):
            log.info(
                f"🤖  AI call [{PRIMARY_MODEL}] via {transport}  "
                f"(QP:{len(qp_parts)} Rubric:{len(rubric_parts)} "
                f"Ans:{len(answer_parts)} Total:{total_imgs} Pages:{page_count})"
                + (f"  — re-try with max_output_tokens={budget}" if budget else "")
            )
            t0 = time.time()
            try:
                call = self._call_ai(prompt, qp_parts, rubric_parts, answer_parts, max_output_tokens=budget)
            except Exception as e:
                if used_file_api and _looks_like_rejected_file(e) and attempt_no == 1:
                    # A cached File API handle expired or was refused — drop the
                    # cache for this key, re-upload, and try once more.
                    log.info(f"   ♻  File API handle rejected ({str(e)[:80]}) — re-uploading")
                    self._forget_file_handles()
                    answer_parts, qp_parts, rubric_parts, used_file_api = self._prepare_documents(
                        answer_pages, qp_files, rubric_files)
                    call = self._call_ai(prompt, qp_parts, rubric_parts, answer_parts, max_output_tokens=budget)
                else:
                    raise
            elapsed  = time.time() - t0
            ai_secs += elapsed
            res_text = call["text"]
            for k, v in (call.get("usage") or {}).items():
                usage_total[k] = usage_total.get(k, 0) + (v or 0)

            log.info(f"📊  Response in {elapsed:.1f}s  [{transport.upper()} — no context cache]  "
                     f"finish_reason={call['finish_reason']}  usage={call['usage']}")
            log.info(f"📝  RAW AI RESPONSE (first 500 chars):\n{res_text[:500]}\n{'·'*40}")

            # ── Parse ────────────────────────────────────────────────────────
            t1 = time.time()
            result = self._parse_response(res_text, student_id, student_name, total_marks)
            parse_secs += time.time() - t1

            incomplete = self._incomplete_reason(call, result)
            attempts.append({"attempt": attempt_no, "finish_reason": call["finish_reason"],
                             "max_output_tokens": call["max_output_tokens"],
                             "ai_secs": round(elapsed, 2), "incomplete": incomplete})

            self._save_run_record(runs_dir, {
                "student_id":        student_id,
                "student_name":      student_name,
                "exam_folder":       str(exam_path),
                "at":                datetime.now().isoformat(timespec="seconds"),
                "model":             self._model_used,
                "prompt_version":    PROMPT_VERSION,
                "prompt_sha":        prompt_sha,
                "attempt":           attempt_no,
                "max_output_tokens": call["max_output_tokens"],
                "finish_reason":     call["finish_reason"],
                "usage":             call["usage"],
                "transport":         transport,
                "pages":             page_count,
                "payload_bytes":     self._payload_bytes,
                "timings":           {"prepare_secs": round(prep_secs, 3), "ai_secs": round(elapsed, 3)},
                "raw_response":      res_text,
                "parsed_stage_scores": result.get("stage_scores"),
                "raw_score":         result.get("raw_score"),
                "final_score":       result.get("total_score"),
                "success":           bool(result.get("success")),
                "error":             result.get("error"),
                "incomplete":        incomplete,
            })

            if not incomplete:
                break
            if attempt_no < len(budgets):
                log.info(f"✂️  {student_id}: {incomplete} — re-trying once with a larger output limit")

        review_reasons = []
        if incomplete and result.get("success"):
            review_reasons.append(f"Report incomplete — {incomplete}")
        elif incomplete and not result.get("success"):
            result["error"] = f"AI reply incomplete ({incomplete}). {result.get('error', '')}".strip()
            result["incomplete"] = True
        if result.get("success"):
            review_reasons += self._score_flags(call["text"], result, total_marks)
            if review_reasons:
                log.info(f"🚩  {student_id} needs review: {'; '.join(review_reasons)}")

        result["_run"] = {
            "model":             self._model_used,
            "prompt_version":    PROMPT_VERSION,
            "finish_reason":     call["finish_reason"],
            "usage":             usage_total,
            "transport":         transport,
            "pages":             page_count,
            "payload_bytes":     self._payload_bytes,
            "prepare_secs":      round(prep_secs, 3),
            "ai_secs":           round(ai_secs, 3),
            "parse_secs":        round(parse_secs, 4),
            "ai_attempts":       attempts,
            "network_retries":   self._network_retries,
            "file_uploads":      self._file_uploads,
            "file_cache_hits":   self._file_cache_hits,
            "review_reasons":    review_reasons,
        }

        log.info(
            f"✅  {student_id} → {result.get('total_score', 'ERR')}%  "
            f"(raw {result.get('raw_score', 'ERR')}/{total_marks}, {page_count} pages)"
        )
        return result

    # ═══════════════════════════════════════════════════════════════════════════
    # Batch marking
    # ═══════════════════════════════════════════════════════════════════════════

    def mark_all_students_in_session(self, exam_path: Path, delay: float = 5.0) -> dict:
        metadata_file = exam_path / "students_metadata.json"
        if not metadata_file.exists():
            return {"success": False, "error": "No students_metadata.json found"}

        with open(metadata_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        exam_info = data.get("exam_info", {})
        students  = data.get("students", [])
        results = {"total": len(students), "marked": 0, "failed": 0, "details": []}

        for student in students:
            sid   = student.get("id") or student.get("student_id")
            sname = student.get("student_name", "Unknown")

            if student.get("status") == "Marked":
                log.info(f"ℹ  {sid} already marked — skipping")
                results["marked"] += 1
                continue

            res = self.mark_student(
                student_id=sid, student_name=sname,
                exam_path=exam_path,
                student_folder=exam_path / f"student_{sid}",
                exam_info=exam_info,
            )

            if res.get("success"):
                student.update(res)
                write_json_atomic(metadata_file, data, indent=4)
                results["marked"] += 1
                results["details"].append({
                    "student_id": sid, "score": res["total_score"], "status": "success",
                })
            else:
                results["failed"] += 1
                results["details"].append({
                    "student_id": sid, "error": res.get("error"), "status": "failed",
                })
                log.info(f"❌  {sid} failed: {res.get('error')}")

            if delay > 0:
                log.info(f"⏳  Waiting {delay}s…")
                time.sleep(delay)

        return results


def mark_exam_session(exam_path: Path, api_key: str = None) -> dict:
    return AIMarker(api_key=api_key).mark_all_students_in_session(exam_path)