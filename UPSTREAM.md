# Upstream provenance and synchronization

## Recorded upstream baseline

OMP Maintainer incorporates selected source from [Oh My Pi](https://github.com/can1357/oh-my-pi), known upstream as RoboOMP. The recorded source baseline is commit [`18781d829586fff77af98b222728b5b29bcaba41`](https://github.com/can1357/oh-my-pi/commit/18781d829586fff77af98b222728b5b29bcaba41).

| Upstream path | Standalone destination | Status |
| --- | --- | --- |
| `python/robomp` | `src/omp_maintainer` and its corresponding tests and dashboard source | Copied and adapted for the standalone package. |
| `python/omp-rpc/src/omp_rpc` | `src/omp_rpc` | Copied Python RPC client. |

This is a source-copy relationship, not a Git subtree or a vendored dependency with automatic updates. The standalone package has its own name, package layout, command, configuration namespace, state defaults, and dashboard branding. Do not represent it as a current upstream checkout.

## License and notices

The recorded upstream source is MIT-licensed. The complete MIT license for this distribution and the copied material is preserved in [LICENSE](LICENSE). [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) records the source paths and exact baseline commit.

When redistributing a substantial portion of copied source, retain the MIT license and this provenance notice. Do not remove, replace, or narrow upstream attribution while reorganizing source, packaging the container image, or updating documentation.

## Synchronization procedure

Synchronize deliberately and in a separate branch or worktree. A sync is not a blind directory replacement.

1. **Choose and record a candidate.** Identify an immutable upstream commit. Review its license, notices, security-relevant changes, public API changes, and the upstream paths listed above. Keep the current baseline SHA and candidate SHA in the sync review record.
2. **Compare the mapped source.** Fetch a clean checkout of the candidate and compare each upstream path to its mapped standalone destination. Account for deliberate downstream changes such as renamed imports, package metadata, container entrypoints, configuration names, and dashboard branding.
3. **Port only reviewed changes.** Apply selected changes with their required tests, imports, and call-site updates. Preserve standalone credential boundaries, configuration validation, policy enforcement, and public contracts unless the sync explicitly changes them.
4. **Reconcile notices.** If copied source moves to the candidate baseline, update the SHA and source mapping in both this file and `THIRD_PARTY_NOTICES.md`. Keep `LICENSE` and all required MIT text intact. If only a subset is ported, state that fact in the sync commit rather than claiming a full sync.
5. **Verify the integrated standalone behavior.** Run the repository's applicable checks and exercise affected operational paths in an isolated environment. A textual diff alone does not prove that renamed packages, container behavior, or credential separation still work.
6. **Commit attribution with the code.** Include the upstream repository URL, old SHA, new SHA, copied paths, and any intentionally omitted changes in the commit or review description. Do not include private paths, credentials, or local deployment details.

## Updating this record

Change the baseline SHA only when the standalone source actually incorporates the corresponding upstream content. Documentation-only changes, dependency updates, or independent downstream work do not advance the upstream baseline. If an upstream sync changes licensing or notice requirements, resolve that legal and attribution change before publishing the updated distribution.
