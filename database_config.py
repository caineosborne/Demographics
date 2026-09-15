"""Compatibility entry point for database configuration."""
from pathlib import Path

_IMPLEMENTATION = Path(__file__).with_name("backend") / "database_config.py"
exec(compile(_IMPLEMENTATION.read_text(), str(_IMPLEMENTATION), "exec"), globals())
