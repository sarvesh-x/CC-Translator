"""SRT parsing, text translation boundaries, and safe UTF-8 output."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable

import srt

TAG_RE = re.compile(r"</?(?:i|b|u|s|font|c|ruby|rt|v|lang)(?:\s+[^>]*)?>", re.IGNORECASE)


def read_subtitles(path: Path) -> list[srt.Subtitle]:
    """Parse a SubRip file with BOM and common Unicode decoding support."""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1252"):
        try:
            content = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        content = raw.decode("utf-8", errors="replace")
    return list(srt.parse(content, ignore_errors=False))


def protect_formatting(text: str) -> tuple[str, list[str]]:
    """Replace inline formatting tags with stable tokens for translation."""
    tags: list[str] = []
    def replace(match: re.Match[str]) -> str:
        token = f"⟦FMT{len(tags)}⟧"
        tags.append(match.group(0))
        return token
    return TAG_RE.sub(replace, text), tags


def restore_formatting(text: str, tags: list[str]) -> str:
    """Restore formatting tags after translation."""
    for i, tag in enumerate(tags):
        token = f"⟦FMT{i}⟧"
        if text.count(token) != 1:
            raise ValueError(f"Translation did not preserve formatting token {token} exactly once.")
        text = text.replace(token, tag)
    return text


def atomic_write(path: Path, content: str, overwrite: bool = True) -> None:
    """Write text atomically so interruption cannot leave a truncated SRT/cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(name, path)
        else:
            # Same-directory hard-link creation fails atomically if the destination appeared.
            os.link(name, path)
            Path(name).unlink(missing_ok=True)
    except Exception:
        Path(name).unlink(missing_ok=True)
        raise


def write_srt(path: Path, subtitles: Iterable[srt.Subtitle], overwrite: bool = True) -> None:
    """Write valid UTF-8 SRT while preserving original timestamps and sequence."""
    atomic_write(path, srt.compose(list(subtitles), reindex=False), overwrite=overwrite)


def load_cache(path: Path) -> dict[str, str]:
    """Load the resumable per-entry translation cache."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(path: Path, cache: dict[str, str]) -> None:
    """Persist cached translations atomically after each successful batch."""
    atomic_write(path, json.dumps(cache, ensure_ascii=False, indent=2))
