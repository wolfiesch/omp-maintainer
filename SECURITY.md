# Security Policy

## Supported versions

Security fixes are applied to the current minor release line. Until a later release states otherwise, only the latest `0.1.x` release is supported.

## Report a vulnerability privately

Use [GitHub private vulnerability reporting](https://github.com/wolfiesch/omp-maintainer/security/advisories/new). Do not disclose the issue publicly until a coordinated fix is available.

Include:

- the affected version or commit
- the deployment topology and configuration relevant to the report
- reproduction steps or a minimal proof of concept
- the expected security boundary and observed bypass
- any known exposure or publication that already occurred

Do not include live GitHub App private keys, webhook secrets, provider credentials, installation tokens, repository data, or other third-party secrets. Replace them with clearly marked test values.

## Response scope

High-priority reports include:

- GitHub App credential or installation-token exposure
- webhook signature or broker-authentication bypass
- repository allowlist or default-branch policy bypass
- unauthorized branch, tag, merge, comment, label, pull-request, or release mutation
- command execution outside the assigned repository workspace
- secret leakage through logs, errors, subprocess environments, or dashboard APIs
- replay-token or intervention authorization bypass

OMP Maintainer intentionally executes repository-scoped agent work and provider requests after policy permits a task. Reports should identify a boundary bypass, not only the presence of those documented capabilities.

## Operational response

If you suspect an active compromise, stop both Compose services, remove public ingress, revoke the GitHub App private key, review App installations and recent GitHub activity, and preserve logs and the data volume for investigation. Rotate provider credentials when their exposure is plausible.
