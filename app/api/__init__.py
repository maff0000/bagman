"""``app/api/`` — the FastAPI application (CD-3 WI-3, PID §24-33).

* ``main.py`` — the FastAPI app instance, router registration,
  structured logging setup, and centralised BAGMAN-error -> HTTP-status
  translation.
* ``composition.py`` — the PID §14 composition root: the ONE place
  ``BAGMAN_RUNTIME_ENV`` is read to decide between in-memory and
  PostgreSQL+MinIO-backed repositories.
* ``routers/`` — thin HTTP marshalling around
  ``core.api.BagmanCanonicalAPI`` (health/readiness, version metadata,
  and the internal canonical operations).
"""
