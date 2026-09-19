"""``compute_review_priority``/``aggregate_discovery_reasons`` — a
bounded, deterministic, NON-AI operator-triage aid for the 90 real
``MAILBOX_DOMAIN_REVIEW`` items ``matt@infosecurs.com``'s historical
mailbox discovery sweep produced (CD-6 GUI-operations-foundation WO,
mailbox-evidence-based triage addendum).

Why this exists — the Xero-correlation avenue is closed
------------------------------------------------------------------------
``services/xero/supplier_correlation.py`` attaches a Xero-assisted
``xero_correlation_class`` to each item as a review aid, but Infosecurs'
own Xero data turned out to have almost no purchase-side history —
most of the 90 real items are ``NONE``/``CONTACT_ONLY``. The architect
has closed that avenue as the PRIMARY triage signal and asked for a
purely mailbox-evidence-based priority instead, reading only data BAGMAN
already has from Stage-A discovery (candidate volume, attachment
ratio, recurrence span) plus ``xero_correlation_class`` as ONE
additional input when it happens to be informative — never the other
way around.

Presentation-layer only (read before changing)
------------------------------------------------------------------------
Both functions here are PURE — no I/O, no repository access, no
persistence. The caller (``app/api/routers/mailboxes_microsoft.py
::list_microsoft_domain_review_items``) computes ``review_priority``/
``discovery_reason_counts`` fresh on every ``GET`` call and adds them as
NEW TOP-LEVEL keys on the response dict, never inside
``item["metadata"]`` and never written back via
``NeedsYouRepository.update_item_metadata`` — exactly like
``services/xero/supplier_correlation.py``'s own ``xero_*`` metadata
keys are a review aid, except this one is not even persisted. A
``NeedsYouItem`` never gains a new stored field because of this module.

Never AI, never a black box (mirrors ``services/mailbox/discovery_signals
.py``'s own explicit doctrine)
------------------------------------------------------------------------
``compute_review_priority`` is plain point-scoring arithmetic over
already-known numbers — never a model call, never a learned weight,
never a confidence score beyond the three closed ``HIGH``/``MEDIUM``/
``LOW`` values below. If a future change here starts resembling a
classifier (weighted training, fuzzy matching, an external call) that no
longer belongs in this module — see ``services/mailbox/discovery_signals
.py``'s own identical warning.

``compute_review_priority`` — the exact scoring rubric
------------------------------------------------------------------------
Five independent, bounded contributions are summed into one integer
``score``, then mapped to a label via two fixed thresholds. Every
contribution is listed here in full — this is the entire rubric, not an
excerpt:

1. **Candidate volume** (``candidate_message_count``):
   ``>= 5`` -> **+3**; ``>= 2`` -> **+2**; ``>= 1`` -> **+1**; ``<= 0``
   -> **+0**.

2. **Attachment ratio** (``attachment_bearing_count /
   candidate_message_count``, ``+0`` when ``candidate_message_count <=
   0`` — never divides by zero):
   ``>= 0.5`` -> **+3**; ``> 0`` -> **+1**; ``== 0`` -> **+0**.

3. **Recurrence / span** (``(last_seen_at - first_seen_at).days``):
   ``>= 30`` -> **+2**; otherwise -> **+0** (a single-day cluster earns
   nothing extra here, per the architect's own framing).

4. **Xero correlation class** (``xero_correlation_class``):
   ``STRONG_PURCHASE_BILL`` or ``STRONG_BANK_SPEND`` -> **+4** (real
   accounting-system corroboration, the strongest single signal);
   ``CONTACT_ONLY`` -> **+1**; ``SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW``
   -> **-1** (a real negative contribution — a shared/public domain
   still requires careful manual review, never "hurry up and
   bulk-approve", however much real purchase/bank-spend history sits
   behind it); ``NONE``, ``None``, or any other value -> **+0**.

5. **Discovery-signal diversity** (``discovery_reason_counts``, the
   bucketed counts from :func:`aggregate_discovery_reasons`): 2 or more
   of the five buckets non-zero -> **+1**; otherwise -> **+0** (a small,
   bounded bonus — multiple independent heuristic hits corroborate each
   other more than one repeated hit, but this is deliberately never a
   dominant factor).

``score`` is then mapped to a label:

* ``score >= 8``  -> ``"HIGH"``
* ``4 <= score < 8`` -> ``"MEDIUM"``
* ``score < 4``   -> ``"LOW"``

**The shared-domain cap (enforced, not incidental).** After the label
above is computed, one final, unconditional rule applies: if
``xero_correlation_class == "SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW"`` and
the computed label is ``"HIGH"``, the label is forced down to
``"MEDIUM"``. This is a real, explicit guard clause — not merely an
accident of the chosen weights: the shared-domain penalty above is
deliberately small (``-1``, not a larger number that would make ``HIGH``
arithmetically unreachable on its own), specifically so this cap clause
is the thing actually doing the work and is worth testing directly (see
``tests/integration/test_domain_review_priority.py
::test_shared_domain_with_high_volume_never_scores_high``, which
constructs a case whose PRE-cap score is exactly ``8`` — i.e. would be
``"HIGH"`` without this rule).

Worked examples (hand-verifiable — the docstring's own checked contract;
see the tests above and the exact assertions in
``tests/integration/test_domain_review_priority.py`` for the real,
executed proof)
------------------------------------------------------------------------
**Example A — HIGH.** 12 candidates, 8 with attachments, spanning 60
days, ``STRONG_PURCHASE_BILL`` correlation, discovery reasons hitting 2
distinct buckets (e.g. ``invoice_subject_signal_count=7``,
``accounting_document_attachment_signal_count=5``):

* volume: ``12 >= 5`` -> ``+3``
* attachment ratio: ``8 / 12 = 0.667 >= 0.5`` -> ``+3``
* span: ``60 >= 30`` -> ``+2``
* xero: ``STRONG_PURCHASE_BILL`` -> ``+4``
* diversity: 2 buckets non-zero -> ``+1``
* **score = 3 + 3 + 2 + 4 + 1 = 13** -> ``13 >= 8`` -> **``"HIGH"``**

**Example B — MEDIUM.** 4 candidates, 1 with attachment, spanning 10
days, ``CONTACT_ONLY`` correlation, discovery reasons hitting 2 distinct
buckets (e.g. ``receipt_subject_signal_count=3``,
``other_bounded_heuristic_reason_count=1``):

* volume: ``4 >= 2`` (not ``>= 5``) -> ``+2``
* attachment ratio: ``1 / 4 = 0.25 > 0`` (not ``>= 0.5``) -> ``+1``
* span: ``10 < 30`` -> ``+0``
* xero: ``CONTACT_ONLY`` -> ``+1``
* diversity: 2 buckets non-zero -> ``+1``
* **score = 2 + 1 + 0 + 1 + 1 = 5** -> ``4 <= 5 < 8`` -> **``"MEDIUM"``**

**Example C — LOW.** 1 candidate, no attachment, single day (``first_seen_at
== last_seen_at``), no correlation (``xero_correlation_class=None``),
one discovery-reason bucket only:

* volume: ``1 >= 1`` (not ``>= 2``) -> ``+1``
* attachment ratio: ``0 / 1 = 0`` -> ``+0``
* span: ``0 < 30`` -> ``+0``
* xero: ``None`` -> ``+0``
* diversity: 1 bucket non-zero (not ``>= 2``) -> ``+0``
* **score = 1 + 0 + 0 + 0 + 0 = 1** -> ``1 < 4`` -> **``"LOW"``**

**Example D — shared-domain cap fires.** 20 candidates, 20 with
attachments (ratio ``1.0``), spanning 100 days,
``SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW`` correlation, discovery reasons
hitting 3 distinct buckets:

* volume: ``20 >= 5`` -> ``+3``
* attachment ratio: ``20 / 20 = 1.0 >= 0.5`` -> ``+3``
* span: ``100 >= 30`` -> ``+2``
* xero: ``SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW`` -> ``-1``
* diversity: 3 buckets non-zero -> ``+1``
* pre-cap score = ``3 + 3 + 2 - 1 + 1 = 8`` -> ``8 >= 8`` -> would be
  ``"HIGH"`` ...
* ... but ``xero_correlation_class`` is the shared-domain class, so the
  cap forces the final label down to **``"MEDIUM"``**, never ``"HIGH"``.

``aggregate_discovery_reasons`` — an exhaustive, closed bucketing of
``discovery_reason``
------------------------------------------------------------------------
``services.mailbox.discovery_signals.evaluate_discovery_candidate`` can
only ever produce a message's own ``discovery_reason`` from THREE
f-string templates over its own small, real, enumerable keyword/
content-type sets (``_CANDIDATE_KEYWORDS`` — 12 entries —
and ``_CANDIDATE_ATTACHMENT_CONTENT_TYPES`` — 5 entries):
``f"subject contains keyword {kw!r}"``,
``f"attachment filename contains keyword {kw!r}"``, and
``f"attachment content_type {ct!r} is a common accounting-document
type"``. That is a fully enumerable, closed set of concrete strings
(12 + 12 + 5 = 29 possible non-``None`` values) — this module builds the
COMPLETE lookup table at import time by generating every one of those
29 strings directly from ``discovery_signals``'s own real
``_CANDIDATE_KEYWORDS``/``_CANDIDATE_ATTACHMENT_CONTENT_TYPES`` sets
(never a hand-typed/approximate copy, never fuzzy string matching at
runtime — see :func:`_build_reason_bucket_map`), so this stays
PROVABLY exhaustive against the real heuristic even if that module's own
keyword/content-type sets are extended later (a plain data change there
regenerates a correct, still-exhaustive map here automatically, with no
code change needed in this module).

Each of the 29 concrete reason strings is bucketed into exactly one of
five closed output keys:

* ``invoice_subject_signal_count`` — subject matched ``"invoice"`` or
  ``"tax invoice"``.
* ``receipt_subject_signal_count`` — subject matched ``"receipt"``.
* ``invoice_like_attachment_filename_count`` — attachment filename
  matched ``"invoice"``, ``"tax invoice"``, or ``"receipt"``.
* ``accounting_document_attachment_signal_count`` — attachment
  ``content_type`` matched the accounting-document-type template (any
  of the 5 real content types).
* ``other_bounded_heuristic_reason_count`` — every other real reason
  string this heuristic can produce (``statement``/``payment``/
  ``remittance``/``order confirmation``/``purchase order``/``billing``/
  ``credit note``/``account statement``/``subscription renewal``, via
  either subject or attachment filename), PLUS the defensive catch-all
  for a ``None`` or genuinely unrecognised reason string (never raises —
  see :func:`_bucket_for_reason`).
"""
from __future__ import annotations

