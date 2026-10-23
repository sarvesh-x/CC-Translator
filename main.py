"""Entry point for the MKV Hindi Subtitle Translator."""
from gui import TranslatorApp


def main() -> None:
    """Launch the desktop application."""
    app = TranslatorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
