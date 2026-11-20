import asyncio
import fcntl
import io
import os
import re
import stat
import warnings
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import HTTPException
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from app.services.images import MAX_EDGE

register_heif_opener(thumbnails=False, depth_images=False)

MAX_FILE = 10 * 1024 * 1024
MAX_PIXELS = 25_000_000
MARKER = ".baroai-storage.json"


def normalize_image(raw: bytes) -> bytes:
    if not raw or len(raw) > MAX_FILE:
        raise HTTPException(413, "upload_too_large")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in {"JPEG", "PNG", "WEBP", "HEIF"}:
                    raise ValueError
                if source.width * source.height > MAX_PIXELS or getattr(source, "n_frames", 1) != 1:
                    raise ValueError
                source.load()
                image = ImageOps.exif_transpose(source).convert("RGB")
                if source.format == "HEIF":
                    image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
                clean = Image.new("RGB", image.size)
                clean.paste(image)
                output = io.BytesIO()
                clean.save(output, format="JPEG", quality=90)
                return output.getvalue()
    except Exception:
        raise HTTPException(422, "invalid_image") from None


def exif_gps(raw: bytes):
    """Extract only bounded GPS coordinates; malformed metadata is ignored."""
    try:
        with Image.open(io.BytesIO(raw)) as image:
            gps = image.getexif().get_ifd(0x8825)
            def degrees(values):
                if len(values) != 3:
                    raise ValueError
                d, m, sec = (float(value) for value in values)
                if not (0 <= m < 60 and 0 <= sec < 60):
                    raise ValueError
                return d + m / 60 + sec / 3600
            lat, lon = degrees(gps[2]), degrees(gps[4])
            lat_ref, lon_ref = gps[1], gps[3]
            if lat_ref not in ("N", "S") or lon_ref not in ("E", "W"):
                return None
            lat *= -1 if lat_ref == "S" else 1
            lon *= -1 if lon_ref == "W" else 1
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                return None
            return lat, lon
    except Exception:
        return None


def open_directory(path: Path, *, create=False):
    """Walk from the filesystem root, refusing symlinks in every path component."""
    path = Path(os.path.abspath(path))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def verify_directory(path, fd):
    check = open_directory(path)
    try:
        a, b = os.fstat(fd), os.fstat(check)
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise OSError("storage directory changed")
    finally:
        os.close(check)


@contextmanager
def locked_directory(path, *, create=False):
    fd = open_directory(path, create=create)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        verify_directory(path, fd)
        yield fd
        verify_directory(path, fd)
    finally:
        os.close(fd)


@asynccontextmanager
async def async_locked_directory(path):
    fd = open_directory(path)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.01)
        verify_directory(path, fd)
        yield fd
        verify_directory(path, fd)
    finally:
        os.close(fd)


def marker_present(path):
    """Any marker entry, including a symlink, denotes an unavailable managed store."""
    with locked_directory(path) as fd:
        try:
            os.stat(MARKER, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return any(name.startswith(".baroai-init-") for name in os.listdir(fd))
        return True


class LocalPrivateStorage:
    """Legacy private storage. Provisioned stores are unavailable until integrated."""

    def __init__(self, root: Path):
        self.root = root
        self._written_directory = None

    def path(self, key: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}\.jpg", key):
            raise ValueError("Invalid object key")
        return self.root / key

    @contextmanager
    def _directory(self, *, create=False):
        with locked_directory(self.root, create=create) as fd:
            try:
                os.stat(MARKER, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if any(name.startswith(".baroai-init-") for name in os.listdir(fd)):
                    raise PermissionError("storage initialization is not yet complete") from None
            else:
                raise PermissionError("provisioned storage is not yet available")
            yield fd

    def ensure_legacy(self) -> None:
        with self._directory():
            pass

    def put(self, key: str, content: bytes) -> None:
        self.path(key)
        with self._directory(create=True) as fd:
            info = os.fstat(fd)
            self._written_directory = (info.st_dev, info.st_ino)
            out = os.open(
                key, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
            )
            created = os.fstat(out)
            try:
                try:
                    stream = os.fdopen(out, "wb")
                except BaseException:
                    os.close(out)
                    raise
                with stream:
                    stream.write(content)
            except BaseException as error:
                try:
                    verify_directory(self.root, fd)
                    current = os.stat(key, dir_fd=fd, follow_symlinks=False)
                    if (created.st_dev, created.st_ino) == (current.st_dev, current.st_ino):
                        os.unlink(key, dir_fd=fd)
                except FileNotFoundError:
                    pass
                except OSError as cleanup_error:
                    error.add_note(f"partial upload cleanup failed: {type(cleanup_error).__name__}")
                raise

    def read(self, key: str) -> bytes:
        self.path(key)
        with self._directory() as fd:
            source = os.open(key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(source, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError("storage object is not a regular file")
                return stream.read()

    def delete(self, key: str) -> None:
        self.path(key)
        with self._directory() as fd:
            info = os.fstat(fd)
            if self._written_directory != (info.st_dev, info.st_ino):
                raise OSError("storage directory changed or object was not written here")
            try:
                os.unlink(key, dir_fd=fd)
            except FileNotFoundError:
                pass
