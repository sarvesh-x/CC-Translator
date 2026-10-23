"""Substitutable translation engines with capability checks and bounded retries."""
from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Callable

import requests

from config import LEGACY_DEEPSEEK_MODEL, SETTINGS

Progress = Callable[[str], None]


class TranslationError(RuntimeError):
    """A provider could not produce a complete valid translation."""


class TranslationEngine(ABC):
    """Common provider interface."""

    @abstractmethod
    def verify_support(self, source: str, target: str) -> None:
        """Raise a clear error unless this provider supports the selected pair."""

    @abstractmethod
    def translate_batch(self, texts: list[str], context: list[dict[str, str]] | None = None) -> list[str]:
        """Translate texts one-for-one, preserving list order."""


class DeepSeekEngine(TranslationEngine):
    """Context-aware DeepSeek Chat Completions translation backend."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("mkv_hindi_translator")
        self.session = requests.Session()
        self.source_language = "English"

    def verify_support(self, source: str, target: str) -> None:
        if target != "Hindi":
            raise TranslationError("This DeepSeek configuration supports Hindi as the target language.")
        self.source_language = source
        if LEGACY_DEEPSEEK_MODEL:
            self.logger.warning("DEEPSEEK_MODEL used a discontinued legacy name; using deepseek-flash. Update .env to match.")
        if not SETTINGS.deepseek_api_key:
            raise TranslationError("DEEPSEEK_API_KEY is not set. Add it to .env or choose another engine.")
        samples = {"English": "Hello", "French": "Bonjour", "Spanish": "Hola", "German": "Hallo",
                   "Japanese": "こんにちは", "Chinese": "你好", "Portuguese": "Olá", "Italian": "Ciao",
                   "Russian": "Привет", "Korean": "안녕하세요"}
        if source not in samples:
            raise TranslationError(f"No capability check is configured for {source} to Hindi.")
        # DeepSeek does not publish a source/target language-pair registry. A short
        # provider request verifies the configured key/model and Hindi output before a batch.
        sample = self.translate_batch([samples[source]])[0]
        if not any("\u0900" <= char <= "\u097f" for char in sample):
            raise TranslationError("The configured DeepSeek model did not return Devanagari Hindi in its capability check.")

    def translate_batch(self, texts: list[str], context: list[dict[str, str]] | None = None) -> list[str]:
        if not texts:
            return []
        rows = []
        for i, text in enumerate(texts):
            nearby = context[i] if context and i < len(context) else {}
            rows.append({"id": i, "previous": nearby.get("previous", ""),
                         "current": text, "next": nearby.get("next", "")})
        prompt = (
            f"Localize the CURRENT dialogue in each record from {self.source_language} into natural spoken Hindi in Devanagari. "
            "Use previous/next only to understand context; do not translate or return them. "
            "Be concise and direct like professional on-screen subtitles: use the shortest natural phrasing that preserves the full meaning, "
            "emotion, and conversational tone. Avoid word-for-word phrasing, unnecessary honorifics, filler, explanations, and added meaning. "
            "Translate idioms by meaning. Keep character names and established terms unchanged. "
            "Preserve every ⟦FMTn⟧ token exactly once in its original position; do not translate or remove it. "
            "Keep meaningful line breaks where practical. Return only a JSON object with a 'translations' array of strings, "
            "one for each record in the same order. Input JSON: " + json.dumps(rows, ensure_ascii=False))
        payload = {"model": SETTINGS.deepseek_model,
                   "messages": [{"role": "system", "content": "You are a professional English-to-Hindi subtitle translator."},
                               {"role": "user", "content": prompt}],
                   "temperature": 0.2, "thinking": {"type": "disabled"},
                   "response_format": {"type": "json_object"}}
        url = SETTINGS.deepseek_base_url.rstrip("/") + "/chat/completions"
        response = self._post_retry(url, payload, {"Authorization": f"Bearer {SETTINGS.deepseek_api_key}"})
        try:
            content = response.json()["choices"][0]["message"]["content"]
            data = json.loads(content)
            translated = data["translations"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise TranslationError(f"DeepSeek returned a malformed translation response: {exc}") from exc
        return _validate(texts, translated)

    def _post_retry(self, url: str, payload: dict, headers: dict) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(SETTINGS.request_retries + 1):
            try:
                response = self.session.post(url, json=payload, headers=headers, timeout=SETTINGS.request_timeout)
            except requests.RequestException as exc:
                last_error = exc
                if attempt < SETTINGS.request_retries:
                    time.sleep(min(30, 2 ** attempt))
                    continue
                break
            if response.status_code == 402:
                raise TranslationError(
                    "DeepSeek API returned HTTP 402 Payment Required. The API account needs billing or credits. "
                    "For natural translations without API billing, install Ollama and run `ollama pull qwen3:4b`; "
                    "Argos (offline) is another local option."
                )
            if response.status_code in (401, 403):
                detail = response.text[:400].strip()
                raise TranslationError(f"DeepSeek API authorization failed (HTTP {response.status_code}). Check the API key and account access. {detail}")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < SETTINGS.request_retries:
                    try:
                        delay = float(response.headers.get("Retry-After", 2 ** attempt))
                    except (TypeError, ValueError):
                        delay = 2 ** attempt
                    time.sleep(max(0.0, min(30.0, delay)))
                    continue
                last_error = requests.HTTPError(f"HTTP {response.status_code}: {response.text[:400]}")
                break
            if not response.ok:
                raise TranslationError(f"DeepSeek API returned HTTP {response.status_code}: {response.text[:400]}")
            return response
        raise TranslationError(f"DeepSeek request failed after retries: {last_error or 'HTTP error'}")


class LibreTranslateEngine(TranslationEngine):
    """Configurable LibreTranslate instance, checked against its language list."""

    def __init__(self) -> None:
        self.session = requests.Session()

    def verify_support(self, source: str, target: str) -> None:
        try:
            response = self.session.get(SETTINGS.libretranslate_url.rstrip("/") + "/languages",
                                        timeout=SETTINGS.request_timeout)
            response.raise_for_status()
            languages = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise TranslationError(f"Cannot check LibreTranslate languages at {SETTINGS.libretranslate_url}: {exc}") from exc
        codes = {item.get("code") for item in languages if isinstance(item, dict)}
        source_code = LANGUAGE_CODES.get(source)
        if not source_code or source_code not in codes or "hi" not in codes:
            raise TranslationError(f"This LibreTranslate server does not advertise {source} and Hindi.")
        self.source_code = source_code
        self.target_code = "hi"

    def translate_batch(self, texts: list[str], context: list[dict[str, str]] | None = None) -> list[str]:
        # LibreTranslate endpoint translates one text per request; the worker batches work
        # by limiting the request group and retains per-entry recovery semantics.
        results = []
        for text in texts:
            data = {"q": text, "source": self.source_code, "target": self.target_code, "format": "text"}
            if SETTINGS.libretranslate_api_key:
                data["api_key"] = SETTINGS.libretranslate_api_key
            try:
                response = _post_with_retry(self.session, SETTINGS.libretranslate_url.rstrip("/") + "/translate", data)
                translated = response.json()["translatedText"]
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                raise TranslationError(f"LibreTranslate request failed: {exc}") from exc
            if not isinstance(translated, str) or not translated.strip():
                raise TranslationError("LibreTranslate returned an empty translation.")
            results.append(translated)
        return results


class ArgosEngine(TranslationEngine):
    """CPU-compatible local Argos Translate backend; it never downloads models itself."""

    def __init__(self) -> None:
        try:
            import argostranslate.translate as argos_translate
        except ImportError as exc:
            raise TranslationError("Argos Translate is not installed. Install it and the English-to-Hindi model; see README.") from exc
        self.argos_translate = argos_translate
        self.translation = None

    def verify_support(self, source: str, target: str) -> None:
        source_code = LANGUAGE_CODES.get(source)
        if not source_code or target != "Hindi":
            raise TranslationError("Argos backend supports configured source languages translated to Hindi.")
        try:
            self.translation = self.argos_translate.get_translation_from_codes(source_code, "hi")
        except Exception as exc:
            raise TranslationError("Argos English-to-Hindi model is missing. Install it via Optional offline setup in README.") from exc
        if self.translation is None:
            raise TranslationError("Argos English-to-Hindi model is missing. Install it via Optional offline setup in README.")

    def translate_batch(self, texts: list[str], context: list[dict[str, str]] | None = None) -> list[str]:
        if self.translation is None:
            self.verify_support("English", "Hindi")
        results = [self.translation.translate(text) for text in texts]
        return _validate(texts, results)


class OpusHindiEngine(TranslationEngine):
    """Local CPU English-to-Hindi MarianMT model from Helsinki-NLP (Apache-2.0)."""

    model_id = "Helsinki-NLP/opus-mt-en-hi"

    def __init__(self) -> None:
        self.torch = None
        self.model = None
        self.tokenizer = None

    def verify_support(self, source: str, target: str) -> None:
        if (source, target) != ("English", "Hindi"):
            raise TranslationError("OPUS-MT is configured for English-to-Hindi translation.")
        try:
            import torch
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        except ImportError as exc:
            raise TranslationError(
                "OPUS-MT dependencies are not installed. Follow the CPU setup steps in README.md "
                "(install requirements-local.txt)."
            ) from exc
        try:
            self.torch = torch
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_id)
            self.model = AutoModelForSeq2SeqLM.from_pretrained(self.model_id).to("cpu")
            self.model.eval()
        except Exception as exc:
            raise TranslationError(
                f"Could not load {self.model_id}. Check the internet connection and available disk space "
                f"for its first download: {exc}"
            ) from exc

    def translate_batch(self, texts: list[str], context: list[dict[str, str]] | None = None) -> list[str]:
        if not texts:
            return []
        if self.model is None or self.tokenizer is None or self.torch is None:
            self.verify_support("English", "Hindi")
        try:
            inputs = self.tokenizer(texts, return_tensors="pt", padding=True,
                                    truncation=True, max_length=256)
            with self.torch.inference_mode():
                tokens = self.model.generate(**inputs, num_beams=4, max_new_tokens=128,
                                             early_stopping=True)
            outputs = self.tokenizer.batch_decode(tokens, skip_special_tokens=True,
                                                  clean_up_tokenization_spaces=True)
        except Exception as exc:
            raise TranslationError(f"OPUS-MT CPU translation failed: {exc}") from exc
        return _validate(texts, outputs)


class OllamaQwenEngine(TranslationEngine):
    """Natural, context-aware Hindi subtitle translation using a local Ollama model."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.source_language = "English"

    def verify_support(self, source: str, target: str) -> None:
        if (source, target) != ("English", "Hindi"):
            raise TranslationError("Qwen3 via Ollama is configured for English-to-Hindi translation.")
        self.source_language = source
        try:
            response = self.session.get(SETTINGS.ollama_base_url.rstrip("/") + "/api/tags",
                                       timeout=SETTINGS.request_timeout)
            response.raise_for_status()
            models = response.json().get("models", [])
        except (requests.RequestException, ValueError, AttributeError) as exc:
            raise TranslationError(
                "Cannot reach Ollama locally. Install and start Ollama for Windows, then run "
                f"`ollama pull {SETTINGS.ollama_model}`. Details: {exc}"
            ) from exc
        names = {item.get("name", "") for item in models if isinstance(item, dict)}
        if SETTINGS.ollama_model not in names:
            raise TranslationError(
                f"Ollama model {SETTINGS.ollama_model!r} is not installed. Open a terminal and run "
                f"`ollama pull {SETTINGS.ollama_model}`, then retry."
            )

    def translate_batch(self, texts: list[str], context: list[dict[str, str]] | None = None) -> list[str]:
        if not texts:
            return []
        rows = []
        for i, text in enumerate(texts):
            nearby = context[i] if context and i < len(context) else {}
            rows.append({"id": i, "previous": nearby.get("previous", ""),
                         "current": text, "next": nearby.get("next", "")})
        system = (
            "You are a professional Hindi subtitle localizer. Translate English dialogue into natural, "
            "concise spoken Hindi in Devanagari. Sound like real conversation, not a dictionary or formal "
            "written Hindi. Preserve the full intent and emotion in as few words as natural. Use surrounding "
            "lines only for context. Keep names unchanged. Do not add explanations, filler, or details. "
            "Return only a JSON object with a translations array containing one string per current line, "
            "in the original order. Preserve any ⟦FMTn⟧ tokens exactly once."
        )
        payload = {
            "model": SETTINGS.ollama_model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": json.dumps(rows, ensure_ascii=False)}],
            "format": "json", "stream": False, "think": False,
            "options": {"temperature": 0.2, "num_predict": max(512, len(texts) * 160)},
        }
        try:
            response = self.session.post(SETTINGS.ollama_base_url.rstrip("/") + "/api/chat",
                                         json=payload, timeout=SETTINGS.ollama_timeout)
            response.raise_for_status()
            content = response.json()["message"]["content"]
            translated = json.loads(content)["translations"]
        except requests.RequestException as exc:
            raise TranslationError(f"Ollama local translation request failed: {exc}") from exc
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise TranslationError(f"Ollama returned a malformed translation response: {exc}") from exc
        return _validate(texts, translated)


