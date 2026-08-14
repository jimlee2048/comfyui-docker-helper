# Cross-module contracts

This document records current authority, ownership, trust, replay, evidence, transfer, and process invariants that cross implementation modules. It is for maintainers changing those boundaries. User operations remain in the [build guide](../user/build-and-lock.md) and [runtime guide](../user/runtime.md).

## Planning and serialized authority

The planning path has four distinct authorities. They must not be collapsed into a second planner or used in the reverse direction.

### Canonical request graph

The [canonical request graph](../../src/comfyui_docker_helper/config/canonical_request.py) is immutable and process-local. It normalizes the effective configuration, target, release inputs, admitted user-authored package requirements, and checkout-owned upstream requirements into one source for desired resolution and BuildPlan phase projection. User-authored marker text remains part of image-configuration identity, while only members active for the fixed target enter ownership and marker-free resolver requests. It is deliberately not a serialized public artifact.

Resolution code may satisfy the graph's desired identities, but it must not invent phase behavior. BuildPlan construction may project phase behavior, but it must use the same graph rather than reconstructing intent from lock rows. This shared origin prevents resolver and image execution from becoming parallel planners.

### Canonical lock

The [canonical lock](../../src/comfyui_docker_helper/config/canonical_lock.py) is strict schema-v1 host reconciliation state. It records exact external or content results. Resolver-backed domains retain the normalized request identity needed to decide whether each result is still reusable. Content-owned local executables use their canonical relative path and digest and deliberately have no resolver request. A content-locked local build file likewise owns a target-keyed SHA-256 row, while an unlocked local build file creates no content row. Request identity and acquired result remain separate concepts.

The lock is complete for its typed direct-input domains, but it is not an installation script or a complete transitive artifact lock. A resolved user package result preserves matching request identity and an exact top-level distribution result while remaining artifact-free. Container helpers do not read the lock, and materialized `config.lock.toml` is excluded from Buildx input.

### Reconciliation policy and purpose

[Canonical reconciliation](../../src/comfyui_docker_helper/config/canonical_resolver.py) separates provider policy from caller purpose. Default, locked, and upgrade policies decide whether compatible results are reused, rejected, refreshed, or acquired. Apply, check, and dry-run purposes decide whether the accepted result may later be published, compared, or previewed. The resolver itself performs no filesystem write.

