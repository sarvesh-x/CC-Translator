"""Timestamped application logging."""
import logging
from pathlib import Path


def configure_logging(directory: Path) -> logging.Logger:
    """Create a UTF-8 log file in the selected output folder."""
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("mkv_hindi_translator")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(directory / "mkv_hindi_translator.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger
