# ExamManager

AI-assisted exam marking for schools. Teachers photograph or upload answer
scripts; Gemini marks each script against the question paper and marking
scheme; the system (never the AI) adds up the marks.

**Requires Python 3.11 or newer** (tested on 3.12 and 3.14).

## Quickstart

### 1. Install

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate      macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Create `.env`

Copy `.env.example` to `.env`, then:

- set `SECRET_KEY` to a long random value:
  `python -c "import secrets; print(secrets.token_hex(32))"`
- paste your Gemini keys into `GEMINI_API_KEY_1` … `GEMINI_API_KEY_5`.
  One marking worker runs per key. Blank slots are ignored. Create each key
  in a **different Google Cloud project**: quota is counted per project, so
  several keys from one project share one limit and add no speed.

`.env` holds secrets and is git-ignored. Never commit it.

### 3. Create a teacher account

```bash
python manage_users.py add santos --name "Santos"
python manage_users.py list            # also: passwd <user>, remove <user>
```

You type the password at a hidden prompt. Accounts are stored hashed in
`users.json`. Changes take effect without restarting the app.

### 4. Start the app

```bash
python app.py
```

This starts one process (waitress, 8 threads). The console prints the
addresses to use, for example:

```
🌐 ExamManager running on http://localhost:5000
   On other devices on this network: http://192.168.1.20:5000
```

Any marking jobs that were waiting or running when the app last stopped are
picked up again automatically.

### 5. Open it from another device

On a phone or laptop on the **same Wi-Fi/network**, open the
`http://192.168.x.x:5000` address shown above and sign in. If it doesn't
load, allow Python through the computer's firewall on port 5000 (Windows
asks the first time the app starts; choose *Private networks*).

### 6. Back up

```bash
python tools/backup.py          # zips uploads/, users.json, settings → backups/
```

The newest 7 backups are kept. To run it daily on Windows, create a Task
Scheduler task that runs `.venv\Scripts\python.exe tools\backup.py` with
the project folder as *Start in*. On macOS/Linux, use a cron entry.

### 7. Run the benchmark

The benchmark re-marks a fixed set of students several times with the real
marker and measures how consistent the marks are. It never changes a
session.

```bash
cp tools/benchmark_set.example.csv benchmark_set.csv     # (Windows: copy) then edit it
python tools/benchmark.py --set benchmark_set.csv --label baseline
# after a change:
python tools/benchmark.py --set benchmark_set.csv --label after --compare benchmarks/<baseline folder>
```

Results go to `benchmarks/<time>_<label>/` (`results.csv`, `summary.txt`).
`--compare` prints **ACCURACY GATE: PASS/FAIL**: the set's average score
must not move by more than 2 percentage points, and the round-to-round
spread (SD) must not get worse.

## Where things are

| What | Where |
|---|---|
| Exam sessions, pages, results | `uploads/exams/<year>/<term>/<class>_<stream>/<subject>/<exam>/` |
| Every AI reply, per student | `…/student_<id>/ai_runs/*.json` |
| Marking log (timings, tokens, retries) | `logs/marking.log` |
| Unfinished marking jobs | `pending_jobs.json` |
| Teacher accounts | `users.json` |

Settings you can change in `.env`: `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`
and `GEMINI_THINKING_LEVEL` (change these only after running the benchmark),
`GEMINI_MAX_OUTPUT_TOKENS`, `PAGE_EXIF_ROTATE`, `PAGE_MAX_EDGE`,
`MARKING_WORKERS_PER_KEY` and `SERVER_THREADS`. See `.env.example`.

## Speed and reliability

- If the model is overloaded, jobs retry with growing, randomised waits
  instead of all hammering it at once.
- If a key hits its quota, the job moves to another key. That key rests for
  as long as Google asks, or an hour for a daily limit. If every key is out
  for more than 15 minutes, the job fails straight away with *"Daily AI
  limit reached"*. Use **Retry failed** later.
- An invalid or expired key is set aside and logged. It no longer fails
  students.
- Paid-tier keys: set `MARKING_WORKERS_PER_KEY=2` (or more) to mark several
  students per key at once.
