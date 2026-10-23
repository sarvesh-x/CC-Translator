"""MKV discovery and subtitle stream inspection via FFprobe."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class MediaToolError(RuntimeError):
    """Raised when FFmpeg/FFprobe is missing or reports a media error."""


@dataclass(frozen=True)
class SubtitleStream:
    """Useful metadata for one subtitle stream."""

    index: int
    language: str
    codec: str
    title: str
    default: bool
    forced: bool

    @property
    def is_bitmap(self) -> bool:
        return self.codec.lower() in {"hdmv_pgs_subtitle", "dvd_subtitle", "xsub"}


def require_media_tools() -> None:
    """Fail early with installation guidance if FFmpeg tools are unavailable."""
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise MediaToolError(f"Missing {', '.join(missing)}. Install FFmpeg and add its bin folder to PATH.")


def discover_mkv_files(folder: Path, recursive: bool = False) -> list[Path]:
    """Return one selected MKV or discover MKVs in a directory."""
    if folder.is_file():
        return [folder] if folder.suffix.lower() == ".mkv" else []
    if not folder.is_dir():
        return []
    iterator = folder.rglob("*") if recursive else folder.iterdir()
    return sorted((p for p in iterator if p.is_file() and p.suffix.lower() == ".mkv"), key=lambda p: str(p).casefold())


def probe_subtitles(video: Path) -> list[SubtitleStream]:
    """Inspect subtitle streams, retaining FFprobe's global stream indices."""
    require_media_tools()
    cmd = ["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
           "stream=index,codec_name:stream_tags=language,title:stream_disposition=default,forced",
           "-of", "json", str(video)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if result.returncode:
        raise MediaToolError(result.stderr.strip() or f"FFprobe failed for {video.name}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaToolError(f"FFprobe returned invalid JSON for {video.name}: {exc}") from exc
    streams = []
    for item in payload.get("streams", []):
        tags = item.get("tags", {})
        disp = item.get("disposition", {})
        streams.append(SubtitleStream(
            index=int(item["index"]), language=tags.get("language", "und"),
            codec=item.get("codec_name", "unknown"), title=tags.get("title", ""),
            default=bool(disp.get("default", 0)), forced=bool(disp.get("forced", 0))))
    return streams


def choose_stream(streams: list[SubtitleStream], source_language: str = "eng", selection: str = "Auto English") -> SubtitleStream | None:
    """Select a stream by the selected policy, preferring text English tracks."""
    if not streams:
        return None
    text_streams = [s for s in streams if not s.is_bitmap]
    candidates = text_streams or streams
    if selection == "First subtitle track":
        return candidates[0]
    if selection.startswith("Stream index "):
        try:
            wanted = int(selection.rsplit(" ", 1)[1])
            return next((s for s in streams if s.index == wanted), None)
        except (ValueError, IndexError):
            return None
    language_codes = {"english": {"en", "eng"}, "french": {"fr", "fre", "fra"},
                      "spanish": {"es", "spa"}, "german": {"de", "ger", "deu"},
                      "japanese": {"ja", "jpn"}, "chinese": {"zh", "chi", "zho"},
                      "portuguese": {"pt", "por"}, "italian": {"it", "ita"},
                      "russian": {"ru", "rus"}, "korean": {"ko", "kor"}}
    english_codes = language_codes.get(source_language.lower(), {source_language.lower()})
    source_label = source_language.lower()
    matches = [s for s in candidates if s.language.lower() in english_codes or
               any(s.language.lower().startswith(code + "-") for code in english_codes) or
               source_label in s.title.lower()]
    if selection.startswith("Source-language track "):
        try:
            wanted = int(selection.rsplit(" ", 1)[1]) - 1
            return matches[wanted] if wanted >= 0 and wanted < len(matches) else None
        except (ValueError, IndexError):
            return None
    if matches:
        return next((s for s in matches if s.default), matches[0])
    # Matroska files commonly omit the subtitle language tag. Use the explicitly
    # default text track only when its language is unknown; never override a known
    # different language with a guess.
    return next((s for s in candidates if s.default and s.language.lower() in {"", "und", "unknown"}), None)
