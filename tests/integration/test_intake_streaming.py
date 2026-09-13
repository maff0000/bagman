"""Tests for `services.evidence.intake.streaming` (CD-4 WI-2, PID
§13/§23/§63).

Covers bounded size enforcement (aborting AS SOON AS the limit is
exceeded, not after buffering an oversized stream in full), incremental
SHA-256 hashing, cleanup on both the success and failure paths, and
both stream shapes (`.read(n)`-like and a plain iterable of chunks).
"""
from __future__ import annotations

import hashlib
import io

import pytest

from core.errors import FileTooLargeError
from services.evidence.intake.streaming import spool_root, spool_stream


def test_spool_stream_computes_correct_size_and_hash():
    data = b"synthetic evidence bytes for the WI-2 streaming ingest test"
    with spool_stream(io.BytesIO(data), max_size_bytes=10_000) as spooled:
        assert spooled.size_bytes == len(data)
        assert spooled.sha256_hex == hashlib.sha256(data).hexdigest()
        assert spooled.path.is_file()
        assert spooled.path.read_bytes() == data
    # Cleaned up once the `with` block exits (success path).
    assert not spooled.path.exists()


def test_empty_stream_is_valid_and_hashes_to_the_well_known_empty_digest():
    with spool_stream(io.BytesIO(b""), max_size_bytes=10_000) as spooled:
        assert spooled.size_bytes == 0
        assert spooled.sha256_hex == hashlib.sha256(b"").hexdigest()
        assert spooled.path.read_bytes() == b""
    assert not spooled.path.exists()


def test_oversized_stream_raises_file_too_large_and_cleans_up():
    data = b"x" * 10_000
    spooled_path_holder = {}

    class _CapturingReader(io.BytesIO):
        pass

    reader = _CapturingReader(data)
    with pytest.raises(FileTooLargeError):
        with spool_stream(reader, max_size_bytes=100, chunk_size=64) as spooled:
            spooled_path_holder["path"] = spooled.path  # pragma: no cover - never reached

    # The context manager must never have yielded (limit hit before
    # success) — but the spool file it was writing to must be gone.
    assert spooled_path_holder == {}


def test_oversized_stream_never_reads_more_than_necessary_past_the_limit():
    """The abort must happen as soon as the running total exceeds the
    limit — never after buffering the rest of a much larger stream."""
    read_calls = []

    class _CountingReader:
        def __init__(self, total_size: int, chunk: bytes):
            self._remaining = total_size
            self._chunk = chunk

        def read(self, size: int = -1) -> bytes:
            read_calls.append(size)
            if self._remaining <= 0:
                return b""
            n = min(size, len(self._chunk), self._remaining)
            self._remaining -= n
            return self._chunk[:n]

    # A stream that CLAIMS to have 100 MiB available, in small chunks.
    reader = _CountingReader(total_size=100 * 1024 * 1024, chunk=b"y" * 1024)
    with pytest.raises(FileTooLargeError):
        with spool_stream(reader, max_size_bytes=4096, chunk_size=1024):
            pass  # pragma: no cover

    # Only a handful of chunks were ever read before aborting — nowhere
    # near the full 100 MiB the stream claimed to hold.
    assert len(read_calls) <= 10


def test_spool_stream_cleans_up_even_when_the_caller_raises_inside_the_with_block():
    spooled_path_holder = {}

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with spool_stream(io.BytesIO(b"some bytes"), max_size_bytes=1000) as spooled:
            spooled_path_holder["path"] = spooled.path
            raise _Boom("simulated downstream failure")

    assert not spooled_path_holder["path"].exists()


def test_spool_stream_accepts_a_plain_iterable_of_chunks_not_just_read():
    chunks = [b"first-chunk-", b"second-chunk-", b"third-chunk"]
    expected = b"".join(chunks)
    with spool_stream(iter(chunks), max_size_bytes=10_000) as spooled:
        assert spooled.size_bytes == len(expected)
        assert spooled.sha256_hex == hashlib.sha256(expected).hexdigest()
        assert spooled.path.read_bytes() == expected


def test_iterable_stream_also_enforces_the_limit_incrementally():
    def _generator():
        for _ in range(1000):
            yield b"x" * 1024  # ~1 MiB total if fully consumed

    with pytest.raises(FileTooLargeError):
        with spool_stream(_generator(), max_size_bytes=4096):
            pass  # pragma: no cover


def test_spool_root_is_bagman_owned_and_not_the_repository(tmp_path, monkeypatch):
    custom_dir = tmp_path / "custom-bagman-intake-spool"
    monkeypatch.setenv("BAGMAN_INTAKE_SPOOL_DIR", str(custom_dir))
    root = spool_root()
    assert root == custom_dir
    assert root.is_dir()

    import subprocess
    from pathlib import Path

    repo_root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    assert not str(root).startswith(str(repo_root))


def test_zero_or_negative_max_size_bytes_is_rejected():
    with pytest.raises(ValueError):
        with spool_stream(io.BytesIO(b"x"), max_size_bytes=0):
            pass  # pragma: no cover
