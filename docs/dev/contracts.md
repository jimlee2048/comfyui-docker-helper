# Cross-module contracts

This document records current authority, ownership, trust, replay, evidence, transfer, and process invariants that cross implementation modules. It is for maintainers changing those boundaries. User operations remain in the [build guide](../user/build-and-lock.md) and [runtime guide](../user/runtime.md).

## Planning and serialized authority

The planning path has four distinct authorities. They must not be collapsed into a second planner or used in the reverse direction.

### Canonical request graph

The [canonical request graph](../../src/comfyui_docker_helper/config/canonical_request.py) is immutable and process-local. It normalizes the effective configuration, target, release inputs, and admitted upstream requirements into one source for desired resolution and BuildPlan phase projection. It is deliberately not a serialized public artifact.

Resolution code may satisfy the graph's desired identities, but it must not invent phase behavior. BuildPlan construction may project phase behavior, but it must use the same graph rather than reconstructing intent from lock rows. This shared origin prevents resolver and image execution from becoming parallel planners.

### Canonical lock

The [canonical lock](../../src/comfyui_docker_helper/config/canonical_lock.py) is strict schema-v1 host reconciliation state. It records exact external or content results. Resolver-backed domains retain the normalized request identity needed to decide whether each result is still reusable; content-owned local executables instead use their canonical relative path and digest and deliberately have no resolver request. Request identity and acquired result remain separate concepts.

The lock is complete for its typed direct-input domains, but it is not an installation script or a complete transitive artifact lock. Container helpers do not read it, and materialized `config.lock.toml` is excluded from Buildx input.

### Reconciliation policy and purpose

[Canonical reconciliation](../../src/comfyui_docker_helper/config/canonical_resolver.py) separates provider policy from caller purpose. Default, locked, and upgrade policies decide whether compatible results are reused, rejected, refreshed, or acquired. Apply, check, and dry-run purposes decide whether the accepted result may later be published, compared, or previewed. The resolver itself performs no filesystem write.