def get_engine(name: str, logger: logging.Logger | None = None) -> TranslationEngine:
    """Construct the selected provider."""
    if name == "DeepSeek API":
        return DeepSeekEngine(logger)
    if name == "LibreTranslate":
        return LibreTranslateEngine()
    if name == "Argos (offline)":
        return ArgosEngine()
    if name == "OPUS-MT (offline)":
        return OpusHindiEngine()
    if name == "Qwen3 4B (Ollama)":
        return OllamaQwenEngine()
    raise TranslationError(f"Unknown translation engine: {name}")


LANGUAGE_CODES = {"English": "en", "French": "fr", "Spanish": "es", "German": "de",
                  "Japanese": "ja", "Chinese": "zh", "Portuguese": "pt", "Italian": "it",
                  "Russian": "ru", "Korean": "ko"}


def _validate(source: list[str], translated: object) -> list[str]:
    if not isinstance(translated, list) or len(translated) != len(source):
        raise TranslationError("Provider returned the wrong number of subtitle translations.")
    clean: list[str] = []
    for item in translated:
        if not isinstance(item, str) or not item.strip():
            raise TranslationError("Provider returned an empty or non-text subtitle translation.")
        clean.append(item.strip())
    return clean


def _post_with_retry(session: requests.Session, url: str, data: dict) -> requests.Response:
    """POST with bounded retries for rate limits, server errors and network failures."""
    last_error: Exception | None = None
    for attempt in range(SETTINGS.request_retries + 1):
        try:
            response = session.post(url, data=data, timeout=SETTINGS.request_timeout)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < SETTINGS.request_retries:
                    try:
                        delay = float(response.headers.get("Retry-After", 2 ** attempt))
                    except (TypeError, ValueError):
                        delay = 2 ** attempt
                    time.sleep(max(0.0, min(30.0, delay)))
                    continue
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < SETTINGS.request_retries:
                time.sleep(min(30, 2 ** attempt))
    raise TranslationError(f"LibreTranslate request failed after retries: {last_error or 'HTTP error'}")
