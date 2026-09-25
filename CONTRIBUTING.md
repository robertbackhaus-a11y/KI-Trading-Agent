# Contributing

This repository holds the Trading Agent code (`tools/trading/`, `openwebui-tools/`,
`docs/`). It does not hold the trading database, backups, audit/candidate JSONs, PDFs,
logs, or any other production data — see `.gitignore`.

## Before you start

For anything beyond a small, self-contained fix, open an issue first and agree on the
approach before writing code. This avoids wasted work on changes that conflict with the
existing architecture or module boundaries.

## Workflow

- Fork the repository, work on a feature branch, open a pull request.
- No direct pushes to `main` from outside the maintainer.
- Keep changes small and reviewable. One logical change per pull request.

## Architecture and code conventions

- Respect the existing architecture and module boundaries — see
  [docs/trading-agent-architecture.md](docs/trading-agent-architecture.md) for the
  current component flow and module inventory before adding a new one.
- Reuse existing modules/functions instead of duplicating logic. If you need something
  a module already provides (analytics scoring, portfolio valuation, FX resolution,
  strategy/campaign state, etc.), import and call it — don't recompute it.
- Read-only components stay read-only. If a module or function is documented as
  read-only/no-write (e.g. `decision_engine.py`, `candidate_decision.py`,
  `strategy_suggestion.py`, `analysis_engine.py`), a change must not give it a database
  write path.
- No fabricated financial data and no hidden fallbacks. Missing or unverifiable data
  must surface as an explicit unavailable/insufficient/unknown status — never a guessed
  or interpolated value presented as real.
- Handle external data sources traceably: name the source, note format/version
  assumptions, and keep parsing/verification logic auditable (see the
  `verified_direct`/`verified_derived`/`verified_secondary`/`not_verified` pattern in
  `Research-TradingFundamentals.py` for the established convention).

## Documentation

Any code change that affects behavior must update the matching documentation in the
same pull request — not as a follow-up:

- `README.md` if the change affects the architecture overview, tool inventory, or
  data flow.
- The relevant file(s) under `docs/` (`trading-agent-architecture.md` plus any more
  specific doc — `trading-candidate-decision.md`, `trading-strategy-suggestion.md`,
  `trading-swing-promotion.md`, `trading-entry-recommendations.md`,
  `openwebui-trading-tool.md` — that covers the touched component).
- Any new or changed business constant, threshold, or status value (a score threshold,
  a confidence weight, a guardrail name, an allowed status string, a schema
  feature-version key, etc.) must be documented with its exact value, matching
  `docs/trading-agent-architecture.md`'s existing constants tables.

## Tests

- New logic requires tests. Follow the existing per-module test file pattern under
  `tests/trading/` (see `tests/trading/_fixtures.py` for the shared fixture DB).
- Run the full regression suite before opening a pull request:

  ```bash
  "C:/KI-Stack/python/venvs/openwebui/Scripts/python.exe" -m unittest discover -s tests/trading -p "test_*.py"
  ```

  (the default `python` on PATH typically lacks the dependencies this suite needs —
  use the venv above, or your own equivalent environment with the same packages.)

## Database / schema changes

- Mark any pull request that adds or changes a table, column, constraint, or
  feature-version marker explicitly in its title and description ("DB/schema change:
  ...").
- Follow the existing additive-migration pattern (`Migrate-Trading*.py`): dry-run by
  default, `--write` to apply, a dedicated `metadata` feature-version key, never a
  destructive rewrite of existing rows.

## Breaking changes

Mark any pull request that changes an existing function's signature/return shape, a
CLI flag, a status vocabulary, or a documented constant's value explicitly in its title
and description ("BREAKING: ..."), and say what downstream code (OpenWebUI tool,
other modules, scripts) is affected.

## What never gets committed

- Secrets, API keys, tokens, credentials, or personal data.
- Production trading data: the production database file, backups, caches, audit/report
  output, or any other file under the paths listed in `.gitignore`.

If you're unsure whether a file belongs in the repository, don't add it — ask first.

## Commit messages

- Write clear, descriptive commit messages that explain the change.
- No automatic `Co-Authored-By` lines.
- No tool/AI attribution of any kind in commit messages.
- No automatic contributor lines.

## Pull requests

Describe, in the PR description:

- The purpose of the change and what it does.
- The tests you ran (and added).
- The documentation files you updated.
- Whether it includes a DB/schema change (see above).
- Whether it includes a breaking change (see above).
