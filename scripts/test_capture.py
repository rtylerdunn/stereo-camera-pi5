"""
Quick smoke test: initialize both cameras, capture a synchronized pair,
generate an anaglyph, and save all three to /tmp/stereo_test/.
Run from the backend/ directory:
  python3 ../scripts/test_capture.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from camera import StereoCamera
from anaglyph import generate_anaglyph, AnaglyphMethod
from config import config
import cv2

OUT = Path("/tmp/stereo_test")
OUT.mkdir(exist_ok=True)

print("Initialising cameras…")
cam = StereoCamera(config)
cam.initialize()

print("Warming up (2 s)…")
cam.start_streaming()
time.sleep(2)

print("Checking preview frames…")
left_prev, right_prev = cam.get_preview_frames()
assert left_prev is not None, "Left preview is None"
assert right_prev is not None, "Right preview is None"
print(f"  Left preview shape:  {left_prev.shape}")
print(f"  Right preview shape: {right_prev.shape}")

print("Capturing synchronized stills…")
left, right = cam.capture_synchronized()
print(f"  Left still shape:  {left.shape}")
print(f"  Right still shape: {right.shape}")

print("Generating anaglyph…")
ana = generate_anaglyph(left, right, AnaglyphMethod.COLOR)
print(f"  Anaglyph shape: {ana.shape}")

print("Saving images…")
cv2.imwrite(str(OUT / "left.jpg"),      left,  [cv2.IMWRITE_JPEG_QUALITY, 90])
cv2.imwrite(str(OUT / "right.jpg"),     right, [cv2.IMWRITE_JPEG_QUALITY, 90])
cv2.imwrite(str(OUT / "anaglyph.jpg"),  ana,   [cv2.IMWRITE_JPEG_QUALITY, 90])
print(f"  Saved to {OUT}/")

cam.stop()
print("\nSMOKE TEST PASSED")
