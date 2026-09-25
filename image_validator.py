"""
Image Quality Validator
========================
Single source of truth for "is this exam page readable enough to mark".
Replaces the three previously-duplicated, unused ImageValidator classes
(init.py / validator.py / image_validator.py) with one implementation that
is actually wired into the upload pipeline (batch_processor.py).

Two entry points:
  - validate_image(path)        -> for files already on disk
  - validate_image_bytes(raw)   -> for in-memory bytes (used during batch
                                    ZIP/folder extraction, before anything
                                    is written to disk)

Both return (is_valid, reason, score) where score is a rough sharpness
metric — higher is sharper. Thresholds are tuned for scanned/photographed
handwritten exam pages, not general photography.
"""

import cv2
import numpy as np
from pathlib import Path

LAPLACIAN_THRESH = 35     # global sharpness (document-clarity zone)
TENENGRAD_THRESH = 250    # edge sharpness
DARK_THRESH      = 25     # mean brightness floor (ink invisible below this)
BRIGHT_THRESH    = 245    # mean brightness ceiling (washed out above this)


class ImageValidator:
    """Strict blur/quality validator tuned for exam page photos."""

    def __init__(self, use_ai=False):
        self.use_ai = use_ai
        self.laplacian_thresh = LAPLACIAN_THRESH
        self.tenengrad_thresh = TENENGRAD_THRESH

    def _score_gray(self, gray):
        h, w = gray.shape[:2]
        if w > 1200:
            gray = cv2.resize(gray, (1200, int(h * 1200 / w)))

        lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()

        gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        tenengrad = float(np.mean(gx ** 2 + gy ** 2))

        brightness = float(np.mean(gray))
        return lap_var, tenengrad, brightness

    def _decide(self, lap_var, tenengrad, brightness):
        if brightness < DARK_THRESH:
            return False, f"Image too dark to read (brightness: {brightness:.0f})", lap_var
        if brightness > BRIGHT_THRESH:
            return False, f"Image washed out / overexposed (brightness: {brightness:.0f})", lap_var
        if lap_var < self.laplacian_thresh:
            return False, f"Too blurry (sharpness: {int(lap_var)}, needs {self.laplacian_thresh}+)", lap_var
        if tenengrad < self.tenengrad_thresh:
            return False, f"Low detail / out of focus (edge score: {int(tenengrad)})", lap_var
        return True, "Image is clear", (lap_var + tenengrad) / 2

    def validate_image(self, image_path):
        image_path = Path(image_path)
        if not image_path.exists():
            return False, "File not found", 0.0

        img = cv2.imread(str(image_path))
        if img is None:
            return False, "Unreadable or corrupted image", 0.0

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        lap_var, tenengrad, brightness = self._score_gray(gray)
        return self._decide(lap_var, tenengrad, brightness)

    def validate_image_bytes(self, raw: bytes):
        """Same checks as validate_image but operating on in-memory bytes —
        used during batch ZIP/folder extraction so pages never need to hit
        disk before we know whether they're usable."""
        try:
            arr = np.frombuffer(raw, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        except Exception:
            img = None

        if img is None:
            return False, "Unreadable or corrupted image", 0.0

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        lap_var, tenengrad, brightness = self._score_gray(gray)
        return self._decide(lap_var, tenengrad, brightness)