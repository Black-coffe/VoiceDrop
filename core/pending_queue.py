"""
Pending Queue - persists recordings whose transcription failed (offline / outage)
so they can be auto-resent later instead of being lost.

Each item = `<id>.wav` (audio) + `<id>.json` (metadata). The .json sidecar is the
commit marker: an item is only considered complete once its .json exists, so a
crash mid-write leaves an orphan .wav that is simply ignored.

Dead-letter: items that fail with a PERMANENT error (HTTP 4xx validation, e.g.
audio_too_short, or auth) must NOT be retried forever — re-sending them would
never succeed. They are moved to a `dead/` subdirectory (out of the resend
glob) so they stop burning requests every scheduler tick. Same for items older
than the TTL. Nothing is silently deleted — the operator can inspect `dead/`.
"""
import json
import logging
import time
import uuid
from pathlib import Path
from typing import List, Optional

from config import BASE_DIR

# Cap to avoid unbounded growth during a long outage; oldest dropped beyond this.
_MAX_PENDING = 100
# Items older than this are swept to dead-letter regardless of error class —
# stops a clip that somehow never errors-out-permanently from lingering for ever.
_TTL_SECONDS = 7 * 24 * 3600
_CREATED_AT_FMT = "%Y-%m-%dT%H:%M:%S"


class PendingQueue:
    def __init__(self, directory: Optional[Path] = None):
        self.dir = Path(directory) if directory else (BASE_DIR / "pending")
        # Dead-letter holding pen for permanently-failing / expired items.
        self.dead_dir = self.dir / "dead"
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logging.error(f"Could not create pending queue dir {self.dir}: {e}")

    def enqueue(self, audio_data: bytes, duration_ms: int, language: Optional[str]) -> Optional[str]:
        """Persist a failed recording. Returns the item id, or None on failure."""
        item_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        wav_path = self.dir / f"{item_id}.wav"
        json_path = self.dir / f"{item_id}.json"
        tmp_json = self.dir / f"{item_id}.json.tmp"
        try:
            # Write audio first; the .json sidecar (written last) commits the item.
            with open(wav_path, 'wb') as f:
                f.write(audio_data)
            meta = {
                "created_at": time.strftime('%Y-%m-%dT%H:%M:%S'),
                "duration_ms": int(duration_ms),
                "language": language,
            }
            with open(tmp_json, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False)
                f.flush()
            tmp_json.replace(json_path)
            logging.info(f"Queued failed recording for resend: {item_id} "
                         f"({duration_ms} ms, lang={language})")
            self._enforce_cap()
            return item_id
        except Exception as e:
            logging.error(f"Failed to queue recording: {e}", exc_info=True)
            for p in (wav_path, tmp_json, json_path):  # best-effort cleanup
                try:
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass
            return None

    def list_pending(self) -> List[dict]:
        """Complete pending items (those with a .json sidecar), oldest first."""
        items: List[dict] = []
        try:
            for json_path in sorted(self.dir.glob("*.json")):
                item_id = json_path.stem
                wav_path = self.dir / f"{item_id}.wav"
                if not wav_path.exists():
                    continue
                try:
                    with open(json_path, 'r', encoding='utf-8') as f:
                        meta = json.load(f)
                except Exception:
                    continue  # half-written / corrupt sidecar -> skip for now
                items.append({
                    "id": item_id,
                    "wav": wav_path,
                    "duration_ms": meta.get("duration_ms", 0),
                    "language": meta.get("language"),
                    "created_at": meta.get("created_at", ""),
                })
        except Exception as e:
            logging.error(f"Failed to list pending queue: {e}")
        return items

    def read_audio(self, item: dict) -> bytes:
        with open(item["wav"], 'rb') as f:
            return f.read()

    def remove(self, item: dict):
        item_id = item.get("id")
        for ext in (".wav", ".json"):
            p = self.dir / f"{item_id}{ext}"
            try:
                if p.exists():
                    p.unlink()
            except Exception as e:
                logging.error(f"Failed to remove pending {p}: {e}")

    def dead_letter(self, item: dict, reason: str) -> bool:
        """Move a permanently-failing / expired item into ``dead/``.

        The audio + sidecar are preserved (not deleted) so the operator can
        inspect what kept failing; the sidecar gets ``dead_reason``/``dead_at``
        stamped in. Returns True if the item left the active queue.
        """
        item_id = item.get("id")
        if not item_id:
            return False
        try:
            self.dead_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logging.error(f"Could not create dead-letter dir {self.dead_dir}: {e}")
            return False

        src_wav = self.dir / f"{item_id}.wav"
        src_json = self.dir / f"{item_id}.json"
        dst_wav = self.dead_dir / f"{item_id}.wav"
        dst_json = self.dead_dir / f"{item_id}.json"

        # Stamp the reason into the sidecar before moving it.
        try:
            meta = {}
            if src_json.exists():
                with open(src_json, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
        except Exception:
            meta = {}
        meta["dead_reason"] = str(reason)[:300]
        meta["dead_at"] = time.strftime(_CREATED_AT_FMT)

        try:
            with open(dst_json, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False)
            if src_wav.exists():
                src_wav.replace(dst_wav)
            # Remove the original sidecar last (its disappearance "commits"
            # the move — list_pending no longer sees the item).
            if src_json.exists():
                src_json.unlink()
            logging.warning(
                f"Pending item dead-lettered: {item_id} ({reason})"
            )
            return True
        except Exception as e:
            logging.error(f"Failed to dead-letter {item_id}: {e}", exc_info=True)
            return False

    def _age_seconds(self, item: dict) -> Optional[float]:
        """Age of an item in seconds from its ``created_at`` (local time),
        falling back to the .wav mtime. None if neither is available."""
        created_at = item.get("created_at")
        if created_at:
            try:
                epoch = time.mktime(time.strptime(created_at, _CREATED_AT_FMT))
                return max(0.0, time.time() - epoch)
            except (ValueError, OverflowError):
                pass
        wav = item.get("wav")
        try:
            if wav and Path(wav).exists():
                return max(0.0, time.time() - Path(wav).stat().st_mtime)
        except Exception:
            pass
        return None

    def sweep_ttl(self, max_age_seconds: float = _TTL_SECONDS) -> List[dict]:
        """Dead-letter any item older than ``max_age_seconds``. Returns the
        list of items moved (so the caller can notify once)."""
        moved: List[dict] = []
        for item in self.list_pending():
            age = self._age_seconds(item)
            if age is not None and age > max_age_seconds:
                if self.dead_letter(item, f"expired (age {age / 86400:.1f}d > TTL)"):
                    moved.append(item)
        return moved

    def count(self) -> int:
        return len(self.list_pending())

    def _enforce_cap(self):
        items = self.list_pending()
        overflow = len(items) - _MAX_PENDING
        if overflow <= 0:
            return
        for item in items[:overflow]:  # drop the oldest
            logging.warning(f"Pending queue over cap; dropping oldest {item['id']}")
            self.remove(item)
