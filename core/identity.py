"""Canonical BAGMAN identifier generation (PID §5).

Implements UUIDv7 (RFC 9562 §5.2) directly — no external dependency is
used for this (PL decision for WI-2): it is a small, well-defined
algorithm, and BAGMAN's canonical identifier shape is pinned exactly by
``contracts/common/bagman.identifier.v1.schema.json``.

Bit layout of the 128-bit value (matching RFC 9562 §5.2 and the
contract's pattern), most-significant bit first:

    48 bits  unix_ts_ms   big-endian Unix milliseconds since the epoch
     4 bits  version      fixed ``0b0111`` (7)
    12 bits  rand_a       monotonic counter, seeded randomly (see below)
     2 bits  variant      fixed ``0b10``
    62 bits  rand_b       random

Rendered as the standard lowercase 8-4-4-4-12 hex UUID string form.

Monotonicity
------------
UUIDv7's defining property is that identifiers generated later sort
later as plain strings. Millisecond wall-clock resolution means many
IDs can legitimately share the same ``unix_ts_ms`` value. To keep
ordering strictly increasing even across such collisions (rather than
leaving it to chance across 12+62 random bits), this generator treats
``rand_a`` as a counter: it is seeded from a random value the first
time a new millisecond is observed, then incremented by one for every
further ID generated within that same millisecond. If the 12-bit
counter would overflow within a single millisecond (4096 IDs), the
timestamp component is advanced by one tick instead of wrapping, so
ordering never goes backwards even under extreme call rates or a
regressing system clock. ``rand_b`` remains independently random on
every call (it is below the counter in significance, so it never
affects ordering).
"""
from __future__ import annotations

import os
import threading
import time

_MASK_48 = (1 << 48) - 1
_MASK_12 = (1 << 12) - 1
_MASK_62 = (1 << 62) - 1

_VERSION_NIBBLE = 0x7
_VARIANT_BITS = 0b10

_lock = threading.Lock()
_last_ts_ms = -1
_last_rand_a = 0


def _random_rand_a() -> int:
    return int.from_bytes(os.urandom(2), "big") & _MASK_12


def _random_rand_b() -> int:
    return int.from_bytes(os.urandom(8), "big") & _MASK_62


def generate_id() -> str:
    """Generate a new lowercase UUIDv7 string identifier.

    The returned string always validates against
    ``contracts/common/bagman.identifier.v1.schema.json``, and is
    monotonically sortable by creation time (see module docstring).
    """
    global _last_ts_ms, _last_rand_a

    with _lock:
        ts_ms = time.time_ns() // 1_000_000

        if ts_ms > _last_ts_ms:
            rand_a = _random_rand_a()
        else:
            # Same millisecond as the previous ID (or the wall clock
            # went backwards) — advance the counter instead of the
            # clock so ordering is preserved.
            ts_ms = _last_ts_ms
            rand_a = (_last_rand_a + 1) & _MASK_12
            if rand_a == 0:
                # 12-bit counter overflowed inside one millisecond;
                # borrow a tick from the future rather than wrap.
                ts_ms += 1
                rand_a = _random_rand_a()

        _last_ts_ms = ts_ms
        _last_rand_a = rand_a
        rand_b = _random_rand_b()

    value = (ts_ms & _MASK_48) << 80
    value |= _VERSION_NIBBLE << 76
    value |= rand_a << 64
    value |= _VARIANT_BITS << 62
    value |= rand_b

    hex_str = f"{value:032x}"
    return (
        f"{hex_str[0:8]}-{hex_str[8:12]}-{hex_str[12:16]}-"
        f"{hex_str[16:20]}-{hex_str[20:32]}"
    )
