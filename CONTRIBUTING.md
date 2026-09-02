# Contributing to OMP Maintainer

Thank you for improving OMP Maintainer. Changes should preserve the product's core boundary: repository policy is authoritative, GitHub App credentials remain broker-only, and publication fails closed.

## Development setup

Prerequisites:

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- [Bun](https://bun.sh/) for dashboard changes

```sh
git clone https://github.com/wolfiesch/omp-maintainer.git
cd omp-maintainer
uv sync --extra dev
bun install --cwd web --frozen-lockfile
```

## Focused checks

Run the checks that cover the changed contract while developing:

```sh
uv run pytest -q tests/path_to_changed_test.py
uv run ruff check src tests
uv run ruff format --check src tests
bun run --cwd web check:types
bun run --cwd web test
```

Before opening a pull request, run the complete local gate:

```sh
uv run pytest -q tests
bun run --cwd web check:types
bun run --cwd web test
bun run --cwd web build
uv build
```

Container or Compose changes should also pass:

```sh
docker compose --env-file .env config --quiet
docker build -t omp-maintainer:local .
```

Never use real credentials in tests, fixtures, examples, logs, screenshots, or pull-request text.

## Change requirements

- Add or update behavior-focused tests when an observable contract changes.
- Keep default-branch policy authoritative. Do not load publication policy from a task branch.
- Keep GitHub App identity and private-key material out of the maintainer process.
- Preserve bounded branches, tag protection, merge denial, replay authentication, and secret redaction.
- Update `README.md`, `.env.example`, `examples/omp-maintainer.yml`, and `UPSTREAM.md` when their contracts change.
- Record imported upstream code and license obligations before merging it.

## Pull requests

Keep each pull request focused. Explain the user-visible result, trust-boundary impact, and exact verification performed. Link the issue when one exists.

Do not include private prompts, agent transcripts, credentials, local absolute paths, or unrelated generated artifacts.

## Security reports

Do not open a public issue for a vulnerability. Follow [SECURITY.md](SECURITY.md) and use GitHub private vulnerability reporting.
