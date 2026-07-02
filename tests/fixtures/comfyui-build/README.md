# ComfyUI Build Smoke Fixtures

These fixtures define reusable ComfyUI build smoke scenarios. They use real
Docker Buildx, real comfy-cli behavior, and real ComfyUI-Manager behavior when
executed. They do not require private tokens.

The TOML fixture files are safe to validate locally with the lightweight tests in
`tests/smoke/`; those tests do not run Docker. The `cdh host build` commands
below are resource-heavy because they build images and contact upstream
services.

Notes:

- `comfyui.version = "nightly"` is a cdh/comfy-cli input, not a GitHub branch
  assumption.
- Smoke tests validate `comfy` inside the generated build container.
- `comfyui-custom-scripts` is the default registry smoke candidate because it is
  a small public node that exercises the Manager registry install path.
- `comfyui-impact-pack` is an optional heavier registry candidate for broader
  Manager/registry coverage.

## Public Node Candidates

| Purpose | Candidate | Evidence |
| --- | --- | --- |
| default registry node | `comfyui-custom-scripts@latest` | Registry ID `comfyui-custom-scripts`; repository `https://github.com/pythongosssss/ComfyUI-Custom-Scripts`; small public node used for default registry coverage. |
| fixed-ref git node | `https://github.com/pythongosssss/ComfyUI-Custom-Scripts.git@609f3afaa74b2f88ef9ce8d939626065e3247469` | Pinned public Git ref used for deterministic git install coverage. |
| optional heavier registry node | `comfyui-impact-pack@latest` | Broader Manager/registry coverage with higher resource risk. |

## Fixture Inputs

Concrete smoke inputs live under `tests/fixtures/comfyui-build/`.

| ID | Scenario | Config |
| --- | --- | --- |
| S1 | pinned minimal | `tests/fixtures/comfyui-build/configs/minimal-pinned.toml` |
| S2 | latest ComfyUI | `tests/fixtures/comfyui-build/configs/latest.toml` |
| S3 | nightly ComfyUI | `tests/fixtures/comfyui-build/configs/nightly.toml` |
| S4 | Manager only | `tests/fixtures/comfyui-build/configs/manager-only.toml` |
| S5 | registry node | `tests/fixtures/comfyui-build/configs/registry-node.toml` |
| S6 | git node | `tests/fixtures/comfyui-build/configs/git-node.toml` |
| S7 | hooks matrix | `tests/fixtures/comfyui-build/configs/hooks.toml` |
| S8 | httpx files | `tests/fixtures/comfyui-build/configs/httpx-files.toml` |
| S9 | aria2 files | `tests/fixtures/comfyui-build/configs/aria2-files.toml` |
| S10 | full workflow | `tests/fixtures/comfyui-build/configs/full.toml` |

## Canonical Build Commands

Run these commands from the repository root. Use the scenario ID when referring
to a command in evidence or scenario notes.

### S1 Pinned Minimal

```bash
cdh host build -f tests/fixtures/comfyui-build/configs/minimal-pinned.toml -t cdh-smoke:minimal-pinned --context-dir .cdh/smoke/comfyui-build/minimal-pinned
```

### S2 Latest ComfyUI

```bash
cdh host build -f tests/fixtures/comfyui-build/configs/latest.toml -t cdh-smoke:latest --context-dir .cdh/smoke/comfyui-build/latest
```

### S3 Nightly ComfyUI

```bash
cdh host build -f tests/fixtures/comfyui-build/configs/nightly.toml -t cdh-smoke:nightly --context-dir .cdh/smoke/comfyui-build/nightly
```

### S4 Manager Only

```bash
cdh host build -f tests/fixtures/comfyui-build/configs/manager-only.toml -t cdh-smoke:manager-only --context-dir .cdh/smoke/comfyui-build/manager-only
```

### S5 Registry Node

```bash
cdh host build -f tests/fixtures/comfyui-build/configs/registry-node.toml -t cdh-smoke:registry-node --context-dir .cdh/smoke/comfyui-build/registry-node
```

### S6 Git Node

```bash
cdh host build -f tests/fixtures/comfyui-build/configs/git-node.toml -t cdh-smoke:git-node --context-dir .cdh/smoke/comfyui-build/git-node
```

### S7 Hooks Matrix

```bash
cdh host build -f tests/fixtures/comfyui-build/configs/hooks.toml -t cdh-smoke:hooks --scripts-dir tests/fixtures/comfyui-build/scripts --context-dir .cdh/smoke/comfyui-build/hooks
```

### S8 Httpx Files

```bash
cdh host build -f tests/fixtures/comfyui-build/configs/httpx-files.toml -t cdh-smoke:httpx-files --context-dir .cdh/smoke/comfyui-build/httpx-files
```

### S9 Aria2 Files

