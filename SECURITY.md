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

- Trading Agent code (`tools/trading/`).
- MCP tool integration (`mcp-tools/`).
- Database and import logic.
- External data-source / API integrations.
- Local deployment scripts.

## Automated Security Checks

The repository runs CodeQL code scanning (`.github/workflows/codeql.yml`), Dependency Review
on pull requests, Dependabot (alerts, security updates, weekly GitHub Actions updates), and
GitHub secret scanning with push protection. They report findings to the maintainer; they do
not replace a private report of a vulnerability you found.

## Responsible Disclosure

Please do not publicly disclose a vulnerability before it has been reasonably
reviewed and addressed.
