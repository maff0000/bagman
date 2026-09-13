"""Explicit, versionable BAGMAN Evidence Intake policy (PID §34, CD-4
WI-2).

Rather than scattering "what size is too big", "which MIME types are
accepted", "do archives get rejected or quarantined" as loose constants
across ``services/evidence/intake/``, every one of those governed
decisions lives on a single, identifiable, immutable
:class:`IntakePolicy` value that
``services.evidence.intake.validation_pipeline.run_intake_validation``
takes as an explicit parameter (defaulting to :data:`DEFAULT_INTAKE_POLICY`).
This is what makes the policy auditable ("which policy_id governed this
intake decision?") and swappable in a future delivery without touching
pipeline code.

Design decisions this module makes (PID §16/§17/§18/§20), documented
here because they are genuine judgment calls FORGE was asked to make:

* **Archives are REJECTED, not quarantined** (``archive_treatment =
  "REJECT"``). PID §17 allows either. An archive is not, by itself,
  adversarial-looking content — it is simply a container format CD-4
  deliberately does not support yet (no safe recursive-unpack
  capability exists). Rejecting it costs nothing operationally (the
  uploader can just re-upload the extracted file) and avoids
  cluttering the quarantine list — reserved for content genuinely
  worth a human's investigative attention — with routine "please
  don't upload a zip" cases.

* **Detected executable/script content is QUARANTINED, not rejected**
  (``executable_treatment = "QUARANTINE"``). Unlike an archive, a file
  whose bytes are an ELF/PE binary or a shebang script — especially
  one whose filename/reported MIME type claimed to be an ordinary
  document — is exactly the kind of thing PID §21 says quarantine
  exists for: "retained... if required for investigation". Discarding
  it immediately (as REJECTED effectively would, since WI-2 does not
  store REJECTED bytes anywhere) would throw away potentially useful
  security signal.

* **An unsupported-but-not-archive/executable content type is
  REJECTED by default** (``unsupported_content_treatment = "REJECT"``)
  — e.g. a `.docx`/`.mp3` upload today. Zero adversarial signal, so
  there is nothing to investigate; PID §16 explicitly allows either
  REJECTED or QUARANTINED here and this is the more operationally
  sensible default. A future delivery that wants to *soft-launch* a new
  format ahead of formally accepting it can flip this to
  ``"QUARANTINE"`` without any pipeline code change.

* **A scanner infrastructure failure (unreachable/timeout/
  ``SCAN_ERROR``) is treated as FAILED, not QUARANTINED**
  (``scan_error_treatment = "FAIL"``) — this is BAGMAN's own systems
  being unavailable, not a property of the uploaded content, so PID
  §7's "unexpected infrastructure failure -> FAILED" is the correct
  terminal state; a caller can legitimately retry once the scanner
  recovers. A scanner-reported ``UNSUPPORTED`` verdict (the scanner IS
  reachable and responded, but says it cannot evaluate this particular
  content) is instead treated as ``scan_unsupported_treatment =
  "QUARANTINE"`` — a genuine content-side ambiguity, not an
  infrastructure fault, and PID §20's fail-closed doctrine ("do not
  mark content safe because the scanner is unavailable") is satisfied
  either way: neither outcome is ever ``ACCEPTED``.

* **A reported/detected MIME mismatch is OBSERVED, not rejected, by
  default** (``mime_mismatch_policy = "OBSERVE"``) — PID §15 requires
  the mismatch to be *observable*, which ``detected_mime_type`` /
  ``reported_mime_type`` being independently, always populated already
  guarantees; it does not require every mismatch to be a hard failure
  (a user innocently uploading "receipt.jpg" that is actually a PNG is
  not adversarial). If the detected type is itself unsupported/
  executable/archive, THAT already routes to reject/quarantine via the
  content-type checks above regardless of this setting — this policy
  knob only governs the residual case where the detected type is
  perfectly fine, just not what was reported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Literal

#: PID §16's recommended initial accepted set.
DEFAULT_ACCEPTED_MIME_TYPES: FrozenSet[str] = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "text/plain",
        "text/csv",
    }
)

_TREATMENT = Literal["REJECT", "QUARANTINE"]
_SCAN_ERROR_TREATMENT = Literal["FAIL", "QUARANTINE"]
_MISMATCH_POLICY = Literal["OBSERVE", "REJECT"]


@dataclass(frozen=True)
class IntakePolicy:
    """One immutable, identifiable set of Evidence Intake governance
    decisions (PID §34).

    ``policy_id`` is the audit-visible identifier for "which version of
    this policy governed this intake attempt" — bump it (a new
    ``IntakePolicy`` instance, never a mutation) whenever any field's
    value changes, exactly as ``schema_version`` identifies a contract
    shape.
    """

    policy_id: str = "bagman.intake_policy.v1"

    #: Per-file size limit (PID §12) — enforced incrementally by
    #: ``services.evidence.intake.streaming.spool_stream`` as bytes
    #: arrive, never by first buffering the whole file.
    max_file_size_bytes: int = 50 * 1024 * 1024  # 50 MiB

    #: Request-level size limit (PID §12). CD-4's manual-upload intake
    #: pipeline (this module) processes exactly one file per
    #: intake attempt, so today this is identical in effect to
    #: ``max_file_size_bytes`` for THIS pipeline; it is modelled as its
    #: own field because PID §12 explicitly asks for both a
    #: request-level and a per-file limit, and a future multi-file-per-
    #: request HTTP endpoint (WI-3's concern, aggregating across
    #: multiple parts of one multipart request) needs a distinct,
    #: already-governed value to sum individual file sizes against
    #: without this policy object needing to change shape.
    max_request_size_bytes: int = 50 * 1024 * 1024  # 50 MiB

    #: Maximum accepted length of ``original_filename`` (PID §14).
    max_filename_length: int = 255

    #: PID §16's initial accepted content types.
    accepted_mime_types: FrozenSet[str] = field(default_factory=lambda: DEFAULT_ACCEPTED_MIME_TYPES)

    #: Content types explicitly known and explicitly routed to
    #: QUARANTINED regardless of the general unsupported-type fallback
    #: below (PID §34) — e.g. a future delivery could list a legacy
    #: office format here ahead of fully accepting it. Empty by default
    #: for CD-4 WI-2: no such explicit type exists yet.
    quarantine_mime_types: FrozenSet[str] = frozenset()

    #: Content types explicitly known and explicitly routed to
    #: REJECTED regardless of the general unsupported-type fallback
    #: (PID §34) — reserved for a future explicit denylist entry more
    #: specific than "not in the accepted set". Empty by default.
    rejected_mime_types: FrozenSet[str] = frozenset()

    #: PID §17 — see module docstring for why REJECT is the default.
    archive_treatment: _TREATMENT = "REJECT"

    #: PID §18 — see module docstring for why QUARANTINE is the default.
    executable_treatment: _TREATMENT = "QUARANTINE"

    #: PID §16 fallback for a detected type that is none of: accepted,
    #: explicitly quarantine-listed, explicitly reject-listed, archive,
    #: or executable. See module docstring for why REJECT is the
    #: default.
    unsupported_content_treatment: _TREATMENT = "REJECT"

    #: PID §60 — "scanner required for manual upload readiness" is the
    #: PID's own explicitly stated preference; only ``True`` is
    #: supported end-to-end by this delivery's pipeline (WI-2 does not
    #: implement an optional-scanner code path), kept as a policy field
    #: rather than a hardcoded assumption so a later delivery can
    #: change it without a pipeline rewrite, and so the decision is
    #: itself audit-visible via ``policy_id``.
    scanner_required: bool = True

    #: PID §20 fail-closed treatment for a scanner INFRASTRUCTURE
    #: failure (unreachable/timeout/``SCAN_ERROR``) — see module
    #: docstring for why FAIL (-> intake ``FAILED``) is the default,
    #: distinct from a content-side scanner ``UNSUPPORTED`` verdict.
    scan_error_treatment: _SCAN_ERROR_TREATMENT = "FAIL"

    #: PID §20 fail-closed treatment for a scanner ``UNSUPPORTED``
    #: verdict (scanner reachable, but declines to evaluate this
    #: content) — see module docstring for why QUARANTINE is the
    #: default, distinct from ``scan_error_treatment`` above.
    scan_unsupported_treatment: _SCAN_ERROR_TREATMENT = "QUARANTINE"

    #: PID §15 — see module docstring for why OBSERVE is the default.
    mime_mismatch_policy: _MISMATCH_POLICY = "OBSERVE"

    def __post_init__(self) -> None:
        if self.max_file_size_bytes <= 0:
            raise ValueError("max_file_size_bytes must be positive")
        if self.max_request_size_bytes <= 0:
            raise ValueError("max_request_size_bytes must be positive")
        if self.max_filename_length <= 0:
            raise ValueError("max_filename_length must be positive")
        if self.archive_treatment not in ("REJECT", "QUARANTINE"):
            raise ValueError(f"archive_treatment must be 'REJECT' or 'QUARANTINE', got {self.archive_treatment!r}")
        if self.executable_treatment not in ("REJECT", "QUARANTINE"):
            raise ValueError(
                f"executable_treatment must be 'REJECT' or 'QUARANTINE', got {self.executable_treatment!r}"
            )
        if self.unsupported_content_treatment not in ("REJECT", "QUARANTINE"):
            raise ValueError(
                "unsupported_content_treatment must be 'REJECT' or 'QUARANTINE', got "
                f"{self.unsupported_content_treatment!r}"
            )
        if self.scan_error_treatment not in ("FAIL", "QUARANTINE"):
            raise ValueError(
                f"scan_error_treatment must be 'FAIL' or 'QUARANTINE', got {self.scan_error_treatment!r}"
            )
        if self.scan_unsupported_treatment not in ("FAIL", "QUARANTINE"):
            raise ValueError(
                "scan_unsupported_treatment must be 'FAIL' or 'QUARANTINE', got "
                f"{self.scan_unsupported_treatment!r}"
            )
        if self.mime_mismatch_policy not in ("OBSERVE", "REJECT"):
            raise ValueError(f"mime_mismatch_policy must be 'OBSERVE' or 'REJECT', got {self.mime_mismatch_policy!r}")


#: The sane default policy CD-4 WI-2 ships with — every field above at
#: its documented default. Callers needing different governance (e.g.
#: a smaller test-only size limit) construct their own
#: ``IntakePolicy(...)`` rather than mutating this one (it is frozen).
DEFAULT_INTAKE_POLICY = IntakePolicy()
