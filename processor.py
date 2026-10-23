"""Sequential batch job orchestration and resumable per-entry translation."""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import srt

from config import SETTINGS
from scanner import MediaToolError, SubtitleStream, choose_stream, discover_mkv_files, probe_subtitles
from subtitle_extractor import extract_srt
from subtitle_writer import (load_cache, protect_formatting, read_subtitles, restore_formatting,
                             save_cache, write_srt)
from translator import TranslationEngine, TranslationError, get_engine

EventCallback = Callable[[str, dict], None]


@dataclass
class FileResult:
    """Outcome for one video."""

    file: str
    status: str
    detail: str = ""


class JobController:
    """Pause, resume, and cancel controls safe checkpoints between requests."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.paused = threading.Event()
        self.paused.clear()

    def checkpoint(self) -> None:
        while self.paused.is_set() and not self.cancelled.is_set():
            self.cancelled.wait(0.2)
        if self.cancelled.is_set():
            raise InterruptedError("Cancelled by user")


def _safe_name(path: Path) -> str:
    return path.name + "." + str(path.stat().st_size) + "." + str(path.stat().st_mtime_ns)


def process_folder(folder: Path, recursive: bool, output_dir: Path | None,
                   overwrite: bool, source_language: str, selection: str,
                   engine_name: str, controller: JobController, emit: EventCallback,
                   logger: logging.Logger) -> list[FileResult]:
    """Translate each discovered MKV sequentially; failures do not stop later files."""
    files = discover_mkv_files(folder, recursive)
    report_dir = output_dir or (folder.parent if folder.is_file() else folder)
    if not files:
        emit("log", {"message": "No MKV files found."})
        return []
    engine: TranslationEngine | None = None
    engine_verified = False
    results: list[FileResult] = []
    total = len(files)
    for file_no, video in enumerate(files, 1):
        try:
            controller.checkpoint()
        except InterruptedError:
            _write_report(report_dir, results)
            raise
        emit("file", {"index": file_no, "total": total, "name": video.name})
        target = (output_dir / f"{video.stem}.hi.srt") if output_dir else video.with_name(f"{video.stem}.hi.srt")
        cache_path = target.with_suffix(target.suffix + ".progress.json")
        if target.exists() and not overwrite:
            results.append(FileResult(str(video), "skipped", f"Output exists: {target}"))
            emit("log", {"message": f"Skipped {video.name}: Hindi subtitle already exists."})
            continue
        work_dir = target.parent / ".mkv-hindi-translator-tmp"
        work_dir.mkdir(parents=True, exist_ok=True)
        extracted = work_dir / f"{video.stem}.{file_no}.source.srt"
        try:
            streams = probe_subtitles(video)
            emit("streams", {"file": video.name, "streams": [asdict(x) | {"bitmap": x.is_bitmap} for x in streams]})
            if not streams:
                results.append(FileResult(str(video), "skipped", "No subtitle streams."))
                emit("log", {"message": f"Skipped {video.name}: no subtitle tracks."})
                continue
            stream = choose_stream(streams, source_language, selection)
            if stream is None:
                detail = "No stream matched selection. Check the source language tag or choose a stream index."
                results.append(FileResult(str(video), "skipped", detail))
                inventory = "; ".join(f"#{s.index} lang={s.language} codec={s.codec} default={s.default}"
                                       for s in streams)
                emit("log", {"message": f"Skipped {video.name}: {detail} Streams: {inventory}"})
                continue
            if stream.is_bitmap:
                detail = f"Selected stream {stream.index} is image-based ({stream.codec}); export with OCR in Subtitle Edit."
                results.append(FileResult(str(video), "skipped", detail))
                emit("log", {"message": f"{video.name}: {detail}"})
                continue
            emit("log", {"message": f"Extracting stream {stream.index} ({stream.language}, {stream.codec}) from {video.name}."})
            extract_srt(video, stream, extracted)
            subtitles = read_subtitles(extracted)
            if not subtitles:
                results.append(FileResult(str(video), "skipped", "Selected subtitle track is empty."))
                emit("log", {"message": f"Skipped {video.name}: selected track contains no subtitle entries."})
                continue
            if engine is None:
                engine = get_engine(engine_name, logger)
            if not engine_verified:
                engine.verify_support(source_language, "Hindi")
                engine_verified = True
            cache = load_cache(cache_path)
            translated_by_index: dict[int, str] = {}
            pending: list[tuple[int, str, list[str]]] = []
            for idx, sub in enumerate(subtitles):
                key = f"{idx}:{sub.content}"
                if key in cache:
                    translated_by_index[idx] = cache[key]
                elif not sub.content.strip():
                    translated_by_index[idx] = sub.content
                else:
                    masked, tags = protect_formatting(sub.content)
                    pending.append((idx, masked, tags))
            done = len(translated_by_index)
            emit("progress", {"current": done, "total": len(subtitles), "file": video.name})
            batch_size = max(1, min(SETTINGS.batch_size, 30))
            for offset in range(0, len(pending), batch_size):
                controller.checkpoint()
                batch = pending[offset:offset + batch_size]
                texts = [item[1] for item in batch]
                contexts = [_neighbor_context(subtitles, item[0]) for item in batch]
                try:
                    translations = engine.translate_batch(texts, context=contexts)
                except TranslationError as batch_error:
                    # Fall back to one-by-one so a bad request cannot lose its neighbors.
                    logger.warning("Batch failed; retrying entries individually: %s", batch_error)
                    emit("log", {"message": f"Batch translation failed; retrying {len(batch)} entries individually."})
                    for (idx, text, tags), context in zip(batch, contexts):
                        controller.checkpoint()
                        translated = engine.translate_batch([text], context=[context])[0]
                        restored = restore_formatting(translated, tags)
                        translated_by_index[idx] = restored
                        cache[f"{idx}:{subtitles[idx].content}"] = restored
                        save_cache(cache_path, cache)
                        done += 1
                        emit("progress", {"current": done, "total": len(subtitles), "file": video.name})
                    continue
                if len(translations) != len(batch):
                    raise TranslationError("Translation provider returned a mismatched batch size.")
                for (idx, _masked, tags), translated in zip(batch, translations):
                    restored = restore_formatting(translated, tags)
                    translated_by_index[idx] = restored
                    cache[f"{idx}:{subtitles[idx].content}"] = restored
                save_cache(cache_path, cache)
                done += len(batch)
                emit("progress", {"current": done, "total": len(subtitles), "file": video.name})
            controller.checkpoint()
            output = [srt.Subtitle(sub.index, sub.start, sub.end, translated_by_index[i], sub.proprietary)
                      for i, sub in enumerate(subtitles)]
            write_srt(target, output, overwrite=overwrite)
            cache_path.unlink(missing_ok=True)
            results.append(FileResult(str(video), "success", str(target)))
            emit("log", {"message": f"Saved {target}"})
        except InterruptedError:
            _write_report(report_dir, results)
            raise
        except Exception as exc:
            logger.exception("Failed processing %s", video)
            results.append(FileResult(str(video), "failed", str(exc)))
            emit("log", {"message": f"Failed {video.name}: {exc}"})
        finally:
            extracted.unlink(missing_ok=True)
            try:
                work_dir.rmdir()
            except OSError:
                pass
    _write_report(report_dir, results)
    return results


def extract_folder(folder: Path, recursive: bool, output_dir: Path | None,
                   overwrite: bool, source_language: str, selection: str,
                   controller: JobController, emit: EventCallback,
                   logger: logging.Logger) -> list[FileResult]:
    """Export the selected embedded text subtitle from each MKV as a language-tagged SRT."""
    files = discover_mkv_files(folder, recursive)
    report_dir = output_dir or (folder.parent if folder.is_file() else folder)
    if not files:
        emit("log", {"message": "No MKV files found."})
        return []
    results: list[FileResult] = []
    total = len(files)
    for file_no, video in enumerate(files, 1):
        try:
            controller.checkpoint()
        except InterruptedError:
            _write_report(report_dir, results)
            raise
        emit("file", {"index": file_no, "total": total, "name": video.name})
        try:
            streams = probe_subtitles(video)
            emit("streams", {"file": video.name,
                              "streams": [asdict(x) | {"bitmap": x.is_bitmap} for x in streams]})
            if not streams:
                results.append(FileResult(str(video), "skipped", "No subtitle streams."))
                emit("log", {"message": f"Skipped {video.name}: no subtitle tracks."})
                continue
            stream = choose_stream(streams, source_language, selection)
            if stream is None:
                detail = "No stream matched selection. Check the source language tag or choose a stream index."
                results.append(FileResult(str(video), "skipped", detail))
                emit("log", {"message": f"Skipped {video.name}: {detail}"})
                continue
            if stream.is_bitmap:
                detail = f"Selected stream {stream.index} is image-based ({stream.codec}); OCR is required."
                results.append(FileResult(str(video), "skipped", detail))
                emit("log", {"message": f"Skipped {video.name}: {detail}"})
                continue
            language = stream.language.strip().lower()
            if language in {"", "unknown"}:
                language = "und"
            # Keep filename suffixes safe even for nonstandard MKV language tags.
            language = "".join(char for char in language if char.isalnum() or char in "-_") or "und"
            target = ((output_dir / f"{video.stem}.{language}.srt") if output_dir
                      else video.with_name(f"{video.stem}.{language}.srt"))
            if target.exists() and not overwrite:
                results.append(FileResult(str(video), "skipped", f"Output exists: {target}"))
                emit("log", {"message": f"Skipped {video.name}: output already exists at {target}."})
                continue
            work_dir = target.parent / ".mkv-hindi-translator-tmp"
            work_dir.mkdir(parents=True, exist_ok=True)
            temporary = work_dir / f"{video.stem}.{file_no}.{stream.index}.extracted.srt"
            try:
                controller.checkpoint()
                emit("log", {"message": f"Extracting stream {stream.index} ({stream.language}, {stream.codec}) from {video.name}."})
                extract_srt(video, stream, temporary)
                if not read_subtitles(temporary):
                    results.append(FileResult(str(video), "skipped", "Selected subtitle track is empty."))
                    emit("log", {"message": f"Skipped {video.name}: selected track contains no subtitle entries."})
                    continue
                controller.checkpoint()
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary.replace(target)
                results.append(FileResult(str(video), "success", str(target)))
                emit("log", {"message": f"Saved extracted subtitle: {target}"})
                emit("progress", {"current": 1, "total": 1, "file": video.name})
            finally:
                temporary.unlink(missing_ok=True)
                try:
                    work_dir.rmdir()
                except OSError:
                    pass
        except InterruptedError:
            _write_report(report_dir, results)
            raise
        except Exception as exc:
            logger.exception("Failed extracting subtitle from %s", video)
            results.append(FileResult(str(video), "failed", str(exc)))
            emit("log", {"message": f"Failed {video.name}: {exc}"})
    _write_report(report_dir, results)
    return results


def _write_report(directory: Path, results: list[FileResult]) -> None:
    """Write the current report snapshot, including when a job is cancelled."""
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / "mkv_hindi_translator_report.json"
    report.write_text(json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2), encoding="utf-8")


def _neighbor_context(subtitles: list[srt.Subtitle], index: int) -> dict[str, str]:
    """Return only the adjacent source dialogue as translation context."""
    def dialogue(position: int) -> str:
        if position < 0 or position >= len(subtitles):
            return ""
        return subtitles[position].content.strip()
    return {"previous": dialogue(index - 1), "next": dialogue(index + 1)}