from datetime import datetime
from typing import Mapping, Optional, Sequence

from services.mailbox.discovery_signals import _CANDIDATE_ATTACHMENT_CONTENT_TYPES, _CANDIDATE_KEYWORDS
from services.mailbox.message import MailboxMessage

#: The three closed `review_priority` values `compute_review_priority`
#: ever returns — a plain, small, DOCUMENTED string vocabulary, the
#: same "closed-vocabulary-but-plain-string" convention
#: `services/xero/supplier_correlation.py`'s own `CORRELATION_CLASSES`
#: already establishes in this codebase.
PRIORITY_HIGH = "HIGH"
PRIORITY_MEDIUM = "MEDIUM"
PRIORITY_LOW = "LOW"
REVIEW_PRIORITIES = frozenset({PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW})

#: The five closed `aggregate_discovery_reasons` output keys — see
#: module docstring's own "aggregate_discovery_reasons" section.
BUCKET_INVOICE_SUBJECT = "invoice_subject_signal_count"
BUCKET_RECEIPT_SUBJECT = "receipt_subject_signal_count"
BUCKET_INVOICE_LIKE_ATTACHMENT = "invoice_like_attachment_filename_count"
BUCKET_ACCOUNTING_DOCUMENT_ATTACHMENT = "accounting_document_attachment_signal_count"
BUCKET_OTHER = "other_bounded_heuristic_reason_count"

