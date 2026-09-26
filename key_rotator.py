"""
Shared API key rotator — used by both app.py (for status/debug routes)
and queue_manager.py (for actually assigning a key to each parallel job).

Keys are read from .env — never from source code:

    GEMINI_API_KEY_1=...
    GEMINI_API_KEY_2=...
    ...                              (any number of numbered slots)
    GEMINI_API_KEYS=key1,key2,...    (optional comma-separated list, also accepted)

Blank slots are ignored, duplicates are dropped, and order is preserved, so
"key slot 1" in the logs always means the first key in .env.

Each worker leases one key per job via acquire() and hands it back with
release(). acquire() only ever returns a key that is NOT cooling down, and
prefers the key with the fewest jobs currently running on it — so with 5
keys and 5 workers every worker normally has a key to itself. If a specific
key hits a quota error, only that key goes into cooldown; workers on other
keys are unaffected. If every key is cooling down, acquire() tells the
caller how long until the earliest one is usable again, so the worker waits
exactly that long instead of a fixed sleep.

How long a key sits out depends on what Google said:
  * a per-minute quota hit uses the reply's own retryDelay (often 10–40 s)
    instead of a blind 60 s, so the key comes back as soon as it can;
  * a per-DAY quota hit parks the key for an hour — re-trying it every
    minute only burns attempts and fails students;
  * an invalid / expired key is parked for 6 hours so it never fails a
    student's job again, and is reported by slot in the log.
"""

import os
import re
import threading
import time
from typing import Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def load_api_keys_from_env() -> list:
    numbered = []
    for name, value in os.environ.items():
        m = re.fullmatch(r"GEMINI_API_KEY_(\d+)", name)
        if m and value.strip():
            numbered.append((int(m.group(1)), value.strip()))
    keys = [v for _, v in sorted(numbered)]
    keys += [k.strip() for k in os.getenv("GEMINI_API_KEYS", "").split(",") if k.strip()]
    seen, unique = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    return unique


_RETRY_DELAY_RE = re.compile(r"retry[_ ]?delay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", re.IGNORECASE)
_RETRY_IN_RE    = re.compile(r"retry in (\d+(?:\.\d+)?)\s*s", re.IGNORECASE)


def quota_cooldown_secs(error_text: str, default: float = 60.0) -> float:
    """
    Seconds a key should rest after this quota error: the server's own
    retryDelay when it gives one, an hour for a per-day quota, else `default`.
    """
    text = error_text or ""
    if re.search(r"per\s*day|perday|daily", text, re.IGNORECASE):
        return APIKeyRotator.DAILY_COOLDOWN_SECS
    m = _RETRY_DELAY_RE.search(text) or _RETRY_IN_RE.search(text)
    if m:
        return min(max(float(m.group(1)) + 1.0, 5.0), APIKeyRotator.DAILY_COOLDOWN_SECS)
    return default


class APIKeyRotator:
    """Thread-safe key pool with per-key cooldowns and per-job leases."""

    QUOTA_KEYWORDS = ("429", "resource_exhausted", "resource exhausted", "quota")
    BAD_KEY_KEYWORDS = ("api key not valid", "api_key_invalid", "api key expired",
                        "api_key_expired", "api key was reported as leaked",
                        "permission_denied: api key", "consumer_suspended")
    COOLDOWN_SECS       = 60          # fallback when a 429 gives no retryDelay
    DAILY_COOLDOWN_SECS = 3600        # per-day quota exhausted
    BAD_KEY_COOLDOWN_SECS = 6 * 3600  # invalid / expired / suspended key

    def __init__(self, keys):
        self._keys = [k for k in keys if k]
        self._lock = threading.Lock()
        self._idx  = 0
        self._exhausted_until = {}   # key -> unix timestamp it becomes usable again
        self._leases = {}            # key -> number of jobs currently using it

    def __len__(self):
        return len(self._keys)

    def is_quota_error(self, error_text: str) -> bool:
        el = (error_text or "").lower()
        return any(k in el for k in self.QUOTA_KEYWORDS)

    def is_bad_key_error(self, error_text: str) -> bool:
        el = (error_text or "").lower()
        return any(k in el for k in self.BAD_KEY_KEYWORDS)

    def slot_of(self, key: str) -> Optional[int]:
        """1-based position of `key` in .env order — safe to log (never the key itself)."""
        try:
            return self._keys.index(key) + 1
        except ValueError:
            return None

    def acquire(self) -> Tuple[Optional[str], float]:
        """
        Returns (key, 0.0) for a key that is not cooling down, leased to the
        caller until release(key). If every key is cooling down, returns
        (None, seconds_until_the_earliest_key_recovers).
        """
        with self._lock:
            if not self._keys:
                return None, 0.0
            now = time.time()
            n = len(self._keys)
            ready = [self._keys[(self._idx + i) % n] for i in range(n)
                     if self._exhausted_until.get(self._keys[(self._idx + i) % n], 0) <= now]
            if not ready:
                wait = min(self._exhausted_until.get(k, 0) for k in self._keys) - now
                return None, max(0.5, wait)
            key = min(ready, key=lambda k: self._leases.get(k, 0))   # stable: first least-used wins
            self._idx = (self._keys.index(key) + 1) % n
            self._leases[key] = self._leases.get(key, 0) + 1
            return key, 0.0

    def release(self, key: Optional[str]):
        if not key:
            return
        with self._lock:
            if self._leases.get(key, 0) > 0:
                self._leases[key] -= 1

    def get_key(self) -> str:
        """
        Backward-compatible: next available (not-in-cooldown) key without a
        lease. If every key is cooling down, returns whichever recovers soonest.
        """
        with self._lock:
            if not self._keys:
                raise ValueError("No Gemini API keys configured in .env")
            now = time.time()
            n = len(self._keys)
            for _ in range(n):
                key = self._keys[self._idx % n]
                self._idx += 1
                if self._exhausted_until.get(key, 0) <= now:
                    return key
            return min(self._keys, key=lambda k: self._exhausted_until.get(k, 0))

    def report_quota_error(self, key: str, reason: str = "") -> float:
        """
        Called by whichever worker's job just failed on THIS specific key.
        Only that key goes into cooldown — other workers on other keys are
        completely unaffected. Returns the cooldown applied, in seconds.
        """
        return self._cool(key, quota_cooldown_secs(reason, self.COOLDOWN_SECS), "hit quota", reason)

    def report_bad_key(self, key: str, reason: str = "") -> float:
        """An invalid/expired key: parked for hours so it stops failing jobs."""
        return self._cool(key, self.BAD_KEY_COOLDOWN_SECS, "was REJECTED (invalid or expired key — replace it in .env)", reason)

    def _cool(self, key: str, secs: float, what: str, reason: str) -> float:
        with self._lock:
            until = time.time() + secs
            # never shorten a longer cooldown another worker already set
            self._exhausted_until[key] = max(until, self._exhausted_until.get(key, 0))
            slot = self.slot_of(key)
        import logging
        logging.getLogger("exammanager.marking").warning(
            f"🔑 Key slot {slot} (…{key[-6:]}) {what} ({(reason or '429')[:160]}). "
            f"Cooling down {secs:.0f}s.")
        return secs

    def status(self) -> dict:
        with self._lock:
            now = time.time()
            cooling = [k for k in self._keys if self._exhausted_until.get(k, 0) > now]
            return {
                "total_keys":        len(self._keys),
                "keys_cooling_down": len(cooling),
                "cooling_key_hints": [f"…{k[-6:]}" for k in cooling],
            }


key_rotator = APIKeyRotator(load_api_keys_from_env())
