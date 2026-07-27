# Releasing

This guide is the recurring operational authority for maintainers publishing `comfyui-docker-helper` to PyPI and GitHub Releases. The tracked workflows remain the machine authority for exact triggers, jobs, permissions, Action versions, and executable checks.

## Publication model and prerequisites

Three workflows separate source integration, package candidates, and production publication:

- `CI` validates pull requests, `main` pushes, and diagnostic manual runs. Its `CI / Required` result covers lint, the supported Python test matrix, and package qualification without retaining a publishable artifact.
- `Package Build` validates direct `v*` tag pushes and is also the read-only build authority reused by CI and formal publication. A direct tag run retains its wheel and sdist for seven days as candidate evidence only.
- `Publish PyPI` starts when a maintainer publishes a GitHub Release. It rebuilds and requalifies from that Release tag, then automatically publishes only the formal run's wheel and sdist through PyPI Trusted Publishing.

Before releasing, confirm that `main` and the `v*` package-tag namespace retain their repository protections, remote `v*` tags cannot be updated or deleted, immutable GitHub Releases remain enabled, and no production publication is already running. Environment `pypi` must continue to allow selected Tag refs matching `v*`, with no required reviewer, wait timer, secret, or variable. The PyPI Trusted Publisher must remain bound to this repository, workflow `publish-pypi.yml`, and Environment `pypi`.

Publishing a stable GitHub Release is the only manual production authorization. There is no later Environment approval. The maintainer owns version preparation, pull requests, tags, GitHub Releases, public-state verification, rerun decisions, and exceptional recovery. Actions has no authority to create, move, delete, or repair tags or Releases.

## Prepare and merge a version

Choose a new, unused, normalized stable PEP 440 version. Update the root `pyproject.toml` and package-owned release-projection `pyproject.toml` together, then refresh `uv.lock` so all three authorities contain the same project version.

Run the applicable local checks from [Contributing](contributing.md), open a pull request, and require `CI / Required` to pass before merging. Confirm that the resulting `main` push also passes. The commit selected for a formal stable release must belong to protected `main` history, but it does not need to remain the current `main` head after its package tag is created.

## Create and validate the package tag

Create the exact tag `v<project.version>` on the reviewed commit and push that package tag by itself. Both lightweight and annotated tags are accepted; after the first remote creation, never move, recreate, force-update, or delete a `v*` tag. Development and prerelease candidate tags use normalized PEP 440 forms such as `v0.6.0.dev1` and `v0.6.0rc1`, but formal PyPI publication initially accepts stable final versions only.

Wait for the direct `Package Build` run and require it to pass. Check that it built exactly one standard sdist and one `py3-none-any` wheel from the intended tag commit. Its seven-day artifact is useful for inspection, but it is never promoted or selected by the production workflow. If the candidate is wrong or fails because the source must change, leave the tag immutable and prepare a new version.

## Publish the GitHub Release

Create a stable GitHub Release using the existing validated tag, review the tag target and release notes, and publish it through the GitHub UI. Do not mark it as a prerelease. This action makes the Release public and immediately authorizes the formal PyPI workflow; there is no second human approval after the formal build.

The GitHub Release owns the human-facing notes and GitHub's automatic repository source archives. Those archives are not Python sdists, and the release process does not attach duplicate wheel or sdist assets to the Release.

## Observe formal publication

`Publish PyPI` checks the public stable Release, tag spelling, static version, event commit, peeled tag commit, and `main` ancestry. It then calls the same Package Build workflow from the tag commit and performs a fresh sdist-to-wheel build. The earlier candidate artifact is not read.

After qualification succeeds, the Environment-protected publisher job starts automatically. That job downloads the exact formal-run artifact and invokes the official Trusted Publishing Action; it has no checkout, project-source execution, long-lived upload credential, or repository-write permission.

The public GitHub Release intentionally precedes PyPI. A formal build or provider failure can therefore leave a public immutable Release while PyPI is incomplete. A rerun also uses the workflow version stored in the tag commit; a later workflow correction on `main` does not change the old run.

## Verify the public result

Require PyPI to expose exactly one wheel and one sdist for the version. Compare both filenames, sizes, and SHA-256 digests with the formal run's retained artifact.

Verify each file independently with the official attestation tool, substituting its exact `files.pythonhosted.org` URL:

```bash
uvx pypi-attestations verify pypi \
  --repository https://github.com/jimlee2048/comfyui-docker-helper \
  '<PyPI file URL>'
```

Run the command once for the wheel and once for the sdist. In the PyPI provenance view, separately confirm that the publisher identity names this repository, workflow `publish-pypi.yml`, and Environment `pypi`; the repository argument alone does not prove the workflow and Environment fields.

Confirm that the GitHub Release is public, stable, Latest as intended, and marked Immutable. Finish with a clean installation of the exact PyPI version and verify `cdh --version` and `cdh --help`.

## Failure and recovery

Fail closed whenever the tag, Release, artifact, PyPI files, digests, attestations, or publisher identity are missing, inconsistent, or uncertain. Never use `skip-existing`, move a package tag, overwrite a PyPI file, promote a candidate artifact, combine artifacts from different runs, add a token publisher, or mutate an immutable Release to make an old version appear complete.

- If a candidate tag build fails, do not publish its GitHub Release. Fix the source and create a new version and tag.
- If the formal build fails before artifact upload, rerun the full workflow only for a transient failure while the immutable source and workflow remain authoritative. If source or workflow correction is required, issue a higher version.
- If the PyPI job fails, inspect the exact public target version before rerunning anything. When PyPI has zero files and the formal artifact still exists, rerun only the failed PyPI job.
- If PyPI still has zero files but the seven-day artifact expired, rerun the full workflow within GitHub's 30-day rerun window so a new `github.run_attempt` rebuilds and names a new artifact. After that window, issue a higher version.
- If PyPI contains any partial, unexpected, mismatched, or attestation-defective file, treat the version as consumed. Preserve the evidence, yank as applicable, and issue a higher version.
- If the publisher reports failure but PyPI already contains the exact wheel and sdist with the expected metadata, digests, and attestations, do not upload again. Preserve the failed run, complete the manual verification, and treat publication as complete.
- If an incorrect tag or public immutable Release exists, do not repair it through automation. Assess it manually and normally issue a higher version.

Handle suspected credential compromise, malicious publication, or another sensitive security incident privately. Do not disclose credentials, exploit details, or unverified allegations in a public issue.