_DISCOVERY_REASON_BUCKETS = (
    BUCKET_INVOICE_SUBJECT,
    BUCKET_RECEIPT_SUBJECT,
    BUCKET_INVOICE_LIKE_ATTACHMENT,
    BUCKET_ACCOUNTING_DOCUMENT_ATTACHMENT,
    BUCKET_OTHER,
)

#: Which of `discovery_signals._CANDIDATE_KEYWORDS`' 12 keywords count
#: as an "invoice-ish"/"receipt-ish" SUBJECT signal specifically (the
#: remaining 9 keywords fall through to `BUCKET_OTHER` for a subject
#: match) — see module docstring.
_INVOICE_SUBJECT_KEYWORDS = frozenset({"invoice", "tax invoice"})
_RECEIPT_SUBJECT_KEYWORDS = frozenset({"receipt"})
#: Which keywords count as an "invoice-like" ATTACHMENT FILENAME signal
#: (a slightly wider set than the subject buckets above — `receipt`
#: folds into the same single attachment-filename bucket here, per the
#: WO's own bucket definition, rather than a separate
#: `receipt_attachment_filename_count` this WO did not ask for).
_INVOICE_LIKE_ATTACHMENT_KEYWORDS = frozenset({"invoice", "tax invoice", "receipt"})


