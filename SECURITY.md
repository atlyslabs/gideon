# Security Policy

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it by following the instructions in the **Vulnerability Disclosure Program**
section at [atlys.com/security](https://www.atlys.com/security).
This lets us triage and fix the issue before any details become public.

Please include:

- What the issue is and why it matters.
- Steps to reproduce, or a proof of concept.
- Affected version or commit.

We will acknowledge your report, keep you updated as we investigate, and credit
you in the advisory when a fix ships, unless you would rather stay anonymous.

## Supported versions

Gideon is pre-1.0. Fixes land on the default branch, and we do not backport to
older tags. Track `main` to stay current.

## Running Gideon safely

Gideon holds OAuth tokens for your accounts and can act on your behalf, so how
you deploy it matters.

- **`AUTH_MODE=single_user` has no authentication.** Every request is treated as
  the configured local user. It exists so self-hosting is frictionless. Never
  expose an instance in this mode to a network you do not control. For anything
  shared or internet-facing, use `AUTH_MODE=oidc`.
- **`ENCRYPTION_KEY` protects OAuth tokens and message content at rest.** Treat
  it like a database password. Use a distinct key per environment, and note that
  rotating it makes existing encrypted rows unreadable.
- **Restrict who can sign in** with `ALLOWED_EMAIL_DOMAINS` when using OIDC.
  Empty means any authenticated account is accepted.
- **Set `CORS_ALLOWED_ORIGINS`** to the exact origins you serve the frontend
  from. Do not use a wildcard.
- **Scope your OAuth apps narrowly.** Request only the scopes for the tools you
  actually plan to use.
- **The analytics tool executes model-written SQL.** It is restricted to
  SELECT statements and auto-limited, but you should still point it at a
  read-only warehouse user.
- **Model output is untrusted input.** Content arriving from email, Slack, or a
  fetched page can attempt prompt injection. Grant Gideon the narrowest set of
  integrations that does the job.

## What we consider a vulnerability

In scope: authentication and authorization bypass, token or secret disclosure,
SQL injection, SSRF, remote code execution, and privilege escalation between
users.

Out of scope: the documented behaviour of `AUTH_MODE=single_user`, issues that
require an already-compromised host or database, and vulnerabilities in
third-party services Gideon integrates with (please report those upstream).
