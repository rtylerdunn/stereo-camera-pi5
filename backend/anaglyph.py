"""
Anaglyph stereo image generation.

Supports five methods:
  color      — full-color (standard, vivid colours)
  halfcolor  — grey left eye, colour right eye (less retinal rivalry)
  gray       — both eyes greyscale (best depth perception)
  wimmer     — optimised luminance weighting (reduced ghosting)
  dubois     — Dubois perceptual least-squares (minimum eye strain)

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
    DUBOIS = "dubois"


def generate_anaglyph(
    left_bgr: np.ndarray,
    right_bgr: np.ndarray,
    method: AnaglyphMethod = AnaglyphMethod.DUBOIS,
) -> np.ndarray:
    """
    Generate a red/cyan anaglyph from a BGR stereo pair.

    Images are automatically resized to match if shapes differ.
    Tilt is corrected automatically via feature matching so epipolar
    lines are horizontal regardless of how the rig was held.
    Returns a uint8 BGR image.
    """
    if left_bgr.shape != right_bgr.shape:
        right_bgr = cv2.resize(right_bgr, (left_bgr.shape[1], left_bgr.shape[0]))

    left_bgr, right_bgr = _align_stereo(left_bgr, right_bgr)

    left_f = left_bgr.astype(np.float32) / 255.0
    right_f = right_bgr.astype(np.float32) / 255.0

    dispatch = {
        AnaglyphMethod.COLOR: _color,
        AnaglyphMethod.HALFCOLOR: _halfcolor,
        AnaglyphMethod.GRAY: _gray,
        AnaglyphMethod.WIMMER: _wimmer,
        AnaglyphMethod.DUBOIS: _dubois,
    }
    result_f = dispatch[method](left_f, right_f)

    return (np.clip(result_f, 0.0, 1.0) * 255.0).astype(np.uint8)



def generate_anaglyphs(
    left_bgr: np.ndarray,
    right_bgr: np.ndarray,
    methods: list = None,
) -> dict:
    """
    Generate multiple anaglyphs from one stereo pair with a single alignment pass.
    Returns {AnaglyphMethod: uint8_bgr_array, ...}.
    """
    if methods is None:
        methods = [AnaglyphMethod.DUBOIS, AnaglyphMethod.HALFCOLOR, AnaglyphMethod.GRAY]

    if left_bgr.shape != right_bgr.shape:
        right_bgr = cv2.resize(right_bgr, (left_bgr.shape[1], left_bgr.shape[0]))

    left_bgr, right_bgr = _align_stereo(left_bgr, right_bgr)
    left_f  = left_bgr.astype(np.float32) / 255.0
    right_f = right_bgr.astype(np.float32) / 255.0

    dispatch = {
        AnaglyphMethod.COLOR:      _color,
        AnaglyphMethod.HALFCOLOR:  _halfcolor,
        AnaglyphMethod.GRAY:       _gray,
        AnaglyphMethod.WIMMER:     _wimmer,
        AnaglyphMethod.DUBOIS:     _dubois,
    }
    return {
        m: (np.clip(dispatch[m](left_f, right_f), 0.0, 1.0) * 255.0).astype(np.uint8)
        for m in methods
    }


def reprocess_pair(
    left_path: str,
    right_path: str,
    method: AnaglyphMethod = AnaglyphMethod.DUBOIS,
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
# Stereo alignment: tilt correction + convergence adjustment
# ------------------------------------------------------------------

def _align_stereo(
    left_bgr: np.ndarray,
    right_bgr: np.ndarray,
) -> tuple:
    """
    Auto-align the stereo pair using feature correspondences:

    1. Tilt correction — rotates both images so epipolar lines are horizontal,
       compensating for the rig being hand-held at an angle.

    2. Convergence adjustment — shifts the right image horizontally so the
       disparity range is centred around zero. With parallel cameras the
       convergence point is at infinity, putting all depth on the "pop out"
       side. Centering splits depth evenly in front of and behind the screen
       plane, halving the maximum disparity the viewer has to fuse.

    Feature detection runs on a 640px-wide downsample for speed; the computed
    corrections are scaled back up and applied to the full-resolution images.
    """
    h, w = left_bgr.shape[:2]

    scale = min(1.0, 640.0 / w)
    small_l = cv2.resize(left_bgr, None, fx=scale, fy=scale) if scale < 1.0 else left_bgr
    small_r = cv2.resize(right_bgr, None, fx=scale, fy=scale) if scale < 1.0 else right_bgr

    gray_l = cv2.cvtColor(small_l, cv2.COLOR_BGR2GRAY)
    gray_r = cv2.cvtColor(small_r, cv2.COLOR_BGR2GRAY)

    detector = cv2.ORB_create(nfeatures=500)
    kp1, des1 = detector.detectAndCompute(gray_l, None)
    kp2, des2 = detector.detectAndCompute(gray_r, None)

    if des1 is None or des2 is None or len(kp1) < 10 or len(kp2) < 10:
        logger.warning("Stereo alignment: not enough features, skipping")
        return left_bgr, right_bgr

    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = matcher.match(des1, des2)
    if len(matches) < 10:
        logger.warning("Stereo alignment: not enough matches, skipping")
        return left_bgr, right_bgr

    matches = sorted(matches, key=lambda m: m.distance)[:200]

    pts1 = np.float32([kp1[m.queryIdx].pt for m in matches])
    pts2 = np.float32([kp2[m.trainIdx].pt for m in matches])

    dx = pts2[:, 0] - pts1[:, 0]
    dy = pts2[:, 1] - pts1[:, 1]

    # --- Tilt correction ---
    # Valid stereo matches for tilt: horizontal shift must dominate.
    tilt_valid = (np.abs(dx) > 5) & (np.abs(dy) < np.abs(dx))
    if tilt_valid.sum() >= 10:
        tilt_angle = float(np.median(np.arctan2(dy[tilt_valid], dx[tilt_valid])))
        tilt_deg = np.degrees(tilt_angle)
        if abs(tilt_deg) > 10.0:
            logger.warning("Tilt: %.1f° exceeds cap, skipping tilt correction", tilt_deg)
            tilt_deg = 0.0
        if abs(tilt_deg) >= 0.1:
            logger.info("Correcting tilt: %.2f degrees", tilt_deg)
            center = (w / 2.0, h / 2.0)
            M = cv2.getRotationMatrix2D(center, -tilt_deg, 1.0)
            left_bgr = cv2.warpAffine(left_bgr, M, (w, h), flags=cv2.INTER_LINEAR,
                                      borderMode=cv2.BORDER_REPLICATE)
            right_bgr = cv2.warpAffine(right_bgr, M, (w, h), flags=cv2.INTER_LINEAR,
                                       borderMode=cv2.BORDER_REPLICATE)

    # --- Convergence adjustment ---
    # Valid matches for convergence: small vertical offset confirms they are
    # genuine stereo pairs (not cross-matched repeated textures). Include
    # background matches even when dx ≈ 0 since we need the full depth range.
    conv_valid = np.abs(dy) < 5
    if conv_valid.sum() >= 10:
        # Use robust percentiles to find the foreground/background disparity
        # extremes, then shift so the midpoint of that range lands at zero.
        p10 = float(np.percentile(dx[conv_valid], 10))   # foreground (large disparity)
        p90 = float(np.percentile(dx[conv_valid], 90))   # background (small disparity)
        # Centre-split: balance foreground pop-out and background recession equally.
        # Optimised for subjects 2 m and beyond; gives natural depth without strain.
        convergence_small = -(p10 + p90) / 2.0
        convergence_px = convergence_small / scale        # scale to full resolution

        # Cap at 1/30 image width — don't overcorrect badly matched scenes
        max_shift = w / 30.0
        convergence_px = float(np.clip(convergence_px, -max_shift, max_shift))

        if abs(convergence_px) > 1.0:
            logger.info("Applying convergence shift: %.1f px", convergence_px)
            M_conv = np.float32([[1, 0, convergence_px], [0, 1, 0]])
            right_bgr = cv2.warpAffine(right_bgr, M_conv, (w, h), flags=cv2.INTER_LINEAR,
                                       borderMode=cv2.BORDER_REPLICATE)

    return left_bgr, right_bgr


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
    out[:, :, 2] = gray
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
    lum_left = 0.114 * left[:, :, 0] + 0.587 * left[:, :, 1] + 0.299 * left[:, :, 2]
    out = np.zeros_like(left)
    out[:, :, 2] = lum_left
    out[:, :, 1] = right[:, :, 1]
    out[:, :, 0] = right[:, :, 0]
    return out


def _dubois(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    # Gamma-expand sRGB to linear light before matrix multiply
    left_lin = np.power(left, 2.2)
    right_lin = np.power(right, 2.2)

    # Arrays are BGR; extract RGB channels for the Dubois matrix
    rl, gl, bl = left_lin[:, :, 2], left_lin[:, :, 1], left_lin[:, :, 0]
    rr, gr, br = right_lin[:, :, 2], right_lin[:, :, 1], right_lin[:, :, 0]

    # LCD Dubois matrix (Dubois 2001, LCD display / REEL3D #7003 glasses)
    r_out =  0.4154*rl + 0.4710*gl + 0.1669*bl - 0.0109*rr - 0.0364*gr - 0.0060*br
    g_out = -0.0458*rl - 0.0484*gl - 0.0257*bl + 0.3756*rr + 0.7333*gr + 0.0111*br
    b_out = -0.0547*rl - 0.0615*gl + 0.0128*bl - 0.0651*rr - 0.1287*gr + 1.2971*br

    out_lin = np.stack([b_out, g_out, r_out], axis=2)
    # Gamma-compress back to sRGB
    return np.power(np.clip(out_lin, 0.0, 1.0), 1.0 / 2.2)
