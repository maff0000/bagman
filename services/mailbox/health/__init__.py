"""Mailbox connection-health incident/notification domain (PID §118,
WO-BAGMAN-118-MAILBOX-HEALTH-ALERTING).

Sibling modules:

* :mod:`services.mailbox.health.incident` — ``MailboxHealthIncident``
  domain object, repository ABC + in-memory reference implementation,
  and the pure episode-detection/triggering functions for
  ``AUTH_REQUIRED``/``PERSISTENT_PROVIDER_ERROR``/
  ``OAUTH_RECONNECT_INCOMPLETE`` (``STALE_INGESTION`` is named but
  deliberately not implemented — PID §118.D/G).
* :mod:`services.mailbox.health.notification` — ``MailboxHealthNotification``
  domain object (the durable notification outbox) and repository ABC +
  in-memory reference implementation, including the claim/fencing/
  exhaustion/pre-dispatch-suppression state machine.
* :mod:`services.mailbox.health.needs_you_sync` — the provider-neutral
  shared helper generalised out of
  ``app/api/routers/mailboxes_microsoft.py``'s own
  ``_sync_needs_you_for_connection_state`` (WO §6.A.4).

Durable PostgreSQL implementations live in
``persistence.postgres.mailbox_health_models``/
``persistence.postgres.mailbox_health_repository`` — mirrors
``services.needs_you.needs_you``'s own in-memory-domain-module vs.
``persistence.postgres.needs_you_repository``'s own durable-sibling
split exactly.
"""
