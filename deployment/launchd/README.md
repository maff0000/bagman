# deployment/launchd/

Owns the canonical macOS `launchd` scheduler definition for BAGMAN's
recurring automatic evidence-classification worker, extending the
existing `deployment/compose/`/`deployment/docker/` convention (per
`PID.md` §114.6): **a governed, Git-tracked definition, applied to the
live appliance by whoever operates it, by an explicit command — never
an undocumented, handcrafted file existing only on the Mac, and never
auto-generated.**

Governing architecture: `PID.md` §114 ("Automatic Evidence
Classification Scheduling", Architect ruling 2026-10-07). Implemented
under `work-orders/WO-BAGMAN-114-AUTOMATIC-CLASSIFICATION-SCHEDULER.md`
§6.A (Stage A — this repository implementation only; production
deployment/activation is Stages D–G of that same Work Order, performed
later by the BAGMAN-authorised production executor, never by this
repository change itself).

---

## What this job does

`com.bagman.evidence-classification-scheduler.plist` periodically
invokes the existing, unmodified classification worker,
`scripts/process_evidence_classification_jobs.py`, inside the already-
running `bagman-api` container on the canonical production Mac mini
(`192.168.11.4`, PID §0) — exactly the same invocation shape already
proven manually during governance recovery (`docker exec bagman-api
python3 scripts/...`, see
`work-orders/WO-BAGMAN-112F-GOVERNANCE-RECOVERY-CLOSURE.md`), just
recurring instead of one-off. The scheduler owns nothing else: no
eligibility logic, no job durability, no retries, no classification
logic, no AI logic, no Needs You logic, no Xero logic, and no
historical-boundary authority (PID §114.2) — those all remain
BAGMAN application responsibilities, unchanged by this artifact.

## Installation path on the Mac

Per the existing precedent on this appliance (the only other BAGMAN-
related `launchd` job, `com.helm.bagman-stack.plist`, is a per-user
LaunchAgent, not a system LaunchDaemon — confirmed in
`work-orders/WO-BAGMAN-112F-GOVERNANCE-RECOVERY-CLOSURE.md`), this job
installs the same way, as a LaunchAgent for the appliance's operating
macOS account:

```text
~/Library/LaunchAgents/com.bagman.evidence-classification-scheduler.plist
```

## Install (Stage D) — copy to disk, validate, leave unloaded

Before copying anything, see "Log directory prerequisite" below —
verify it first.

Copy the canonical file from the merged `main` SHA onto the appliance
unchanged:

```bash
cp deployment/launchd/com.bagman.evidence-classification-scheduler.plist \
   ~/Library/LaunchAgents/com.bagman.evidence-classification-scheduler.plist
```

Then validate it natively on macOS — **without loading it**:

```bash
plutil -lint ~/Library/LaunchAgents/com.bagman.evidence-classification-scheduler.plist
```

`plutil -lint` reports syntax validity only; it does not load, enable,
or otherwise touch `launchd`'s job table. This is the first genuine
macOS-native validation this file undergoes (Stage B's own validation,
performed on a non-macOS Linux development host, used Python's
`plistlib` instead — see "Plist syntax validation" below). Confirm
`plutil -lint` reports the file valid before proceeding to any later
stage; if it reports a problem, STOP and return to the Architect
rather than hand-editing the installed file.

**Installing and lint-validating the file is not the same thing as it
being "enabled" in the sense the governing Work Order (§6.D vs §6.F)
means.** This plist defines no `RunAtLoad` key at all — launchd's own
default for an omitted `RunAtLoad` is `false`. This document states
only the governed `StartInterval` (300 seconds) itself; it does not
assert or guarantee an exact first-fire timestamp beyond that governed
interval — actual first-fire timing, once loaded, is determined by
`launchd` itself. Per the governing Work Order's staged-activation
doctrine (PID §114.12, WO §6.D–§6.F): **"deploy disabled" for Stage D
means this file must not be loaded into `launchd` at all** — it is
copied onto disk and lint-validated only. `launchctl load` itself is
reserved for Stage F's deliberate, individually-recorded enablement
action below, and must never be run as part of, or as an incidental
side effect of, Stage D's install.

## Enable (Stage F only) — `launchctl load`

Only after every Stage E gate (PID §114.12, WO §6.E) is independently
confirmed GREEN may the BAGMAN-authorised production executor load the
job:

```bash
launchctl load ~/Library/LaunchAgents/com.bagman.evidence-classification-scheduler.plist
```

This is the one, deliberate, individually-recorded enablement action —
never run as part of Stage D's install, and never run merely to
validate plist syntax (that is `plutil -lint`'s job, above, which does
not load anything). Stage G (WO §6.G) then observes real scheduled
executions to confirm the job fires on the governed cadence; this
document does not assert or guarantee the exact real-world time of any
particular fire, only the governed `StartInterval` itself.

## Disable / unload (kill switch)

```bash
launchctl unload ~/Library/LaunchAgents/com.bagman.evidence-classification-scheduler.plist
```

Per PID §114.9, the scheduler itself is the kill switch: unloading it
stops new scheduled invocations only. It does not delete jobs, change
`EvidenceItem`s, alter classifications, change `T_ACT`, modify Needs
You items, or change Xero state. No additional BAGMAN kill-switch
subsystem exists or is needed. Removing the file from
`~/Library/LaunchAgents/` after unloading fully removes the schedule;
leaving it unloaded-but-present on disk is also a valid disabled state.

## Cadence and bounded invocation

- **Cadence:** `StartInterval = 300` (seconds) — PID §114.2's initial
  value.
- **Bounded invocation command** (functionally equivalent to the
  already-proven manual canary invocation shape, just with this
  delivery's own governed bounds, PID §114.3):

  ```bash
  /opt/homebrew/bin/docker exec bagman-api python3 \
      scripts/process_evidence_classification_jobs.py \
      --process --limit 20 --discovery-limit 50 \
      --runtime-dir /opt/bagman/runtime/evidence-classification-jobs \
      --worker-id launchd-scheduler
  ```

  `/opt/homebrew/bin/docker` is the exact, Git-tracked-evidenced docker
  CLI path on the canonical Mac mini appliance (confirmed by
  `work-orders/WO-BAGMAN-112F-GOVERNANCE-RECOVERY-CLOSURE.md`'s own
  "Methodology" note: all read-only production inspection for this host,
  2026-10-07, used `/opt/homebrew/bin/docker` over SSH) — used directly
  because `launchd` jobs do not inherit a login shell's `PATH`. `python3
  scripts/process_evidence_classification_jobs.py` runs with the
  container's own `WORKDIR` (`/app`, per
  `deployment/docker/api/Dockerfile`), exactly like every other operator
  script in this directory — no working-directory override is needed or
  given.

- `--limit 20` / `--discovery-limit 50` — PID §114.3's exact initial
  bounds.
- `--runtime-dir /opt/bagman/runtime/evidence-classification-jobs` — the
  directory this run's own JSON report is written into, matching the
  worker script's own documented usage example
  (`scripts/process_evidence_classification_jobs.py`'s module docstring,
  "Usage" section).
- `--worker-id launchd-scheduler` — a fixed, human-readable identity
  stamped on jobs this scheduler claims, so `EvidenceClassificationJob`
  rows created by scheduled runs are distinguishable, in the existing
  `claimed_by` column, from any manual operator invocation. This is
  purely a label; it changes no logic.

## `T_ACT` isolation

This scheduler never carries, passes, calculates, defaults, or alters
`BAGMAN_EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY` (`T_ACT`) in any
way. The plist defines no `EnvironmentVariables` dict and no CLI
argument for it (the worker script itself has no `T_ACT` CLI flag —
see PID §114.5). `T_ACT` is resolved exclusively inside the `bagman-api`
container from the BAGMAN application/container environment, exactly
as already governed by PID §112.A/B and
`services/evidence/classification_job.py` — this scheduler supplies
only `--process`, `--limit`, `--discovery-limit`, `--runtime-dir`, and
`--worker-id`, nothing else.

## Log directory prerequisite — verify before any install/load

Before Stage D's install (`cp`, above), and in any case strictly before
Stage F's `launchctl load`, the BAGMAN-authorised production executor
**MUST independently verify, on the real appliance**:

- `/opt/bagman/runtime/launchd/` exists;
- the macOS account this LaunchAgent will run as (the same account
  that owns `~/Library/LaunchAgents/`, since this is a per-user
  LaunchAgent, not a system LaunchDaemon) has write permission into
  that directory;
- the exact intended files —
  `/opt/bagman/runtime/launchd/evidence-classification-scheduler.out.log`
  and
  `/opt/bagman/runtime/launchd/evidence-classification-scheduler.err.log`
  — can actually be created there (`launchd` itself creates/appends to
  them on first write; the directory and its permissions must already
  support that before any load is attempted).

**If the directory is missing, or is not writable by that account,
STOP and report the exact condition to the Architect.** Per the
governing Work Order's absolute exclusions (§8), do **not** silently
create the directory, `chmod`/`chown` it, or otherwise change its
permissions without a separately confirmed authority under the
governing Work Order — provisioning filesystem state on the appliance
is outside this delivery's own scope. Do not change these log paths
either, unless a concrete incompatibility is actually found and
proven; in that case, name the exact problem and return it to the
Architect rather than silently substituting a different path.

## Logging

```text
stdout: /opt/bagman/runtime/launchd/evidence-classification-scheduler.out.log
stderr: /opt/bagman/runtime/launchd/evidence-classification-scheduler.err.log
```

These are host-side paths on the appliance (`StandardOutPath`/
`StandardErrorPath` plist keys) — `launchd` creates/appends to them
itself; this delivery does not pre-create them or manage rotation (no
monitoring/log-management subsystem is authorised by PID §114.11). See
"Log directory prerequisite" above — their existence/writability must
be verified before any load, not assumed. Each invocation's own
structured JSON run report is written separately, inside the
container, under `--runtime-dir`
(`/opt/bagman/runtime/evidence-classification-jobs`), by the worker
script itself — unchanged, existing behaviour.

## Plist syntax validation

- **Stage B** (this repository implementation, performed on a
  non-macOS Linux development host): `plutil -lint` — the preferred
  Apple-native syntax check — was **not available** in that
  environment (`launchctl`/`plutil` do not exist on Linux). The
  equivalent well-formedness check actually used was Python's
  standard-library `plistlib` (`plistlib.load(open(path, "rb"))`),
  which parsed this file as a valid XML property list with no
  exception and yielded the exact expected key/value structure
  documented above. This is a genuine, disclosed substitution, not a
  silently skipped or falsely-claimed-passed check.
- **Stage D** (on the real Mac mini appliance): the BAGMAN-authorised
  production executor runs real, macOS-native `plutil -lint` against
  the exact installed file — **without loading it** (see "Install
  (Stage D)" above). This is the first genuine macOS-native validation
  this file undergoes. `launchctl load` is explicitly **not** required,
  and must not be used, merely to validate plist syntax — `plutil
  -lint` alone is sufficient for that and, by itself, does not enable
  the job.
- **Stage F**: only after Stage D's `plutil -lint` passes, and every
  Stage E safety gate is independently confirmed GREEN, does the
  BAGMAN-authorised production executor perform the one, explicit,
  individually-recorded `launchctl load` (see "Enable (Stage F only)"
  above) — the job's actual enablement, not a validation step.
- **Stage G**: observes real scheduled executions to confirm the job
  fires on the governed 300-second cadence; this delivery does not
  assert or guarantee an exact first-fire timestamp, only the governed
  interval itself.

## Governing architecture

`PID.md` §114 (Automatic Evidence Classification Scheduling, Architect
ruling 2026-10-07) is the sole governing architecture for this
artifact. See also the parent amendment chain it depends on: §112.A
(classification architecture), §112.B (`T_ACT`), §113 (Work Order
lifecycle), and
`work-orders/WO-BAGMAN-114-AUTOMATIC-CLASSIFICATION-SCHEDULER.md` for
the full staged delivery/activation doctrine this file is deployed and
enabled under.
