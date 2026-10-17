# Security Policy

## Supported Versions

Only the currently maintained `main` branch and the latest tagged release are supported.
Older releases or commits are covered only when explicitly stated otherwise.

## Reporting a Vulnerability

Please do not report security vulnerabilities through public GitHub issues.

Preferred: use GitHub's [Private Vulnerability Reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)
of this repository (Security tab -> "Report a vulnerability").

If that is not possible, use the repository owner's private contact channel instead
of any public issue, discussion, or pull request.

A report should include:

- The affected component.
- The affected version / commit.
- A description of the issue.
- Steps to reproduce.
- Potential impact.
- A proof of concept, if it can be shared safely.

## Do Not Share Publicly

Never include any of the following in a public report, issue, discussion, or pull
request:

- API keys, tokens, or credentials.
- Personal data.
- Private portfolio/trading data.
- Production databases.

## Scope

- VIPER-AI code (`tools/trading/`, `viper_ai/`, `gui/`).
- Packaging and runtime isolation (`pyproject.toml`, `tools/trading/runtime_paths.py`): production data under `C:\tools\trading` is never part of
  the package, and `pip install` / `--upgrade` / `uninstall` must not read, overwrite or delete it.
- MCP tool integration (`mcp-tools/`).
- Database and import logic.
- External data-source / API integrations.
- Local deployment scripts.

## MCP Tool Profiles

The MCP server enforces which tools a client process can reach (`mcp-tools/tool_policy.py`, since v0.6.0); a client-side filter is
not relied on. `TRADING_TOOL_PROFILE` selects the profile:

- `read` (default, also when unset): the 17 READ tools only; nothing can write. Use it for every analysis or chat client.
- `gui`: READ plus the eight narrow GUI actions; the desktop GUI starts its own server with it and never offers the actions to its chat.
- `admin`: every classified tool, including `sql_execute` (arbitrary SQL) and `approve_swing_promotion`; only for a deliberate manual session.

Any other value stops the server. A tool that is not classified is never registered. `TRADING_EXCLUDE` can only remove further tools.
The import plan token binds a write to the database rows the check saw (see the import documentation).

## Automated Security Checks

The repository runs CodeQL code scanning (`.github/workflows/codeql.yml`), Dependency Review
on pull requests, Dependabot (alerts, security updates, weekly GitHub Actions updates), and
GitHub secret scanning with push protection. They report findings to the maintainer; they do
not replace a private report of a vulnerability you found.

## Responsible Disclosure

Please do not publicly disclose a vulnerability before it has been reasonably
reviewed and addressed.
