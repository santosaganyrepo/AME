"""
Marking benchmark (D1.3) — re-marks a fixed set of students N times with the
real marker and reports how consistent the marks are.

Standalone: it does not import app.py, does not start the marking queue, and
never writes into a session — every output (CSV, summary, one JSON record
per AI call) goes into its own folder under benchmarks/.

Usage
-----
  # a set file: one row per student — session folder, student ID, and
  # optionally the teacher's own mark (%)
  python tools/benchmark.py --set benchmark_set.csv --label baseline

  # or name one session directly
  python tools/benchmark.py --session "uploads/exams/2026/Term 1/S3_A/Biology/Test 1" \
                            --students 001,002,003 --label baseline

  # after a change, run again and compare against the baseline (accuracy gate)
  python tools/benchmark.py --set benchmark_set.csv --label exif-rotate \
                            --exif-rotate --compare benchmarks/<baseline folder>

Set file (CSV, header row required):
    session,student_id,teacher_mark
    uploads/exams/2026/Term 1/S3_A/Biology/Test 1,001,64
    uploads/exams/2026/Term 1/S3_A/Mathematics/Test 1,014,

A teacher mark can also come from --teacher-marks (same columns) or, when
neither gives one, from a manual override already saved in the session
(its final_score).

Outputs (benchmarks/<timestamp>_<label>/):
    results.csv    per student: every round's score, mean, SD, range,
                   teacher mark and difference, time, truncations
    summary.json   average SD per subject, average time per student,
                   truncation rate, settings used
    summary.txt    the same, readable
    runs/          the raw AI record of every call (same format as ai_runs/)
"""

import argparse
import csv
import json
import os
import re
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

GATE_MEAN_PP = 2.0          # accuracy gate: max allowed move of the set's average score
GATE_SD_TOLERANCE = 0.05    # SD may not get worse by more than this (percentage points)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:80]


def _read_csv(path: Path) -> list:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_items(args) -> list:
    rows = []
    if args.set:
        rows += _read_csv(Path(args.set))
    if args.session:
        for sid in [s.strip() for s in (args.students or "").split(",") if s.strip()]:
            rows.append({"session": args.session, "student_id": sid, "teacher_mark": ""})
    teacher = {}
    if args.teacher_marks:
        for r in _read_csv(Path(args.teacher_marks)):
            teacher[(str(Path(r.get("session", "")).resolve()), r.get("student_id", ""))] = _to_float(r.get("teacher_mark"))

    items = []
    for r in rows:
        session = Path(r.get("session", ""))
        if not session.is_absolute():
            session = (ROOT / session)
        meta_file = session / "students_metadata.json"
        if not meta_file.exists():
            print(f"⚠  Skipped — no students_metadata.json in {session}")
            continue
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        sid = r.get("student_id", "")
        student = next((s for s in meta.get("students", []) if str(s.get("id")) == sid), None)
        if student is None:
            print(f"⚠  Skipped — student {sid} not found in {session}")
            continue
        folder = session / (student.get("folder") or f"student_{sid}")
        mark = _to_float(r.get("teacher_mark"))
        if mark is None:
            mark = teacher.get((str(session.resolve()), sid))
        if mark is None and student.get("overridden"):
            mark = _to_float(student.get("final_score", student.get("total_score")))
        items.append({
            "session": session, "student_id": sid, "name": student.get("student_name", ""),
            "folder": folder, "exam_info": meta.get("exam_info", {}),
            "subject": meta.get("exam_info", {}).get("subject", ""), "teacher_mark": mark,
        })
    return items


