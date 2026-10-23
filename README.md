# MKV Hindi Subtitle Translator

A Windows Tkinter application that finds embedded subtitle streams in MKV files, extracts text tracks to UTF-8 SRT files, and can optionally translate dialogue into Hindi. It never remuxes, re-encodes, or writes to the source MKV.

## Architecture and translation choices

- `scanner.py` discovers MKVs and reads stream indices, language, codec, title, default and forced flags from FFprobe.
- `subtitle_extractor.py` maps only the chosen text subtitle stream and converts it to SubRip. PGS, VobSub and XSub image tracks are detected and skipped with OCR guidance; the app does not pretend that bitmap images are text.
- `subtitle_writer.py` parses actual SRT entries, masks common inline formatting tags during translation, retains each entry's timing and sequence, and writes atomically in UTF-8.
- `translator.py` provides five interchangeable engines: Qwen3 through local Ollama, local OPUS-MT, DeepSeek API, a configured LibreTranslate server, and Argos Translate. Qwen3 uses nearby subtitle dialogue for context and requests concise, conversational Hindi. All Qwen inference stays on this computer. LibreTranslate's `/languages` list and Argos's installed pair are checked before use.
- `processor.py` processes one file at a time, retries failed batches entry-by-entry, saves completed translations in an atomic progress cache, and writes a JSON summary.
- `gui.py` keeps media and network work on a worker thread. Pause takes effect at safe request boundaries; Cancel stops safely at a boundary and leaves the cache for resume.

