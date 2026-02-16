import cv2
import numpy as np
from pathlib import Path

class ImageValidator:
    """
    STRICT Blur Validator.
    Tuned to reject 'badly off' images while allowing clear handwriting.
    """

    def __init__(self, use_ai=False):
        self.use_ai = use_ai
        # INCREASED THRESHOLDS: 
        # 30-40 is the 'Strict' zone for document clarity.
        self.laplacian_thresh = 35 
        self.tenengrad_thresh = 250

    def validate_image(self, image_path):
        image_path = Path(image_path)
        img = cv2.imread(str(image_path))
        
        if img is None:
            return False, "Unreadable Image", 0.0

        # Standardize size so the math is always the same
        h, w = img.shape[:2]
        if w > 1200:
            img = cv2.resize(img, (1200, int(h * 1200 / w)))

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # 1. Laplacian Variance (Global Sharpness)
        lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()

        # 2. Tenengrad (Edge Sharpness)
        gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        tenengrad = np.mean(gx**2 + gy**2)

        # ---------- THE STRICT CRITERIA ----------
        # If it's below 35, it's likely too 'soft' for the AI to mark accurately.
        if lap_var < self.laplacian_thresh:
            return False, f"Too Blurry (Score: {int(lap_var)})", lap_var

        if tenengrad < self.tenengrad_thresh:
            return False, "Low Detail / Out of Focus", tenengrad

        return True, "Perfect", (lap_var + tenengrad) / 2
