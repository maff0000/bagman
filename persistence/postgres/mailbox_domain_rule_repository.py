"""PostgreSQL-backed implementation of
``services.mailbox.domain_rule.MailboxDomainRuleRepository`` (CD-6
architect amendment; extended by the CD-6 GUI-operations-foundation
follow-on WO — three-state policy model + EXACT_ADDRESS match mode).
Mirrors ``persistence/postgres/mailbox_repository.py``'s own "stateless,
fresh Session per call, with_for_update row-locking, never let a raw
SQLAlchemy exception escape" discipline.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import ConflictError, NotFoundError, PersistenceError, ValidationError
from core.timestamps import utc_now
from persistence.postgres.mailbox_domain_rule_models import MailboxDomainRuleRow
from persistence.postgres.session import get_engine, session_scope
from services.mailbox.domain_rule import (
    MATCH_MODE_EXACT_ADDRESS,
    MATCH_MODE_EXACT_DOMAIN_SUBJECT,
    MATCH_MODE_INCLUDE_SUBDOMAINS,
    SUBJECT_PREDICATE_EXACT,
    SUBJECT_PREDICATE_STARTS_WITH,
    MailboxDomainRule,
    MailboxDomainRuleRepository,
    domain_from_address,
    normalize_address,
    normalize_domain,
    normalize_subject_for_policy,
    subject_matches_predicate,
    validate_match_fields_or_raise,
    validate_policy_fields_or_raise,
)

_SCHEMA = "mailbox/bagman.mailbox_domain_rule.v1.schema.json"


def _row_to_domain(row: MailboxDomainRuleRow) -> MailboxDomainRule:
    return MailboxDomainRule(
        rule_id=row.rule_id,
        mailbox_id=row.mailbox_id,
        sender_domain=row.sender_domain,
        sender_address=row.sender_address,
        subject_predicate_type=row.subject_predicate_type,
        subject_predicate_value=row.subject_predicate_value,
        match_mode=row.match_mode,
        policy=row.policy,
        destination_entity_id=row.destination_entity_id,
        destination_mode=row.destination_mode,
        source=row.source,
        processor_hint=row.processor_hint,
        approved_at=row.approved_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_seen_at=row.last_seen_at,
    )


class PostgresMailboxDomainRuleRepository(MailboxDomainRuleRepository):
    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def upsert_rule(
        self,
        *,
        mailbox_id: str,
        sender_domain: str,
        match_mode: str,
        policy: str,
        destination_entity_id: Optional[str],
        destination_mode: Optional[str],
        source: str,
        processor_hint: Optional[str] = None,
        approved_at=None,
        sender_address: Optional[str] = None,
        subject_predicate_type: Optional[str] = None,
        subject_predicate_value: Optional[str] = None,
    ) -> MailboxDomainRule:
        validate_match_fields_or_raise(
            match_mode=match_mode, sender_address=sender_address,
            subject_predicate_type=subject_predicate_type, subject_predicate_value=subject_predicate_value,
        )
        validate_policy_fields_or_raise(
            policy=policy, destination_entity_id=destination_entity_id, destination_mode=destination_mode,
            match_mode=match_mode,
        )

        is_address_rule = match_mode == MATCH_MODE_EXACT_ADDRESS
        is_subject_rule = match_mode == MATCH_MODE_EXACT_DOMAIN_SUBJECT
        if is_address_rule:
            normalized_address = normalize_address(sender_address)
            derived_domain = domain_from_address(normalized_address)
            if sender_domain and normalize_domain(sender_domain) != derived_domain:
                raise ValidationError(
                    f"sender_domain '{sender_domain}' does not match the domain of sender_address "
                    f"'{sender_address}' ('{derived_domain}')"
                )
            normalized_domain = derived_domain
            normalized_subject_type = None
            normalized_subject_value = None
        elif is_subject_rule:
            normalized_address = None
            normalized_domain = normalize_domain(sender_domain)
            normalized_subject_type = subject_predicate_type
            normalized_subject_value = normalize_subject_for_policy(subject_predicate_value)
        else:
            normalized_address = None
            normalized_domain = normalize_domain(sender_domain)
            normalized_subject_type = None
            normalized_subject_value = None

        now = utc_now()

        try:
            with session_scope(self._engine) as session:
                query = session.query(MailboxDomainRuleRow).filter_by(mailbox_id=mailbox_id)
                if is_address_rule:
                    row = (
                        query.filter_by(sender_address=normalized_address, match_mode=MATCH_MODE_EXACT_ADDRESS)
                        .with_for_update()
                        .one_or_none()
                    )
                elif is_subject_rule:
                    row = (
                        query.filter_by(
                            sender_domain=normalized_domain,
                            match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
                            subject_predicate_type=normalized_subject_type,
                            subject_predicate_value=normalized_subject_value,
                        )
                        .with_for_update()
                        .one_or_none()
                    )
                else:
                    row = (
                        query.filter_by(sender_domain=normalized_domain)
                        .filter(
                            MailboxDomainRuleRow.match_mode.notin_(
                                [MATCH_MODE_EXACT_ADDRESS, MATCH_MODE_EXACT_DOMAIN_SUBJECT]
                            )
                        )
                        .with_for_update()
                        .one_or_none()
                    )

                if row is None:
                    candidate = MailboxDomainRule(
                        rule_id=identity.generate_id(),
                        mailbox_id=mailbox_id,
                        sender_domain=normalized_domain,
                        sender_address=normalized_address,
                        subject_predicate_type=normalized_subject_type,
                        subject_predicate_value=normalized_subject_value,
                        match_mode=match_mode,
                        policy=policy,
                        destination_entity_id=destination_entity_id,
                        destination_mode=destination_mode,
                        source=source,
                        processor_hint=processor_hint,
                        approved_at=approved_at,
                        created_at=now,
                        updated_at=now,
                        last_seen_at=now,
                    )
                    validate_against_contract(candidate.to_dict(), _SCHEMA)
                    session.add(
                        MailboxDomainRuleRow(
                            rule_id=candidate.rule_id,
                            mailbox_id=candidate.mailbox_id,
                            sender_domain=candidate.sender_domain,
                            sender_address=candidate.sender_address,
                            subject_predicate_type=candidate.subject_predicate_type,
                            subject_predicate_value=candidate.subject_predicate_value,
                            match_mode=candidate.match_mode,
                            policy=candidate.policy,
                            destination_entity_id=candidate.destination_entity_id,
                            destination_mode=candidate.destination_mode,
                            source=candidate.source,
                            processor_hint=candidate.processor_hint,
                            approved_at=candidate.approved_at,
                            created_at=candidate.created_at,
                            updated_at=candidate.updated_at,
                            last_seen_at=candidate.last_seen_at,
                        )
                    )
                    return candidate

                current = _row_to_domain(row)
                updated = MailboxDomainRule(
                    rule_id=current.rule_id,
                    mailbox_id=current.mailbox_id,
                    sender_domain=normalized_domain,
                    sender_address=normalized_address,
                    subject_predicate_type=normalized_subject_type,
                    subject_predicate_value=normalized_subject_value,
                    match_mode=match_mode,
                    policy=policy,
                    destination_entity_id=destination_entity_id,
                    destination_mode=destination_mode,
                    source=source,
                    processor_hint=processor_hint,
                    approved_at=approved_at if approved_at is not None else current.approved_at,
                    created_at=current.created_at,
                    updated_at=now,
                    last_seen_at=current.last_seen_at,
                )
                validate_against_contract(updated.to_dict(), _SCHEMA)
                row.sender_domain = updated.sender_domain
                row.sender_address = updated.sender_address
                row.subject_predicate_type = updated.subject_predicate_type
                row.subject_predicate_value = updated.subject_predicate_value
                row.match_mode = updated.match_mode
                row.policy = updated.policy
                row.destination_entity_id = updated.destination_entity_id
                row.destination_mode = updated.destination_mode
                row.source = updated.source
                row.processor_hint = updated.processor_hint
                row.approved_at = updated.approved_at
                row.updated_at = updated.updated_at
                return updated
        except (ValidationError, NotFoundError):
            raise
        except IntegrityError as exc:
            raise PersistenceError(f"could not upsert MailboxDomainRule: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not upsert MailboxDomainRule: {exc}") from exc

    def get_rule(self, rule_id: str) -> MailboxDomainRule:
        try:
            with session_scope(self._engine) as session:
                row = session.get(MailboxDomainRuleRow, rule_id)
                if row is None:
                    raise NotFoundError(f"no MailboxDomainRule with rule_id '{rule_id}'")
                return _row_to_domain(row)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read MailboxDomainRule: {exc}") from exc

    def find_for_sender(
        self,
        *,
        mailbox_id: str,
        sender_domain: str,
        sender_address: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> Optional[MailboxDomainRule]:
        normalized_domain = normalize_domain(sender_domain)
        normalized_subject = normalize_subject_for_policy(subject)
        try:
            with session_scope(self._engine) as session:
                # Tier 1 — EXACT_ADDRESS (subject-independent, always
                # wins if present).
                if sender_address:
                    normalized_address = normalize_address(sender_address)
                    address_row = (
                        session.query(MailboxDomainRuleRow)
                        .filter_by(
                            mailbox_id=mailbox_id,
                            sender_address=normalized_address,
                            match_mode=MATCH_MODE_EXACT_ADDRESS,
                        )
                        .one_or_none()
                    )
                    if address_row is not None:
                        return _row_to_domain(address_row)

                # Tier 2 — EXACT_DOMAIN_SUBJECT at this EXACT domain.
                if normalized_subject:
                    subject_rows = (
                        session.query(MailboxDomainRuleRow)
                        .filter_by(
                            mailbox_id=mailbox_id,
                            sender_domain=normalized_domain,
                            match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
                        )
                        .all()
                    )
                    matching = [
                        row
                        for row in subject_rows
                        if subject_matches_predicate(
                            subject, predicate_type=row.subject_predicate_type, predicate_value=row.subject_predicate_value
                        )
                    ]
                    if matching:
                        exact_matches = [r for r in matching if r.subject_predicate_type == SUBJECT_PREDICATE_EXACT]
                        if exact_matches:
                            if len(exact_matches) > 1:
                                raise ConflictError(
                                    f"ambiguous EXACT_DOMAIN_SUBJECT resolution for mailbox '{mailbox_id}' domain "
                                    f"'{normalized_domain}': {len(exact_matches)} EXACT predicate rules all match "
                                    "this subject — refusing to silently pick one (this indicates data corruption "
                                    "bypassing the normal validated write path)"
                                )
                            return _row_to_domain(exact_matches[0])

                        starts_with_matches = [
                            r for r in matching if r.subject_predicate_type == SUBJECT_PREDICATE_STARTS_WITH
                        ]
                        max_len = max(len(r.subject_predicate_value) for r in starts_with_matches)
                        longest = [r for r in starts_with_matches if len(r.subject_predicate_value) == max_len]
                        if len(longest) > 1:
                            raise ConflictError(
                                f"ambiguous EXACT_DOMAIN_SUBJECT resolution for mailbox '{mailbox_id}' domain "
                                f"'{normalized_domain}': {len(longest)} STARTS_WITH predicate rules of equal, "
                                f"longest-matching prefix length {max_len} all match this subject — refusing to "
                                "silently pick one (this indicates data corruption bypassing the normal "
                                "validated write path)"
                            )
                        return _row_to_domain(longest[0])

                # Tier 3 — direct domain-level fallback (EXACT or
                # INCLUDE_SUBDOMAINS) at this exact domain.
                row = (
                    session.query(MailboxDomainRuleRow)
                    .filter_by(mailbox_id=mailbox_id, sender_domain=normalized_domain)
                    .filter(
                        MailboxDomainRuleRow.match_mode.notin_(
                            [MATCH_MODE_EXACT_ADDRESS, MATCH_MODE_EXACT_DOMAIN_SUBJECT]
                        )
                    )
                    .one_or_none()
                )
                if row is not None:
                    return _row_to_domain(row)

                # Tier 4 — parent INCLUDE_SUBDOMAINS rule.
                candidates = (
                    session.query(MailboxDomainRuleRow)
                    .filter_by(mailbox_id=mailbox_id, match_mode=MATCH_MODE_INCLUDE_SUBDOMAINS)
                    .all()
                )
                for candidate_row in candidates:
                    if normalized_domain.endswith(f".{candidate_row.sender_domain}"):
                        return _row_to_domain(candidate_row)
                return None
        except ConflictError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up MailboxDomainRule: {exc}") from exc

    def find_exact(
        self,
        *,
        mailbox_id: str,
        sender_domain: str,
        match_mode: str,
        sender_address: Optional[str] = None,
        subject_predicate_type: Optional[str] = None,
        subject_predicate_value: Optional[str] = None,
    ) -> Optional[MailboxDomainRule]:
        if match_mode == MATCH_MODE_EXACT_ADDRESS and not sender_address:
            raise ValidationError(f"sender_address is required when match_mode is '{MATCH_MODE_EXACT_ADDRESS}'")
        if match_mode == MATCH_MODE_EXACT_DOMAIN_SUBJECT and not (subject_predicate_type and subject_predicate_value):
            raise ValidationError(
                "subject_predicate_type and subject_predicate_value are both required when match_mode is "
                f"'{MATCH_MODE_EXACT_DOMAIN_SUBJECT}'"
            )
        try:
            with session_scope(self._engine) as session:
                if match_mode == MATCH_MODE_EXACT_ADDRESS:
                    normalized_address = normalize_address(sender_address)
                    row = (
                        session.query(MailboxDomainRuleRow)
                        .filter_by(
                            mailbox_id=mailbox_id,
                            sender_address=normalized_address,
                            match_mode=MATCH_MODE_EXACT_ADDRESS,
                        )
                        .one_or_none()
                    )
                elif match_mode == MATCH_MODE_EXACT_DOMAIN_SUBJECT:
                    normalized_domain = normalize_domain(sender_domain)
                    normalized_value = normalize_subject_for_policy(subject_predicate_value)
                    row = (
                        session.query(MailboxDomainRuleRow)
                        .filter_by(
                            mailbox_id=mailbox_id,
                            sender_domain=normalized_domain,
                            match_mode=MATCH_MODE_EXACT_DOMAIN_SUBJECT,
                            subject_predicate_type=subject_predicate_type,
                            subject_predicate_value=normalized_value,
                        )
                        .one_or_none()
                    )
                else:
                    normalized_domain = normalize_domain(sender_domain)
                    row = (
                        session.query(MailboxDomainRuleRow)
                        .filter_by(mailbox_id=mailbox_id, sender_domain=normalized_domain)
                        .filter(
                            MailboxDomainRuleRow.match_mode.notin_(
                                [MATCH_MODE_EXACT_ADDRESS, MATCH_MODE_EXACT_DOMAIN_SUBJECT]
                            )
                        )
                        .one_or_none()
                    )
                return _row_to_domain(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up MailboxDomainRule at exact identity: {exc}") from exc

    def touch_last_seen(
        self,
        *,
        mailbox_id: str,
        sender_domain: str,
        seen_at,
        sender_address: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> MailboxDomainRule:
        rule = self.find_for_sender(
            mailbox_id=mailbox_id, sender_domain=sender_domain, sender_address=sender_address, subject=subject
        )
        if rule is None:
            raise NotFoundError(
                f"no MailboxDomainRule governs mailbox_id={mailbox_id!r} sender_domain={sender_domain!r}"
            )
        try:
            with session_scope(self._engine) as session:
                row = session.get(MailboxDomainRuleRow, rule.rule_id)
                if row is None:
                    raise NotFoundError(f"no MailboxDomainRule with rule_id '{rule.rule_id}'")
                row.last_seen_at = seen_at
                return _row_to_domain(row)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not touch MailboxDomainRule.last_seen_at: {exc}") from exc

    def list_rules(self, *, mailbox_id: str) -> list[MailboxDomainRule]:
        try:
            with session_scope(self._engine) as session:
                rows = (
                    session.query(MailboxDomainRuleRow)
                    .filter_by(mailbox_id=mailbox_id)
                    .order_by(MailboxDomainRuleRow.sender_domain, MailboxDomainRuleRow.rule_id)
                    .all()
                )
                return [_row_to_domain(r) for r in rows]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list MailboxDomainRule rows: {exc}") from exc
