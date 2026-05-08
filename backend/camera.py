"""
Dual-camera management for the stereo camera system.

Manages two Picamera2 instances (left=index 0, right=index 1) with:
- Continuous low-resolution preview streams for the web UI
- Synchronized high-resolution still capture using a threading barrier
"""

import cv2
import logging
import threading
import time
from typing import Optional, Tuple

import numpy as np
from picamera2 import Picamera2

logger = logging.getLogger(__name__)


class CameraError(Exception):
    pass


class StereoCamera:
    def __init__(self, config: dict):
        self._cfg = config["cameras"]
        self._left_cam: Optional[Picamera2] = None
        self._right_cam: Optional[Picamera2] = None

        self._left_frame: Optional[np.ndarray] = None
        self._right_frame: Optional[np.ndarray] = None
        self._frame_lock = threading.Lock()

        self._streaming = False
        self._stream_thread: Optional[threading.Thread] = None
        self._initialized = False
        self._recovery_lock = threading.Lock()
        self._consecutive_errors = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Open both cameras and start them in video mode."""
        try:
            left_idx = self._cfg["left"]["index"]
            right_idx = self._cfg["right"]["index"]

            self._left_cam = Picamera2(left_idx)
            self._right_cam = Picamera2(right_idx)

            main_w = self._cfg["resolution"]["width"]
            main_h = self._cfg["resolution"]["height"]
            prev_w = self._cfg["preview_resolution"]["width"]
            prev_h = self._cfg["preview_resolution"]["height"]
            fps = self._cfg.get("framerate", 30)

            # Video configuration exposes a high-res main stream (for stills)
            # and a low-res lores stream (for preview) simultaneously.
            for cam in (self._left_cam, self._right_cam):
                cfg = cam.create_video_configuration(
                    main={"size": (main_w, main_h), "format": "RGB888"},
                    lores={"size": (prev_w, prev_h), "format": "YUV420"},
                    controls={"FrameRate": fps},
                )
                cam.configure(cfg)
                cam.start()

            # Let AGC/AWB settle
            time.sleep(2)

            self._initialized = True
            logger.info(
                "Cameras initialised: left=%d right=%d res=%dx%d",
                left_idx, right_idx, main_w, main_h,
            )
        except Exception as exc:
            raise CameraError(f"Camera initialisation failed: {exc}") from exc

    def start_streaming(self) -> None:
        """Spawn background thread that continuously grabs preview frames."""
        if not self._initialized:
            raise CameraError("Call initialize() before start_streaming()")
        self._streaming = True
        self._stream_thread = threading.Thread(
            target=self._preview_loop, daemon=True, name="preview-loop"
        )
        self._stream_thread.start()
        logger.info("Preview streaming started")

    def stop(self) -> None:
        """Stop streaming and release both cameras."""
        self._streaming = False
        if self._stream_thread:
            self._stream_thread.join(timeout=3)
        for cam in (self._left_cam, self._right_cam):
            if cam is not None:
                try:
                    cam.stop()
                except Exception:
                    pass
        self._initialized = False
        logger.info("Cameras stopped")

    def get_preview_frames(self) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        """Return the latest BGR preview frames (thread-safe copy)."""
        with self._frame_lock:
            left = self._left_frame.copy() if self._left_frame is not None else None
            right = self._right_frame.copy() if self._right_frame is not None else None
        return left, right

    def capture_synchronized(self) -> Tuple[np.ndarray, np.ndarray]:
        """
        Capture high-resolution BGR images from both cameras as simultaneously
        as possible using a threading barrier.

        Returns (left_bgr, right_bgr).
        """
        if not self._initialized:
            raise CameraError("Cameras not initialised")

        results: list = [None, None]
        errors: list = []
        barrier = threading.Barrier(2)

        def _capture(cam: Picamera2, slot: int) -> None:
            try:
                barrier.wait(timeout=5)        # sync both threads
                arr = cam.capture_array("main")  # RGB888
                results[slot] = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            except Exception as exc:
                errors.append(exc)

        t_left = threading.Thread(target=_capture, args=(self._left_cam, 0))
        t_right = threading.Thread(target=_capture, args=(self._right_cam, 1))
        t_left.start()
        t_right.start()
        t_left.join(timeout=15)
        t_right.join(timeout=15)

        if errors:
            raise CameraError(f"Capture failed: {errors[0]}")
        if results[0] is None or results[1] is None:
            raise CameraError("One or both cameras did not return a frame")

        logger.info("Synchronized capture complete")
        return results[0], results[1]

    @staticmethod
    def encode_jpeg(frame: np.ndarray, quality: int = 80) -> bytes:
        """Encode a BGR numpy array to JPEG bytes."""
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise CameraError("JPEG encoding failed")
        return buf.tobytes()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _preview_loop(self) -> None:
        """Background thread: grab lores frames and convert to BGR."""
        while self._streaming:
            try:
                left_yuv = self._left_cam.capture_array("lores")
                right_yuv = self._right_cam.capture_array("lores")

                left_bgr = cv2.cvtColor(left_yuv, cv2.COLOR_YUV420p2BGR)
                right_bgr = cv2.cvtColor(right_yuv, cv2.COLOR_YUV420p2BGR)

                with self._frame_lock:
                    self._left_frame = left_bgr
                    self._right_frame = right_bgr

                self._consecutive_errors = 0

            except Exception as exc:
                self._consecutive_errors += 1
                logger.error("Preview frame error (#%d): %s", self._consecutive_errors, exc)

                if self._consecutive_errors >= 5:
                    logger.warning("Too many consecutive errors — attempting camera recovery")
                    self._try_recover()

                time.sleep(0.5)

    def _try_recover(self) -> None:
        """Attempt to stop and reinitialize cameras after repeated failures."""
        if not self._recovery_lock.acquire(blocking=False):
            return  # another recovery already in progress
        try:
            self._consecutive_errors = 0

            for cam in (self._left_cam, self._right_cam):
                if cam is not None:
                    try:
                        cam.stop()
                    except Exception:
                        pass
            self._initialized = False

            time.sleep(3)

            self.initialize()
            logger.info("Camera recovery succeeded")
        except Exception as exc:
            logger.error("Camera recovery failed: %s", exc)
        finally:
            self._recovery_lock.release()
