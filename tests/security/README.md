# tests/security/

Deterministic security tests for BAGMAN CD-1, proving the properties
required by `PID.md` §15 (Security Tests) and the doctrine in §8–§10.
These are the tests referenced by `README.md`'s "Running security checks"
section and exercised by `.github/workflows/security.yml`.

Run the whole suite from the repository root:

```bash
pip install -r requirements-dev.txt
pytest tests/security/ -v
```

## Test files → PID.md §15 mapping

| File | Proves | PID.md reference |
|------|--------|-------------------|
| `test_repo_hygiene.py` | Test A — no prohibited secret/runtime filenames (`.env`, `*.pem`, `*.key`, `credentials.json`, `token.json`, `*.sqlite`/`*.db`, anything under a tracked `secrets/`, `runtime/`, `data/`, or `backups/` directory) are tracked by git. | §15 Test A, §9 |
| `test_config_examples.py` | Test B — every committed configuration file under `config/` holds only secret references/placeholders (`credential_ref: ...`, `<SET_VIA_...>`, `*_FILE=/run/secrets/...`), never a real-looking `PASSWORD=`/`_TOKEN=`/`_SECRET=` value. | §15 Test B, §5, §6 |
| `test_production_data_prohibition.py` | Test C — `runtime/`, `data/`, `backups/` are both excluded by `.gitignore` (via `git check-ignore`) and not currently tracked. | §15 Test C, §8 |
| `test_gitleaks.py` | Test D — `gitleaks detect` finds no leaks in this repository. | §15 Test D, §10 |
| `test_synthetic_fixtures.py` | Test E — every fixture under `tests/fixtures/` is demonstrably synthetic (naming convention + explicit "SYNTHETIC TEST DATA" header). | §15 Test E, §8, §13 |
| `conftest.py` | Shared fixtures (`repo_root`, `tracked_files`) used by all of the above — resolved via `git rev-parse`/`git ls-files` so the suite reflects what is actually committed, wherever it runs. | — |

## Design notes

- Every test operates against **actually committed** state (`git ls-files`,
  `git check-ignore`), not a hardcoded snapshot of the repository — so the
  suite keeps proving these properties as the repository grows.
- Each test file also carries at least one small "matcher sanity check"
  test that proves the detection logic can actually fail (feed it an
  obviously bad synthetic example and confirm it's flagged) — this is the
  non-vacuity discipline required for a security test suite: a test that
  can never fail proves nothing.
- Test D (`gitleaks`) skips itself (rather than failing) if the `gitleaks`
  binary is not present in the environment running the suite; CI always
  installs it first, so this only matters for ad-hoc local runs on a
  machine without it.
