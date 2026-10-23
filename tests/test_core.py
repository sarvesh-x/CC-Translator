"""Offline unit tests for subtitle integrity and recoverable processing."""
from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import srt

from processor import JobController, process_folder
from scanner import SubtitleStream, choose_stream, discover_mkv_files
from subtitle_extractor import extract_srt
from subtitle_writer import (load_cache, protect_formatting, read_subtitles, restore_formatting,
                             save_cache, write_srt)
from translator import DeepSeekEngine, TranslationError, LibreTranslateEngine, OllamaQwenEngine, _validate, get_engine


class SubtitleTests(unittest.TestCase):
    def test_multiline_unicode_formatting_and_timing_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Hindi movie.srt"
            original = srt.Subtitle(7, timedelta(seconds=1), timedelta(seconds=3),
                                    "<i>नमस्ते</i>\nदुनिया")
            write_srt(path, [original])
            parsed = read_subtitles(path)
            self.assertEqual(parsed[0].index, 7)
            self.assertEqual(parsed[0].start, original.start)
            self.assertEqual(parsed[0].end, original.end)
            self.assertEqual(parsed[0].content, original.content)

    def test_formatting_mask_restore(self):
        masked, tags = protect_formatting("<i>Hello</i>\n<b>world</b>")
        self.assertEqual(restore_formatting(masked, tags), "<i>Hello</i>\n<b>world</b>")
        with self.assertRaisesRegex(ValueError, "formatting token"):
            restore_formatting("नमस्ते", tags)

    def test_empty_srt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.srt"
            path.write_text("", encoding="utf-8")
            self.assertEqual(read_subtitles(path), [])

    def test_invalid_provider_response_is_not_accepted(self):
        with self.assertRaises(TranslationError):
            _validate(["Hello", "Bye"], ["नमस्ते"])
        with self.assertRaises(TranslationError):
            _validate(["Hello"], [""])


class ScannerTests(unittest.TestCase):
    def test_language_selection_prefers_default_text_stream(self):
        streams = [SubtitleStream(2, "eng", "subrip", "", False, False),
                   SubtitleStream(4, "eng", "ass", "Full", True, False)]
        self.assertEqual(choose_stream(streams).index, 4)
        self.assertEqual(choose_stream(streams, selection="Stream index 2").index, 2)

    def test_auto_uses_default_text_track_when_language_tag_is_unknown(self):
        stream = SubtitleStream(3, "und", "subrip", "", True, False)
        self.assertEqual(choose_stream([stream], "English").index, 3)
        titled = SubtitleStream(4, "und", "ass", "English SDH", False, False)
        self.assertEqual(choose_stream([titled], "English").index, 4)

    def test_auto_does_not_guess_when_default_is_tagged_as_another_language(self):
        stream = SubtitleStream(3, "jpn", "subrip", "", True, False)
        self.assertIsNone(choose_stream([stream], "English"))

    def test_bitmap_detection(self):
        stream = SubtitleStream(5, "eng", "hdmv_pgs_subtitle", "", True, False)
        self.assertTrue(stream.is_bitmap)
        self.assertEqual(choose_stream([stream]).index, 5)

    def test_windows_style_filename_with_spaces_is_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            movie = Path(tmp) / "My Movie 01.mkv"
            movie.touch()
            self.assertEqual(discover_mkv_files(Path(tmp)), [movie])

    def test_ffmpeg_extraction_uses_explicit_stream_mapping_and_path_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "Movie with spaces.mkv"
            target = Path(tmp) / "subtitles.srt"
            stream = SubtitleStream(6, "eng", "ass", "", True, False)
            completed = Mock(returncode=0, stderr="")
            with patch("subtitle_extractor.require_media_tools"), \
                 patch("subtitle_extractor.subprocess.run", return_value=completed) as run:
                extract_srt(video, stream, target)
            command = run.call_args.args[0]
            self.assertIn("0:6", command)
            self.assertEqual(command[-1], str(target))
            self.assertIn(str(video), command)

    def test_bitmap_extraction_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            stream = SubtitleStream(6, "eng", "hdmv_pgs_subtitle", "", True, False)
            with patch("subtitle_extractor.require_media_tools"), \
                 patch("subtitle_extractor.subprocess.run") as run:
                with self.assertRaisesRegex(Exception, "OCR is required"):
                    extract_srt(Path(tmp) / "film.mkv", stream, Path(tmp) / "out.srt")
            run.assert_not_called()


