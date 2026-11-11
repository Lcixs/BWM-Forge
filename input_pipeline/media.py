from pathlib import Path
import io
import struct
import warnings

from PIL import Image, ImageOps


from config import (INPUT_SMALL_IMAGE_SHORT_SIDE as SMALL_IMAGE_SHORT_SIDE,
                    INPUT_MAX_IMAGE_BYTES, INPUT_IMAGE_JPEG_QUALITY,
                    INPUT_MAX_VIDEO_BYTES as MAX_VIDEO_BYTES)


def prepare_image(path: Path, max_side: int) -> tuple[bytes, dict]:
    path = Path(path)
    try:
        if path.stat().st_size > INPUT_MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds 12 MiB. Reduce its size before uploading.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as source:
                if source.format not in {"PNG", "JPEG", "WEBP"} or getattr(source, "n_frames", 1) != 1:
                    raise ValueError("Provide a single PNG, JPG, or WebP image.")
                original_size = source.size
                oriented = ImageOps.exif_transpose(source)
                oriented_size = oriented.size
                rgba = oriented.convert("RGBA")
                canvas = Image.new("RGBA", rgba.size, "white")
                canvas.alpha_composite(rgba)
                rgb = canvas.convert("RGB")
                rgb.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                rgb.save(buffer, "JPEG", quality=INPUT_IMAGE_JPEG_QUALITY)
                metadata = {"width": rgb.width, "height": rgb.height,
                            "original_size": list(original_size), "oriented_size": list(oriented_size),
                            "small_image": min(rgb.size) < SMALL_IMAGE_SHORT_SIDE,
                            "detail_note": ("Analyze small images at their original resolution, prioritizing the main outlines and basic geometry."
                                            if min(rgb.size) < SMALL_IMAGE_SHORT_SIDE else
                                            "Extract geometry from visible outlines and components."),
                            "processing": "EXIF orientation correction, white background for transparency, proportional downscaling, no cropping, JPEG encoding"}
                return buffer.getvalue(), metadata
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("Unable to read the image. The file may be missing, corrupt, or exceed the pixel limit.") from None


VIDEO_EXTENSIONS = {".mp4", ".mov"}


def inspect_video(path):
    path = Path(path)
    size = path.stat().st_size
    if path.suffix.lower() not in VIDEO_EXTENSIONS or not 0 < size <= MAX_VIDEO_BYTES:
        raise ValueError("Upload an MP4 or MOV demonstration video no larger than 512 MiB.")
    count, media_bytes, video_track, file_type = 0, 0, False, False
    with path.open("rb") as stream:
        def boxes(start, end, parents=()):
            nonlocal count, media_bytes, video_track, file_type
            position = start
            while position < end:
                count += 1
                if count > 50000 or end - position < 8:
                    raise ValueError("The video container is incomplete or too complex. Export it again as MP4.")
                stream.seek(position)
                length, kind = struct.unpack(">I4s", stream.read(8))
                header = 8
                if length == 1:
                    if end - position < 16:
                        raise ValueError("The video file is truncated.")
                    length = struct.unpack(">Q", stream.read(8))[0]
                    header = 16
                elif length == 0:
                    length = end - position
                if length < header or position + length > end:
                    raise ValueError("The video file is incomplete. Wait for the export to finish, then upload it again.")
                payload = position + header
                if not parents and kind == b"ftyp" and length >= header + 8:
                    file_type = True
                if not parents and kind == b"mdat":
                    media_bytes += length - header
                if kind == b"hdlr" and parents == (b"moov", b"trak", b"mdia") and length >= header + 12:
                    stream.seek(payload + 8)
                    video_track |= stream.read(4) == b"vide"
                if (not parents and kind == b"moov" or parents == (b"moov",) and kind == b"trak"
                        or parents == (b"moov", b"trak") and kind == b"mdia"):
                    boxes(payload, position + length, parents + (kind,))
                position += length
        boxes(0, size)
    # 旧版 QuickTime MOV 可能没有 ftyp。
    if (not file_type and path.suffix.lower() != ".mov") or not video_track or not media_bytes:
        raise ValueError("No complete video track and media data were found. Upload a valid MP4/MOV video; changing the extension is not sufficient.")
    return {"container": path.suffix.lower()[1:], "has_video_track": True,
            "inspection": "container_only", "content_analyzed": False}
