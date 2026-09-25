"""Incident Commander — multi-agent active differential diagnosis for SRE incidents."""

__version__ = "0.1.0"

# Single swappable model constant (see README). Used only when IC_USE_LLM=1 and a
# key is present; the deterministic analytical engine is the offline default.
MODEL = "claude-sonnet-5"


def enable_utf8_stdout() -> None:
    """Make stdout/stderr UTF-8 so the CLIs render their box-drawing, ★, Δ and →
    characters everywhere.

    Windows consoles default to cp1252, which raises UnicodeEncodeError *partway
    through* a print block — the harness would emit its first few lines and then
    die, which reads as an engine failure rather than a console-encoding one.
    Called explicitly from each CLI entry point rather than on import, so
    importing `ic` never mutates a host process's streams (the server imports
    this package too).
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # Stream replaced by a capture object (pytest), already closed, or
            # not reconfigurable — printing plain ASCII still works.
            pass