A no-write purpose may still need provider or Docker-backed acquisition. Locked policy instead requires the existing resolver-backed identities and freshly read local content identities to match without external provider calls. Docker Buildx is downstream of successful planning and remains outside reconciliation policy. The [user build guide](../user/build-and-lock.md#reconciliation-modes) owns the operational mode matrix.

### BuildPlan

The [BuildPlan](../../src/comfyui_docker_helper/config/build_plan.py) is strict schema-v1 immutable build execution authority. It is constructed once from the canonical request graph and an accepted lock. Construction rejects missing, incompatible, duplicate, or unused lock identities and binds the effective configuration and canonical-lock digests.

The canonical `build-plan.json` is admitted in the container as a regular file whose bytes match the expected digest. The [container admission boundary](../../src/comfyui_docker_helper/container/build_plan_input.py) then exposes only command-specific typed projections. Installers and other helpers must not reload host configuration, read the canonical lock, or reconstruct unrelated phases.

### Final manifest

The [final manifest schema](../../src/comfyui_docker_helper/config/final_manifest.py) and [observer](../../src/comfyui_docker_helper/container/final_manifest.py) own strict schema-v1 final image evidence. Observation runs only after build mutations, re-proves the selected final state, binds the effective-config, lock, and BuildPlan digests, and publishes no partial manifest when an observation fails.

The manifest is downstream evidence. It is not configuration, a resolver result, canonical-lock input, BuildPlan input, an attestation, a support verdict, or a general application health check.

## Materialization boundary

[Host rendering](../../src/comfyui_docker_helper/host/render_service.py) constructs the accepted lock and BuildPlan. [Materialization](../../src/comfyui_docker_helper/rendering/final_materializer.py) does not plan. It accepts exactly one validated BuildPlan, the exact canonical cdh wheel, and the complete set of plan-owned local executable inputs.

The internal materializer accepts only a fresh, empty private stage created by the host service. It reopens local inputs as contained regular files, verifies their digests before copying them, and applies deterministic directory and file modes. The host service is the sole owner of whole-stage cleanup and either publishes the complete stage or compares a complete expected tree.

First publication renames the complete stage into place. Replacement is restricted to a marked cdh-owned context and uses a portable two-rename sequence: the host moves the old context to a unique backup, renames the complete stage into place, and attempts an in-process restore if the second rename fails. This sequence is not a gap-free or crash-durable directory exchange; an interrupted overwrite can leave the output absent and the complete old context in its backup. These rules keep partial output, host-local paths, stale hook bytes, and mismatched wheel bytes from becoming Docker input.

The Dockerfile is rendered from the BuildPlan only. Host reconciliation state and the ownership marker are excluded through `.dockerignore`; the BuildPlan, canonical wheel, derived runtime configuration, and verified selected hooks are the build inputs.

## Managed Python authority

The root and release-projection [`pyproject.toml`](../../pyproject.toml) files own the supported Python minor envelope. Public configuration selects an exact stable patch within that envelope, and the [exact ledger](../../src/comfyui_docker_helper/exact_ledger.py) owns the omitted-value default.

The typed [acceptance catalog](../../tests/acceptance_scenarios.py) owns the exact release-test profiles. Those profiles are evidence, not a product allowlist: another exact stable patch inside the package envelope remains admissible. Neither documentation nor a fixture list may narrow the package-supported range.

Planning resolves one exact uv-managed CPython artifact for the selected target platform and libc and binds its catalog identity to the exact uv image descriptor. Rendering installs and verifies that exact interpreter. It must not silently change version, select another provider, or introduce an automatic fallback.

## Application and isolated tool ownership

The application environment at `/opt/venv` owns ComfyUI and application packages. cdh, optional comfy-cli, and each configured uv tool own separate environments under `/opt/uv/tools`, with public command links under `/opt/uv/bin`.

cdh runs from its own environment and invokes the application interpreter explicitly when application ownership is required. It must not import the application environment as its dependency domain. Public executable ownership is exclusive: command collisions fail instead of force-replacing another tool's link.

This separation prevents application dependency mutations from changing the host/runtime control tool and prevents user tools from becoming implicit image construction authorities.

## uv, release backend, Docker transport, and cdh wheel

Four related toolchain identities have independent owners:

- The configuration-selected official uv OCI image resolves to an exact descriptor. That descriptor owns uv-backed canonical resolution and the final image's `uv` and `uvx`.
- The exact `uv_build` requirement in the root and [release projection](../../src/comfyui_docker_helper/resources/release-projection/pyproject.toml) owns only the isolated backend used to build the cdh wheel.
- `python-on-whales` owns the closed host Docker transport used by resolver containers and Buildx. It is not serialized as an image identity.
- The one validated canonical cdh wheel, bound by version and SHA-256, is the package-to-image installation boundary.

Current version equality between uv and `uv_build` does not couple them. The installed cdh package has no host uv runtime dependency. Canonical wheel construction uses PyPA's isolated build interface and package-owned release projection inputs rather than the repository checkout as an installed runtime source.

The release projection deliberately contains the package/build metadata and runtime resources required for the canonical wheel. The root and release-projection project tables declare identical package-owned inline Markdown `project.readme.text`; repository README file bytes are not package or wheel inputs. Replacing the inline value with a repository file or otherwise changing this input boundary requires an explicit packaging decision rather than an incidental documentation edit.

## Python and PyTorch package-source ownership

cdh-controlled ordinary Python resolution and installation use the typed `[python].index_url`. Ambient pip or uv package configuration must not replace that authority.

Configured PyTorch members and target-active protected requirements from the exact ComfyUI checkout form one atomic direct group. Every direct member uses only the CUDA-derived explicit PyTorch index; ordinary transitive dependencies use the Python index. A missing direct member must fail rather than fall back to a same-named package from the ordinary source.

The exact direct results and any compatibility constraint derived from the selected torch wheel protect later cdh-controlled application mutations. Requirements consumed by cdh cannot change package sources or introduce direct URL, VCS, local, editable, or raw-option inputs. This keeps upstream requirements and later installers from replacing the selected inference foundation or package-source policy.

## Official ComfyUI source and requirements

cdh owns one fixed official ComfyUI repository. A user selector resolves to one exact commit. The container exclusively creates the required-absent final directory, materializes the detached checkout there, and proves its repository, HEAD, and ancestry from the immutable support-floor commit before package mutation.

The exact checkout owns the admitted root `requirements.txt` snapshot and, when Manager is enabled, `manager_requirements.txt`. Host acquisition, canonical lock, BuildPlan, and container installation must refer to that same source identity and content. cdh installs the checkout directly; comfy-cli is not an installer authority, and Manager has no independent floating source.

## Manager/cm-cli and comfy-cli

Manager and comfy-cli are independently controlled optional capabilities. Both are enabled when their configuration switches are omitted, and either can be disabled without disabling the other.

Manager belongs to the application environment and the exact ComfyUI checkout. cdh verifies its declared distributions, import bridge, and exclusive `/opt/venv/bin/cm-cli` command ownership. Registry custom nodes require this capability and use that absolute command as their control boundary.

comfy-cli is a separately resolved isolated user tool under `/opt/uv/tools`. Its public commands are not invoked during image construction, and it is not the Registry control path. Enabling Manager also does not imply a ComfyUI runtime activation argument; runtime arguments remain separately owned.

## Custom-node identity, order, and trust

Registry and direct-Git custom nodes share one declaration-order, fail-fast orchestrator:

```text
pre-install hooks -> node installation -> post-install hooks
```

The source types retain distinct identity contracts:

- A Registry node is selected by exact locked `id@version` and controlled through verified checkout-owned `cm-cli`. Success requires proof of the normalized installed project identity and version; process exit zero alone is insufficient.
- A direct-Git node retains the configured raw URL as an acquisition locator, while the exact root commit and recursive gitlinks are the content authority. User Git and SSH configuration may rewrite transport, so cdh does not attest the network endpoint.

Direct-Git automatic execution is limited to a root `requirements.txt` and then root `install.py` when present. Hooks and installers are trusted code. cdh re-proves admitted repository and target state across these mutations, but an exact commit does not make the resulting worktree or arbitrary script effects deterministic. Final custom-node observation does not import or execute node code.

For an opted-in host build containing a direct-Git node, default SSH-agent forwarding and existing default user/system known-hosts files are invocation-only compatibility inputs. They do not enter configuration, canonical reconciliation, the BuildPlan, the rendered context, final observation, or runtime configuration. The direct-Git Dockerfile projection declares stable optional BuildKit mount identities and forces strict host-key checking with ambient container SSH configuration disabled. cdh and BuildKit do not automatically copy or persist the socket, host paths, or trust contents into an image layer, and private key bytes remain agent-owned.

The SSH and trust mounts span the existing complete custom-node instruction, so selected hooks, Registry/direct-Git installers, and other trusted code in that instruction can access the agent socket and mounted trust files when forwarding is enabled. Such code can deliberately copy or disclose mounted trust content; cdh does not sandbox it, restrict agent use to Git, or attest per-use confirmation. Authentication, host-key, and submodule errors remain Git/OpenSSH diagnostics streamed through Buildx. SSH and secret contents do not ordinarily invalidate the instruction cache, so a cache hit may reuse a completed layer without exercising the current inputs.

## Executable input identity and opaque effects

Selected baked build hooks and baked runtime hooks enter the canonical lock and BuildPlan as a canonical relative path plus SHA-256 source identity. Host absolute source paths remain materialization-only data. Admission and materialization re-verify the selected source bytes, and final image observation re-verifies retained baked hook bytes. Build-hook execution also re-verifies the expected digest and executes sealed bytes. Runtime startup validates hook shape and order, but does not re-authenticate baked or mounted runtime-hook bytes against the BuildPlan.

That digest proves the executable bytes, not their filesystem, network, process, or package effects. Hooks and custom-node installers run as trusted code without a cdh sandbox. A checksum must never be described as effect reproducibility or isolation.

Verified build-hook source remains in the final image as audit input and can remain visible in image layers; it must not contain secrets. Baked runtime hooks are image inputs, while deployment-mounted runtime hooks remain trusted external inputs outside the image lock.

## Final observation and replay ceiling

cdh verifies selected direct identities and their typed consumers and records the final observed state. The resulting evidence does not strengthen the identity guarantees of its inputs:

- exact direct Python package versions do not imply a complete transitive graph or wheel hashes;
- Registry `id@version` does not imply a cdh-verified archive or tree digest;
- a Git commit and gitlinks do not attest the retrieval endpoint, a clean post-script worktree, or arbitrary installer effects;
- executable digests do not identify script effects;
- checksum-free downloads have no cdh authenticity claim; and
- runtime configuration, mounted hooks, environment overrides, and other deployment mutations are outside image replay.

Accordingly, the contract is not an offline-build, byte-identical-build, complete software-bill-of-materials, artifact-attestation, deterministic opaque-code, or production-health guarantee. The final manifest observes accepted outcomes; it must never feed resolution or retroactively promote an observed value into a replay identity.

## Runtime, transfer, and process boundaries

The [runtime and lifecycle guide](../user/runtime.md) owns deployment instructions, supported overrides, file outcomes, and operator-visible timing. This section defines the cross-module ownership rules that implementations must preserve.

### Runtime configuration stays separate from build planning

[`RuntimeConfig`](../../src/comfyui_docker_helper/config/runtime_models.py) is the strict container-start schema. [`runtime_config.py`](../../src/comfyui_docker_helper/config/runtime_config.py) owns the precedence of code defaults, baked defaults, mounted input, and supported environment input, then validates one effective runtime value. Host configuration and the BuildPlan may project baked defaults, but container startup does not rerun host resolution, installation planning, or image materialization.

The runtime loader deliberately distinguishes known host-only input from unknown runtime input: the former is ignored with a warning, while the latter fails validation. Broadly accepting host configuration at runtime would create a second, misleading build authority.

[`runtime_lifecycle.py`](../../src/comfyui_docker_helper/container/runtime_lifecycle.py) owns construction of the ComfyUI process arguments from structured runtime fields. Runtime validation must continue to reject extra arguments that would replace cdh-owned listen, port, or auto-launch controls. Mounted configuration and environment values are deployment inputs and therefore remain outside the baked image's verified replay boundary.

### Transfer policy, mechanism, and state have different owners

The transfer path deliberately splits policy from mechanism:

| Owner | Owns | Must not become |
| --- | --- | --- |
| Build and runtime orchestrators | Ordering, attempt budgets, applicable backend and mode selection, failure policy, and cancellation | A second placement implementation |
| [`transfer_core.py`](../../src/comfyui_docker_helper/container/transfer_core.py) | Target admission, transfer identity, transport sinks, verification, atomic final placement, durability, and exact cleanup | A scheduler or runtime-policy interpreter |
| Transport adapters in [`download_files.py`](../../src/comfyui_docker_helper/container/download_files.py) | Moving bytes through the sink supplied by the core | Owners of final paths, overwrite policy, verification, or cleanup |
| Runtime state modules | Minimal persisted recovery authority and its transition API | User configuration, history, telemetry, or a second retry policy |

Build orchestration currently shares [`download_files.py`](../../src/comfyui_docker_helper/container/download_files.py) with the transport adapters. Runtime orchestration is split between [`runtime_files.py`](../../src/comfyui_docker_helper/container/runtime_files.py) and [`runtime_downloads.py`](../../src/comfyui_docker_helper/container/runtime_downloads.py). Module placement does not weaken the ownership boundaries in the table.

The HTTPX adapter can write through a core-opened sink. aria2 instead requires an anchored directory/name interface and maintains its own control file. The core defends ordinary replacement races and unsafe filesystem shapes around that interface, but it cannot isolate aria2 artifacts from independently malicious code running with the same effective UID. Tests and documentation must not turn those defenses into a same-UID isolation claim.

A configured checksum is trusted content intent supplied from outside the transport. Without it, completed transport and atomic placement do not prove content authenticity. Existing final content remains in place until a complete verified replacement commits through rename. Rename is the placement commit point: a later durability, final-verification, cleanup, or state-persistence failure remains fatal, but the complete new final may remain and is not rolled back. Containment, type, permission, identity, persistence, and durability failures fail closed and are never converted into ordinary runtime `continue` outcomes. The user guide owns the complete target-result matrix.

### Runtime state is minimal recovery authority

[`runtime_state.py`](../../src/comfyui_docker_helper/container/runtime_state.py) owns the strict serialized recovery model and durable store. [`runtime_download_state.py`](../../src/comfyui_docker_helper/container/runtime_download_state.py) owns current-run state transitions, while [`runtime_files.py`](../../src/comfyui_docker_helper/container/runtime_files.py) owns reconciliation against desired files.

The state records only the startup generation, desired download identity, actionable recovery status, and exact resume authority needed for safe reconciliation. It is not a durable attempt log, error history, progress feed, or user-editable control surface. Do not add telemetry fields and then make runtime policy depend on them.

Reconciliation acts only on desired input and state-backed transfer authority. It preserves completed final files when configuration changes and does not replace exact ownership with a broad filesystem scan. Invalid state or a required persistence failure stops reconciliation. The user runtime guide owns mounting and persistence instructions.

One active cdh instance owns the state file, its state-indexed transfer namespaces, and the declared targets governed by those entries. Container replacement must be serial: the old owner stops before its replacement takes ownership. Overlapping rolling instances, replicas sharing writable state, and different state files controlling the same target or staging namespace are not supported. The state `run_id` admits the synchronous-to-asynchronous handoff within one container start; it is not a lock or lease between instances.

### SSH protects a narrow credential path, not arbitrary values

[`ssh.py`](../../src/comfyui_docker_helper/container/ssh.py) owns root credential preparation and foreground sshd startup. Password material reaches credential tooling through standard input rather than command arguments or diagnostic text. Image finalization removes package-generated host keys; an activated SSH service generates container host keys at startup. [`runtime_ssh_service.py`](../../src/comfyui_docker_helper/container/runtime_ssh_service.py) owns the published startup operation, foreground child, monitoring, bounded shutdown, and reap.

These controls protect cdh's explicit credential path. They are not a general secret-classification or redaction system for arbitrary TOML values, URLs, environment variables, process arguments, rendered contexts, images, or logs. Root-access risk, host-port publication, registry exposure, and deployment access control remain outside this boundary.

### Baked and mounted hooks have different identity authority

Baked runtime hooks cross [`runtime_hook_inputs.py`](../../src/comfyui_docker_helper/host/runtime_hook_inputs.py), the [`canonical lock`](../../src/comfyui_docker_helper/config/canonical_lock.py), the [`BuildPlan`](../../src/comfyui_docker_helper/config/build_plan.py), and [`final materialization`](../../src/comfyui_docker_helper/rendering/final_materializer.py) as selected content-identified image inputs. Mounted hooks are external deployment inputs and never enter that identity chain.

[`runtime_hooks.py`](../../src/comfyui_docker_helper/container/runtime_hooks.py) discovers the two sources without turning them into an override overlay: baked hooks run first, followed by mounted hooks, with lexical ordering inside each source and phase. Both sources are trusted executable code. Content identity for baked bytes does not sandbox their effects or make their filesystem, network, package, or process behavior reproducible.

### Readiness gates post-start hooks only

[`readiness.py`](../../src/comfyui_docker_helper/container/readiness.py) and the lifecycle owner invoke the loopback ComfyUI probe only when post-start hooks exist. A successful probe admits those hooks; process exit or timeout fails that startup path. It is not continuous monitoring, a container health check, or evidence that nodes, models, workflows, GPUs, or production workloads function correctly.

Do not move this probe into unconditional startup or give it wider health meaning without defining a new public lifecycle contract.

### Hook ownership ends when the leader is reaped

Each active hook runs as a new session leader. [`runtime_hooks.py`](../../src/comfyui_docker_helper/container/runtime_hooks.py) owns that leader and its process group through cancellation or deadline escalation, terminal result, and leader reap. [`process_control.py`](../../src/comfyui_docker_helper/container/process_control.py) provides the narrow spawn, signal, terminate, kill, wait, and reap mechanisms; it does not decide lifecycle policy.

After the hook leader's terminal result is accepted and reaped, cdh releases that execution's group authority. It does not enumerate descendants, discover or supervise deliberately backgrounded services, health-check them, or broadly signal them later. A process that detaches from the original group is also outside hook ownership.

A hook-created service therefore requires a paired stop hook that uses the service's control interface or a carefully validated process identity and waits for exit. A missing or failed stop hook, natural ComfyUI exit, external `SIGKILL`, or container teardown carries no graceful-shutdown guarantee. Tini reaping an orphan is process hygiene, not service supervision.

### One owner and one monotonic shutdown timeline

[`final_renderer.py`](../../src/comfyui_docker_helper/rendering/final_renderer.py) places Tini at PID 1 and cdh as its direct child. Tini forwards the container signal and reaps adopted orphans. cdh, through [`runtime_lifecycle.py`](../../src/comfyui_docker_helper/container/runtime_lifecycle.py), is the lifecycle policy owner.

The first accepted `SIGTERM` or `SIGINT` creates one monotonic shutdown timeline. Asynchronous downloads and SSH begin cancellation, ordered stop hooks run while ComfyUI is available, the original signal is forwarded to ComfyUI, and cdh waits for the exact managed processes to terminate and be reaped. Components may use narrower bounds inside that timeline; they must not mint fresh deadlines that extend it. A repeated catchable signal requests immediate force escalation.

Disabling the cdh outer and hook deadline does not disable component bounds or an orchestrator's independent hard limit. An external `SIGKILL` permits no further cleanup.

Natural ComfyUI exit follows a different path: cdh preserves its result, disables later signal admission, cleans up owned auxiliary work, and does not run signal-only stop hooks. Do not turn natural exit into synthetic signal shutdown or broadcast signals beyond exact cdh-owned processes. Operator-facing timeout values and external-grace configuration remain in the user runtime guide.
