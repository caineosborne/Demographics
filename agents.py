"""Compatibility entry point for the shared agent implementation."""
from pathlib import Path

_IMPLEMENTATION = Path(__file__).with_name("backend") / "agents.py"
exec(compile(_IMPLEMENTATION.read_text(), str(_IMPLEMENTATION), "exec"), globals())
