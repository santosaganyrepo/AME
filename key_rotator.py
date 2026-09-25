"""
Shared API key rotator — used by both app.py (for status/debug routes)
and queue_manager.py (for actually assigning a key to each parallel job).

Each caller gets an explicit key handed back to it via get_key() and uses
that key for one job only. If that specific key hits a quota error, only
that key goes into cooldown — other workers running on other keys are
completely unaffected. This is safe under N parallel worker threads,
unlike mutating a single global os.environ['GOOGLE_API_KEY'].
"""

import time
import threading

API_KEY_POOL = [
    "AQ.Ab8RN6Kp1q5_Uw2zX5thaopwAixNFeqfdrkMptbeHByuKvjYmw",
    "AQ.Ab8RN6LoqvvLMsaIE9VmKGNHl-hMpQsh8Djk1cqT_Z-uOAGXpg",
    "AQ.Ab8RN6Lo3xsdOJe79nwJ7_3yQL_774pqBx3HcXlDgMqiy1izIg",
    "AQ.Ab8RN6JM8evLAka7Dbdu9cai91J3rb4EIja-Bwh6Sgtk3am4Wg",
    "AQ.Ab8RN6Kf3bzlFWk7xMUHzUnh6uJX52aingL8do_r6g09KXkwTw",
    "AQ.Ab8RN6ILKHrWiU6thVdWtz-yvtz4tPmInYMJMnTl7fYclmguTQ",
    "AQ.Ab8RN6LT4U94gLPJZYpv9Em_tq5mAM_DhuzU7chUbw4NJ_RdVg",
]


class APIKeyRotator:
    """Thread-safe round-robin key pool with per-key cooldowns."""

    QUOTA_KEYWORDS = ("429", "resource_exhausted", "resource exhausted", "quota")
    COOLDOWN_SECS  = 60   # how long a key sits out after a 429/quota hit

    def __init__(self, keys):
        self._keys = [k for k in keys if k]
        if not self._keys:
            raise ValueError("No API keys configured in API_KEY_POOL")
        self._lock = threading.Lock()
        self._idx  = 0
        self._exhausted_until = {}   # key -> unix timestamp it becomes usable again

    def __len__(self):
        return len(self._keys)

    def is_quota_error(self, error_text: str) -> bool:
        el = (error_text or "").lower()
        return any(k in el for k in self.QUOTA_KEYWORDS)

    def get_key(self) -> str:
        """
        Hand back the next available (not-in-cooldown) key. Safe to call
        concurrently from multiple worker threads — each call advances the
        round-robin pointer atomically under the lock.
        """
        with self._lock:
            now = time.time()
            n = len(self._keys)
            for _ in range(n):
                key = self._keys[self._idx % n]
                self._idx += 1
                if self._exhausted_until.get(key, 0) <= now:
                    return key
            # every key currently cooling down — hand back whichever recovers soonest
            return min(self._keys, key=lambda k: self._exhausted_until.get(k, 0))

    def report_quota_error(self, key: str, reason: str = ""):
        """
        Called by whichever worker's job just failed on THIS specific key.
        Only that key goes into cooldown — other workers on other keys are
        completely unaffected.
        """
        with self._lock:
            self._exhausted_until[key] = time.time() + self.COOLDOWN_SECS
            print(f"🔑 Key …{key[-6:]} hit quota ({reason or '429'}). Cooling down {self.COOLDOWN_SECS}s.")

    def status(self) -> dict:
        with self._lock:
            now = time.time()
            cooling = [k for k in self._keys if self._exhausted_until.get(k, 0) > now]
            return {
                "total_keys":        len(self._keys),
                "keys_cooling_down": len(cooling),
                "cooling_key_hints": [f"…{k[-6:]}" for k in cooling],
            }


key_rotator = APIKeyRotator(API_KEY_POOL)