class TranslationRetryTests(unittest.TestCase):
    def test_qwen_ollama_builds_concise_contextual_local_request(self):
        engine = OllamaQwenEngine()
        response = Mock()
        response.json.return_value = {"message": {"content": '{"translations":["तुम आ रहे हो?"]}'}}
        with patch.object(engine.session, "post", return_value=response) as post:
            self.assertEqual(engine.translate_batch(
                ["Are you coming?"], context=[{"previous": "We're waiting.", "next": "Let's go."}]),
                ["तुम आ रहे हो?"])
        args, kwargs = post.call_args
        self.assertTrue(args[0].endswith("/api/chat"))
        self.assertFalse(kwargs["json"]["stream"])
        self.assertFalse(kwargs["json"]["think"])
        self.assertEqual(kwargs["json"]["format"], "json")
        self.assertIn("We're waiting.", kwargs["json"]["messages"][1]["content"])
        self.assertIn("natural, concise spoken Hindi", kwargs["json"]["messages"][0]["content"])

    def test_qwen_requires_model_to_be_installed_in_ollama(self):
        engine = OllamaQwenEngine()
        response = Mock()
        response.json.return_value = {"models": [{"name": "other:latest"}]}
        with patch.object(engine.session, "get", return_value=response):
            with self.assertRaisesRegex(TranslationError, "ollama pull qwen3:4b"):
                engine.verify_support("English", "Hindi")

    def test_qwen_selected_by_engine_factory(self):
        self.assertIsInstance(get_engine("Qwen3 4B (Ollama)"), OllamaQwenEngine)

    def test_http_rate_limit_retries_then_succeeds(self):
        engine = DeepSeekEngine()
        first = Mock(status_code=429, headers={"Retry-After": "0"})
        second = Mock(status_code=200, headers={})
        second.raise_for_status.return_value = None
        with patch.object(engine.session, "post", side_effect=[first, second]) as post, \
             patch("translator.time.sleep"):
            response = engine._post_retry("https://example.invalid", {}, {})
        self.assertIs(response, second)
        self.assertEqual(post.call_count, 2)

    def test_payment_required_fails_immediately_with_offline_option(self):
        engine = DeepSeekEngine()
        payment_required = Mock(status_code=402, headers={}, text="Payment Required")
        with patch.object(engine.session, "post", return_value=payment_required) as post:
            with self.assertRaisesRegex(TranslationError, r"install Ollama"):
                engine._post_retry("https://example.invalid", {}, {})
        self.assertEqual(post.call_count, 1)

    def test_provider_api_error_raises(self):
        engine = DeepSeekEngine()
        with patch.object(engine, "_post_retry", side_effect=TranslationError("provider down")):
            with self.assertRaisesRegex(TranslationError, "provider down"):
                engine.translate_batch(["Hello"])

    def test_deepseek_prompt_includes_neighbor_context_and_concision_rules(self):
        engine = DeepSeekEngine()
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": '{"translations":["मैं जा रहा हूँ।"]}'}}]}
        with patch.object(engine, "_post_retry", return_value=response) as post:
            self.assertEqual(engine.translate_batch(
                ["I'm leaving."], context=[{"previous": "Wait!", "next": "Come back."}]), ["मैं जा रहा हूँ।"])
        payload = post.call_args.args[1]
        prompt = payload["messages"][1]["content"]
        self.assertIn("shortest natural phrasing", prompt)
        self.assertIn("Wait!", prompt)
        self.assertIn("Come back.", prompt)
        self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_libretranslate_rate_limit_retries(self):
        engine = LibreTranslateEngine()
        engine.source_code = "en"
        engine.target_code = "hi"
        limited = Mock(status_code=429, headers={"Retry-After": "0"})
        success = Mock(status_code=200, headers={})
        success.raise_for_status.return_value = None
        success.json.return_value = {"translatedText": "नमस्ते"}
        with patch.object(engine.session, "post", side_effect=[limited, success]), \
             patch("translator.time.sleep"):
            self.assertEqual(engine.translate_batch(["Hello"]), ["नमस्ते"])


