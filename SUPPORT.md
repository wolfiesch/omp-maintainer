# Support

Use the channel that matches the request:

- **Deployment, configuration, policy, or provider question:** start a [GitHub Discussion](https://github.com/wolfiesch/omp-maintainer/discussions).
- **Reproducible defect:** open a [bug report](https://github.com/wolfiesch/omp-maintainer/issues/new?template=bug.yml).
- **Bounded capability proposal:** open a [feature request](https://github.com/wolfiesch/omp-maintainer/issues/new?template=feature.yml).
- **Security vulnerability:** use [private vulnerability reporting](https://github.com/wolfiesch/omp-maintainer/security/advisories/new) and follow [SECURITY.md](SECURITY.md).

Before requesting help, run:

```sh
docker compose --env-file .env ps
docker compose --env-file .env exec maintainer omp-maintainer doctor
```

Share the machine-readable doctor result only after checking it for repository or environment details you do not want to publish. Never post credentials, private-key material, tokens, raw webhook payloads, private repository contents, or unredacted logs.

This is an early self-hosted project. Support is best-effort and does not replace incident response, provider support, or a security review of your deployment.
