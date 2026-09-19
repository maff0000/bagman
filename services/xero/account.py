"""``XeroAccount`` — the synced Chart-of-Accounts projection (CD-6
Slice 2, PID §98.4, architect spec §4).

Idempotency (architect spec §4/§29)
--------------------------------------
Durable external identity is ``(tenant_id, account_id)``. Upserting the
same account twice must never create a second row — see
:meth:`XeroAccountRepository.upsert_account`'s own docstring, and
``persistence/postgres/xero_models.py``'s real unique constraint for
the database-level backstop (the in-memory implementation below mirrors
it with a plain dict keyed on the same tuple, same "application check
first, database constraint is the real proof under a race" discipline
every other repository in this codebase already follows).

History preservation (architect spec §5/§6)
------------------------------------------------
There is no delete method on this repository at all — an account that
Xero reports as archived/deleted is upserted with its new ``status``
like any other field change, never removed. A BAGMAN record that
historically referenced this ``account_id`` must always be able to
resolve it (see ``app/api/routers/xero.py``'s account-detail lookup and
``services.xero.eligibility``'s own "never hide a referenced account"
rule).

Cross-company isolation (architect spec §29 — "must be tested")
------------------------------------------------------------------
Every row carries its own denormalised ``entity_id`` (see the
contract's own field description for why) — every read method here
takes ``entity_id`` as a REQUIRED filter, never an unscoped "all
accounts" query, so it is structurally impossible for a caller scoped
to one company to receive another company's rows by omission.
"""
from __future__ import annotations

import abc
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now

_SCHEMA = "xero/bagman.xero_account.v1.schema.json"

SCHEMA_VERSION = "bagman.xero_account.v1"


