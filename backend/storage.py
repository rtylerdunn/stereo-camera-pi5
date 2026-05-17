"""
Organised image storage for stereo captures.

Directory layout:
  <base_path>/YYYY-MM-DD/session_NNNN/
      YYYYMMDD_HHMMSS_left.jpg
      YYYYMMDD_HHMMSS_right.jpg
      YYYYMMDD_HHMMSS_anaglyph.jpg
      metadata.json
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

JPEG_QUALITY = 95


class StorageError(Exception):
    pass


class ImageStorage:
    def __init__(self, base_path: str):
        self.base = Path(base_path)
        self.base.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def save_capture(
        self,
        left_bgr: np.ndarray,
        right_bgr: np.ndarray,
        anaglyphs: dict,
        timestamp: Optional[datetime] = None,
    ) -> dict:
        """
        Save stereo images to a new session folder and return metadata.

        anaglyphs: mapping of kind→BGR array, e.g.
            {"anaglyph": dubois_arr, "halfcolor": hc_arr, "gray": gray_arr}
        """
        ts = timestamp or datetime.now()
        session_dir = self._new_session_dir(ts)
        prefix = ts.strftime("%Y%m%d_%H%M%S")

        to_save = {
            "left":  (session_dir / f"{prefix}_left.jpg",  left_bgr),
            "right": (session_dir / f"{prefix}_right.jpg", right_bgr),
        }
        for kind, arr in anaglyphs.items():
            to_save[kind] = (session_dir / f"{prefix}_{kind}.jpg", arr)

        for key, (fpath, arr) in to_save.items():
            ok = cv2.imwrite(str(fpath), arr, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if not ok:
                raise StorageError(f"Failed to write {fpath}")

        meta = {
            "timestamp": ts.isoformat(),
            "date": session_dir.parent.name,
            "session": session_dir.name,
            "files": {k: v[0].name for k, v in to_save.items()},
        }
        (session_dir / "metadata.json").write_text(json.dumps(meta, indent=2))

        logger.info("Saved capture → %s", session_dir)
        return {**meta, "session_dir": str(session_dir)}

    def list_captures(self) -> list:
        """Return all sessions sorted newest-first."""
        sessions = []
        for date_dir in sorted(self.base.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            for sess in sorted(date_dir.iterdir(), reverse=True):
                if not (sess.is_dir() and sess.name.startswith("session_")):
                    continue
                meta_path = sess / "metadata.json"
                if meta_path.exists():
                    try:
                        sessions.append(json.loads(meta_path.read_text()))
                    except (json.JSONDecodeError, OSError) as exc:
                        logger.warning("Skipping corrupt metadata %s: %s", meta_path, exc)
        return sessions

    def get_session(self, date: str, session: str) -> Optional[dict]:
        meta_path = self.base / date / session / "metadata.json"
        return json.loads(meta_path.read_text()) if meta_path.exists() else None

    def get_image_path(self, date: str, session: str, kind: str) -> Optional[Path]:
        """Return the Path to left/right/anaglyph for a session, or None."""
        sess_dir = self.base / date / session
        meta = self.get_session(date, session)
        if not meta:
            return None
        filename = meta["files"].get(kind)
        if not filename:
            return None
        path = sess_dir / filename
        return path if path.exists() else None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _new_session_dir(self, ts: datetime) -> Path:
        date_dir = self.base / ts.strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(date_dir.glob("session_*"))
        n = len(existing) + 1
        session_dir = date_dir / f"session_{n:04d}"
        session_dir.mkdir()
        return session_dir
