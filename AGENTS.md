# Contributing to hermes-hostd

## Project structure

- `src/hermes_hostd/`: daemon implementation, request models, transports, approval implementations, and executor.
- Root Python modules: compatibility wrappers for source-checkout execution. Preserve them alongside `src/` when changing compatibility behavior.
- `packaging/systemd/`: portable, operator-installed systemd user-service template.
- `tests/`: automated package and integration tests.
- `docs/release.md`: installation, configuration, extension APIs, and known limitations.

## Development rules

- Keep request transport, approval, and execution responsibilities separate.
- Preserve the documented wire protocol or explicitly version incompatible changes.
- Write regression tests before changing behavior. Exercise approved, denied, malformed, and timed-out requests with disposable fixtures.
- Use synthetic data and task-owned temporary directories. Never read or modify production configuration, credentials, or deployed services during tests.
- Do not commit runtime configuration, credentials, private notification topics, logs, environments, or deployment-specific metadata.
- Do not install, activate, or restart services as a side effect of packaging or testing.
- State what was actually tested; automated terminal input is not independent human approval, and local tests do not establish deployment security.

## Running tests

From the repository root, set `TMPDIR` to an existing task-owned scratch directory and run:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

See `docs/release.md` for optional separate-client compatibility checks and installed-artifact validation.