def mark_once(item, round_no, runs_root, key_pool, lock):
    """One real marking call for one student; returns a round record."""
    from ai_marker_gemini_improved import AIMarker

    runs_dir = runs_root / _slug(f"{item['subject']}_{item['session'].name}") / _slug(item["student_id"]) / f"round_{round_no}"
    t0 = time.time()
    last_err = None
    for _ in range(8):
        key, wait = key_pool.acquire()
        if key is None:
            time.sleep(min(wait, 60))
            continue
        try:
            res = AIMarker(api_key=key).mark_student(
                student_id=item["student_id"], student_name=item["name"],
                exam_path=item["session"], student_folder=item["folder"],
                exam_info=item["exam_info"], runs_dir=runs_dir)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if key_pool.is_quota_error(last_err):
                key_pool.report_quota_error(key, last_err[:120])
                continue
            break
        finally:
            key_pool.release(key)
        run = res.get("_run") or {}
        attempts = run.get("ai_attempts") or []
        return {
            "ok": bool(res.get("success")), "score": res.get("total_score"),
            "raw": res.get("raw_score"), "secs": time.time() - t0,
            "truncated": bool(attempts and attempts[0].get("incomplete")),
            "still_incomplete": bool(attempts and attempts[-1].get("incomplete")),
            "needs_review": bool(run.get("review_reasons")),
            "review": "; ".join(run.get("review_reasons") or []),
            "error": res.get("error"), "usage": run.get("usage") or {},
            "payload": run.get("payload_bytes"), "finish": run.get("finish_reason"),
        }
    return {"ok": False, "score": None, "secs": time.time() - t0, "truncated": False,
            "still_incomplete": False, "needs_review": False, "review": "", "error": last_err or "no key available",
            "usage": {}, "payload": None, "finish": None}


def _sd(values):
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def summarise(items, rounds, results, settings):
    rows, per_subject = [], {}
    all_rounds = [r for rs in results.values() for r in rs]
    for i, item in enumerate(items):
        rs = results[i]
        scores = [r["score"] for r in rs if r["ok"] and r["score"] is not None]
        mean = statistics.mean(scores) if scores else None
        sd = _sd(scores) if scores else None
        row = {
            "session": str(item["session"].relative_to(ROOT) if ROOT in item["session"].parents else item["session"]),
            "subject": item["subject"], "student_id": item["student_id"], "name": item["name"],
        }
        for n in range(rounds):
            row[f"round_{n+1}"] = rs[n]["score"] if n < len(rs) and rs[n]["ok"] else ""
        row.update({
            "mean": round(mean, 2) if mean is not None else "",
            "sd": round(sd, 2) if sd is not None else "",
            "range": round(max(scores) - min(scores), 2) if scores else "",
            "teacher_mark": item["teacher_mark"] if item["teacher_mark"] is not None else "",
            "mean_minus_teacher": round(mean - item["teacher_mark"], 2) if (mean is not None and item["teacher_mark"] is not None) else "",
            "avg_secs": round(statistics.mean(r["secs"] for r in rs), 1) if rs else "",
            "truncated_rounds": sum(r["truncated"] for r in rs),
            "needs_review_rounds": sum(r["needs_review"] for r in rs),
            "failed_rounds": sum(not r["ok"] for r in rs),
        })
        rows.append(row)
        if sd is not None:
            per_subject.setdefault(item["subject"] or "?", []).append(sd)

    means = [r["mean"] for r in rows if r["mean"] != ""]
    sds = [r["sd"] for r in rows if r["sd"] != ""]
    diffs = [abs(r["mean_minus_teacher"]) for r in rows if r["mean_minus_teacher"] != ""]
    n = len(all_rounds) or 1
    summary = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "settings": settings,
        "students": len(items), "rounds": rounds, "ai_markings": len(all_rounds),
        "set_average_score": round(statistics.mean(means), 2) if means else None,
        "average_sd": round(statistics.mean(sds), 3) if sds else None,
        "average_sd_by_subject": {k: round(statistics.mean(v), 3) for k, v in sorted(per_subject.items())},
        "mean_abs_diff_vs_teacher": round(statistics.mean(diffs), 2) if diffs else None,
        "avg_secs_per_marking": round(statistics.mean(r["secs"] for r in all_rounds), 2) if all_rounds else None,
        "truncation_rate": round(sum(r["truncated"] for r in all_rounds) / n, 4),
        "still_incomplete_rate": round(sum(r["still_incomplete"] for r in all_rounds) / n, 4),
        "needs_review_rate": round(sum(r["needs_review"] for r in all_rounds) / n, 4),
        "failure_rate": round(sum(not r["ok"] for r in all_rounds) / n, 4),
        "avg_payload_kb": round(statistics.mean(r["payload"] for r in all_rounds if r["payload"]) / 1024, 1)
                          if any(r["payload"] for r in all_rounds) else None,
        "avg_tokens": {k: round(statistics.mean(r["usage"].get(k, 0) for r in all_rounds if r["usage"]), 1)
                       for k in ("prompt", "output", "thinking", "total")} if any(r["usage"] for r in all_rounds) else {},
    }
    return rows, summary


