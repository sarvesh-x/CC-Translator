"""Extract text subtitle tracks and normalize them to UTF-8 SubRip."""
from __future__ import annotations

import subprocess
from pathlib import Path

from scanner import MediaToolError, SubtitleStream, require_media_tools


def extract_srt(video: Path, stream: SubtitleStream, destination: Path) -> Path:
    """Extract one text stream as SRT; video/audio streams are never copied or changed."""
    require_media_tools()
    if stream.is_bitmap:
        raise MediaToolError(f"Stream {stream.index} uses image codec {stream.codec}; OCR is required.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-map", f"0:{stream.index}",
           "-c:s", "srt", "-f", "srt", "-y", str(destination)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if result.returncode:
        destination.unlink(missing_ok=True)
        raise MediaToolError(result.stderr.strip() or f"FFmpeg could not extract stream {stream.index}.")
    return destination
