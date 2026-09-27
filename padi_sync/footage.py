"""Estimate water current from dive footage.

Idea: suspended particles drift with the water, the reef does not. The camera moves too,
so measure both in the same frames and subtract:
  camera motion  = median optical flow over textured bottom (reef, sand)
  particle drift = median flow of small bright specks in open water
  current signal = particle drift - camera motion  (image px per second)
Without knowing particle distances this cannot give m/s, so it is mapped to PADI's
None / Light / Medium / Strong with conservative thresholds and reported with a confidence.
A human answer always overrides it.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

W = 960  # analysis width
FPS = 4  # frames sampled per second
# Median relative drift, in analysis pixels per second, at the category boundaries.
# Calibrated on one labelled reference so far (a reef dive the diver logged as "slight" current):
# median 15.4 px/s (IQR 11 to 22); two other dives the same day read 14.8 and 13.2. Camera-motion parallax
# inflates the reading even in still water, so the estimate is a suggestion the diver confirms.
THRESHOLDS = [(8.0, "None"), (35.0, "Light"), (70.0, "Medium")]


@dataclass
class CurrentEstimate:
    category: str | None
    drift_px_s: float | None
    samples: int
    confidence: str  # "low" or "medium"; never "high" for a footage estimate
    note: str


def _frames(path: Path, start: float, seconds: float):
    p = subprocess.Popen(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-ss",
            str(start),
            "-t",
            str(seconds),
            "-i",
            str(path),
            "-vf",
            f"fps={FPS},scale={W}:-2",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ],
        stdout=subprocess.PIPE,
    )
    probe = (
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
        )
        .stdout.split()[0]
        .split(",")
    )
    w0, h0 = int(probe[0]), int(probe[1])
    h = int(round(h0 * W / w0 / 2) * 2)
    size = W * h
    while True:
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        yield np.frombuffer(buf, np.uint8).reshape(h, W)
    p.wait()


def texture_mask(g: np.ndarray) -> np.ndarray:
    """True where the image is textured (reef, sand, divers); open water is smooth.
    A median blur first removes the small specks, which are what we want to measure in the water."""
    g = cv2.medianBlur(g, 7).astype(np.float32)
    mu = cv2.blur(g, (15, 15))
    sd = np.sqrt(np.maximum(cv2.blur(g * g, (15, 15)) - mu * mu, 0))
    return sd > 8


def pair_drift(a: np.ndarray, b: np.ndarray) -> float | None:
    tex = texture_mask(a)
    if tex.mean() < 0.1 or tex.mean() > 0.9:
        return None  # need both bottom and open water in frame
    flow = cv2.calcOpticalFlowFarneback(a, b, None, 0.5, 3, 21, 3, 5, 1.1, 0)
    cam = np.median(flow[tex], axis=0)
    # Particles: small bright specks in open water.
    water = ~cv2.dilate(tex.astype(np.uint8), np.ones((25, 25), np.uint8)).astype(bool)
    tophat = cv2.morphologyEx(a, cv2.MORPH_TOPHAT, np.ones((7, 7), np.uint8))
    specks = water & (tophat > 12)
    if specks.sum() < 30:
        return None
    rel = flow[specks] - cam
    return float(np.linalg.norm(np.median(rel, axis=0))) * FPS


def classify(drift: float) -> str:
    return next((name for lim, name in THRESHOLDS if drift < lim), "Strong")


def estimate_current(clips: list[Path], seconds_per_clip: float = 20) -> CurrentEstimate:
    drifts = []
    for clip in clips:
        prev = None
        for g in _frames(clip, 2, seconds_per_clip):
            if prev is not None:
                d = pair_drift(prev, g)
                if d is not None:
                    drifts.append(d)
            prev = g
    if len(drifts) < 20:
        return CurrentEstimate(None, None, len(drifts), "low", "not enough frames showing both bottom and open water")
    m = float(np.median(drifts))
    conf = "medium" if len(drifts) >= 100 else "low"
    return CurrentEstimate(
        classify(m),
        round(m, 1),
        len(drifts),
        conf,
        "relative drift of suspended particles vs the bottom; no absolute speed",
    )
