"""Compatibility import for the legacy Gradio visualisation helpers."""

from pathlib import Path


_LEGACY_VISUALISATION = Path(__file__).with_name("gradio") / "visualisation.py"
exec(compile(_LEGACY_VISUALISATION.read_text(), str(_LEGACY_VISUALISATION), "exec"), globals())
