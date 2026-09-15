"""Compatibility entry point for the shared research implementation."""
from pathlib import Path

_IMPLEMENTATION = Path(__file__).with_name("backend") / "research.py"
exec(compile(_IMPLEMENTATION.read_text(), str(_IMPLEMENTATION), "exec"), globals())
