"""``MicrosoftGraphMailboxAdapter`` — the ONE place token-lifecycle
orchestration (refresh-on-expiry, reactive-refresh-on-401, connection-
state side effects) and Microsoft-Graph-specific request shaping live
(CD-6 Slice 4).

Provider adapter vs. provider-neutral orchestration — the seam
--------------------------------------------------------------------------
``services/mailbox/sweep.py`` (the provider-neutral sweep engine) knows
nothing about OAuth tokens, Microsoft Graph URLs, or the
``Prefer: IdType="ImmutableId"`` header — it only calls this adapter's
three methods (:meth:`fetch_folder_delta`, :meth:`fetch_message_content`,
plus the connection lifecycle methods below) and reacts to their
outcome. This IS the "provider-neutral sweep orchestration / Microsoft
Graph adapter" separation the architect spec names explicitly — no
Microsoft-specific logic exists anywhere in ``services/mailbox/mailbox.py``
or ``services/mailbox/sweep.py``.

Token refresh discipline — mirrors ``services.xero.sync.run_sync``
--------------------------------------------------------------------------
* Pre-emptive refresh: if the stored access token is within
  :data:`_REFRESH_SKEW_SECONDS` of ``expires_at``, refresh BEFORE the
  first request of a sweep.
* Reactive refresh: if a request still comes back ``AUTH_ERROR``
  despite a token this adapter believed was fresh, refresh once and
  retry the SAME request once — never more than once (bounded retry).
* A refresh that itself fails (`GraphOutcomeStatus` anything but `OK`)
  means Microsoft has revoked/expired consent — this adapter marks the
  mailbox ``connection_state -> AUTH_REQUIRED`` via
  ``services.mailbox.mailbox.MailboxSourceRepository
  .mark_microsoft_auth_required`` (architect spec's own explicit
  "On refresh failure (revoked consent): connection_state becomes
  AUTH_REQUIRED" — NOT `ERROR`; see that method's own docstring for the
  distinction). No tight retry loop: a caller sees this outcome and
  stops for THIS sweep attempt entirely — the next sweep attempt (after
  an operator reconnects) starts fresh.
* A 403 (`PERMISSION_ERROR`) or a genuinely unexpected provider fault
  outside a single sweep's own per-message handling instead marks the
  mailbox ``connection_state -> ERROR`` (see
  :meth:`report_connection_error`) — refreshing would not fix a
  permission problem.

Identity verification — the callback-time server-side check
--------------------------------------------------------------------------
:meth:`verify_identity_and_connect` is the ONE place a completed OAuth
callback is allowed to mark a mailbox ``CONNECTED`` — it always calls
Graph's own ``/me`` endpoint (never trusts the callback's own query
string/login_hint) and compares the real ``mail``/``userPrincipalName``
claim (case-insensitively) against the mailbox's own stored
``email_address`` (architect spec, verbatim). A mismatch fails
honestly, persists no usable token, and never mutates the mailbox's
connection_state — see that method's own docstring.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core.timestamps import utc_now
from services.mailbox.mailbox import MailboxSourceRepository
from services.mailbox.microsoft.graph_client import (
    GraphDeltaPageResult,
    GraphMessageContentResult,
    GraphOutcomeStatus,
    MicrosoftGraphClientProtocol,
    MicrosoftOAuthClientProtocol,
)
from services.mailbox.microsoft.secrets import MicrosoftTokenStoreProtocol

#: Same safety-margin reasoning as `services.xero.sync._REFRESH_SKEW_SECONDS`.
_REFRESH_SKEW_SECONDS = 120.0


@dataclass(frozen=True)
class IdentityVerificationResult:
    ok: bool
    reason: str  # one of a small closed set — see verify_identity_and_connect


class MicrosoftGraphMailboxAdapter:
    def __init__(
        self,
        *,
        oauth_client: MicrosoftOAuthClientProtocol,
        graph_client: MicrosoftGraphClientProtocol,
        token_store: MicrosoftTokenStoreProtocol,
        mailbox_repository: MailboxSourceRepository,
    ) -> None:
        self._oauth_client = oauth_client
        self._graph_client = graph_client
        self._token_store = token_store
        self._mailbox_repository = mailbox_repository

    # -- token lifecycle -------------------------------------------------

    def _ensure_fresh_access_token(self, mailbox_id: str) -> tuple[Optional[str], Optional[str]]:
        """Returns (access_token, error_detail) — exactly one is
        non-None. On a refresh failure, marks the mailbox
        AUTH_REQUIRED as a side effect (see module docstring)."""
        tokens = self._token_store.read(mailbox_id)
        if tokens is None:
            return None, "no stored Microsoft OAuth tokens for this mailbox — it must be (re)connected"

        now = utc_now()
        if (tokens.expires_at - now).total_seconds() > _REFRESH_SKEW_SECONDS:
            return tokens.access_token, None

        refreshed = self._oauth_client.refresh(refresh_token=tokens.refresh_token)
        if refreshed.status != GraphOutcomeStatus.OK or refreshed.tokens is None:
            detail = refreshed.error_detail or f"token refresh failed ({refreshed.status.value})"
            self._mailbox_repository.mark_microsoft_auth_required(mailbox_id, error_detail=detail)
            return None, detail

        self._token_store.write(
            mailbox_id,
            access_token=refreshed.tokens.access_token,
            refresh_token=refreshed.tokens.refresh_token,
            expires_at=refreshed.tokens.expires_at,
        )
        return refreshed.tokens.access_token, None

    def _reactive_refresh(self, mailbox_id: str) -> tuple[Optional[str], Optional[str]]:
        """A request came back AUTH_ERROR despite what this adapter
        believed was a fresh token — refresh once (reactive path,
        mirrors `services.xero.sync.run_sync`'s own identical
        reasoning) and return the new token, or the failure detail."""
        tokens = self._token_store.read(mailbox_id)
        refresh_token = tokens.refresh_token if tokens is not None else None
        if refresh_token is None:
            detail = "no stored refresh_token to attempt a reactive refresh with"
            self._mailbox_repository.mark_microsoft_auth_required(mailbox_id, error_detail=detail)
            return None, detail

        refreshed = self._oauth_client.refresh(refresh_token=refresh_token)
        if refreshed.status != GraphOutcomeStatus.OK or refreshed.tokens is None:
            detail = refreshed.error_detail or f"reactive token refresh failed ({refreshed.status.value})"
            self._mailbox_repository.mark_microsoft_auth_required(mailbox_id, error_detail=detail)
            return None, detail

        self._token_store.write(
            mailbox_id,
            access_token=refreshed.tokens.access_token,
            refresh_token=refreshed.tokens.refresh_token,
            expires_at=refreshed.tokens.expires_at,
        )
        return refreshed.tokens.access_token, None

    def report_connection_error(self, mailbox_id: str, *, error_code: str, error_detail: str) -> None:
        """A genuine, non-auth provider fault (403, or an unexpected
        condition outside single-message handling) — see module
        docstring for why this is ERROR, not AUTH_REQUIRED."""
        self._mailbox_repository.mark_microsoft_connection_error(
            mailbox_id, error_code=error_code, error_detail=error_detail
        )

    # -- delta / content fetch, with bounded 401-retry-once --------------

    def fetch_folder_delta(
        self,
        *,
        mailbox_id: str,
        folder: str,
        delta_link: Optional[str] = None,
        next_link: Optional[str] = None,
        bootstrap_timestamp: Optional[datetime] = None,
    ) -> GraphDeltaPageResult:
        access_token, error_detail = self._ensure_fresh_access_token(mailbox_id)
        if access_token is None:
            return GraphDeltaPageResult(status=GraphOutcomeStatus.AUTH_ERROR, error_detail=error_detail)

        result = self._graph_client.fetch_delta(
            access_token=access_token,
            folder=folder,
            delta_link=delta_link,
            next_link=next_link,
            bootstrap_timestamp=bootstrap_timestamp,
        )
        if result.status != GraphOutcomeStatus.AUTH_ERROR:
            return result

        retried_token, retry_error = self._reactive_refresh(mailbox_id)
        if retried_token is None:
            return GraphDeltaPageResult(status=GraphOutcomeStatus.AUTH_ERROR, error_detail=retry_error)
        return self._graph_client.fetch_delta(
            access_token=retried_token,
            folder=folder,
            delta_link=delta_link,
            next_link=next_link,
            bootstrap_timestamp=bootstrap_timestamp,
        )

    def fetch_message_content(self, *, mailbox_id: str, immutable_message_id: str) -> GraphMessageContentResult:
        access_token, error_detail = self._ensure_fresh_access_token(mailbox_id)
        if access_token is None:
            return GraphMessageContentResult(status=GraphOutcomeStatus.AUTH_ERROR, error_detail=error_detail)

        result = self._graph_client.fetch_message_content(
            access_token=access_token, immutable_message_id=immutable_message_id
        )
        if result.status != GraphOutcomeStatus.AUTH_ERROR:
            return result

        retried_token, retry_error = self._reactive_refresh(mailbox_id)
        if retried_token is None:
            return GraphMessageContentResult(status=GraphOutcomeStatus.AUTH_ERROR, error_detail=retry_error)
        return self._graph_client.fetch_message_content(
            access_token=retried_token, immutable_message_id=immutable_message_id
        )

    # -- OAuth begin / callback identity verification ---------------------

    def build_authorize_url(self, *, state: str, redirect_uri: str) -> str:
        return self._oauth_client.build_authorize_url(state=state, redirect_uri=redirect_uri)

    def is_configured(self) -> bool:
        return self._oauth_client.is_configured()

    def exchange_code_and_verify_identity(
        self, *, mailbox_id: str, expected_email_address: str, code: str, redirect_uri: str
    ):
        """The full callback-time sequence: exchange the authorization
        code, then verify the real, server-fetched Microsoft identity
        against `expected_email_address` — architect spec's explicit
        "never trust login_hint/browser-supplied address; compare the
        real verified mail/userPrincipalName claim". Returns a
        dataclass the router renders into an honest outcome; NEVER
        writes a token to the token store, and NEVER changes
        connection_state, unless identity verification actually passes
        — see this method's own return type for the exact outcome
        space.
        """
        token_result = self._oauth_client.exchange_code(code=code, redirect_uri=redirect_uri)
        if token_result.status != GraphOutcomeStatus.OK or token_result.tokens is None:
            return _CallbackOutcome(
                ok=False,
                reason="token_exchange_failed",
                detail=token_result.error_detail or token_result.status.value,
            )

        identity_result = self._oauth_client.get_me(access_token=token_result.tokens.access_token)
        if identity_result.status != GraphOutcomeStatus.OK or identity_result.identity is None:
            return _CallbackOutcome(
                ok=False,
                reason="identity_lookup_failed",
                detail=identity_result.error_detail or identity_result.status.value,
            )

        identity = identity_result.identity
        candidates = {
            (identity.mail or "").strip().lower(),
            (identity.user_principal_name or "").strip().lower(),
        }
        candidates.discard("")
        if expected_email_address.strip().lower() not in candidates:
            return _CallbackOutcome(
                ok=False,
                reason="wrong_account",
                detail=(
                    f"authenticated Microsoft identity ({identity.mail!r}/"
                    f"{identity.user_principal_name!r}) does not match this mailbox's "
                    f"own address ({expected_email_address!r})"
                ),
            )

        # Identity verified — persist tokens and mark CONNECTED.
        self._token_store.write(
            mailbox_id,
            access_token=token_result.tokens.access_token,
            refresh_token=token_result.tokens.refresh_token,
            expires_at=token_result.tokens.expires_at,
        )
        self._mailbox_repository.mark_microsoft_connected(mailbox_id)
        return _CallbackOutcome(ok=True, reason="connected", detail="")


@dataclass(frozen=True)
class _CallbackOutcome:
    ok: bool
    reason: str
    detail: str
