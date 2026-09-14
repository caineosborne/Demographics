"""Compatibility entry point for the legacy Gradio application.

The implementation lives in ``gradio/main.py``.  Keeping this small launcher
preserves the historical ``python main.py`` command without coupling the API
to the legacy UI directory.
"""

from pathlib import Path


_LEGACY_MAIN = Path(__file__).with_name("gradio") / "main.py"
exec(compile(_LEGACY_MAIN.read_text(), str(_LEGACY_MAIN), "exec"), globals())
