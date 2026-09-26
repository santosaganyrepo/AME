"""
Page file helpers shared by the upload routes, the marker and the tools.

  natural_key / sorted_natural   page_2 before page_10 (D2.4)
  list_answer_pages              a student's page_* images in page order
  list_session_docs              question_paper_* / rubric_* in number order
  pdf_to_page_images             PDF answer script -> page_N.jpg (D5.1)
  prepare_page_bytes             in-memory copy sent to the AI (D2.5):
                                 EXIF-upright and/or downscaled — originals
                                 on disk are never modified

D2.5 is off by default and switched on from .env once the benchmark has
confirmed it doesn't change marks:
    PAGE_EXIF_ROTATE=1      rotate photos upright using their EXIF tag
    PAGE_MAX_EDGE=2400      downscale only if the long edge is larger (0 = off)
"""

import io
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

_num_re = re.compile(r"(\d+)")


def natural_key(p) -> list:
    name = p.name if isinstance(p, Path) else str(p)
    return [int(t) if t.isdigit() else t.lower() for t in _num_re.split(name)]


def sorted_natural(paths) -> list:
    return sorted(paths, key=natural_key)


def list_answer_pages(student_folder: Path) -> List[Path]:
    """page_* images in true page order (falls back to any image if none are named page_*)."""
    files = [p for p in student_folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    pages = [p for p in files if p.name.startswith("page_")]
    return sorted_natural(pages or files)


def list_session_docs(exam_path: Path, prefix: str) -> List[Path]:
    """question_paper_1, _2, … _10 in number order (not alphabetical)."""
    return sorted_natural(exam_path.glob(f"{prefix}_*"))


# ─── PDF answer scripts (D5.1) ────────────────────────────────────────────────
PDF_RENDER_DPI = 200


def is_pdf_bytes(raw: bytes) -> bool:
    return raw[:5] == b"%PDF-"


def pdf_to_page_images(pdf_bytes: bytes, dpi: int = PDF_RENDER_DPI) -> List[bytes]:
    """Renders every PDF page to a JPEG (≈200 DPI keeps handwriting readable)."""
    import fitz  # PyMuPDF
    from PIL import Image

    out = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=92, optimize=True)
            out.append(buf.getvalue())
    return out


def convert_pdf_pages_in_folder(student_folder: Path) -> int:
    """
    For students uploaded as PDFs before D5.1 (page_N.pdf on disk, no page
    images): renders the PDFs into page_N.jpg so the marker can read them.
    The PDFs are kept (renamed original.pdf, original_2.pdf, …). Returns the
    number of page images created (0 if nothing needed converting).
    """
    if list_answer_pages(student_folder):
        return 0
    pdfs = sorted_natural(p for p in student_folder.glob("page_*") if p.suffix.lower() == ".pdf")
    if not pdfs:
        return 0
    n = 0
    for i, pdf in enumerate(pdfs, start=1):
        for img in pdf_to_page_images(pdf.read_bytes()):
            n += 1
            (student_folder / f"page_{n}.jpg").write_bytes(img)
        pdf.rename(student_folder / ("original.pdf" if i == 1 else f"original_{i}.pdf"))
    return n


# ─── In-memory page preparation for the AI call (D2.5) ────────────────────────

def _flag(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in ("1", "true", "yes", "on")


def prep_settings() -> Tuple[bool, int]:
    try:
        max_edge = int(os.getenv("PAGE_MAX_EDGE", "0") or 0)
    except ValueError:
        max_edge = 0
    return _flag("PAGE_EXIF_ROTATE"), max(0, max_edge)


def prepare_page_bytes(path: Path, raw: bytes, default_mime: str) -> Tuple[bytes, str, Optional[str]]:
    """
    Returns (bytes, mime, note). With both D2.5 settings off, or for
    non-images, returns the original bytes untouched. Otherwise returns a
    re-encoded high-quality JPEG copy; `note` says what was done.
    """
    exif_rotate, max_edge = prep_settings()
    if (not exif_rotate and not max_edge) or path.suffix.lower() not in IMAGE_EXTS:
        return raw, default_mime, None
    try:
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(raw))
        notes = []
        if exif_rotate:
            orientation = img.getexif().get(0x0112, 1)
            if orientation not in (None, 1):
                img = ImageOps.exif_transpose(img)
                notes.append(f"rotated(exif={orientation})")
        if max_edge and max(img.size) > max_edge:
            scale = max_edge / max(img.size)
            new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
            notes.append(f"resized {img.width}x{img.height}->{new_size[0]}x{new_size[1]}")
            img = img.resize(new_size, Image.LANCZOS)
        if not notes:
            return raw, default_mime, None
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92, optimize=True)
        return buf.getvalue(), "image/jpeg", ", ".join(notes)
    except Exception as e:
        return raw, default_mime, f"prep skipped ({type(e).__name__})"