def _build_reason_bucket_map() -> Mapping[str, str]:
    """Generate the COMPLETE, closed `discovery_reason string -> bucket
    key` lookup table directly from `discovery_signals`'s own real
    keyword/content-type sets, at import time — see module docstring's
    own "aggregate_discovery_reasons" section for why this is provably
    exhaustive rather than an approximation. Never called at request
    time; `_REASON_BUCKET_MAP` below is the one, frozen result."""
    mapping: dict[str, str] = {}

    for keyword in _CANDIDATE_KEYWORDS:
        subject_reason = f"subject contains keyword {keyword!r}"
        if keyword in _INVOICE_SUBJECT_KEYWORDS:
            mapping[subject_reason] = BUCKET_INVOICE_SUBJECT
        elif keyword in _RECEIPT_SUBJECT_KEYWORDS:
            mapping[subject_reason] = BUCKET_RECEIPT_SUBJECT
        else:
            mapping[subject_reason] = BUCKET_OTHER

        attachment_filename_reason = f"attachment filename contains keyword {keyword!r}"
        if keyword in _INVOICE_LIKE_ATTACHMENT_KEYWORDS:
            mapping[attachment_filename_reason] = BUCKET_INVOICE_LIKE_ATTACHMENT
        else:
            mapping[attachment_filename_reason] = BUCKET_OTHER

    for content_type in _CANDIDATE_ATTACHMENT_CONTENT_TYPES:
        content_type_reason = f"attachment content_type {content_type!r} is a common accounting-document type"
        mapping[content_type_reason] = BUCKET_ACCOUNTING_DOCUMENT_ATTACHMENT

    return mapping


#: The frozen, exhaustive lookup table — 29 real `discovery_reason`
#: strings (12 subject + 12 attachment-filename + 5 content-type), each
#: mapped to exactly one of the five closed bucket keys above.
_REASON_BUCKET_MAP: Mapping[str, str] = _build_reason_bucket_map()


def _bucket_for_reason(reason: Optional[str]) -> str:
    """`None` (shouldn't happen for a `discovery_candidate is True` row
    per `services/mailbox/message.py`'s own persistence discipline, but
    defended against anyway) and any string this module does not
    recognise both fall into `BUCKET_OTHER` — this function NEVER
    raises on an unexpected input."""
    if reason is None:
        return BUCKET_OTHER
    return _REASON_BUCKET_MAP.get(reason, BUCKET_OTHER)


def aggregate_discovery_reasons(messages: Sequence[MailboxMessage]) -> dict[str, int]:
    """Bucket `messages`' own `discovery_reason` strings into the five
    closed counts described in this module's own docstring. PURE — no
    I/O; `messages` is expected to already be filtered to
    `discovery_candidate == True` by the caller, via the existing
    `MailboxMessageRepository.list_candidate_messages_for_domain` (see
    module docstring) — this function does not itself re-check that
    flag, it only reads `.discovery_reason`.

    Every one of the five keys is always present in the returned dict
    (defaulted to `0`), even for an empty `messages` sequence — a
    caller never has to guard a `KeyError` for a bucket that simply had
    no hits."""
    counts: dict[str, int] = {bucket: 0 for bucket in _DISCOVERY_REASON_BUCKETS}
    for message in messages:
        bucket = _bucket_for_reason(getattr(message, "discovery_reason", None))
        counts[bucket] += 1
    return counts


#: `compute_review_priority`'s own scoring constants — named here
#: (rather than left as inline magic numbers) so the docstring's worked
#: examples and this implementation can never silently drift apart; a
#: reviewer changing one changes the other in the same diff.
_VOLUME_HIGH_COUNT = 5
_VOLUME_MEDIUM_COUNT = 2
_VOLUME_HIGH_POINTS = 3
_VOLUME_MEDIUM_POINTS = 2
_VOLUME_LOW_POINTS = 1