def compare(rows, summary, baseline_dir: Path) -> list:
    base_rows = {(r["session"], r["student_id"]): r for r in _read_csv(baseline_dir / "results.csv")}
    base_sum = json.loads((baseline_dir / "summary.json").read_text(encoding="utf-8"))
    lines = [f"Compared with baseline: {baseline_dir}"]
    pairs = []
    for r in rows:
        b = base_rows.get((r["session"].lower(), r["student_id"].lower())) or base_rows.get((r["session"], r["student_id"]))
        if b and r["mean"] != "" and _to_float(b.get("mean")) is not None:
            pairs.append((r, float(r["mean"]) - float(b["mean"]), _to_float(r["sd"]), _to_float(b.get("sd"))))
    if not pairs:
        lines.append("  No students in common with the baseline — nothing to compare.")
        return lines
    avg_shift = statistics.mean(d for _, d, _, _ in pairs)
    sd_now = statistics.mean(s for _, _, s, _ in pairs if s is not None)
    sd_base = statistics.mean(s for _, _, _, s in pairs if s is not None)
    lines.append(f"  Students compared: {len(pairs)}")
    lines.append(f"  Average score moved by {avg_shift:+.2f} percentage points (gate: ±{GATE_MEAN_PP})")
    lines.append(f"  Average SD: {sd_base:.3f} → {sd_now:.3f}")
    for r, d, _, _ in pairs:
        if abs(d) > GATE_MEAN_PP:
            lines.append(f"    {r['student_id']} ({r['subject']}): mean moved {d:+.2f} pp")
    t0, t1 = base_sum.get("avg_secs_per_marking"), summary.get("avg_secs_per_marking")
    if t0 and t1:
        lines.append(f"  Time per marking: {t0:.1f}s → {t1:.1f}s")
    p0, p1 = base_sum.get("avg_payload_kb"), summary.get("avg_payload_kb")
    if p0 and p1:
        lines.append(f"  Payload per marking: {p0:.0f} KB → {p1:.0f} KB")
    lines.append(f"  Truncation rate: {base_sum.get('truncation_rate')} → {summary.get('truncation_rate')}")
    ok = abs(avg_shift) <= GATE_MEAN_PP and sd_now <= sd_base + GATE_SD_TOLERANCE
    lines.append("  ACCURACY GATE: " + ("PASS" if ok else "FAIL — stop and report to Santos before keeping this change"))
    return lines


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Re-mark a benchmark set N times and measure consistency.")
    p.add_argument("--set", help="CSV with columns session,student_id[,teacher_mark]")
    p.add_argument("--session", help="one session folder (use with --students)")
    p.add_argument("--students", help="comma-separated student IDs in --session")
    p.add_argument("--teacher-marks", help="optional CSV session,student_id,teacher_mark")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--label", default="run")
    p.add_argument("--out", help="output folder (default benchmarks/<timestamp>_<label>)")
    p.add_argument("--workers", type=int, default=0, help="parallel markings (default: one per API key)")
    p.add_argument("--exif-rotate", action="store_true", help="D2.5(a): send photos rotated upright")
    p.add_argument("--max-edge", type=int, default=None, help="D2.5(b): downscale pages above this long edge (px)")
    p.add_argument("--compare", help="a previous benchmark folder to compare against (accuracy gate)")
    args = p.parse_args(argv)

    if args.exif_rotate:
        os.environ["PAGE_EXIF_ROTATE"] = "1"
    if args.max_edge is not None:
        os.environ["PAGE_MAX_EDGE"] = str(args.max_edge)

    from key_rotator import key_rotator
    import ai_marker_gemini_improved as marker_mod
    from page_prep import prep_settings

    if len(key_rotator) == 0:
        print("❌ No Gemini API keys in .env (GEMINI_API_KEY_1 …). The benchmark needs real AI calls.")
        return 1
    items = load_items(args)
    if not items:
        print("❌ No students to benchmark. Give --set FILE or --session DIR --students IDS.")
        return 1

    out = Path(args.out) if args.out else ROOT / "benchmarks" / f"{datetime.now():%Y%m%d-%H%M%S}_{_slug(args.label)}"
    out.mkdir(parents=True, exist_ok=True)
    runs_root = out / "runs"
    exif, max_edge = prep_settings()
    settings = {"label": args.label, "model": marker_mod.PRIMARY_MODEL, "prompt_version": marker_mod.PROMPT_VERSION,
                "max_output_tokens": marker_mod.MAX_OUTPUT_TOKENS, "exif_rotate": exif, "max_edge": max_edge,
                "keys": len(key_rotator)}
    workers = args.workers or len(key_rotator)
    print(f"📏 Benchmark '{args.label}': {len(items)} student(s) × {args.rounds} round(s), "
          f"{workers} in parallel — model {settings['model']}, exif_rotate={exif}, max_edge={max_edge or 'off'}")
    print(f"   Output: {out}")

    results = {i: [None] * args.rounds for i in range(len(items))}
    lock = threading.Lock()
    done = 0
    total = len(items) * args.rounds
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(mark_once, items[i], n + 1, runs_root, key_rotator, lock): (i, n)
                for n in range(args.rounds) for i in range(len(items))}
        for fut in as_completed(futs):
            i, n = futs[fut]
            try:
                results[i][n] = fut.result()
            except Exception as e:
                results[i][n] = {"ok": False, "score": None, "secs": 0, "truncated": False, "still_incomplete": False,
                                 "needs_review": False, "review": "", "error": str(e), "usage": {}, "payload": None, "finish": None}
            done += 1
            r = results[i][n]
            print(f"   [{done}/{total}] {items[i]['student_id']} round {n+1}: "
                  f"{(str(r['score']) + '%') if r['ok'] else 'FAILED ' + str(r['error'])[:60]}  ({r['secs']:.0f}s)")

    rows, summary = summarise(items, args.rounds, results, settings)
    fields = list(rows[0].keys())
    with open(out / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    lines = [f"Benchmark '{args.label}' — {summary['created']}",
             f"Model {settings['model']} · prompt {settings['prompt_version']} · exif_rotate={exif} · max_edge={max_edge or 'off'}",
             f"Students: {summary['students']} × rounds: {summary['rounds']} = {summary['ai_markings']} markings",
             f"Set average score: {summary['set_average_score']}%",
             f"Average SD (round-to-round): {summary['average_sd']}",
             *[f"   {subj}: SD {sd}" for subj, sd in summary["average_sd_by_subject"].items()],
             f"Mean |AI − teacher|: {summary['mean_abs_diff_vs_teacher']}",
             f"Average time per marking: {summary['avg_secs_per_marking']}s",
             f"Truncation rate (first reply cut off): {summary['truncation_rate']:.1%}",
             f"Still incomplete after re-try: {summary['still_incomplete_rate']:.1%}",
             f"Needs review: {summary['needs_review_rate']:.1%} · Failed: {summary['failure_rate']:.1%}",
             f"Average payload: {summary['avg_payload_kb']} KB · tokens: {summary['avg_tokens']}"]
    if args.compare:
        lines += [""] + compare(rows, summary, Path(args.compare))
    text = "\n".join(lines)
    (out / "summary.txt").write_text(text + "\n", encoding="utf-8")
    print("\n" + text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
