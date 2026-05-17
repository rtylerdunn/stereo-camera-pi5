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
    Alignment is corrected automatically via AKAZE feature matching and
    RANSAC affine estimation so epipolar lines are horizontal and scale
    differences are compensated.
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
# Stereo alignment: full affine correction + convergence + crop
# ------------------------------------------------------------------

def _compute_valid_crop(
    h: int,
    w: int,
    angle_deg: float,
    scale_corr: float,
    ty_full: float,
    conv_px: float,
) -> tuple:
    """
    Return (x0, y0, x1, y1) — the valid intersection crop box after all
    right-image transforms have been applied. Values rounded to even pixels
    to avoid JPEG chroma subsampling seams.
    """
    theta = abs(np.radians(angle_deg))
    rot_x = int(np.ceil(h * np.sin(theta) / 2.0))
    rot_y = int(np.ceil(w * np.sin(theta) / 2.0))
    sc_x  = int(np.ceil(max(0.0, scale_corr - 1.0) * w / 2.0))
    sc_y  = int(np.ceil(max(0.0, scale_corr - 1.0) * h / 2.0))
    ty_t  = int(np.ceil(max(0.0,  ty_full)))
    ty_b  = int(np.ceil(max(0.0, -ty_full)))
    cv_l  = int(np.ceil(max(0.0,  conv_px)))
    cv_r  = int(np.ceil(max(0.0, -conv_px)))

    x0 = rot_x + sc_x + cv_l
    x1 = w - rot_x - sc_x - cv_r
    y0 = rot_y + sc_y + ty_t
    y1 = h - rot_y - sc_y - ty_b

    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)

    if x0 % 2: x0 += 1
    if x1 % 2: x1 -= 1
    if y0 % 2: y0 += 1
    if y1 % 2: y1 -= 1

    return x0, y0, x1, y1