The GUI defaults to Qwen3 4B through Ollama, a free Apache-2.0 model that runs locally and supports more natural conversational output than the small dedicated OPUS-MT translation model. The Ollama library lists its Q4_K_M download at about 2.5 GB and supports 100+ languages ([Qwen3 4B model details](https://ollama.com/library/qwen3:4b)). The first model download needs internet; after that, translation runs locally without API billing. OPUS-MT and Argos remain smaller local alternatives. DeepSeek remains an optional online engine; its model name can be changed in `.env`. A legacy `.env` value of `deepseek-chat` or `deepseek-reasoner` maps to `deepseek-flash`. See the [official DeepSeek model list](https://api-docs.deepseek.com/quick_start/pricing/) and [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/).

The application validates provider configuration and the selected language pair before translating. Ollama is checked for a reachable local service and installed model; LibreTranslate's advertised languages and Argos's installed pair are checked directly. DeepSeek does not publish a fixed source/target pair registry through this interface, so the app makes one short greeting translation request and requires Devanagari output before processing; this uses a small amount of API quota. The greeting check only confirms basic capability, so review quality on a sample before a large batch.


![alt text](/images/image.png)


## Project layout

```text
CC Translator/
├── main.py
├── gui.py
├── scanner.py
├── subtitle_extractor.py
├── translator.py
├── processor.py
├── subtitle_writer.py
├── config.py
├── logger_config.py
├── requirements.txt
├── requirements-local.txt
├── run.bat
├── .env.example
├── .gitignore
├── README.md
├── examples/example.srt
├── examples/example.hi.srt
└── tests/test_core.py
```

## Windows setup

1. Install **64-bit Python 3.11** from [python.org](https://www.python.org/downloads/windows/). Enable the Python launcher and PATH options in the installer.
2. Install FFmpeg. For example, with Windows Package Manager run `winget install Gyan.FFmpeg`. Alternatively download a trusted FFmpeg build, extract it, and add its `bin` folder to PATH.
3. Open a new Command Prompt and verify:

   ```bat
   ffmpeg -version
   ffprobe -version
   ```

4. In the project directory, install required packages:

   ```bat
   py -3.11 -m pip install -r requirements.txt
   ```

5. Install [Ollama for Windows](https://ollama.com/download/windows), then open Command Prompt and download the default model:

   ```bat
   ollama pull qwen3:4b
   ```

   The model download is about 2.5 GB and requires internet once. Ollama runs the model on your computer; this app connects only to Ollama at `localhost`. Qwen3 4B is the default because it can use neighboring lines and a style prompt to produce more conversational Hindi. For model settings, edit `OLLAMA_MODEL` or `OLLAMA_BASE_URL` in `.env`.

6. OPUS-MT is a smaller optional local alternative. To use it, install its optional Python dependencies and PyTorch CPU build:

   ```bat
   py -3.11 -m pip install torch --index-url https://download.pytorch.org/whl/cpu
   py -3.11 -m pip install -r requirements-local.txt
   ```

   The initial model download needs an internet connection and roughly 300 MB for the PyTorch weights, plus cache and temporary space. After it is cached, translation can run offline. Helsinki-NLP supplies standard MarianMT model weights and tokenizer files; the app loads them with Transformers on CPU.

7. For DeepSeek, copy `.env.example` to `.env` and set `DEEPSEEK_API_KEY` to your key. The base URL and model are configurable there. Subtitle dialogue is sent to the configured online provider.
8. Or configure LibreTranslate by setting `LIBRETRANSLATE_URL` to your server URL and optional `LIBRETRANSLATE_API_KEY`. The app checks that the server advertises both selected source language and `hi`. A local server must already be installed and running.
9. To use Argos offline, select that engine in the app. Install the optional package:

   ```bat
   py -3.11 -m pip install argostranslate
   ```

   Then use Argos's package index to install an English-to-Hindi model (you can run this in a Python script):

   ```python
   import argostranslate.package
   argostranslate.package.update_package_index()
   packages = argostranslate.package.get_available_packages()
   package = next(p for p in packages if p.from_code == "en" and p.to_code == "hi")
   download_path = package.download()
   argostranslate.package.install_from_path(download_path)
   ```

   Model download and disk requirements vary by release. The app never downloads a model automatically. Argos inference runs on CPU and requires no internet after installation.
10. Launch using `run.bat`, or from Command Prompt:

   ```bat
   py -3.11 main.py
   ```

## Translate a folder

1. Make sure FFmpeg and FFprobe work. Choose **Translate to Hindi** to translate or **Extract SRT** to export the selected embedded track without translation; extraction does not need any translation engine or Ollama setup.
2. Start the app, choose a folder containing `.mkv` files, or click **Choose MKV…** to process one individual file. The subfolder option applies only to folder input.
3. Choose the source language (English by default) and Hindi target. `Auto English` selects a track matching the chosen source language, preferring a default track; when no language is tagged, it uses the default text track or a track whose title names the selected language. Select `Source-language track 1`, `Source-language track 2`, or `Source-language track 3` to choose among matching tracks. `First subtitle track` selects the first text stream. You can also type a specific global stream index such as `Stream index 4` after checking the stream list in the live log.
4. To translate, choose the translator. Qwen3 via Ollama is the default local engine. Argos and OPUS-MT also run locally after setup. DeepSeek and LibreTranslate are online. To extract only, skip engine setup.
5. Choose an output directory or leave it blank to save beside each video. Existing SRT files are skipped by default. Tick the overwrite option only when you want to replace them.
6. Click **Translate to Hindi** to create `Movie.hi.srt`, or **Extract SRT** to export the selected embedded text track as `Movie.eng.srt` (using that track's language tag; untagged tracks use `Movie.und.srt`). Extracted SRTs are plain external files, with no translation or API/model needed. Image-based tracks such as PGS need OCR and are skipped with guidance.
7. Completed translations are cached in an adjacent `.progress.json` until the translated SRT completes, then the cache is removed. Rerun with the same output location to reuse completed entries. The output folder also receives a timestamped log and `mkv_hindi_translator_report.json`.
8. In VLC, use **Subtitle → Add Subtitle File…** to select the SRT. VLC may auto-detect external subtitles when their name and directory follow its subtitle naming conventions; manual loading is the reliable fallback. The external SRT does not permanently modify any embedded stream.

## OCR for image subtitles

PGS and VobSub tracks contain images, so FFmpeg cannot extract their dialogue as text. Install Subtitle Edit separately, open the MKV or extracted image subtitle, run OCR, correct the recognized text, and save as SRT. OCR output needs proofreading. This project does not bundle Subtitle Edit or run hidden OCR.

## Tests and example

`examples/example.srt` and `examples/example.hi.srt` show the input and a sample Hindi output with multiline text and italic formatting. A `unittest` suite is in `tests/`; it uses mocks and does not make paid API calls. Run:

```bat
py -3.11 -m unittest discover -s tests -v
```

## Troubleshooting

- **FFmpeg/FFprobe missing:** install FFmpeg, add the folder containing `ffmpeg.exe` and `ffprobe.exe` to PATH, open a new terminal, and verify both version commands.
- **No subtitles or wrong track:** inspect metadata in the live log. Track language tags may be missing or inaccurate; type the global stream number as `Stream index N` to select it.
- **Bitmap subtitle warning:** use Subtitle Edit OCR as above; bitmap subtitles cannot directly become text.
- **Hindi displays incorrectly:** keep output UTF-8 and use a font with Devanagari support in VLC. Avoid saving the SRT from an editor using legacy encoding.
- **DeepSeek auth/model/HTTP errors:** check `.env`, key validity, account access to the configured model, endpoint and network. Completed entries remain cached; persistent errors are reported instead of copying English.
- **DeepSeek says Payment Required (HTTP 402):** this API account needs billing or credits. For natural translations without API billing, install Ollama and run `ollama pull qwen3:4b`, then select **Qwen3 4B (Ollama)**. The model runs locally and has no translation API charge.
- **LibreTranslate lacks Hindi:** inspect its `/languages` endpoint and use a server that advertises both chosen source language and `hi`.
- **Argos model missing:** install Argos and the exact selected source-to-Hindi pair. The app reports a missing model rather than returning source text.
- **Resume after interruption:** rerun the same input folder with the same output path and settings. Completed subtitle entries are loaded from the progress cache; remaining entries are retried.

## Limitations and translation quality

Qwen3 receives adjacent subtitle lines for context and is prompted for short, natural spoken Hindi without filler or added meaning. It runs through the local Ollama service, so subtitle dialogue is not sent to a translation API. OPUS-MT is a smaller local alternative with a dedicated English-to-Hindi model; it cannot use a prompt to resolve film-wide character voice, so check names, gender, sarcasm, and idioms in a sample. Argos is another offline option and may sound more literal. Machine translation still needs human review for speaker formality, names, idioms and line length. Batches are bounded; a movie transcript is never sent in one request. A failed batch is retried entry-by-entry; persistent provider failures fail that file instead of silently copying English. Timing and sequence identifiers are preserved. ASS/SSA styling is reduced by conversion to SRT; common inline tags are kept where FFmpeg emits them. Existing outputs are protected by default.

The source-language choices are limited to common languages. The provider/model or installed Argos pair must support the chosen language and Hindi. Pause waits for the current network request or extraction operation; cancel is honored at safe checkpoints. The progress cache contains subtitle text, so keep the output folder private when appropriate.

## Optional executable

Install PyInstaller (`py -3.11 -m pip install pyinstaller`) and run `pyinstaller --onefile --windowed main.py`. FFmpeg/FFprobe and Argos language models remain separate runtime assets.
