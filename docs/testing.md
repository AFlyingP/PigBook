# Testing

Run `python scripts/verify.py bootstrap` when dependencies are missing or locks/runtime change. It uses frozen backend and frontend lockfiles. During development use `ticket --ticket <identifier>` or targeted pytest/Vitest tests for the changed behavior. Before integration run the affected regression/security checks once. `python scripts/verify.py regression` covers every implemented gate in order; it does not need an additional lint or ticket invocation for tests already included.

`python scripts/verify.py regression --fresh` forces a complete fresh run of the implemented gates. Use it at concurrency activation, rollback/completion milestones and before release. Missing future gates are reported as unimplemented, never as successful proofs. Current standalone targets include lint, unit, integration, frontend, ticket and build. Integration includes the current permission tests; full HTTP concurrency and standalone permissions are not yet implemented.

For frontend changes, `python scripts/verify.py frontend` runs lint, type checking, components and build. Add relevant browser verification when browser behavior is implemented; the ticket runner currently fails explicitly for unsupported Playwright paths. Narrative documentation changes need review of links/contracts/provenance and whitespace, without database or browser tests. CI compares its base with HEAD, selecting documentation, frontend or full verification conservatively. Unknown paths or unavailable base require complete checks. CI uses one bootstrap and fresh verification, with no private planning dependency or evidence cache.

## Evidence

Each stage writes an ignored local `evidence/<run>/<stage>/manifest.json`, raw command output, stream SHA-256 hashes, source Git SHA, actual input fingerprint, configuration/runtime/package versions and elapsed seconds. Uncommitted changes are represented by the content fingerprint; the source SHA alone does not identify them. Failed child commands, setup/cleanup failures and exceptions produce failure evidence. Never present reused output as a fresh run.

Enabled standard gates may reuse an original successful execution only when complete relevant inputs, tools and configuration match and all original artifacts exist with matching hashes. Reused manifests name the original source path, manifest hash and source SHA. Earlier failed or malformed evidence, uncertain tools or changed inputs cause execution. Legacy manifests without validated provenance are historical evidence, not automatic reuse candidates. `--fresh` always overrides reuse. Changes to acceptance meaning outside the application require fresh affected verification; record that reason with the decision.

Backend fingerprints cover source, tests, fixtures, locks, scripts, manifests, Compose, infrastructure, CI and schema/OpenAPI snapshots. Frontend gates include the full frontend tree, including configuration. Installed backend packages, runtimes, environment hash and PostgreSQL image identity are checked. Dependency/output directories are pruned during fingerprint traversal. Keep installed dependencies consistent with locks; run bootstrap after dependency changes. Evidence directories must be new or empty; runner databases always have fresh unique namespaces even if TEST_RUN_ID repeats.

## Isolation

Real PostgreSQL 16 runs in runner-owned Compose namespaces bound to loopback with tmpfs storage. Setup and teardown logs are retained; cleanup failure fails verification. No database state is reused for evidence reuse. Alembic upgrades once per test process; the migration lifecycle test still exercises downgrade and re-upgrade. Transactional fixtures roll back their own writes; tests that exercise committed requests create unique users/resources in the disposable database. SQLite is forbidden. Tests with zero collection or skips fail.

On Windows, if sandbox permissions prevent pytest from creating its temporary directory, use an authorized disposable `--basetemp` outside the restricted default directory; do not disable tests or change shared directory permissions.

The five existing branch-protection check names are preserved by lightweight status jobs. They require the shared verification job to succeed, including its selected checks and artifact upload. They perform no second test execution or dependency installation. This preserves repository protection without changing GitHub permissions. Inspect the shared job and evidence for actual gate execution and unimplemented status; status jobs are not separate proofs.
