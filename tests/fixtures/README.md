# tests/fixtures/

**Synthetic data only.**

Every file under this directory is fabricated test data and must clearly
identify itself as such — a filename that follows the `synthetic` naming
convention, and an explicit "SYNTHETIC TEST DATA" header near the top of
the file. `tests/security/test_synthetic_fixtures.py` (Test E, `PID.md`
§15/§13/§8) enforces both signals for every tracked file here.

No real invoice, receipt, bank statement, email, customer record, or any
other production evidence may ever be added to this directory. See
`PID.md` §8 (Production Data Doctrine).

As of CD-1 this contains one example fixture
(`invoices/example_synthetic_invoice.txt`) demonstrating the convention;
no fixture-consuming implementation exists yet.

## CD-2 fixtures

- `invoices/synthetic_cloud_services_invoice_to_noustai.txt` — a
  fabricated invoice from the fictional **Synthetic Cloud Services
  Ltd** (PID §30's suggested fictional party) to **NoustAI Limited**,
  used by `tests/integration/test_domain_and_lineage.py` and
  `tests/integration/test_runtime_proof.py` as the underlying
  "document" a synthetic `EvidenceItem` is registered for (content
  hashed, linked to an external reference, and traced through
  provenance). "NoustAI Limited" here is only text inside this
  fabricated document's own body — it is not, and must never become,
  a fourth `GovernedEntity`; the three real governed entities remain
  exactly `NOUSTAI_LIMITED`, `INFOSECURS_LIMITED`,
  `MATTHEW_SCOTT_PERSONAL` (PID §23).
