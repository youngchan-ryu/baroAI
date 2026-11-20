"""Local redaction only. Ready means processed, never a guarantee of privacy.

Cascade API: https://docs.opencv.org/4.12.0/d1/de5/classcv_1_1CascadeClassifier.html
No source retention or external calls; callers must obtain consent for each variant.
"""

import hashlib
import io
import math
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageOps

VERSION = "haar-solid-v1"
MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 25_000_000
MAX_EDGE = 4096
MAX_MASKS = 100


@dataclass(frozen=True)
class ProcessedImage:
    status: Literal["ready", "blocked"]
    data: bytes | None
    sha256: str | None
    version: str
    face_count: int
    error_code: str | None
    detector_version: str


def _detector_version() -> str:
    try:
        import cv2

        return f"opencv-{cv2.__version__}:frontalface_default+profileface-v1"
    except Exception:
        return "opencv-unavailable:frontalface_default+profileface-v1"


def _blocked(code: str) -> ProcessedImage:
    return ProcessedImage("blocked", None, None, VERSION, 0, code, _detector_version())


def _load_detectors():
    import cv2

    base = Path(cv2.data.haarcascades)
    detectors = [
        cv2.CascadeClassifier(str(base / name))
        for name in ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml")
    ]
    if any(detector.empty() for detector in detectors):
        raise RuntimeError("detector_unavailable")
    return detectors


def _face_boxes(image: Image.Image) -> list[tuple[int, int, int, int]]:
    import cv2
    import numpy as np

    frontal, profile = _load_detectors()
    gray = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
    boxes = []
    # Profile cascade has one orientation; inspect its horizontal mirror as well.
    for detector, pixels, mirrored in (
        (frontal, gray, False),
        (profile, gray, False),
        (profile, cv2.flip(gray, 1), True),
    ):
        detected = detector.detectMultiScale(
            pixels, scaleFactor=1.1, minNeighbors=3, minSize=(20, 20)
        )
        for raw in detected:
            x, y, width, height = map(int, raw)
            if width <= 0 or height <= 0:
                raise ValueError("invalid_detection")
            if mirrored:
                x = image.width - x - width
            boxes.append((x, y, width, height))
    # Exact duplicates can arise across cascades; overlapping boxes are all masked.
    return sorted(set(boxes))


def _manual_boxes(masks, size):
    if masks is None:
        return []
    if not isinstance(masks, list) or len(masks) > MAX_MASKS:
        raise ValueError("invalid_masks")
    boxes = []
    for mask in masks:
        if not isinstance(mask, dict) or set(mask) != {"x", "y", "width", "height"}:
            raise ValueError("invalid_masks")
        if any(
            type(value) not in {int, float} or not math.isfinite(value) for value in mask.values()
        ):
            raise ValueError("invalid_masks")
        x, y, width, height = (mask[key] for key in ("x", "y", "width", "height"))
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
            raise ValueError("invalid_masks")
        # Round outward so subpixel rectangles still cover their full requested extent.
        left, top = math.floor(x * size[0]), math.floor(y * size[1])
        right, bottom = math.ceil((x + width) * size[0]), math.ceil((y + height) * size[1])
        boxes.append((left, top, right, bottom))
    return boxes


def process_image(
    normalized_bytes: bytes, masks: list[dict] | None = None, *, detect_faces: bool = False
) -> ProcessedImage:
    """Produce metadata-free JPEG with opaque masks; any failure blocks transmission.

    Zero detected faces does not exclude faces, documents, badges or other secrets.
    face_count counts unique detection boxes, not independently verified people.
    """
    if not isinstance(normalized_bytes, bytes) or not normalized_bytes:
        return _blocked("invalid_image")
    if len(normalized_bytes) > MAX_BYTES:
        return _blocked("image_limits")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(normalized_bytes)) as source:
                if (
                    source.format not in {"JPEG", "PNG", "WEBP"}
                    or getattr(source, "n_frames", 1) != 1
                ):
                    return _blocked("invalid_image")
                if source.width * source.height > MAX_PIXELS or max(source.size) > MAX_EDGE:
                    return _blocked("image_limits")
                source.load()
                oriented = ImageOps.exif_transpose(source).convert("RGB")
                image = Image.new("RGB", oriented.size)
                image.paste(oriented)
    except Exception:  # Fail closed on decoder errors, with no raw exception or bytes exposed.
        return _blocked("invalid_image")
    try:
        manual = _manual_boxes(masks, image.size)
    except (ValueError, TypeError, OverflowError):
        return _blocked("invalid_masks")
    try:
        faces = _face_boxes(image) if detect_faces else []
        draw = ImageDraw.Draw(image)
        for x, y, width, height in faces:
            padding_x, padding_y = max(4, math.ceil(width * 0.25)), max(4, math.ceil(height * 0.25))
            left, top = max(0, x - padding_x), max(0, y - padding_y)
            right = min(image.width, x + width + padding_x)
            bottom = min(image.height, y + height + padding_y)
            if right <= left or bottom <= top:
                raise ValueError("invalid_detection")
            draw.rectangle((left, top, right - 1, bottom - 1), fill=(0, 0, 0))
        for left, top, right, bottom in manual:
            draw.rectangle((left, top, right - 1, bottom - 1), fill=(0, 0, 0))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=95, subsampling=0)
        data = output.getvalue()
        if len(data) > MAX_BYTES:
            return _blocked("image_limits")
        return ProcessedImage(
            "ready",
            data,
            hashlib.sha256(data).hexdigest(),
            VERSION,
            len(faces),
            None,
            _detector_version() if detect_faces else "manual-only",
        )
    except Exception:
        return _blocked("redaction_failed")