class RecoveryTests(unittest.TestCase):
    def test_progress_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "resume.progress.json"
            cache = {"0:Hello": "नमस्ते"}
            save_cache(cache_path, cache)
            self.assertEqual(load_cache(cache_path), cache)

    def test_existing_output_is_skipped_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            video = folder / "film with spaces.mkv"
            video.touch()
            output = folder / "film with spaces.hi.srt"
            output.write_text("keep", encoding="utf-8")
            emit = Mock()
            logger = logging.getLogger("test")
            with patch("processor.get_engine") as get_engine:
                get_engine.return_value.verify_support.return_value = None
                results = process_folder(folder, False, None, False, "English", "Auto English",
                                         "DeepSeek API", JobController(), emit, logger)
            self.assertEqual(results[0].status, "skipped")
            self.assertEqual(output.read_text(encoding="utf-8"), "keep")

    def test_missing_subtitle_tracks_are_reported_as_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "silent.mkv").touch()
            emit = Mock()
            with patch("processor.get_engine") as get_engine, patch("processor.probe_subtitles", return_value=[]):
                engine = Mock()
                get_engine.return_value = engine
                results = process_folder(folder, False, None, False, "English", "Auto English",
                                         "DeepSeek API", JobController(), emit, logging.getLogger("test"))
            self.assertEqual(results[0].status, "skipped")
            engine.verify_support.assert_not_called()

    def test_partial_progress_is_reused_after_interruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            video = folder / "resume movie.mkv"
            video.touch()
            output = folder / "out"
            source_entries = [srt.Subtitle(1, timedelta(seconds=1), timedelta(seconds=2), "Hello"),
                              srt.Subtitle(2, timedelta(seconds=3), timedelta(seconds=4), "Goodbye")]

            def fake_extract(_video, _stream, destination):
                destination.write_text(srt.compose(source_entries), encoding="utf-8")
                return destination

            controller = JobController()
            first_engine = Mock()
            def first_translation(texts, context=None):
                controller.cancelled.set()
                return ["नमस्ते"]
            first_engine.translate_batch.side_effect = first_translation
            first_engine.verify_support.return_value = None
            with patch("processor.probe_subtitles", return_value=[SubtitleStream(2, "eng", "subrip", "", True, False)]), \
                 patch("processor.extract_srt", side_effect=fake_extract), \
                 patch("processor.get_engine", return_value=first_engine), \
                 patch("processor.SETTINGS", SimpleNamespace(batch_size=1)):
                with self.assertRaises(InterruptedError):
                    process_folder(folder, False, output, False, "English", "Auto English", "DeepSeek API",
                                   controller, Mock(), logging.getLogger("test"))
            first_engine.translate_batch.assert_called_once()
            cache_path = output / "resume movie.hi.srt.progress.json"
            self.assertTrue(cache_path.exists())

            second_engine = Mock()
            second_engine.verify_support.return_value = None
            second_engine.translate_batch.return_value = ["अलविदा"]
            with patch("processor.probe_subtitles", return_value=[SubtitleStream(2, "eng", "subrip", "", True, False)]), \
                 patch("processor.extract_srt", side_effect=fake_extract), \
                 patch("processor.get_engine", return_value=second_engine), \
                 patch("processor.SETTINGS", SimpleNamespace(batch_size=1)):
                results = process_folder(folder, False, output, False, "English", "Auto English", "DeepSeek API",
                                         JobController(), Mock(), logging.getLogger("test"))
            self.assertEqual(results[0].status, "success")
            self.assertEqual(second_engine.translate_batch.call_count, 1)
            final = read_subtitles(output / "resume movie.hi.srt")
            self.assertEqual([item.content for item in final], ["नमस्ते", "अलविदा"])


if __name__ == "__main__":
    unittest.main()
