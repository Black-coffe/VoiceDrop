"""
Pending Queue - persists recordings whose transcription failed (offline / outage)
so they can be auto-resent later instead of being lost.

Each item = `<id>.wav` (audio) + `<id>.json` (metadata). The .json sidecar is the
commit marker: an item is only considered complete once its .json exists, so a
crash mid-write leaves an orphan .wav that is simply ignored.
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


class PendingQueue:
    def __init__(self, directory: Optional[Path] = None):
        self.dir = Path(directory) if directory else (BASE_DIR / "pending")
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
