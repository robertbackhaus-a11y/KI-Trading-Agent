# Contributing

This repository holds the VIPER-AI code (`tools/trading/`, `mcp-tools/`, `gui/`, the
pip packaging `pyproject.toml` / `viper_ai/`, `docs/`). It does not hold the trading database, backups, audit/candidate JSONs, PDFs,
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
  [docs/trading-agent-architecture.en.md](docs/trading-agent-architecture.en.md) for the
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

## Documentation (mandatory)

Documentation is part of the change, not a follow-up: a change to any of the following areas must be documented **in the same commit**
(and therefore in the same pull request):

- public CLI commands and their arguments;
- new or removed entry points (`[project.scripts]` / `[project.gui-scripts]` in `pyproject.toml`);
- the package or the installation procedure (`pyproject.toml`, `viper_ai/`, extras, the deploy);
- runtime paths and environment variables (`runtime_paths.py`, `VIPER_TRADING_ROOT`, `TRADING_DB_PATH`, ...);
- MCP tools or other public contracts (tool names, parameters, result fields);
- new status or reason codes;
- scheduler tasks (`scheduler/Manage-TradingTasks.ps1`);
- GUI pages and visible functions;
- data formats and private runtime files (`*_local.json`, reports);
- breaking changes and migrations.

Where it goes:

- `README.md` whenever installation or operation is affected (install/update commands, main commands, runtime data, architecture overview,
  data flow).
- The matching technical document(s) under `docs/` (`trading-agent-architecture` plus any more specific doc that covers the touched component:
  `trading-candidate-decision`, `trading-strategy-suggestion`, `trading-swing-promotion`, `trading-entry-recommendations`, `trading-import`,
  `trading-agent-query-examples`, `gui`). The MCP tool (`mcp-tools/`) is documented in `trading-agent-architecture` §5, installation and
  entry points in §6.1. Every technical document exists in German and English (`<name>.de.md` / `<name>.en.md`); update **both** language
  versions together.
- `CHANGELOG.md` under `[Unreleased]`.
- Any new or changed business constant, threshold, or status value (a score threshold, a confidence weight, a guardrail name, an allowed status
  string, a schema feature-version key, etc.) with its exact value, matching `docs/trading-agent-architecture.en.md`'s existing constants
  tables (and the German twin).

No new function ships without matching documentation. The documentation tests (`tests/trading/test_docs_languages.py`: German/English
parity, links, README version against the changelog) must stay green.

## Tests

- New logic requires tests. Follow the existing per-module test file pattern under
  `tests/trading/` (see `tests/trading/_fixtures.py` for the shared fixture DB).
- Run the full regression suite before opening a pull request:

  ```powershell
  C:\tools\trading\.venv\Scripts\python.exe -m unittest discover -s tests/trading -p "test_*.py"
  ```

  Run it from the repository root, where `tests/trading`
  resolves. The default `python` on PATH typically lacks the dependencies this suite needs
  (e.g. `pydantic`) — use the Trading venv above, or your own equivalent environment with
  the same packages.

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
and description ("BREAKING: ..."), and say what downstream code (MCP tool `mcp-tools/trading_sqlite.py`,
other modules, scripts) is affected.

## What never gets committed

- Secrets, API keys, tokens, credentials, or personal data.
- Production trading data: the production database file, backups, caches, audit/report
  output, or any other file under the paths listed in `.gitignore`.

If you're unsure whether a file belongs in the repository, don't add it — ask first.

## Security checks

- CodeQL, Dependency Review, Dependabot and GitHub secret scanning (with push protection) run
  automatically; see [SECURITY.md](SECURITY.md). Do not silence or work around a finding —
  fix it or explain it in the pull request.
- Never commit secrets, not even test ones that look real. Use obviously fake values.
- Dependencies and GitHub Actions versions are kept current by Dependabot pull requests.
  GitHub Actions are pinned to a full commit SHA with the version in a trailing comment;
  workflows use the minimal `permissions:` they need and never `pull_request_target`.

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
