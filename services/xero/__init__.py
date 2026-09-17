"""``services.xero`` — CD-6 Slice 2's Xero reference-data domain (PID
§98.4, architect spec §1-24).

Deliberately its own top-level ``services/`` package, not folded into
``services/evidence/`` or ``services/needs_you/`` — it owns three
distinct durable concepts (``XeroConnection``, ``XeroAccount``,
``XeroSyncRun``) plus the OAuth anti-replay state (``OAuthState``) and
the account-eligibility/AI-suggestion policy functions, none of which
belong to an existing component. See each submodule's own docstring
for its slice of the responsibility, and ``component.yaml`` for the
house-convention manifest.

Submodules
----------
* ``connection`` — ``XeroConnection`` domain object, its closed state
  machine, and repository (one company <-> one Xero organisation).
* ``account`` — ``XeroAccount`` projection domain object and
  repository (the synced Chart of Accounts).
* ``sync`` — ``XeroSyncRun`` domain object/repository and the
  idempotent, atomic-at-the-projection-level sync orchestration.
* ``oauth_state`` — the server-side anti-CSRF/replay ``state`` token
  BAGMAN itself mints and validates for the OAuth Authorization Code
  flow.
* ``eligibility`` — the documented, extensible "which accounts does
  the default dropdown show" policy (architect spec §5/§6).
* ``ai_suggestion`` — the closed-candidate-set validation an AI-proposed
  Xero account must pass before BAGMAN ever treats it as resolved
  (architect spec §6).
* ``client`` — ``XeroAccountingClientProtocol``/``XeroAccountingClient``
  (the real HTTP adapter) and ``XeroOAuthClient`` (the real OAuth
  token-exchange/refresh/connections adapter).
* ``fake_client`` — ``FakeXeroAccountingClient``/``FakeXeroOAuthClient``,
  this codebase's established ``Fake*`` deterministic-substitute
  pattern (mirrors ``ai/providers/litellm/fake.py``,
  ``agent/claude_code/fake.py``).
* ``secrets`` — missing-file-tolerant reading of the Xero app's
  ``client_id``/``client_secret`` and of per-connection OAuth tokens,
  matching this codebase's established secret-file discipline
  (``app/api/composition.py::_read_secret_file``,
  ``agent/claude_code/runner.py``).
"""
