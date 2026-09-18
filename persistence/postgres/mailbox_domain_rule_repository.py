"""PostgreSQL-backed implementation of
``services.mailbox.domain_rule.MailboxDomainRuleRepository`` (CD-6
architect amendment). Mirrors ``persistence/postgres/mailbox_repository.py``'s
own "stateless, fresh Session per call, with_for_update row-locking,
never let a raw SQLAlchemy exception escape" discipline.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import NotFoundError, PersistenceError, ValidationError
from core.timestamps import utc_now
from persistence.postgres.mailbox_domain_rule_models import MailboxDomainRuleRow
from persistence.postgres.session import get_engine, session_scope
from services.mailbox.domain_rule import (
    MATCH_MODE_INCLUDE_SUBDOMAINS,
    MATCH_MODES,
    MailboxDomainRule,
    MailboxDomainRuleRepository,
    normalize_domain,
    validate_policy_fields_or_raise,
)

_SCHEMA = "mailbox/bagman.mailbox_domain_rule.v1.schema.json"


def _row_to_domain(row: MailboxDomainRuleRow) -> MailboxDomainRule:
    return MailboxDomainRule(
        rule_id=row.rule_id,
        mailbox_id=row.mailbox_id,
        sender_domain=row.sender_domain,
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
    ) -> MailboxDomainRule:
        if match_mode not in MATCH_MODES:
            raise ValidationError(f"'{match_mode}' is not a governed match_mode — must be one of {sorted(MATCH_MODES)}")
        validate_policy_fields_or_raise(
            policy=policy, destination_entity_id=destination_entity_id, destination_mode=destination_mode
        )
        normalized_domain = normalize_domain(sender_domain)
        now = utc_now()

        try:
            with session_scope(self._engine) as session:
                row = (
                    session.query(MailboxDomainRuleRow)
                    .filter_by(mailbox_id=mailbox_id, sender_domain=normalized_domain)
                    .with_for_update()
                    .one_or_none()
                )
                if row is None:
                    candidate = MailboxDomainRule(
                        rule_id=identity.generate_id(),
                        mailbox_id=mailbox_id,
                        sender_domain=normalized_domain,
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
                    sender_domain=current.sender_domain,
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

    def find_for_sender(self, *, mailbox_id: str, sender_domain: str) -> Optional[MailboxDomainRule]:
        normalized_domain = normalize_domain(sender_domain)
        try:
            with session_scope(self._engine) as session:
                row = (
                    session.query(MailboxDomainRuleRow)
                    .filter_by(mailbox_id=mailbox_id, sender_domain=normalized_domain)
                    .one_or_none()
                )
                if row is not None:
                    return _row_to_domain(row)

                candidates = (
                    session.query(MailboxDomainRuleRow)
                    .filter_by(mailbox_id=mailbox_id, match_mode=MATCH_MODE_INCLUDE_SUBDOMAINS)
                    .all()
                )
                for candidate_row in candidates:
                    if normalized_domain.endswith(f".{candidate_row.sender_domain}"):
                        return _row_to_domain(candidate_row)
                return None
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up MailboxDomainRule: {exc}") from exc

    def touch_last_seen(self, *, mailbox_id: str, sender_domain: str, seen_at) -> MailboxDomainRule:
        rule = self.find_for_sender(mailbox_id=mailbox_id, sender_domain=sender_domain)
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
