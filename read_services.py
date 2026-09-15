"""Compatibility entry point for read services."""
from pathlib import Path

_IMPLEMENTATION = Path(__file__).with_name("backend") / "read_services.py"
exec(compile(_IMPLEMENTATION.read_text(), str(_IMPLEMENTATION), "exec"), globals())
