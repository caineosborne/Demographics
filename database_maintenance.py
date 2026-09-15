"""Compatibility entry point for database maintenance."""
from pathlib import Path

_IMPLEMENTATION = Path(__file__).with_name("backend") / "database_maintenance.py"
exec(compile(_IMPLEMENTATION.read_text(), str(_IMPLEMENTATION), "exec"), globals())
