"""Experimental bounded file reader; no claim of trusted source provenance."""
import os
import stat

from .config_json import MAX_CONFIG_BYTES, decode_candidates


def read_candidates_file(path):
    """Read at most the configured bound plus one byte from a regular file.

    This is a synthetic convenience boundary, not authentication, a sandbox,
    or an atomic snapshot of a concurrently modified file.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("invalid_source")
            raw = source.read(MAX_CONFIG_BYTES + 1)
        return decode_candidates(raw)
    except (OSError, TypeError, ValueError, OverflowError):
        raise ValueError("malformed_configuration") from None
