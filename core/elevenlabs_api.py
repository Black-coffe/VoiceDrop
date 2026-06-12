"""
ElevenLabs API Client - Speech to Text

Resilient against the real-world failures seen in the field:
429 system_busy, 5xx, read/write timeouts, dropped connections (WinError 10054),
SSL handshake timeouts, and DNS failures (offline). Transient failures are
retried with exponential backoff (honoring Retry-After); validation/auth errors
are not retried.
"""
import logging
import re
import time
from typing import Optional

import httpx

from config import ELEVENLABS_API_KEY, ELEVENLABS_STT_URL

# Scribe (v1 and v2) tags non-speech sounds as parentheticals like "(смеётся)",
# "(тишина)". We disable them at source via tag_audio_events=false; this strips
# any residue (only parentheses that contain a known audio-event word, so real
# text is safe).
_AUDIO_EVENT_RE = re.compile(
    r"\s*\([^)]*?(?:смеёт|смеет|смех|хохот|кашл|вздыха|вздох|тишина|молчан|музык|"
    r"аплодисм|шум|шёпот|шепот|laugh|cough|sigh|silence|music|applause|noise|"
    r"whisper|breath)[^)]*?\)",
    re.IGNORECASE,
)


def _strip_audio_events(text: str) -> str:
    if not text:
        return text
    text = _AUDIO_EVENT_RE.sub("", text)
    text = re.sub(r"[ ]{2,}", " ", text)
    return text.strip()

# HTTP statuses worth retrying (transient server-side / rate limiting).
_RETRY_STATUS = {429, 500, 502, 503, 504}
# Total attempts = _MAX_RETRIES + 1.
_MAX_RETRIES = 3
# Exponential backoff base (seconds): 0.8, 1.6, 3.2 ...
_BASE_BACKOFF = 0.8
_MAX_BACKOFF = 30.0

# Connect short so true offline fails fast; read long for larger audio uploads.
_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=5.0)


class TranscriptionError(Exception):
    """Transcription failed.

    offline   -> looks like no connectivity (DNS / connect failure).
    retryable -> the error class is transient (already retried internally).
    """
    def __init__(self, message: str, offline: bool = False, retryable: bool = False):
        super().__init__(message)
        self.offline = offline
        self.retryable = retryable


_SUBSCRIPTION_CACHE_SEC = 300.0  # don't re-poll /v1/user/subscription more often


class ElevenLabsClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or ELEVENLABS_API_KEY
        self._client: Optional[httpx.Client] = None
        self.last_language_code: Optional[str] = None  # language scribe detected last
        self.max_retries = _MAX_RETRIES
        self.timeout = _TIMEOUT
        # STT quality knobs (scribe_v2). no_verbatim removes fillers/false-starts
        # at the STT side for free (B1); keyterms biases recognition toward a
        # word list (B2, +20% cost when used). Both default off here; the app
        # sets them from settings + keyterms.json before each transcription.
        self.no_verbatim: bool = False
        self.keyterms: list = []
        # Cached snapshot of /v1/user/subscription. The endpoint changes only
        # when something is billed, so 5 min is plenty fresh for the UI.
        self._subscription_cache: Optional[dict] = None
        self._subscription_cache_at: float = 0.0

    def configure(self, max_retries: Optional[int] = None,
                  read_timeout: Optional[float] = None):
        """Apply user-tunable network settings (retries, read timeout)."""
        if max_retries is not None:
            try:
                self.max_retries = max(0, min(int(max_retries), 5))
            except (TypeError, ValueError):
                pass
        if read_timeout is not None:
            try:
                rt = max(10.0, min(float(read_timeout), 180.0))
                self.timeout = httpx.Timeout(connect=10.0, read=rt, write=30.0, pool=5.0)
                if self._client and not self._client.is_closed:
                    self._client.close()  # rebuild with new timeout on next use
                self._client = None
            except (TypeError, ValueError):
                pass

    def _get_client(self) -> httpx.Client:
        """Get or create HTTP client with connection pooling"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=self.timeout,
                limits=httpx.Limits(max_keepalive_connections=5)
            )
        return self._client

    def warm_up(self) -> None:
        """Establish TCP+TLS to api.elevenlabs.io so the first transcribe()
        skips the handshake (~100–300 ms saved on cold start).

        Best-effort: any failure is silent. We send a HEAD to the STT endpoint —
        the server replies 405/4xx but the keepalive connection is now in the
        httpx pool. No request body, no audio uploaded, doesn't bill.
        """
        if not self.api_key:
            return
        try:
            client = self._get_client()
            # Short timeout: if warm-up can't finish in 5s, we don't want it
            # piling up behind a real transcribe call later.
            client.head(
                ELEVENLABS_STT_URL,
                headers={"xi-api-key": self.api_key},
                timeout=httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0),
            )
            logging.debug("ElevenLabs connection warmed up")
        except Exception as e:
            # Cold start will just pay the handshake. Not worth surfacing.
            logging.debug(f"ElevenLabs warm-up skipped: {e}")

    def transcribe(self, audio_data: bytes, language: str = None) -> str:
        """
        Transcribe audio to text using ElevenLabs API, with retries/backoff.

        Args:
            audio_data: WAV audio data as bytes
            language: Language code (None = auto-detect)

        Returns:
            Transcribed text

        Raises:
            TranscriptionError: after retries are exhausted or on a non-retryable
            error. Inspect .offline / .retryable for messaging.
        """
        if not self.api_key:
            raise TranscriptionError("ElevenLabs API key is not configured")

        client = self._get_client()
        headers = {"xi-api-key": self.api_key}
        # scribe_v2: scribe_v1 is removed by ElevenLabs on 2026-07-09. v2 is
        # API-compatible (same multipart form), better WER (2.3%), RU/UK both
        # tier "Excellent". tag_audio_events still defaults to true in v2, so we
        # keep sending false to suppress "(laughs)"/"(тишина)" non-speech tags.
        data = {
            "model_id": "scribe_v2",
            "tag_audio_events": "false",
            # B1: scribe_v2-only. Removes filler words / false starts / non-speech
            # at the STT side — free, zero added latency (replaces much of polish).
            "no_verbatim": "true" if self.no_verbatim else "false",
        }
        # Only pin the language if specified (otherwise scribe_v2 auto-detects).
        if language:
            data["language_code"] = language
        # B2: bias recognition toward a keyterm list (names, brands, tech terms).
        # httpx sends a list value as repeated multipart fields — the encoding
        # ElevenLabs expects for array form params. Empty list = not sent.
        if self.keyterms:
            data["keyterms"] = list(self.keyterms)

        last_error: Optional[TranscriptionError] = None

        for attempt in range(self.max_retries + 1):
            # Rebuild the multipart payload each attempt (the body is consumed).
            files = {"file": ("audio.wav", audio_data, "audio/wav")}

            try:
                response = client.post(
                    ELEVENLABS_STT_URL, headers=headers, files=files, data=data
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # DNS / no route -> almost certainly offline.
                last_error = TranscriptionError(
                    "Нет связи с ElevenLabs (проверьте интернет)",
                    offline=True, retryable=True
                )
                logging.warning(f"Connect failed (offline?), attempt {attempt + 1}: {e}")
            except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
                    httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError) as e:
                # Transient transport error (timeouts, dropped connection 10054).
                last_error = TranscriptionError(
                    f"Сетевая ошибка при обращении к ElevenLabs: {e}",
                    retryable=True
                )
                logging.warning(f"Transport error, attempt {attempt + 1}: {e}")
            except httpx.HTTPError as e:
                last_error = TranscriptionError(f"HTTP ошибка: {e}", retryable=True)
                logging.warning(f"HTTP error, attempt {attempt + 1}: {e}")
            else:
                if response.status_code == 200:
                    result = response.json()
                    lang = result.get("language_code")
                    if lang:
                        self.last_language_code = lang
                        prob = result.get("language_probability")
                        suffix = f" (p={prob:.2f})" if isinstance(prob, (int, float)) else ""
                        logging.info(f"STT detected language: {lang}{suffix}")
                    return _strip_audio_events(result.get("text", ""))

                is_retryable_status = response.status_code in _RETRY_STATUS

                # Retryable HTTP status (rate limit / server hiccup)?
                if is_retryable_status and attempt < self.max_retries:
                    wait = self._retry_after(response)
                    if wait is None:
                        wait = self._backoff(attempt)
                    logging.warning(
                        f"ElevenLabs {response.status_code}, retrying in {wait:.1f}s "
                        f"(attempt {attempt + 1}/{self.max_retries})"
                    )
                    time.sleep(wait)
                    continue

                # Out of retries on a transient status -> retryable=True (worth
                # queueing). Permanent errors (400 audio_too_short, 401/403 auth)
                # -> retryable=False (re-sending would never succeed).
                raise self._error_from_response(response, retryable=is_retryable_status)

            # We got here only from an exception branch; back off and retry.
            if attempt < self.max_retries:
                time.sleep(self._backoff(attempt))
                continue
            raise last_error

        # Loop exhausted (shouldn't normally reach here).
        raise last_error or TranscriptionError("Transcription failed")

    def _backoff(self, attempt: int) -> float:
        return min(_BASE_BACKOFF * (2 ** attempt), _MAX_BACKOFF)

    @staticmethod
    def _retry_after(response: httpx.Response) -> Optional[float]:
        """Parse Retry-After header (seconds), capped, if present."""
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return min(float(raw), _MAX_BACKOFF)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _error_from_response(response: httpx.Response, retryable: bool = False) -> "TranscriptionError":
        msg = f"ElevenLabs API error: {response.status_code}"
        try:
            data = response.json()
            detail = data.get("detail") or data.get("message") or data
            msg += f" - {detail}"
        except Exception:
            text = (response.text or "")[:200]
            if text:
                msg += f" - {text}"
        return TranscriptionError(msg, retryable=retryable)

    def get_subscription(self, force: bool = False) -> Optional[dict]:
        """Fetch /v1/user/subscription (cached). Returns a small flat dict or None.

        Fields surfaced for the UI:
          used       — character_count consumed in the current billing period
          limit      — character_limit for the current billing period
          reset_unix — next_character_count_reset_unix (UTC seconds, or None)
          tier       — plan tier (e.g. 'free', 'creator', 'pro')
          status     — subscription status string

        STT minutes get converted into this same character/credit pool on the
        server side; the exact ratio isn't documented, so we display the raw
        credits and let the user reason about it.
        """
        if not self.api_key:
            return None
        now = time.time()
        if (not force
                and self._subscription_cache is not None
                and (now - self._subscription_cache_at) < _SUBSCRIPTION_CACHE_SEC):
            return self._subscription_cache

        try:
            client = self._get_client()
            response = client.get(
                "https://api.elevenlabs.io/v1/user/subscription",
                headers={"xi-api-key": self.api_key},
            )
        except httpx.HTTPError as e:
            # Offline / transient — keep the previous cache if we have one.
            logging.warning(f"Subscription fetch failed (network): {e}")
            return self._subscription_cache

        if response.status_code != 200:
            logging.warning(
                f"Subscription fetch failed: HTTP {response.status_code} "
                f"{response.text[:200]}"
            )
            return self._subscription_cache

        try:
            data = response.json()
        except ValueError as e:
            logging.warning(f"Subscription response not JSON: {e}")
            return self._subscription_cache

        snapshot = {
            "used": int(data.get("character_count") or 0),
            "limit": int(data.get("character_limit") or 0),
            "reset_unix": data.get("next_character_count_reset_unix"),
            "tier": data.get("tier"),
            "status": data.get("status"),
        }
        self._subscription_cache = snapshot
        self._subscription_cache_at = now
        return snapshot

    def close(self):
        """Close the HTTP client"""
        if self._client and not self._client.is_closed:
            self._client.close()
