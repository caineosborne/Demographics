"""Compatibility entry point for shared data and web tools."""
from pathlib import Path

_IMPLEMENTATION = Path(__file__).with_name("backend") / "tools.py"
exec(compile(_IMPLEMENTATION.read_text(), str(_IMPLEMENTATION), "exec"), globals())
