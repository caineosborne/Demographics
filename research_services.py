"""Compatibility entry point for research services."""
from pathlib import Path

_IMPLEMENTATION = Path(__file__).with_name("backend") / "research_services.py"
exec(compile(_IMPLEMENTATION.read_text(), str(_IMPLEMENTATION), "exec"), globals())
