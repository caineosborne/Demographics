"""Compatibility entry point for temporal context helpers."""
from pathlib import Path

_IMPLEMENTATION = Path(__file__).with_name("backend") / "temporal_context.py"
exec(compile(_IMPLEMENTATION.read_text(), str(_IMPLEMENTATION), "exec"), globals())