```bash
cdh host build -f tests/fixtures/comfyui-build/configs/aria2-files.toml -t cdh-smoke:aria2-files --context-dir .cdh/smoke/comfyui-build/aria2-files
```

### S10 Full Workflow

```bash
cdh host build -f tests/fixtures/comfyui-build/configs/full.toml -t cdh-smoke:full --scripts-dir tests/fixtures/comfyui-build/scripts --context-dir .cdh/smoke/comfyui-build/full
```

## Scenario Matrix

| ID | Scenario | Config intent | Command | Required evidence | Pass criteria |
| --- | --- | --- | --- | --- | --- |
| S1 | pinned minimal | `comfyui.version` pinned or stable, `install_manager=false`, no nodes/files | S1 command | build log, rendered context, image ID, `docker image inspect`, container filesystem checks | Build succeeds; ComfyUI installed under `COMFYUI_PATH`; `/opt/venv` exists; `$COMFYUI_PATH/.venv` absent; final image does not include build-only context artifacts such as `/tmp/cdh`, `packages/cdh`, `scripts`, or root render artifacts; `WORKDIR`/`CMD` match config. |
| S2 | latest ComfyUI | `comfyui.version="latest"`, Manager off | S2 command | comfy-cli install output, ComfyUI git/version evidence | Build succeeds or external conflict is recorded; latest behavior is identified from real install output. |
| S3 | nightly ComfyUI | `comfyui.version="nightly"`, Manager off | S3 command | comfy-cli install output, ComfyUI git/version evidence | Build succeeds or the conflict is recorded without an undocumented workaround. |
| S4 | Manager on, no nodes | `install_manager=true`, no custom nodes | S4 command | `cm_cli` availability, Manager source/version, manager requirements relationship | Manager is available from final venv; Manager source/version assumptions are verified from the final image. |
| S5 | registry node | default candidate `comfyui-custom-scripts@latest`; optional heavier candidate `comfyui-impact-pack@latest` for broader coverage | S5 command | cache update log, one update invocation, `cm_cli update-cache`, install log, installed custom node path | Clean-cache registry install succeeds; version/source evidence recorded; failure is classified as product, upstream, or network behavior. |
| S6 | git node | `https://github.com/pythongosssss/ComfyUI-Custom-Scripts.git@609f3afaa74b2f88ef9ce8d939626065e3247469` | S6 command | git ref evidence, install order, installed path | Git URL/ref install succeeds and source/ref are recorded. |
| S7 | hooks matrix | `.sh` and `.py` hooks in both pre-install and post-install phases | S7 command | hook side-effect files, cwd/env output, log order | All four hook phase/type combinations execute with expected cwd/env; failure behavior is covered by controlled negative or integration evidence. |
| S8 | httpx downloader | local public/small HTTP server or small public asset, redirect, overwrite, skip | S8 command | build log, target file contents/path, request log, overwrite/skip evidence | Real httpx backend covers download, redirect, overwrite, skip, and target path. |
| S9 | aria2 downloader | local public/small HTTP asset through aria2 backend | S9 command | aria2 RPC log, process cleanup check, target file, no secret in logs | Real aria2 backend succeeds; no persistent RPC config/process remains; secret is absent from captured logs/config. |
| S10 | mixed full workflow | nodes, hooks, httpx, aria2, custom env/CMD | S10 command | full build log, context/image inspection, startup check | Combined workflow succeeds; serial order and backend override evidence captured; image starts configured ComfyUI from `WORKSPACE`. |

## Resource And Cleanup Guidance

These actions can be expensive during a smoke run:

- repeated `docker buildx build --load`;
- pulling CUDA, uv, Python, ComfyUI, comfy-cli, Manager, and dependency layers;
- loading temporary images into the local Docker daemon;
- starting containers for inspection/startup checks;
- using CUDA runtime checks when startup verification needs them.

Expected resource impact:

- time: potentially tens of minutes to multiple hours depending on upstream
  availability and cache state;
- disk: multiple CUDA-derived images and build cache layers;
- network: Docker Hub/GHCR, GitHub, PyPI, Comfy registry, and small local/public
  HTTP assets;
- secrets: none.

Cleanup guidance:

- tag smoke images with `cdh-smoke:*`;
- record exact image IDs before cleanup;
- remove only smoke images/containers created by the fixture run;
- do not remove unrelated Docker images, volumes, or caches;
- preserve rendered contexts and evidence logs until they are no longer needed.

## Evidence Capture Rules

For every executed scenario, record:

1. Config path and SHA256.
2. Exact `cdh host build` command.
3. Rendered context path and `.cdh-rendered` marker status.
4. Image tag and image ID.
5. Selected build log excerpts without private tokens.
6. Image inspection command and output.
7. Container filesystem inspection command and output.
8. Startup command and output where applicable.
9. Whether failures are cdh defects or external upstream/network instability.

If behavior conflicts with documented expectations, record the conflict instead
of hiding it in a workaround.
