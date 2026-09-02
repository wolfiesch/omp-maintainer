## Result

Describe the user-visible or operator-visible result.

## Trust-boundary impact

State any change to credentials, webhook intake, repository policy, command execution, GitHub publication, merge, release, dashboard access, or durable state. Write `None` when no boundary changes.

## Verification

List the exact commands or runtime scenarios exercised and their results.

## Checklist

- [ ] The change is focused and all affected callers are updated.
- [ ] Observable contract changes have behavior-focused coverage.
- [ ] Default-branch repository policy remains authoritative.
- [ ] GitHub App credentials remain broker-only.
- [ ] Publication paths fail closed and preserve branch, tag, and merge restrictions.
- [ ] Logs, fixtures, screenshots, and public text contain no secrets or private local paths.
- [ ] Documentation and example configuration match the implemented behavior.
- [ ] Imported upstream code and license obligations are recorded in `UPSTREAM.md` and `THIRD_PARTY_NOTICES.md`.
