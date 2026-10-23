"""Responsive Tkinter user interface for batch subtitle translation."""
from __future__ import annotations

import logging
import os
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from logger_config import configure_logging
from processor import JobController, extract_folder, process_folder
from scanner import MediaToolError, require_media_tools
from translator import TranslationError


class TranslatorApp(tk.Tk):
    """Windows-friendly GUI whose worker communicates through a thread-safe queue."""

    def __init__(self) -> None:
        super().__init__()
        self.title("MKV Hindi Subtitle Translator")
        self.geometry("850x650")
        self.minsize(720, 540)
        self.events: queue.Queue[tuple[str, dict]] = queue.Queue()
        self.controller: JobController | None = None
        self.worker: threading.Thread | None = None
        self.folder_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.recursive_var = tk.BooleanVar(value=False)
        self.source_var = tk.StringVar(value="English")
        self.target_var = tk.StringVar(value="Hindi")
        self.selection_var = tk.StringVar(value="Auto English")
        self.engine_var = tk.StringVar(value="Qwen3 4B (Ollama)")
        self.overwrite_var = tk.BooleanVar(value=False)
        self.current_var = tk.StringVar(value="Ready")
        self.progress_var = tk.DoubleVar(value=0)
        self.file_index = 1
        self.file_total = 1
        self._build()
        self.after(100, self._drain_events)

    def _build(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        ttk.Label(root, text="MKV folder or file").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(root, textvariable=self.folder_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(root, text="Browse…", command=self._choose_folder).grid(row=0, column=2)
        ttk.Button(root, text="Choose MKV…", command=self._choose_file).grid(row=0, column=3, padx=(6, 0))
        ttk.Checkbutton(root, text="Include subfolders", variable=self.recursive_var).grid(row=1, column=1, sticky="w")
        ttk.Label(root, text="Output folder (blank = beside each video)").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(root, textvariable=self.output_var).grid(row=2, column=1, sticky="ew", padx=6)
        ttk.Button(root, text="Browse…", command=self._choose_output).grid(row=2, column=2)
        ttk.Label(root, text="Source language").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Combobox(root, textvariable=self.source_var, state="readonly",
                     values=("English", "French", "Spanish", "German", "Japanese", "Chinese", "Portuguese", "Italian", "Russian", "Korean")).grid(row=3, column=1, sticky="w", padx=6)
        ttk.Label(root, text="Target language").grid(row=3, column=2, sticky="e")
        ttk.Combobox(root, textvariable=self.target_var, state="readonly", values=("Hindi",), width=12).grid(row=3, column=3, sticky="w", padx=6)
        ttk.Label(root, text="Subtitle track").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Combobox(root, textvariable=self.selection_var, state="normal",
                     values=("Auto English", "Source-language track 1", "Source-language track 2",
                             "Source-language track 3", "First subtitle track", "Stream index 3")).grid(row=4, column=1, sticky="w", padx=6)
        ttk.Label(root, text="Engine").grid(row=4, column=2, sticky="e")
        ttk.Combobox(root, textvariable=self.engine_var, state="readonly",
                     values=("Qwen3 4B (Ollama)", "OPUS-MT (offline)", "Argos (offline)", "DeepSeek API", "LibreTranslate"),
                     width=22).grid(row=4, column=3, sticky="w", padx=6)
        ttk.Checkbutton(root, text="Overwrite existing output SRT files (off = skip)", variable=self.overwrite_var).grid(row=5, column=1, sticky="w", pady=5)
        buttons = ttk.Frame(root)
        buttons.grid(row=6, column=0, columnspan=4, sticky="w", pady=8)
        self.start_button = ttk.Button(buttons, text="Translate to Hindi", command=self._start)
        self.start_button.pack(side="left", padx=(0, 6))
        self.extract_button = ttk.Button(buttons, text="Extract SRT", command=self._start_extract)
        self.extract_button.pack(side="left", padx=6)
        self.pause_button = ttk.Button(buttons, text="Pause", command=self._pause, state="disabled")
        self.pause_button.pack(side="left", padx=6)
        self.resume_button = ttk.Button(buttons, text="Resume", command=self._resume, state="disabled")
        self.resume_button.pack(side="left", padx=6)
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=6)
        ttk.Button(buttons, text="Open output folder", command=self._open_output).pack(side="left", padx=6)
        ttk.Label(root, textvariable=self.current_var).grid(row=7, column=0, columnspan=4, sticky="w", pady=(8, 3))
        self.overall = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.overall.grid(row=8, column=0, columnspan=4, sticky="ew", pady=3)
        self.subtitle_progress = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.subtitle_progress.grid(row=9, column=0, columnspan=4, sticky="ew", pady=3)
        ttk.Label(root, text="Live log").grid(row=10, column=0, sticky="w", pady=(8, 2))
        self.log = tk.Text(root, height=18, wrap="word", state="disabled")
        self.log.grid(row=11, column=0, columnspan=4, sticky="nsew")
        root.rowconfigure(11, weight=1)
        ttk.Label(root, text="Qwen3 via Ollama, OPUS-MT, and Argos run locally. DeepSeek and LibreTranslate send dialogue online.",
                  wraplength=780).grid(row=12, column=0, columnspan=4, sticky="w", pady=(8, 0))

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory()
        if selected:
            self.folder_var.set(selected)

    def _choose_file(self) -> None:
        selected = filedialog.askopenfilename(
            title="Choose one MKV file",
            filetypes=(("MKV video", "*.mkv"), ("All files", "*.*")))
        if selected:
            self.folder_var.set(selected)

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory()
        if selected:
            self.output_var.set(selected)

    def _start(self) -> None:
        self._launch_job(extract_only=False)

    def _start_extract(self) -> None:
        self._launch_job(extract_only=True)

    def _launch_job(self, extract_only: bool) -> None:
        if self.worker and self.worker.is_alive():
            return
        folder = Path(self.folder_var.get()).expanduser()
        if not (folder.is_dir() or (folder.is_file() and folder.suffix.lower() == ".mkv")):
            messagebox.showerror("Input required", "Choose an MKV folder or one .mkv file.")
            return
        try:
            require_media_tools()
        except MediaToolError as exc:
            messagebox.showerror("FFmpeg not found", str(exc))
            return
        output_text = self.output_var.get().strip()
        output = Path(output_text).expanduser() if output_text else None
        recursive = self.recursive_var.get()
        overwrite = self.overwrite_var.get()
        source = self.source_var.get()
        selection = self.selection_var.get().strip()
        engine_name = self.engine_var.get()
        report_dir = output or (folder.parent if folder.is_file() else folder)
        logger = configure_logging(report_dir)
        self.controller = JobController()
        self.start_button.configure(state="disabled")
        self.extract_button.configure(state="disabled")
        self.pause_button.configure(state="normal")
        self.cancel_button.configure(state="normal")
        self.current_var.set("Preparing subtitle extraction…" if extract_only else "Checking translation provider…")
        self.worker = threading.Thread(target=self._run_job,
                                       args=(folder, output, logger, recursive, overwrite, source, selection,
                                             engine_name, extract_only), daemon=True)
        self.worker.start()

    def _run_job(self, folder: Path, output: Path | None, logger: logging.Logger, recursive: bool,
                 overwrite: bool, source: str, selection: str, engine_name: str,
                 extract_only: bool = False) -> None:
        assert self.controller is not None
        try:
            if extract_only:
                results = extract_folder(folder, recursive, output, overwrite, source, selection,
                                         self.controller, self._emit, logger)
            else:
                results = process_folder(folder, recursive, output, overwrite,
                                         source, selection, engine_name,
                                         self.controller, self._emit, logger)
            self._emit("done", {"results": results})
        except InterruptedError:
            self._emit("cancelled", {})
        except (TranslationError, MediaToolError, Exception) as exc:
            logger.exception("Job could not start or finish")
            self._emit("fatal", {"message": str(exc)})

    def _emit(self, name: str, payload: dict) -> None:
        self.events.put((name, payload))

    def _drain_events(self) -> None:
        try:
            while True:
                name, payload = self.events.get_nowait()
                if name == "log":
                    self._append_log(payload["message"])
                elif name == "file":
                    self.file_index, self.file_total = payload["index"], payload["total"]
                    self.current_var.set(f"File {payload['index']} of {payload['total']}: {payload['name']}")
                    self.overall.configure(maximum=payload["total"])
                    self.overall.configure(value=payload["index"] - 1)
                    self.subtitle_progress.configure(value=0, maximum=100)
                elif name == "progress":
                    current, total = payload["current"], payload["total"]
                    self.current_var.set(f"{payload['file']} — subtitle {current} of {total}")
                    self.subtitle_progress.configure(maximum=max(1, total), value=current)
                    self.overall.configure(value=max(0, self.file_index - 1) + current / max(1, total))
                elif name == "streams":
                    tracks = payload["streams"]
                    details = "; ".join(f"#{s['index']} {s['language']} {s['codec']} {s['title']} "
                                         f"default={s['default']} forced={s['forced']}" for s in tracks)
                    self._append_log(f"Tracks in {payload['file']}: {details}")
                elif name == "done":
                    self._finish(payload["results"])
                elif name == "cancelled":
                    self._append_log("Job cancelled. Completed files remain saved.")
                    self._reset_buttons()
                elif name == "fatal":
                    self._append_log(f"Job stopped: {payload['message']}")
                    messagebox.showerror("Translation job", payload["message"])
                    self._reset_buttons()
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _finish(self, results: list) -> None:
        success = sum(x.status == "success" for x in results)
        skipped = sum(x.status == "skipped" for x in results)
        failed = sum(x.status == "failed" for x in results)
        self._append_log(f"Finished: {success} successful, {skipped} skipped, {failed} failed.")
        for item in results:
            if item.status != "success":
                self._append_log(f"{item.status.upper()}: {Path(item.file).name} — {item.detail}")
        self.current_var.set("Complete")
        self.overall.configure(value=self.file_total)
        self._reset_buttons()
        messagebox.showinfo("Batch complete", f"Successful: {success}\nSkipped: {skipped}\nFailed: {failed}")

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _pause(self) -> None:
        if self.controller:
            self.controller.paused.set()
            self.pause_button.configure(state="disabled")
            self.resume_button.configure(state="normal")
            self.current_var.set("Paused (current network request will finish first)")

    def _resume(self) -> None:
        if self.controller:
            self.controller.paused.clear()
            self.resume_button.configure(state="disabled")
            self.pause_button.configure(state="normal")

    def _cancel(self) -> None:
        if self.controller:
            self.controller.cancelled.set()
            self.controller.paused.clear()
            self.cancel_button.configure(state="disabled")

    def _open_output(self) -> None:
        path = Path(self.output_var.get()).expanduser() if self.output_var.get().strip() else Path(self.folder_var.get()).expanduser()
        if path.is_file():
            path = path.parent
        if path.is_dir():
            if hasattr(os, "startfile"):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                webbrowser.open(path.as_uri())
        else:
            messagebox.showinfo("Output folder", "Choose an input folder or output directory first.")

    def _reset_buttons(self) -> None:
        self.start_button.configure(state="normal")
        self.extract_button.configure(state="normal")
        self.pause_button.configure(state="disabled")
        self.resume_button.configure(state="disabled")
        self.cancel_button.configure(state="disabled")


if __name__ == "__main__":
    TranslatorApp().mainloop()
