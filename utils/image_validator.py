import cv2
import numpy as np
from pathlib import Path

class ImageValidator:
    def __init__(self, use_ai=False):
        self.use_ai = use_ai
        self.size = 50         # Your original size for FFT masking          
        self.fft_thresh = 10     # Your original FFT threshold
        self.sharp_thresh = 10 # NEW: Laplacian threshold for edge sharpness

    def validate_image(self, image_path):
        """
        Returns (is_valid, reason, score) to match your app.py requirements.
        """
        image_path = Path(image_path)
        if not image_path.exists():
            return False, "File not found", 0.0

        img = cv2.imread(str(image_path))
        if img is None:
            return False, "Unreadable or corrupted image", 0.0

        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # 1. NEW: Sharpness Check (Laplacian Variance)
        # This specifically detects "fuzziness" that FFT sometimes misses.
        # Blurry images like yours usually score < 30.
        sharpness_score = cv2.Laplacian(gray, cv2.CV_64F).var()

        # 2. Brightness Check (Ink Visibility)
        avg_brightness = np.mean(gray)
        if avg_brightness < 30:
            return False, "Image too dark to read ink", sharpness_score
        if avg_brightness > 2000:
            return False, "Image washed out (too bright)", sharpness_score

        # 3. Your Original FFT Logic
        gray_res = cv2.resize(gray, (500, 500))
        f = np.fft.fft2(gray_res)
        fshift = np.fft.fftshift(f)
        cy, cx = 250, 250
        fshift[cy - self.size:cy + self.size, cx - self.size:cx + self.size] = 0
        f_ishift = np.fft.ifftshift(fshift)
        img_back = np.fft.ifft2(f_ishift)
        magnitude = 20 * np.log(np.abs(img_back) + 1e-10)
        fft_score = np.mean(magnitude)

        # 4. FINAL DECISION: Must pass BOTH tests
        if sharpness_score < self.sharp_thresh:
            return False, f"Blurry or out of focus (Sharpness: {sharpness_score:.1f})", sharpness_score
        
        if fft_score < self.fft_thresh:
            return False, f"Low frequency detail (FFT: {fft_score:.1f})", fft_score

        return True, "Accepted", sharpness_score