_ATTACHMENT_RATIO_HIGH = 0.5
_ATTACHMENT_RATIO_HIGH_POINTS = 3
_ATTACHMENT_RATIO_ANY_POINTS = 1

_WIDE_SPAN_THRESHOLD_DAYS = 30
_WIDE_SPAN_POINTS = 2

_STRONG_CORRELATION_CLASSES = frozenset({"STRONG_PURCHASE_BILL", "STRONG_BANK_SPEND"})
_STRONG_CORRELATION_POINTS = 4
_CONTACT_ONLY_CORRELATION_CLASS = "CONTACT_ONLY"
_CONTACT_ONLY_CORRELATION_POINTS = 1
_SHARED_DOMAIN_CORRELATION_CLASS = "SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW"
_SHARED_DOMAIN_CORRELATION_POINTS = -1

_DIVERSITY_MIN_DISTINCT_BUCKETS = 2
_DIVERSITY_POINTS = 1

_HIGH_SCORE_THRESHOLD = 8
_MEDIUM_SCORE_THRESHOLD = 4


def compute_review_priority(
    *,
    candidate_message_count: int,
    attachment_bearing_count: int,
    first_seen_at: datetime,
    last_seen_at: datetime,
    xero_correlation_class: Optional[str],
    discovery_reason_counts: Mapping[str, int],
) -> str:
    """Deterministic, bounded, non-AI `"HIGH"`/`"MEDIUM"`/`"LOW"`
    operator-triage priority. See this module's own docstring for the
    exact rubric, the shared-domain cap, and four fully worked numeric
    examples a reviewer can verify by hand."""
    score = 0

    if candidate_message_count >= _VOLUME_HIGH_COUNT:
        score += _VOLUME_HIGH_POINTS
    elif candidate_message_count >= _VOLUME_MEDIUM_COUNT:
        score += _VOLUME_MEDIUM_POINTS
    elif candidate_message_count >= 1:
        score += _VOLUME_LOW_POINTS

    if candidate_message_count > 0:
        attachment_ratio = attachment_bearing_count / candidate_message_count
        if attachment_ratio >= _ATTACHMENT_RATIO_HIGH:
            score += _ATTACHMENT_RATIO_HIGH_POINTS
        elif attachment_ratio > 0:
            score += _ATTACHMENT_RATIO_ANY_POINTS

    span_days = (last_seen_at - first_seen_at).days
    if span_days >= _WIDE_SPAN_THRESHOLD_DAYS:
        score += _WIDE_SPAN_POINTS

    is_shared_domain = xero_correlation_class == _SHARED_DOMAIN_CORRELATION_CLASS
    if xero_correlation_class in _STRONG_CORRELATION_CLASSES:
        score += _STRONG_CORRELATION_POINTS
    elif xero_correlation_class == _CONTACT_ONLY_CORRELATION_CLASS:
        score += _CONTACT_ONLY_CORRELATION_POINTS
    elif is_shared_domain:
        score += _SHARED_DOMAIN_CORRELATION_POINTS
    # NONE / None / any other value: +0.

    distinct_buckets_hit = sum(1 for count in discovery_reason_counts.values() if count > 0)
    if distinct_buckets_hit >= _DIVERSITY_MIN_DISTINCT_BUCKETS:
        score += _DIVERSITY_POINTS

    if score >= _HIGH_SCORE_THRESHOLD:
        priority = PRIORITY_HIGH
    elif score >= _MEDIUM_SCORE_THRESHOLD:
        priority = PRIORITY_MEDIUM
    else:
        priority = PRIORITY_LOW

    # The shared-domain cap — a real, explicit, unconditional guard
    # clause, not an accident of the weights above (see module
    # docstring's own "shared-domain cap" section, and
    # test_shared_domain_with_high_volume_never_scores_high, which
    # constructs a case whose PRE-cap score is exactly the HIGH
    # threshold): a shared/public domain can never be reported HIGH,
    # however much real volume/attachment/correlation evidence exists,
    # because the domain itself still cannot safely represent one
    # supplier — it always still needs careful manual review.
    if is_shared_domain and priority == PRIORITY_HIGH:
        priority = PRIORITY_MEDIUM

    return priority