A no-write purpose may still need provider or Docker-backed acquisition. Locked policy instead requires the existing resolver-backed identities and freshly read local content identities to match without external provider calls. Docker Buildx is downstream of successful planning and remains outside reconciliation policy. The [user build guide](../user/build-and-lock.md#reconciliation-modes) owns the operational mode matrix.

### BuildPlan

The [BuildPlan](../../src/comfyui_docker_helper/config/build_plan.py) is strict schema-v1 immutable build execution authority. It is constructed once from the canonical request graph and an accepted lock. Construction rejects missing, incompatible, duplicate, or unused lock identities and binds the image-configuration and canonical-lock digests. A user package plan combines the matching accepted request with its exact top-level lock result; an authored direct source remains request-owned rather than becoming lock-owned. A local build-file plan carries its normalized target, fixed context slot, verification mode, and an opted-in digest, but never its host locator. Image configuration excludes Secret source definitions, host local-file execution policy, and the process-local publication fields `build.tags` and `build.output`, while retaining safe Git and downloader credential route metadata needed by image execution.

The canonical `build-plan.json` remains in the rendered context. Each Dockerfile instruction that consumes it mounts that context file read-only at the fixed internal path for the lifetime of that instruction; the Plan is admitted as a regular file whose bytes match the expected digest, and the mount does not persist into the final image filesystem. The [container admission boundary](../../src/comfyui_docker_helper/container/build_plan_input.py) then exposes only command-specific typed projections. Installers and other helpers must not reload host configuration, read the canonical lock, reconstruct unrelated phases, or introduce a runtime Plan consumer.

Resolved publication tags and output selection belong to the host's process-local Buildx output plan. They never enter the canonical lock, BuildPlan, rendered context, final manifest, or image identity. CLI tags replace configured tags as one publication list; neither source may become a second image-construction authority.

### Final manifest

The [final manifest schema](../../src/comfyui_docker_helper/config/final_manifest.py) and [observer](../../src/comfyui_docker_helper/container/final_manifest.py) own strict schema-v1 final image evidence. Observation runs only after build mutations, re-proves the selected final state, binds the image-config, lock, and BuildPlan digests, and publishes no partial manifest when an observation fails.

The manifest is downstream evidence. It is not configuration, a resolver result, canonical-lock input, BuildPlan input, an attestation, a support verdict, or a general application health check.

## Host local-filesystem boundaries

### Cooperative source reads

Host Secret files, selected baked hook sources, and local build files are collaborative local inputs. cdh validates their canonical absolute local path shape, statically inspects the components observed during admission, rejects an observed symlink, junction or other reparse point, or special file, and opens the leaf without following it. Regular-file type, size, consumed bytes, and any applicable byte bound or content identity are derived from that same descriptor or handle. Windows additionally rejects device and extended namespaces, alternate data streams, reserved DOS device components, UNC paths, and mapped or remote drives where the required local-file observations cannot be established.

Local build files have no cdh size ceiling and are consumed through fixed-size bounded-memory reads. Opting into content locking streams SHA-256 into the canonical lock and BuildPlan and requires later materialization to match that identity. The unlocked path performs the same shape, target, separation, and materialization admission but deliberately computes no cdh content digest. Neither mode serializes the host locator. Closing the admitted descriptor or handle does not replace an earlier operation failure; a close failure remains observable when no primary failure exists.

This source-read boundary is not an atomic filesystem snapshot or a namespace-isolation guarantee. cdh does not defend these user-selected paths against an untrusted local process that concurrently mutates their directory or contents; callers must not expose the selected input directory to that threat. The shared reader also admits fixed read-only artifacts inside the Linux image, but this limitation does not weaken cdh-owned private state or any container download target, write, replacement, runtime-state, or executable-containment boundary. Those container boundaries retain their independent containment and placement rules.

### cdh-owned private state

cdh creates host-private session and rendering state with `0700` directories and `0600` files on POSIX. On Windows it supplies a protected DACL limited to the current user and SYSTEM in the creation call and verifies the created or opened object and its ACL through the same handle; it does not retain and validate handles for every ancestor of the platform temporary directory. Cross-process coordination uses `filelock`'s native descriptor lock while cdh retains descriptor, path, cleanup, and error-priority ownership.

## Materialization boundary

[Host rendering](../../src/comfyui_docker_helper/host/render_service.py) constructs the accepted lock and BuildPlan. [Materialization](../../src/comfyui_docker_helper/rendering/final_materializer.py) does not plan. It accepts exactly one validated BuildPlan, the exact canonical cdh wheel, and the complete set of plan-owned local executable and local build-file inputs. Host locators travel only through this in-memory materialization boundary and never enter the lock, Plan, context metadata, final manifest, or image configuration.

The internal materializer accepts only a fresh, empty private stage created by the host service. It rereads local inputs through the cooperative source-read boundary and writes the complete expected tree. Local build files occupy deterministic Plan-owned regular-file slots. Copy mode streams into an independent slot; clone mode requires a copy-on-write clone; automatic mode attempts the clone and falls back to streaming copy only for classified unsupported or cross-filesystem results. Linux supplies the descriptor-based `FICLONE` primitive; platforms without an admitted clone primitive report it unavailable rather than substituting a hardlink or symlink. A locked local file is rehashed against its Plan identity, while an unlocked file is copied or cloned without computing a cdh digest.

POSIX materialization applies and compares deterministic directory and file modes, including `0644` for local build-file slots. Windows materialization does not treat NTFS permission bits as POSIX mode evidence; the Dockerfile applies `0644` to runtime configuration and local build targets and `0755` to selected hook trees. A no-write context check hashes a locked local slot against the existing intended digest; for an unlocked slot it first rejects a shape or size mismatch and otherwise compares bytes in fixed chunks. It does not construct another complete local-file copy. The host service is the sole owner of whole-stage cleanup and either publishes the complete stage or compares a complete expected tree.

First publication renames the complete stage into place. Replacement is restricted to a marked cdh-owned context and uses a portable two-rename sequence: the host moves the old context to a unique backup, renames the complete stage into place, and attempts an in-process restore if the second rename fails. This sequence is not a gap-free or crash-durable directory exchange; an interrupted overwrite can leave the output absent and the complete old context in its backup. These rules keep partial output, host-local paths, stale hook bytes, and mismatched wheel bytes from becoming Docker input.

The Dockerfile is rendered from the BuildPlan only. Host reconciliation state and the ownership marker are excluded through `.dockerignore`; the BuildPlan, canonical wheel, derived runtime configuration, and verified selected hooks are the build inputs. The Plan is supplied to each owning build helper by a read-only context bind, and cdh-owned behavior does not retain it at `/opt/cdh/build/build-plan.json` in the final filesystem; this retention rule does not hide the rendered context or BuildKit input/cache from the selected builder. Trusted hooks and custom-node installers running in the same instruction can deliberately read, copy, or disclose mounted inputs, just as they can for other inputs in their execution boundary.

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
- Host Docker access has two boundaries. Tag-to-descriptor acquisition goes through the selected Docker Engine and remains metadata-only, so daemon-owned registry routing and authentication apply without materializing image layers. Exact-image materialization and inspection, resolver-container lifecycle, and Buildx use the CLI-backed Docker transport. The current adapters are the official Docker SDK and `python-on-whales`, respectively; neither transport dependency is serialized as an image identity.
- The one validated canonical cdh wheel, bound by version and SHA-256, is the package-to-image installation boundary.

Current version equality between uv and `uv_build` does not couple them. The installed cdh package has no host uv runtime dependency. Canonical wheel construction uses PyPA's isolated build interface and package-owned release projection inputs rather than the repository checkout as an installed runtime source.

The host Buildx invocation owns selected external-cache specifications and passes them as opaque values through the Docker transport. Docker and Buildx own backend syntax, credentials, compatibility, and cache transport. Cache selection is external execution state, so it remains outside the canonical planning, materialization, and evidence chain and carries no cdh replay or authentication claim.

The release projection deliberately contains the package/build metadata and runtime resources required for the canonical wheel. The root and release-projection project tables declare identical package-owned inline Markdown `project.readme.text`; repository README file bytes are not package or wheel inputs. Replacing the inline value with a repository file or otherwise changing this input boundary requires an explicit packaging decision rather than an incidental documentation edit.

## Python and PyTorch package-source ownership

User-authored `python.extra_packages`, `python.uv_tools`, and `pytorch.extra_packages` are standard named requirements admitted through `packaging.Requirement`. Target markers are evaluated against the fixed configured CPython/Linux `amd64` environment before package ownership. Checkout-owned ComfyUI requirements remain a separate content authority: they may constrain the protected PyTorch foundation or enter ordinary application installation, but they cannot contribute a user package source.

cdh-controlled ordinary Python resolution and installation use the typed `[python].index_url`. A target-active admitted named HTTP(S) or Git-over-HTTP(S) direct reference instead remains opaque source text in the canonical request and BuildPlan. Ambient pip or uv package configuration must not replace either authority. Target-active package references are public configuration that enters the rendered context; target-active configured direct uv-tool references additionally enter Dockerfile instruction text and may appear in image history. They reject URL userinfo and have no Secret-routing contract.

Target-active configured PyTorch members and target-active protected requirements from the exact ComfyUI checkout form one atomic direct group. Its members partition exactly by source: every index-backed member uses the CUDA-derived explicit PyTorch index, while a non-protected configured direct reference keeps its authored source and receives no index route. Ordinary transitive dependencies use the Python index. A missing index-backed direct member must fail rather than fall back to a same-named package from the ordinary source.

The exact direct results and any compatibility constraint derived from the selected torch wheel protect later cdh-controlled application mutations. Protected PyTorch foundation members cannot use a direct reference. Checkout-owned requirements, including Manager requirements, and cdh-admitted direct-Git root requirements cannot change package sources or introduce direct URL, VCS, local, editable, or raw-option inputs. User-authored package direct references remain confined to their accepted owner and use the existing uv resolver/install path rather than a downloader or alternate installer.

Registry Manager and the Direct-Git Python processes for root requirements and `install.py` receive the BuildPlan-owned ordinary and CUDA-derived PyTorch indexes. Runtime and isolated-build constraint projections both retain the exact PyTorch-group results, while only the runtime projection adds the selected torch wheel's setuptools compatibility; installer-specific propagation of ordinary runtime constraints into build isolation remains installer-owned. uv considers compatible candidates across both indexes, matching pip's merged-candidate behavior, and ambient pip or uv configuration cannot replace these plan-owned inputs. This environment governs available sources and protected versions without widening the [custom-node identity and trust contract](#custom-node-identity-order-and-trust).

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

- A Registry node is selected by exact locked `id@version` and controlled through verified checkout-owned `cm-cli`. Success requires proof of the normalized installed project identity and version; process exit zero alone is insufficient. Manager remains the trusted executor of node-specific installation effects.
- A direct-Git node retains the configured raw URL as an acquisition locator, while the exact root commit and recursive gitlinks are the content authority. User Git and SSH configuration may rewrite transport, so cdh does not attest the network endpoint.

Direct-Git automatic execution is limited to a root `requirements.txt` and then root `install.py` when present. cdh parses and admits the root requirements before installation; dependencies chosen internally by Registry Manager or a Direct-Git `install.py` remain trusted executor effects and may include a node-authored moving VCS source that cdh does not independently lock or attest. cdh re-proves admitted repository and target state across these mutations, but an exact commit does not make the resulting worktree or arbitrary script effects deterministic. Final custom-node observation does not import or execute node code.

## Host Secret source and credential boundary

Top-level host Secret definitions are build-only source locators. Each logical source selects exactly one environment variable or file and is acquired lazily inside one command-scoped [host Secret session](../../src/comfyui_docker_helper/host/secret_session.py). Raw acquisition is consumer-neutral and cached once per logical source; Git password and downloader Bearer consumers validate that same snapshot independently. POSIX environment bytes are preserved exactly; Windows environment strings are encoded as UTF-8. File sources use the [cooperative source-read boundary](#cooperative-source-reads) with a consumer-neutral 65,525-byte credential limit. Snapshots are private regular files in cdh-owned private state, are protected by the cross-process descriptor lock, and are reused for the same logical source. POSIX can issue a content-free warning for a permissive source mode. Windows source-file ACL restriction remains the operator's responsibility because cdh does not approximate effective ACL access with POSIX mode bits or implement a general Windows access evaluator; cdh-owned snapshots still use a protected DACL. Normal stack unwinding attempts cleanup after success, handled failure, or `KeyboardInterrupt`. An ordinary cleanup failure is observable without exposing a path: it is the command error when no primary failure exists, or one warning while the primary failure is preserved. This contract does not promise infallible deletion, secure erase, crash recovery, or cleanup after uncatchable termination.

The canonical request graph and BuildPlan carry only normalized route match context, username, and a stable logical Secret ID. They never carry the source kind, source locator, or resolved credential value. The host Git provider uses the command session through a fixed credential helper with inherited helpers reset, path-aware matching enabled, and interactive prompting disabled. The helper remains a Git-defined shell snippet; on Windows cdh converts the Python executable to Git-for-Windows forward-slash form before shell quoting it. Buildx receives each distinct Secret needed by a direct-Git plan once under its stable required mount ID. Host paths are passed to the Docker adapter as path values rather than manually shell-quoted strings, and only Buildx's Secret CSV fields use CSV encoding. The image helper re-admits the expected BuildPlan, applies the same longest-match route policy, and reads only the selected fixed mount.

Credential mounts span the complete combined custom-node instruction. Root clone and checkout, provenance checks, recursive submodules, and applicable Git calls share the fixed helper policy; SSH forwarding and known-hosts mounts remain independent and may coexist. Git configuration can rewrite a URL before helper selection, and redirects remain Git-owned, so a credential route selects a context rather than attesting the contacted endpoint.

The structural non-persistence guarantee is limited to cdh-owned behavior: cdh does not print or persist resolved values or Secret source locators in the canonical lock, BuildPlan, rendered context, final manifest, image metadata, image history, or cdh-controlled final filesystem. Credential values do not appear in Git URLs or command arguments. Hooks and custom-node installers are trusted code inside the same instruction and can deliberately read, print, transform, or copy mounted values; cdh provides no general sandbox or arbitrary-output redactor.

BuildKit Secret contents do not ordinarily invalidate the combined instruction cache; only Secret IDs and mount properties participate in that cache key. Token rotation can therefore reuse a completed layer. cdh must not add a token digest or other value-derived cachebuster to serialized authority; callers use ordinary BuildKit cache controls when they require a fresh authentication attempt.

Downloader credential routes carry only canonical scheme, host, effective port and raw path-segment scope, Bearer type, logical Secret reference, and consumer-isolated stable mount ID into the canonical request and BuildPlan. Query is preserved as part of the ordinary file request but is not a credential selector. `host build` acquires the complete distinct route Secret set only when the accepted plan contains an effective HTTPX file, then grants those required mounts only to the download instruction. The image helper re-admits the Plan and applies the same bounded Bearer value validation to the mounted bytes; host validation and rendering remain value-lazy.

The HTTPX adapter owns one narrow per-request authorization policy rather than redirect control. Before each actual outbound request it removes only a Bearer value previously injected by cdh, performs longest path-segment-prefix selection on the transport-effective scheme, host, port, and path, and injects the selected route's value. A match replaces any current Authorization; without a match, Authorization that cdh did not own remains transport-owned. Thus leaving every route removes the prior cdh token, another route switches tokens, and HTTPS-to-HTTP receives no credential unless an explicit HTTP route matches. aria2 remains available for public downloads but is rejected before network activity when an initial protected URL selects it because its redirect behavior cannot enforce this scope.

Downloader source locators, resolved values, generated Authorization bytes, and value hashes never enter canonical lock, BuildPlan, rendered metadata, final manifest, image metadata/history, or the final filesystem. Safe route metadata affects build identity and remains visible to the rendered context and selected builder, but the ephemeral Plan mount keeps that aggregate out of the final rootfs. BuildKit Secret contents do not invalidate the download instruction cache; no value-derived cachebuster is added. Build downloader routes and Secret definitions never project into baked runtime configuration.

For an opted-in host build containing a direct-Git node, default SSH-agent forwarding and existing known-hosts files are invocation-only compatibility inputs. POSIX hosts require a non-empty `SSH_AUTH_SOCK` and discover the defined user and system known-hosts paths. Windows does not use `SSH_AUTH_SOCK` as a precondition: cdh passes BuildKit's `default` agent selector through and lets Docker report whether the current named-pipe or agent setup is usable, and it discovers only user-profile known-hosts paths. These inputs do not enter configuration, canonical reconciliation, the BuildPlan, the rendered context, final observation, or runtime configuration. The direct-Git Dockerfile projection declares stable optional BuildKit mount identities and forces strict host-key checking with ambient container SSH configuration disabled. cdh and BuildKit do not automatically copy or persist the agent endpoint, host paths, or trust contents into an image layer, and private key bytes remain agent-owned.

The SSH and trust mounts span the existing complete custom-node instruction, so selected hooks, Registry/direct-Git installers, and other trusted code in that instruction can access the agent socket and mounted trust files when forwarding is enabled. Such code can deliberately copy or disclose mounted trust content; cdh does not sandbox it, restrict agent use to Git, or attest per-use confirmation. Authentication, host-key, and submodule errors remain Git/OpenSSH diagnostics streamed through Buildx. SSH-agent state and mounted trust contents do not ordinarily invalidate the instruction cache, so a cache hit may reuse a completed layer without exercising the current inputs.

## Executable input identity and opaque effects

Selected baked build hooks and baked runtime hooks enter the canonical lock and BuildPlan as a canonical relative path plus SHA-256 source identity. Host absolute source paths remain materialization-only data. Admission and materialization re-verify the selected source bytes, and final image observation re-verifies retained baked hook bytes. Build-hook execution also re-verifies the expected digest and executes sealed bytes. Runtime startup validates hook shape and order, but does not re-authenticate baked or mounted runtime-hook bytes against the BuildPlan.

That digest proves the executable bytes, not their filesystem, network, process, or package effects. Hooks and custom-node installers run as trusted code without a cdh sandbox. A checksum must never be described as effect reproducibility or isolation.

Verified build-hook source remains in the final image as audit input and can remain visible in image layers; it must not contain secrets. Baked runtime hooks are image inputs, while deployment-mounted runtime hooks remain trusted external inputs outside the image lock.

## Final observation and replay ceiling

cdh verifies selected direct identities and their typed consumers and records the final observed state. The resulting evidence does not strengthen the identity guarantees of its inputs:

- exact direct Python package versions do not imply a complete transitive graph or wheel hashes;
- a target-active authored package URL or moving VCS ref plus an exact installed version does not identify fetched bytes, a redirect target, or a VCS commit; locked host reconciliation does not contact the source, but image execution may still need to fetch and install that active source;
- Registry `id@version` does not imply a cdh-verified archive or tree digest;
- a Git commit and gitlinks do not attest the retrieval endpoint, a clean post-script worktree, or arbitrary installer effects;
- executable digests do not identify script effects;
- checksum-free downloads have no cdh authenticity claim; and
- runtime configuration, mounted hooks, environment overrides, and other deployment mutations are outside image replay.

Accordingly, the contract is not an offline-build, byte-identical-build, complete software-bill-of-materials, artifact-attestation, deterministic opaque-code, or production-health guarantee. The final manifest observes accepted outcomes; it must never feed resolution or retroactively promote an observed value into a replay identity.

## Runtime, transfer, and process boundaries

The [runtime and lifecycle guide](../user/runtime.md) owns deployment instructions, supported overrides, file outcomes, and operator-visible timing. This section defines the cross-module ownership rules that implementations must preserve.

### Runtime configuration stays separate from build planning

[`RuntimeConfig`](../../src/comfyui_docker_helper/config/runtime_models.py) is the strict per-generation admission schema. [`runtime_config.py`](../../src/comfyui_docker_helper/config/runtime_config.py) owns the precedence of code defaults, baked defaults, mounted input, and supported environment input, then validates one effective runtime value for the initial generation and each manually restarted successor. Each admission rereads baked and mounted inputs but uses the environment snapshot captured when `runtime serve` began. Environment supplied only to an exec or SSH control client does not become generation input. Host configuration and the BuildPlan project only HTTP file defaults; local build-file sources, host downloader routes and Secret sources, and their host execution policy remain build-only. Mounted runtime configuration may independently declare downloader routes and exactly-one env/file source per logical Secret. Generation admission does not rerun host resolution, installation planning, or image materialization.

The generation-owned runtime Secret session validates source structure and references during admission but reads content only when an actual protected request first selects the logical Secret. It caches one exact value or content-safe acquisition failure per name for that generation across synchronous/asynchronous work and redirects. Restart discards the session; a successor re-resolves projected files while retaining the process-start environment snapshot. Runtime file locators are absolute container paths and may traverse deployment-managed symlink projections, but the opened target must be a regular file and satisfy the same 65,525-byte bounded read. Deployment owns file mode, ACL, namespace delivery, and access by other code under the same UID.

The runtime loader deliberately distinguishes known host-only input from unknown runtime input: the former is ignored with a warning, while the latter fails validation. Broadly accepting host configuration at runtime would create a second, misleading build authority.

[`runtime_lifecycle.py`](../../src/comfyui_docker_helper/container/runtime_lifecycle.py) owns construction of the ComfyUI process arguments from structured runtime fields. Runtime validation must continue to reject extra arguments that would replace cdh-owned listen, port, or auto-launch controls. Mounted configuration and environment values are deployment inputs and therefore remain outside the baked image's verified replay boundary.

### Runtime control has one owner and no local fallback

One [`runtime_serve.py`](../../src/comfyui_docker_helper/container/runtime_serve.py) process remains Tini's direct child and owns the controller, control endpoint, log broker, and every serial runtime generation. A control client never starts a lifecycle locally or assumes ownership when the endpoint is missing, rejects it, or returns a malformed response. [`runtime_controller.py`](../../src/comfyui_docker_helper/container/runtime_controller.py) owns cross-generation arbitration, operation and generation identities, and the in-memory status snapshot; [`runtime_lifecycle.py`](../../src/comfyui_docker_helper/container/runtime_lifecycle.py) owns one generation and admits a pending restart only on the main lifecycle thread after child liveness has been resolved.

The private Linux Unix-domain endpoint admits only the same effective UID and uses bounded, strict typed frames. This is a container-local ownership boundary, not isolation from other code already running as that UID. A missing endpoint, rejected peer, malformed frame, or disconnected client fails that client without starting a second owner or changing runtime policy. Controller status, operation identities, and restart results remain in memory and must not enter runtime download state.

An accepted restart is a complete serial replacement. The old generation requests cancellation of its download and SSH owners, runs ordered stop hooks while ComfyUI remains available, terminates ComfyUI, and reaps all exact cdh-owned work before a successor can be admitted. Concurrent restart requests fail busy rather than queue, merge, or join an operation. A Docker shutdown outranks a pending or active restart, makes successor admission impossible, and cannot create a later deadline. Natural ComfyUI exit retains the ordinary container-exit contract rather than becoming an idle controller state.

An old-generation stop-hook failure prevents successor admission after exact owned cleanup and makes cdh exit nonzero. A successor admission failure starts no successor owners. A failure after successor admission cleans its exact hook, download, SSH, and ComfyUI owners under that successor's admitted timeout and does not run successor stop hooks. Both failure paths publish a failed operation result and make cdh exit nonzero. Terminal delivery to a directly waiting client may use a short bounded acknowledgement drain, but client loss must never prevent exit or extend an external shutdown deadline.

### Original container output remains primary

[`runtime_logging.py`](../../src/comfyui_docker_helper/container/runtime_logging.py) owns a controller-lifetime byte-preserving tee for stdout and stderr. It writes each stream to the saved original container descriptor before publishing it to live followers, so the original descriptors and the deployment logging backend remain the primary log authority. The broker and its pipe drains do not end at a generation boundary: a released hook or SSH descendant may retain a standard-output descriptor and continue producing unattributed output, as it could through ordinary container logs.

Followers receive only bytes published after subscription, with no replay, persistence, generation attribution, or runtime-state coupling. Each follower has bounded independent buffering and may be disconnected when it falls behind; no follower may backpressure primary output or change lifecycle policy. Failure of a primary drain is instead controller-fatal: it wakes the sole runtime owner, triggers exact managed cleanup, and ends cdh nonzero rather than silently discarding authoritative output.

### Transfer policy, mechanism, and state have different owners

The transfer path deliberately splits policy from mechanism:

| Owner | Owns | Must not become |
| --- | --- | --- |
| Build and runtime orchestrators | Ordering, attempt budgets, applicable backend and mode selection, failure policy, and cancellation | A second placement implementation |
| [`transfer_core.py`](../../src/comfyui_docker_helper/container/transfer_core.py) | Target admission, transfer identity, transport sinks, verification, atomic final placement, durability, and exact cleanup | A scheduler or runtime-policy interpreter |
| Transport adapters in [`download_files.py`](../../src/comfyui_docker_helper/container/download_files.py) | Moving bytes through the sink supplied by the core | Owners of final paths, overwrite policy, verification, or cleanup |
| Runtime state modules | Minimal persisted recovery authority and its transition API | User configuration, history, telemetry, or a second retry policy |

Build orchestration currently shares [`download_files.py`](../../src/comfyui_docker_helper/container/download_files.py) with the transport adapters. HTTP build-file declarations are authoritative final content: a matching checksum may avoid a transfer, but otherwise the core atomically replaces the target without a build-time conditional-overwrite branch. Runtime orchestration is split between [`runtime_files.py`](../../src/comfyui_docker_helper/container/runtime_files.py) and [`runtime_downloads.py`](../../src/comfyui_docker_helper/container/runtime_downloads.py) and retains its independent conditional-overwrite policy. Module placement does not weaken the ownership boundaries in the table.

Local build files do not enter the transfer core or runtime state. The rendered Dockerfile places each admitted context slot with `COPY --link` after application and custom-node mutations and before final observation. That instruction makes the declared local bytes authoritative over lower image state while keeping source acquisition, context isolation, target containment, and final evidence in their existing owners. Runtime file planning admits only HTTP inputs, and only build HTTP declarations project into baked defaults.

The HTTPX adapter can write through a core-opened sink. aria2 instead requires an anchored directory/name interface and maintains its own control file. The core defends ordinary replacement races and unsafe filesystem shapes around that interface, but it cannot isolate aria2 artifacts from independently malicious code running with the same effective UID. Tests and documentation must not turn those defenses into a same-UID isolation claim.

One HTTPX credential policy is evaluated before each actual request for build and runtime. Runtime credential acquisition failure is a typed local failure at the file-policy seam, not an HTTP response and not a retryable transport attempt. Failure before the initial request records zero network attempts; redirect-time acquisition preserves earlier request-attempt evidence. The existing synchronous/asynchronous `continue` or `fail` policy decides queue behavior without creating a second retry engine. Route, reference, source locator, value, and value hash remain outside desired download identity and persisted runtime state, so credential rotation affects pending requests but does not make a completed target stale.

A configured checksum is trusted content intent supplied from outside the transport. Without it, completed transport and atomic placement do not prove content authenticity. Existing final content remains in place until a complete verified replacement commits through rename. Rename is the placement commit point: a later durability, final-verification, cleanup, or state-persistence failure remains fatal, but the complete new final may remain and is not rolled back. Containment, type, permission, identity, persistence, and durability failures fail closed and are never converted into ordinary runtime `continue` outcomes. The user guide owns the complete target-result matrix.

### Runtime state is minimal recovery authority

[`runtime_state.py`](../../src/comfyui_docker_helper/container/runtime_state.py) owns the strict serialized recovery model and durable store. [`runtime_download_state.py`](../../src/comfyui_docker_helper/container/runtime_download_state.py) owns current-run state transitions, while [`runtime_files.py`](../../src/comfyui_docker_helper/container/runtime_files.py) owns reconciliation against desired files.

The state records only the startup generation, desired download identity, actionable recovery status, and exact resume authority needed for safe reconciliation. It is not a durable attempt log, error history, progress feed, or user-editable control surface. Do not add telemetry fields and then make runtime policy depend on them.

Reconciliation acts only on desired input and state-backed transfer authority. It preserves completed final files when configuration changes and does not replace exact ownership with a broad filesystem scan. Invalid state or a required persistence failure stops reconciliation. The user runtime guide owns mounting and persistence instructions.

One active cdh instance owns the state file, its state-indexed transfer namespaces, and the declared targets governed by those entries. Container replacement must be serial: the old owner stops before its replacement takes ownership. Overlapping rolling instances, replicas sharing writable state, and different state files controlling the same target or staging namespace are not supported. The state `run_id` admits the synchronous-to-asynchronous handoff within one admitted runtime generation; it is not a lock or lease between instances, a controller operation identity, status authority, or logging channel.

### SSH protects a narrow credential path, not arbitrary values

[`ssh.py`](../../src/comfyui_docker_helper/container/ssh.py) owns root credential preparation and foreground sshd startup. Password material reaches credential tooling through standard input rather than command arguments or diagnostic text. Image finalization removes package-generated host keys; an activated SSH service generates container host keys at startup. [`runtime_ssh_service.py`](../../src/comfyui_docker_helper/container/runtime_ssh_service.py) owns the published startup operation, foreground child, monitoring, bounded shutdown, and reap.

These controls protect cdh's explicit credential path. They are not a general secret-classification or redaction system for arbitrary TOML values, URLs, environment variables, process arguments, rendered contexts, images, or logs. Root-access risk, host-port publication, registry exposure, and deployment access control remain outside this boundary.

### Baked and mounted hooks have different identity authority

Baked runtime hooks cross [`runtime_hook_inputs.py`](../../src/comfyui_docker_helper/host/runtime_hook_inputs.py), the [`canonical lock`](../../src/comfyui_docker_helper/config/canonical_lock.py), the [`BuildPlan`](../../src/comfyui_docker_helper/config/build_plan.py), and [`final materialization`](../../src/comfyui_docker_helper/rendering/final_materializer.py) as selected content-identified image inputs. Mounted hooks are external deployment inputs and never enter that identity chain.

[`runtime_hooks.py`](../../src/comfyui_docker_helper/container/runtime_hooks.py) discovers the two sources without turning them into an override overlay: baked hooks run first, followed by mounted hooks, with lexical ordering inside each source and phase. Both sources are trusted executable code. Content identity for baked bytes does not sandbox their effects or make their filesystem, network, package, or process behavior reproducible.

Host baking and container discovery select only direct regular `.sh` and `.py` files inside known phase directories. Ordinary unselected files and directories are ignored without recursion and reported through bounded source/phase warnings; symlinks, special files, inspection/read failures, invalid phase paths, and selected-hook failures remain hard boundaries.

### Readiness gates post-start hooks only

[`readiness.py`](../../src/comfyui_docker_helper/container/readiness.py) and the lifecycle owner invoke the loopback ComfyUI probe only when post-start hooks exist. A successful probe admits those hooks; process exit or timeout fails that startup path. It is not continuous monitoring, a container health check, or evidence that nodes, models, workflows, GPUs, or production workloads function correctly.

Do not move this probe into unconditional startup or give it wider health meaning without defining a new public lifecycle contract.

### Hook ownership ends when the leader is reaped

Each active hook runs as a new session leader. [`runtime_hooks.py`](../../src/comfyui_docker_helper/container/runtime_hooks.py) owns that leader and its process group through cancellation or deadline escalation, terminal result, and leader reap. [`process_control.py`](../../src/comfyui_docker_helper/container/process_control.py) provides the narrow spawn, signal, terminate, kill, wait, and reap mechanisms; it does not decide lifecycle policy.

After the hook leader's terminal result is accepted and reaped, cdh releases that execution's group authority. It does not enumerate descendants, discover or supervise deliberately backgrounded services, health-check them, or broadly signal them later. A process that detaches from the original group is also outside hook ownership.

A hook-created service therefore requires a paired stop hook that uses the service's control interface or a carefully validated process identity and waits for exit. A missing or failed stop hook, natural ComfyUI exit, external `SIGKILL`, or container teardown carries no graceful-shutdown guarantee. Tini reaping an orphan is process hygiene, not service supervision.

### One owner and one monotonic shutdown timeline

[`final_renderer.py`](../../src/comfyui_docker_helper/rendering/final_renderer.py) places Tini at PID 1 and cdh as its direct child. Tini forwards the container signal and reaps adopted orphans. [`runtime_controller.py`](../../src/comfyui_docker_helper/container/runtime_controller.py) owns irreversible cross-generation shutdown arbitration, while [`runtime_lifecycle.py`](../../src/comfyui_docker_helper/container/runtime_lifecycle.py) owns lifecycle and exact process policy for one generation. Neither listener threads nor control clients signal managed processes or run hooks.

Acceptance of a manual restart creates one monotonic shutdown timeline from the old generation's admitted `shutdown_timeout`. Without an active restart, the first accepted `SIGTERM` or `SIGINT` creates the same kind of timeline. Asynchronous downloads and SSH begin cancellation, ordered stop hooks run while ComfyUI is available, ComfyUI receives the restart's fixed `SIGTERM` or the original external signal, and cdh waits for the exact managed processes to terminate and be reaped. An external shutdown accepted while restart is in progress adopts the existing timeline and suppresses the successor; it cannot extend or restart the budget. Components may use narrower bounds inside that timeline, but they must not mint fresh deadlines that extend it. A repeated catchable external signal requests immediate force escalation.

Disabling the cdh outer and hook deadline does not disable component bounds or an orchestrator's independent hard limit. An external `SIGKILL` permits no further cleanup.

Natural ComfyUI exit follows a different path: cdh preserves its result, disables later signal admission, cleans up owned auxiliary work, and does not run signal-only stop hooks. Do not turn natural exit into synthetic signal shutdown or broadcast signals beyond exact cdh-owned processes. Operator-facing timeout values and external-grace configuration remain in the user runtime guide.
