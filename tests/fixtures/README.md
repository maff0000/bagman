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
