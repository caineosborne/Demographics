"""Compatibility import for the legacy Gradio research controls."""

from pathlib import Path


_LEGACY_RESEARCH_UI = Path(__file__).with_name("gradio") / "research_ui.py"
exec(compile(_LEGACY_RESEARCH_UI.read_text(), str(_LEGACY_RESEARCH_UI), "exec"), globals())
