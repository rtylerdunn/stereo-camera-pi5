"""
Anaglyph stereo image generation.

Supports four methods:
  color      — full-color (standard, vivid colours)
  halfcolor  — grey left eye, colour right eye (less retinal rivalry)
  gray       — both eyes greyscale (best depth perception)
  wimmer     — optimised luminance weighting (reduced ghosting)

Left image → red channel.
Right image → cyan channels (green + blue).
All input/output arrays are BGR (OpenCV convention).
"""

import cv2
import logging
from enum import Enum
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class AnaglyphMethod(Enum):
    COLOR = "color"
    HALFCOLOR = "halfcolor"
    GRAY = "gray"
    WIMMER = "wimmer"


def generate_anaglyph(
    left_bgr: np.ndarray,
    right_bgr: np.ndarray,
    method: AnaglyphMethod = AnaglyphMethod.COLOR,
) -> np.ndarray:
    """
    Generate a red/cyan anaglyph from a BGR stereo pair.

    Images are automatically resized to match if shapes differ.
    Returns a uint8 BGR image.
    """
    if left_bgr.shape != right_bgr.shape:
        right_bgr = cv2.resize(right_bgr, (left_bgr.shape[1], left_bgr.shape[0]))

    left_f = left_bgr.astype(np.float32) / 255.0
    right_f = right_bgr.astype(np.float32) / 255.0

    dispatch = {
        AnaglyphMethod.COLOR: _color,
        AnaglyphMethod.HALFCOLOR: _halfcolor,
        AnaglyphMethod.GRAY: _gray,
        AnaglyphMethod.WIMMER: _wimmer,
    }
    result_f = dispatch[method](left_f, right_f)

    return (np.clip(result_f, 0.0, 1.0) * 255.0).astype(np.uint8)


def reprocess_pair(
    left_path: str,
    right_path: str,
    method: AnaglyphMethod = AnaglyphMethod.COLOR,
) -> np.ndarray:
    """Load an existing image pair from disk and regenerate the anaglyph."""
    left = cv2.imread(str(left_path))
    right = cv2.imread(str(right_path))
    if left is None:
        raise FileNotFoundError(f"Cannot read left image: {left_path}")
    if right is None:
        raise FileNotFoundError(f"Cannot read right image: {right_path}")
    return generate_anaglyph(left, right, method)


# ------------------------------------------------------------------
# Private compositing functions (all inputs/outputs float32 BGR 0-1)
# ------------------------------------------------------------------

def _color(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    out = np.zeros_like(left)
    out[:, :, 2] = left[:, :, 2]    # R ← left red
    out[:, :, 1] = right[:, :, 1]   # G ← right green
    out[:, :, 0] = right[:, :, 0]   # B ← right blue
    return out


def _halfcolor(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    left_u8 = (left * 255).astype(np.uint8)
    gray = cv2.cvtColor(left_u8, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    out = np.zeros_like(left)
    out[:, :, 2] = gray              # R ← left luminance
    out[:, :, 1] = right[:, :, 1]
    out[:, :, 0] = right[:, :, 0]
    return out


def _gray(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    def lum(img):
        u8 = (img * 255).astype(np.uint8)
        return cv2.cvtColor(u8, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0

    out = np.zeros_like(left)
    out[:, :, 2] = lum(left)
    out[:, :, 1] = lum(right)
    out[:, :, 0] = lum(right)
    return out


def _wimmer(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    # ITU-R BT.601 luminance (BGR order)
    lum_left = 0.114 * left[:, :, 0] + 0.587 * left[:, :, 1] + 0.299 * left[:, :, 2]
    out = np.zeros_like(left)
    out[:, :, 2] = lum_left          # R ← weighted luminance
    out[:, :, 1] = right[:, :, 1]
    out[:, :, 0] = right[:, :, 0]
    return out
