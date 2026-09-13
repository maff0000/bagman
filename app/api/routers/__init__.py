"""HTTP routers for ``bagman-api`` — thin marshalling only.

Every router in this package translates HTTP requests/responses to and
from calls on ``core.api.BagmanCanonicalAPI`` (via
``app.api.composition.get_composition()``). No domain/business
logic is implemented here — see each module's own docstring.
"""
