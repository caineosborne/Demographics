"""Compatibility entry point for the shared research store."""
from pathlib import Path

_IMPLEMENTATION = Path(__file__).with_name("backend") / "research_store.py"
exec(compile(_IMPLEMENTATION.read_text(), str(_IMPLEMENTATION), "exec"), globals())
