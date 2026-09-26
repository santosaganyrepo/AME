"""
Rubric sanity check at /setup (D3.3).

Reads mark allocations from whatever rubric text is machine-readable — the
typed rubric notes and any rubric PDF with a text layer (photos/scans have no
text and are skipped). Only explicit allocations in brackets are counted,
e.g. "(5 marks)", "(2 mks)", "[3 marks]", "[4]". A line that states a total
("Total: 50 marks") is read as the rubric's declared total instead.

Returns a plain warning string when the numbers clearly disagree with the
total marks the teacher entered, or None when they agree or can't be read.
It never blocks setup.
"""

import re
from pathlib import Path
from typing import List, Optional, Tuple

# "(5 marks)" / "[5 marks]" need a unit; a bare "[5]" is accepted in square
# brackets only — "(1)", "(2)" are far more often sub-question numbers.
_ALLOC_RE = re.compile(
    r"\(\s*(\d+(?:\.\d+)?)\s*(?:marks?|mks?|pts?|points?)\s*\)"
    r"|\[\s*(\d+(?:\.\d+)?)\s*(?:marks?|mks?|pts?|points?)?\s*\]",
    re.IGNORECASE,
)
_TOTAL_RE = re.compile(r"total[^0-9\n]{0,20}(\d+(?:\.\d+)?)\s*(?:marks?|mks?)?", re.IGNORECASE)

MIN_ALLOCATIONS = 2


def rubric_stage_marks(text: str) -> Tuple[List[float], Optional[float]]:
    """(allocations found, declared total or None)."""
    allocs, declared = [], None
    for line in (text or "").splitlines():
        if re.search(r"\btotal\b", line, re.IGNORECASE):
            m = _TOTAL_RE.search(line)
            if m and declared is None:
                declared = float(m.group(1))
            continue
        for m in _ALLOC_RE.finditer(line):
            val = float(m.group(1) or m.group(2))
            if 0 < val <= 100:
                allocs.append(val)
    return allocs, declared


def readable_rubric_text(exam_folder: Path, rubric_text: str) -> str:
    parts = [rubric_text or ""]
    for pdf in sorted(Path(exam_folder).glob("rubric_*.pdf")):
        try:
            import fitz  # PyMuPDF
            with fitz.open(str(pdf)) as doc:
                parts.append("\n".join(page.get_text() for page in doc))
        except Exception:
            continue
    return "\n".join(parts)


def _fmt(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else f"{n:g}"


def rubric_total_warning(exam_folder: Path, rubric_text: str, total_marks: float) -> Optional[str]:
    text = readable_rubric_text(exam_folder, rubric_text)
    allocs, declared = rubric_stage_marks(text)

    if declared is not None:
        if abs(declared - float(total_marks)) > 0.01:
            return (f"The marking scheme states a total of {_fmt(declared)} marks, but the total "
                    f"marks entered for this paper is {_fmt(total_marks)}. Percentages are "
                    f"calculated from the total you entered — please check it is correct.")
        return None

    if len(allocs) < MIN_ALLOCATIONS:
        return None
    s = sum(allocs)
    if abs(s - float(total_marks)) > 0.01:
        return (f"The stage marks readable in the marking scheme add up to {_fmt(s)}, but the "
                f"total marks entered for this paper is {_fmt(total_marks)}. If the paper has "
                f"optional questions this can be expected; otherwise please check the total.")
    return None