@dataclass(frozen=True)
class XeroAccount:
    """One synced Xero Chart-of-Accounts row. Every provider-supplied
    field (``type``/``account_class``/``tax_type``/``status``) is
    captured VERBATIM — never validated against a hand-typed closed
    enum (PID §102.1's own doctrine, see the contract's description)."""

    xero_account_row_id: str
    entity_id: str
    tenant_id: str
    account_id: str
    code: Optional[str]
    name: str
    type: str
    account_class: Optional[str]
    tax_type: Optional[str]
    status: Optional[str]
    show_in_expense_claims: Optional[bool]
    reporting_code: Optional[str]
    reporting_code_name: Optional[str]
    source_updated_date_utc: Optional[datetime]
    first_synced_at: datetime
    last_synced_at: datetime
    last_sync_run_id: str
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Render exactly the shape required by
        ``contracts/xero/bagman.xero_account.v1.schema.json``."""
        return {
            "xero_account_row_id": self.xero_account_row_id,
            "entity_id": self.entity_id,
            "tenant_id": self.tenant_id,
            "account_id": self.account_id,
            "code": self.code,
            "name": self.name,
            "type": self.type,
            "account_class": self.account_class,
            "tax_type": self.tax_type,
            "status": self.status,
            "show_in_expense_claims": self.show_in_expense_claims,
            "reporting_code": self.reporting_code,
            "reporting_code_name": self.reporting_code_name,
            "source_updated_date_utc": (
                to_contract_string(self.source_updated_date_utc)
                if self.source_updated_date_utc is not None
                else None
            ),
            "first_synced_at": to_contract_string(self.first_synced_at),
            "last_synced_at": to_contract_string(self.last_synced_at),
            "last_sync_run_id": self.last_sync_run_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class RawXeroAccount:
    """The plain, provider-shaped fields one ``GET /Accounts`` entry
    yields (``services.xero.client.XeroAccountingClientProtocol``'s own
    return shape) — kept distinct from :class:`XeroAccount` (BAGMAN's
    own canonical projection, which additionally carries `entity_id`/
    `xero_account_row_id`/sync bookkeeping) so the client layer never
    needs to know about BAGMAN identity concerns at all."""

    account_id: str
    code: Optional[str]
    name: str
    type: str
    account_class: Optional[str]
    tax_type: Optional[str]
    status: Optional[str]
    show_in_expense_claims: Optional[bool]
    reporting_code: Optional[str]
    reporting_code_name: Optional[str]
    updated_date_utc: Optional[datetime]


class XeroAccountRepository(abc.ABC):
    """Repository abstraction for XeroAccount."""

    @abc.abstractmethod
    def upsert_account(
        self,
        *,
        entity_id: str,
        tenant_id: str,
        raw: RawXeroAccount,
        sync_run_id: str,
    ) -> XeroAccount:
        """Idempotent create-or-update keyed on ``(tenant_id,
        raw.account_id)`` — a repeated sync of the same account UPDATES
        the existing row (preserving `xero_account_row_id` and
        `first_synced_at`, refreshing every other field plus
        `last_synced_at`/`last_sync_run_id`) rather than creating a
        duplicate. Never called directly by an HTTP handler — only by
        ``services.xero.sync``'s own all-or-nothing sync transaction."""
        raise NotImplementedError

    @abc.abstractmethod
    def upsert_accounts(
        self,
        *,
        entity_id: str,
        tenant_id: str,
        raws: Sequence[RawXeroAccount],
        sync_run_id: str,
    ) -> tuple[int, int]:
        """Upsert every account in `raws` as ONE atomic unit — either
        every row is written or (on any failure partway through) NONE
        of them is (architect spec §5/§20's 'never replace last-known-
        good data with an empty/partial set', see
        `services.xero.sync`'s own module docstring for the full
        reasoning). The real Postgres implementation wraps this in a
        single database transaction (`persistence.postgres.session
        .session_scope`); this in-memory reference implementation
        achieves the same effective guarantee for a single-process test
        context by staging every candidate row in a local dict and only
        publishing the whole batch into this repository's own state
        once every row has been validated successfully.

        Returns `(created_count, updated_count)`.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_account(self, xero_account_row_id: str) -> XeroAccount:
        raise NotImplementedError

    @abc.abstractmethod
    def get_by_external_id(self, *, tenant_id: str, account_id: str) -> Optional[XeroAccount]:
        raise NotImplementedError

    @abc.abstractmethod
    def list_accounts(self, *, entity_id: str) -> list[XeroAccount]:
        """Every synced account for `entity_id` — REQUIRED filter (see
        module docstring's "Cross-company isolation" section); there is
        no unscoped "every account across every company" query on this
        repository at all."""
        raise NotImplementedError


class InMemoryXeroAccountRepository(XeroAccountRepository):
    """Narrow in-memory reference implementation (PID §21 Option A)."""

    def __init__(self) -> None:
        self._by_id: dict[str, XeroAccount] = {}
        self._id_by_external: dict[tuple[str, str], str] = {}

    def _staged_upsert(
        self,
        by_id: dict[str, XeroAccount],
        id_by_external: dict[tuple[str, str], str],
        *,
        entity_id: str,
        tenant_id: str,
        raw: RawXeroAccount,
        sync_run_id: str,
        now: datetime,
    ) -> tuple[XeroAccount, bool]:
        """Pure staging helper: computes the row `raw` upserts to
        WITHOUT touching `self._by_id`/`self._id_by_external` — the
        caller (`upsert_accounts`) applies every staged result to the
        real dicts only once every row in a batch has been computed
        successfully, which is what gives this in-memory implementation
        its all-or-nothing guarantee (see `upsert_accounts`'s own
        docstring). Returns `(row, is_new)`."""
        key = (tenant_id, raw.account_id)
        existing_id = id_by_external.get(key)

        if existing_id is not None:
            current = by_id[existing_id]
            try:
                updated = dataclasses.replace(
                    current,
                    entity_id=entity_id,
                    code=raw.code,
                    name=raw.name,
                    type=raw.type,
                    account_class=raw.account_class,
                    tax_type=raw.tax_type,
                    status=raw.status,
                    show_in_expense_claims=raw.show_in_expense_claims,
                    reporting_code=raw.reporting_code,
                    reporting_code_name=raw.reporting_code_name,
                    source_updated_date_utc=raw.updated_date_utc,
                    last_synced_at=now,
                    last_sync_run_id=sync_run_id,
                )
                validate_against_contract(updated.to_dict(), _SCHEMA)
            except ValidationError:
                raise
            except Exception as exc:  # noqa: BLE001 - never leak a raw exception
                raise ValidationError(f"could not update XeroAccount: {exc}") from exc
            return updated, False

        try:
            candidate = XeroAccount(
                xero_account_row_id=identity.generate_id(),
                entity_id=entity_id,
                tenant_id=tenant_id,
                account_id=raw.account_id,
                code=raw.code,
                name=raw.name,
                type=raw.type,
                account_class=raw.account_class,
                tax_type=raw.tax_type,
                status=raw.status,
                show_in_expense_claims=raw.show_in_expense_claims,
                reporting_code=raw.reporting_code,
                reporting_code_name=raw.reporting_code_name,
                source_updated_date_utc=raw.updated_date_utc,
                first_synced_at=now,
                last_synced_at=now,
                last_sync_run_id=sync_run_id,
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create XeroAccount: {exc}") from exc
        return candidate, True

    def upsert_account(
        self,
        *,
        entity_id: str,
        tenant_id: str,
        raw: RawXeroAccount,
        sync_run_id: str,
    ) -> XeroAccount:
        created, _updated = self.upsert_accounts(
            entity_id=entity_id, tenant_id=tenant_id, raws=[raw], sync_run_id=sync_run_id
        )
        return self.get_by_external_id(tenant_id=tenant_id, account_id=raw.account_id)  # type: ignore[return-value]

    def upsert_accounts(
        self,
        *,
        entity_id: str,
        tenant_id: str,
        raws: Sequence[RawXeroAccount],
        sync_run_id: str,
    ) -> tuple[int, int]:
        now = utc_now()
        # Stage against COPIES of the real dicts — a failure partway
        # through never touches `self._by_id`/`self._id_by_external`
        # (see this method's own ABC docstring for the atomicity this
        # gives, mirroring the real Postgres transaction).
        staged_by_id = dict(self._by_id)
        staged_id_by_external = dict(self._id_by_external)
        created = 0
        updated = 0

        for raw in raws:
            row, is_new = self._staged_upsert(
                staged_by_id,
                staged_id_by_external,
                entity_id=entity_id,
                tenant_id=tenant_id,
                raw=raw,
                sync_run_id=sync_run_id,
                now=now,
            )
            staged_by_id[row.xero_account_row_id] = row
            staged_id_by_external[(tenant_id, raw.account_id)] = row.xero_account_row_id
            if is_new:
                created += 1
            else:
                updated += 1

        # Every row staged successfully — publish the whole batch.
        self._by_id = staged_by_id
        self._id_by_external = staged_id_by_external
        return created, updated

    def get_account(self, xero_account_row_id: str) -> XeroAccount:
        try:
            return self._by_id[xero_account_row_id]
        except KeyError:
            raise NotFoundError(f"no XeroAccount with xero_account_row_id '{xero_account_row_id}'") from None

    def get_by_external_id(self, *, tenant_id: str, account_id: str) -> Optional[XeroAccount]:
        existing_id = self._id_by_external.get((tenant_id, account_id))
        return self._by_id[existing_id] if existing_id is not None else None

    def list_accounts(self, *, entity_id: str) -> list[XeroAccount]:
        return sorted(
            (a for a in self._by_id.values() if a.entity_id == entity_id),
            key=lambda a: (a.code or "", a.name),
        )