def _align_stereo(
    left_bgr: np.ndarray,
    right_bgr: np.ndarray,
) -> tuple:
    """
    Auto-align the stereo pair using AKAZE features and RANSAC affine estimation.

    Pipeline (mirrors StereoPhoto Maker's approach):
    1. AKAZE feature detection + Lowe ratio test for quality matches
    2. RANSAC estimateAffinePartial2D → rotation, scale, tx, ty
    3. Apply rotation + scale + vertical shift to RIGHT image only (left is reference)
    4. Compute remaining horizontal disparity from RANSAC inliers, apply centre-split
    5. Crop both images to the valid overlap rectangle (eliminates warp border artifacts)

    Returns images that may be slightly smaller than the inputs (cropped to valid overlap).
    Falls back to returning the originals at any stage if alignment cannot be determined.
    """
    h, w = left_bgr.shape[:2]

    WORK_W = 640
    scale  = w / WORK_W
    work_h = int(round(h / scale))

    small_l = cv2.resize(left_bgr,  (WORK_W, work_h), interpolation=cv2.INTER_AREA)
    small_r = cv2.resize(right_bgr, (WORK_W, work_h), interpolation=cv2.INTER_AREA)
    gray_l  = cv2.cvtColor(small_l, cv2.COLOR_BGR2GRAY)
    gray_r  = cv2.cvtColor(small_r, cv2.COLOR_BGR2GRAY)

    # --- Step 1: AKAZE + Lowe ratio test ---
    # AKAZE is scale+rotation invariant with binary descriptors (no contrib needed).
    akaze = cv2.AKAZE_create()
    kp1, des1 = akaze.detectAndCompute(gray_l, None)
    kp2, des2 = akaze.detectAndCompute(gray_r, None)

    if des1 is None or des2 is None or len(kp1) < 20 or len(kp2) < 20:
        logger.warning("Stereo alignment: insufficient features (L=%d R=%d), skipping",
                       len(kp1) if kp1 else 0, len(kp2) if kp2 else 0)
        return left_bgr, right_bgr

    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    raw_matches = bf.knnMatch(des1, des2, k=2)
    good = []
    for pair in raw_matches:
        if len(pair) == 2:
            m, n = pair
            if m.distance < 0.75 * n.distance:
                good.append(m)

    if len(good) < 15:
        logger.warning("Stereo alignment: only %d good matches after ratio test, skipping", len(good))
        return left_bgr, right_bgr

    pts1 = np.float32([kp1[m.queryIdx].pt for m in good])
    pts2 = np.float32([kp2[m.trainIdx].pt for m in good])

    # --- Step 2: RANSAC affine estimation (right keypoints → left keypoints) ---
    M, inlier_mask = cv2.estimateAffinePartial2D(
        pts2, pts1,
        method=cv2.RANSAC,
        ransacReprojThreshold=3.0,
        maxIters=2000,
        confidence=0.99,
    )

    if M is None:
        logger.warning("Stereo alignment: RANSAC affine estimation failed, skipping")
        return left_bgr, right_bgr

    n_inliers = int(inlier_mask.sum()) if inlier_mask is not None else 0
    if n_inliers < 8:
        logger.warning("Stereo alignment: only %d RANSAC inliers, skipping", n_inliers)
        return left_bgr, right_bgr

    # Decompose: M = [[a, -b, tx], [b, a, ty]]
    a, b    = float(M[0, 0]), float(M[1, 0])
    s_raw   = float(np.sqrt(a**2 + b**2))
    ang_raw = float(np.degrees(np.arctan2(b, a)))
    ty_raw  = float(M[1, 2])

    # Sanity caps — prevent wild corrections if RANSAC finds a bad consensus
    ANG_CAP   = 10.0
    SCALE_CAP = 0.05
    angle      = float(np.clip(ang_raw,  -ANG_CAP,           ANG_CAP))
    scale_corr = float(np.clip(s_raw, 1.0 - SCALE_CAP, 1.0 + SCALE_CAP))

    logger.info(
        "Stereo alignment: RANSAC → angle=%.3f° (raw %.3f°) scale=%.4f (raw %.4f) "
        "tx=%.1f ty=%.1f px  inliers=%d/%d",
        angle, ang_raw, scale_corr, s_raw, float(M[0, 2]), ty_raw, n_inliers, len(good),
    )

    # --- Step 3: Build full-resolution alignment matrix ---
    # Corrects rotation + scale + vertical shift only.
    # Horizontal (convergence) is deferred to Step 4 so it can be capped independently.
    # Left image is the fixed reference; only right image is warped.
    angle_rad = np.radians(angle)
    c = scale_corr * np.cos(angle_rad)
    s = scale_corr * np.sin(angle_rad)
    cx_f = w / 2.0
    cy_f = h / 2.0
    ty_full = ty_raw * scale

    # Rotation+scale about the full-res image centre, plus RANSAC vertical shift
    M_align = np.array([
        [c, -s,  cx_f * (1.0 - c) + cy_f * s],
        [s,  c,  cy_f * (1.0 - c) - cx_f * s + ty_full],
    ], dtype=np.float64)

    # --- Step 4: Convergence from RANSAC inliers ---
    # Project right inlier keypoints through the working-res version of M_align,
    # measure residual horizontal disparity, and apply centre-split.
    cx_sm = WORK_W / 2.0
    cy_sm = work_h / 2.0
    M_align_sm = np.array([
        [c, -s,  cx_sm * (1.0 - c) + cy_sm * s],
        [s,  c,  cy_sm * (1.0 - c) - cx_sm * s + ty_raw],
    ], dtype=np.float64)

    inlier_idx = np.where(inlier_mask.ravel() == 1)[0]
    pts1_in    = pts1[inlier_idx].astype(np.float64)
    pts2_in    = pts2[inlier_idx].astype(np.float64)

    ones     = np.ones((len(pts2_in), 1), dtype=np.float64)
    pts2_hom = np.hstack([pts2_in, ones])
    pts2_new = (M_align_sm @ pts2_hom.T).T

    dx = pts2_new[:, 0] - pts1_in[:, 0]
    p10 = float(np.percentile(dx, 10))
    p90 = float(np.percentile(dx, 90))

    # Centre-split: balance foreground pop-out and background recession.
    # Cap at 2.5% of working width (SPM recommends 2-3% of image width).
    CONV_CAP      = WORK_W / 40.0
    conv_px_small = float(np.clip(-(p10 + p90) / 2.0, -CONV_CAP, CONV_CAP))
    conv_px_full  = conv_px_small * scale

    logger.info(
        "Stereo alignment: convergence p10=%.1f p90=%.1f → shift %.1f px (small) / %.1f px (full)",
        p10, p90, conv_px_small, conv_px_full,
    )

    # Build the final warp matrix: alignment + convergence in a single pass
    M_full = M_align.copy()
    M_full[0, 2] += conv_px_full

    right_out = cv2.warpAffine(
        right_bgr, M_full, (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )

    # --- Step 5: Intelligent crop to valid overlap rectangle ---
    x0, y0, x1, y1 = _compute_valid_crop(h, w, angle, scale_corr, ty_full, conv_px_full)

    if (x1 - x0) < 320 or (y1 - y0) < 320:
        logger.warning(
            "Stereo alignment: crop box too small (%dx%d), returning aligned-uncropped",
            x1 - x0, y1 - y0,
        )
        return left_bgr, right_out

    logger.info(
        "Stereo alignment: crop (%d,%d)→(%d,%d) = %dx%d (was %dx%d, %.1f%% area)",
        x0, y0, x1, y1, x1 - x0, y1 - y0, w, h,
        100.0 * (x1 - x0) * (y1 - y0) / (w * h),
    )
    return left_bgr[y0:y1, x0:x1], right_out[y0:y1, x0:x1]


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
