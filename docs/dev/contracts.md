# Cross-module contracts

Use this guide when changing how modules share data, access credentials and files, or manage processes. It describes the rules those changes must preserve. User operations remain in the [build guide](../user/build-and-lock.md) and [runtime guide](../user/runtime.md).

## Planning and serialized authority

Use this section when changing resolution, serialization or the inputs passed to image helpers. Intent, acquisition results, execution plans and observed outcomes have different owners; none may reconstruct another's authority.

### Canonical request graph

The immutable, process-local [request graph](../../src/comfyui_docker_helper/config/planning/request.py) normalizes effective configuration and target requirements. Reconciliation and BuildPlan construction consume this same graph: resolution satisfies identities, while plan construction projects execution phases. Neither may independently infer intent from lock rows.

Public local-file requests contain target and content-lock intent, not host paths, file/tree classification or inventories. User package marker text participates in configuration identity, but only requirements active for the fixed target enter resolution. The graph is not a public serialized artifact.

### Host local-admission bundle

| Input | Owner and admitted result |
| --- | --- |
| Public local files/trees | [`local_inputs.py`](../../src/comfyui_docker_helper/host/context/local_inputs.py): one admission per effective declaration and preparation attempt, producing planning facts, materialization sources and warnings |
| Local custom nodes | [`local_nodes.py`](../../src/comfyui_docker_helper/host/context/local_nodes.py): a separate target-directory-keyed bundle with selected inventory and frozen ignore rules |
| Hooks and local executables | Separate executable admission/acquisition paths |

Planning, reconciliation and rendering reuse these bundles rather than rescanning live sources into new intent. Public trees inventory every real directory and regular file; opted-in content locking adds member records and one aggregate digest. Host paths remain process-local. `host validate` reads neither sources nor ignore files.

Local nodes use only the root `.dockerignore`, decoded as UTF-8 with optional BOM and matched through Docker SDK `PatternMatcher`. The shared walker owns safe traversal, required ancestors and selected members; fully excluded subtrees need not be opened. The root rule file is retained as a selected member. This does not promise complete Moby matching compatibility.

Rule presence or bytes changing during preparation fail even for unlocked nodes. Source revalidation reuses frozen selection; stage and published-context comparison inspect the selected snapshot without applying ignore rules again. Public file trees remain unfiltered and use a separate target domain, allowing later file overlays on nodes.

### Canonical lock

The strict schema-v1 [lock](../../src/comfyui_docker_helper/config/planning/canonical_lock.py) records host resolution results. Resolver-backed entries retain the request identity required for reuse; content-owned executables use relative path and digest without a resolver request.

| Local input | Locked record |
| --- | --- |
| File | Target-keyed SHA-256 |
| Public tree | Target-keyed aggregate digest |
| Local node | `custom_nodes.local` entry keyed by `target_dir`, with aggregate digest |
| Unlocked file/tree/node | No content row |

The lock does not duplicate member inventories or host paths. Direct package results identify the matching request and exact top-level version, not artifact bytes or all transitive dependencies. Container helpers never read it; `config.lock.toml` is excluded from Buildx input.

### Reconciliation policy and purpose

The [resolver](../../src/comfyui_docker_helper/config/planning/resolver.py) separates provider policy (default, locked, upgrade) from caller purpose (apply, check, dry-run), and performs no filesystem writes. No-write purposes may still acquire provider or Docker results. Locked policy instead requires matching existing results and opted-in local content identities without external acquisition; unlocked bytes remain outside that comparison.

See the [operator mode table](../user/build-and-lock.md#reconciliation-modes) for command usage. Buildx execution occurs downstream and is not made offline by locked reconciliation.

### BuildPlan

The immutable strict schema-v1 [BuildPlan](../../src/comfyui_docker_helper/config/planning/build_plan.py) is the sole serialized build execution authority. It is constructed from the request graph, accepted lock and matching admission bundles; missing, incompatible, duplicate or unused inputs fail construction.

- The plan binds image-configuration and lock digests. Direct package sources remain request-owned alongside their resolved top-level versions.
- Local files carry normalized targets, deterministic slots and optional digests. Trees carry the complete sorted inventory, with locked file sizes/digests. Node inputs use separate collections and `build/local-nodes/` slots.
- Tree modes derive from kind (`0755` directory, `0644` file), participate in digest rules and are applied/checked by execution. They are not independent user-authored values.
- Public file targets are absolute image paths. Admission derives their relative identity from the ComfyUI root to verify context slots, without storing a second target field.
- Host paths and ignore-policy objects stay process-local. Retained ignore-file bytes cross as selected data. Local-node input identity does not prohibit trusted installation mutations.
- Image configuration excludes secret sources, local-copy policy, tags and output selection. Safe credential route metadata needed for execution remains included. Canonical serialization includes null unverified identities.

Each consuming Dockerfile instruction mounts `build-plan.json` read-only for that instruction only. The [admission owner](../../src/comfyui_docker_helper/container/build/admission.py) verifies regular-file shape and the Dockerfile-supplied expected digest, then gives the helper its narrow typed projection. Helpers must not reload host configuration, read the lock or create a runtime Plan consumer.

Publication tags and output selection belong to the process-local Buildx output plan. They do not enter the lock, context, manifest or image identity. CLI tags replace the configured publication list as a whole.

### Final manifest

The strict schema-v1 [manifest](../../src/comfyui_docker_helper/config/evidence/manifest.py) is published by the [observer](../../src/comfyui_docker_helper/container/build/manifest/observer.py) only after all image mutations and required final proofs succeed. It binds configuration, lock and Plan digests; failed observation publishes no partial manifest.

Verified file/tree evidence records observed checksums only after comparison with Plan intent; unverified rows omit content identity. Tree rows are compact Plan-bound evidence, not another inventory. Reading the manifest alone does not rerun those comparisons. Keep the manifest downstream of planning: it records observed results and must not supply configuration or resolver inputs.

## Host local-filesystem boundaries

### Cooperative source reads

Host secrets, selected hooks and local sources are cooperative inputs. They are not isolated from a malicious process concurrently changing their namespace or contents. This limitation does not weaken private-state ownership or container write/execute containment.

| Boundary | Required behavior |
| --- | --- |
| Path admission | Inspect canonical absolute components; reject observed symlinks, junctions/reparse points and special files; open leaves without following links |
| Windows paths | Reject device/extended namespaces, alternate streams, reserved DOS names, UNC and mapped/remote drives where required observations cannot be established |
| Tree admission | Inventory real descendants, including hidden/empty directories; reject traversal failures and unsafe names; preserve exact case and Unicode |
| File reads | Derive type, size, consumed bytes and applicable limits/digests from the admitted descriptor/handle; use bounded-memory streaming |
| Unlocked planning | Observe structure without draining file contents or comparing content size/digest fields |
| Content locking | Hash file bytes or selected tree members and aggregate identity; only the aggregate tree identity enters the lock |
| Error priority | Closing a handle must not replace an earlier operation failure; report close failure when no primary failure exists |

Public trees have no cdh size ceiling and no ignore filtering. Local nodes use their separate selected inventory. Hardlinks are consumed as independent regular files; topology is not preserved. Image modes are derived from file kind rather than host metadata, and host locators are never serialized.

Structural revalidation checks paths and kinds without reading unlocked content. Complete readability is established by consumption (copy, explicit comparison or Docker), not by dry-run or a successful unlocked clone. The shared reader may also admit fixed read-only Linux-image inputs; it does not take ownership of container mutation policy.

### cdh-owned private state

cdh creates host-private session and rendering state with `0700` directories and `0600` files on POSIX. On Windows it supplies a protected DACL limited to the current user and SYSTEM in the creation call and verifies the created or opened object and its ACL through the same handle; it does not retain and validate handles for every ancestor of the platform temporary directory. Cross-process coordination uses `filelock`'s native descriptor lock while cdh retains descriptor, path, cleanup, and error-priority ownership.

## Materialization boundary

### Inputs

[Host rendering](../../src/comfyui_docker_helper/host/context/service.py) constructs the accepted lock and BuildPlan using the public-local admission bundle alongside separate local-executable identities. [Materialization](../../src/comfyui_docker_helper/rendering/final_materializer.py) does not plan. It accepts exactly one validated BuildPlan, the exact canonical cdh wheel, and the complete set of plan-owned local executable, local file/tree, and local-node inputs. Host locators travel only through this in-memory materialization boundary and never enter the lock, Plan, context metadata, final manifest, or image configuration.

### Acquisition and copy policy

- The host service creates a fresh, empty private stage. Materialization writes only the complete expected context into that stage.
- Local files use deterministic Plan-owned slots; public trees use `build/trees/` with their complete inventory, and local nodes use `build/local-nodes/` with their selected inventory.
- Acquisition retains the [cooperative source-read contract](#cooperative-source-reads). Tree paths and kinds are rechecked before and after acquisition without reading content for those structural checks.
- `copy` streams bytes; `clone` requires copy-on-write cloning; `auto` falls back only for classified clone unavailability. No mode substitutes a hardlink or symlink.
- Existing targets, permission/storage/I/O errors and post-clone normalization failures remain hard errors. Never delete an existing target to retry a clone.

| Platform | Clone mechanism | Destination handling |
| --- | --- | --- |
| Linux | `FICLONE` from the admitted source descriptor | Clone into the exclusively created target descriptor |
| macOS | `fclonefileat` from the admitted source descriptor | Create an absent leaf relative to an owned directory descriptor; apply the renderer-owned mode before opening it |
| No admitted clone primitive | Report unavailable | Only `auto` falls back to streaming copy |

The macOS adapter excludes user/system immutable and append-only source flags before creation: inheriting them prevents ordinary target handling or cleanup. Other flags and extended attributes do not preclude an attempt. Extended attributes remain intact; source ACL copying is not requested, ownership follows ordinary destination creation, and the source is unchanged.

### Verification and comparison

- Locked copy hashes transferred chunks; locked clone hashes the acquired target descriptor. Both must match Plan intent before publication. Unlocked acquisition computes no cdh content digest. These checks verify the acquired input, not continuing source stability.
- POSIX applies and compares deterministic modes (`0755` for selected directories, `0644` for files). Windows does not use NTFS mode bits as POSIX evidence; the Dockerfile applies the image-owned modes.
- A no-write context check creates placeholders from the admitted Plan without rescanning sources, then compares the complete expected structure. Locked slots are checked against their digests; explicit unlocked checks stream source/context bytes after shape and size checks. Locked reconciliation does not add byte comparison to unlocked inputs, and no check creates another complete source copy.

### Cleanup and publication

The host service alone owns whole-stage cleanup and publication. It publishes only a complete stage or compares a complete expected tree.

1. First publication renames the complete stage into place.
2. Replacement requires a marked cdh-owned context. The host moves it to a unique backup, then renames the new stage into place.
3. If the second rename fails, the host attempts to restore the backup.

This is not a gap-free or crash-durable directory exchange: interruption can leave the output absent and the old context in its backup. Partial output and mismatched inputs must not become Docker input.

### Docker build inputs

- The Dockerfile is rendered from the BuildPlan only. `.dockerignore` excludes host reconciliation state and the ownership marker.
- Build inputs are the BuildPlan, canonical wheel, wheel-derived workspace profile, derived runtime configuration and verified selected hooks. The profile comes from the retained wheel, not the installed package.
- Helpers receive the Plan through a read-only context bind; cdh does not retain it at `/opt/cdh/build/build-plan.json` in the final filesystem.
- The selected builder can still read context and cache inputs. Trusted hooks and installers in the same instruction can read, copy or disclose mounted inputs.

### Rendered context layout

This inventory is for maintainers debugging context generation. Operators should copy the whole context and use the [manual build instructions](../user/build-and-lock.md#build-a-prepared-context-directly).

| Path | Role |
| --- | --- |
| `.cdh-rendered` | Host ownership marker |
| `config.lock.toml` | Host reconciliation state |
| `build-plan.json` | Instruction-scoped execution authority |
| `bootstrap/comfyui_docker_helper-<version>-py3-none-any.whl` | Exact validated cdh wheel |
| `build/hooks/` | Referenced verified build scripts |
| `build/files/`, `build/trees/`, `build/local-nodes/` | Independent local input slots |
| `runtime/config.toml`, `runtime/hooks/` | Derived defaults and optional baked runtime scripts |
| `Dockerfile`, `.dockerignore` | Build instructions and input exclusions |

There is no root `config.toml`. The marker and lock are excluded from Buildx input. Host-source locators and secret values are not generated metadata; local source bytes and trusted scripts remain visible to the builder.

### Image local-tree projection and final evidence

This section governs public `[[files]]` directory trees. Local custom-node installation inputs have their separate [pre-hook copy and final-root evidence contract](#custom-node-identity-order-and-trust).

Complete file destinations and selected local-tree member paths reject `.cdh-staging` components and exact case-sensitive `.wh.` prefixes. The former protects transfer-owned staging; the latter prevents image-layer deletion markers from replacing literal input names. The same target grammar applies to authored HTTP/local and mounted runtime HTTP declarations; runtime consistency does not imply that ordinary runtime writes interpret whiteouts. Validate effective destination prefixes before planning or transfer, but do not apply this mapped-name restriction to host source locators or URLs. Source-directory basenames are not mapped members.

Each local tree is one logical image operation. Before copying any tree, the read-only, Plan-mounted `cdh container build validate-tree-targets` helper checks existing destination roots, required ancestors, and selected members. Missing paths and compatible file/directory overlays are allowed; links, special nodes, and incompatible shapes fail before a linked layer can replace them and erase the conflict. The check neither enumerates unrelated destination entries nor reads file contents.

The renderer emits one directory `COPY --link` from each private `build/trees/` context subtree to its exact target root, preserving source-relative contents without adding the source basename. Local file and tree destinations are escaped for Dockerfile COPY substitution so accepted literal names do not become environment-expanded paths. When any tree exists, the Dockerfile then mounts the BuildPlan read-only for `cdh container build normalize-local-trees`. That image normalizer admits only the Plan-projected roots and members, creates an empty root when needed, rejects links, special nodes, and incompatible shapes, and applies `0755` to selected directories and `0644` to selected files. Neither helper mutates, deletes, or claims unrelated lower-image entries.

After all image mutations, the read-only final observer re-admits only the expected tree roots and members, verifies containment, kind, and mode, and streams selected files only for a content-locked tree. It computes and compares the aggregate tree digest when required, then emits one compact local-tree evidence row. The row is Plan-bound evidence rather than a second inventory; the BuildPlan remains the only complete serialized/planning inventory authority, while process-local and typed execution projections carry only fields their owners require. Unrelated overlay content remains outside the evidence claim.

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

The uv CLI and `uv_build` backend are independently versioned. The installed cdh package has no host uv runtime dependency. Canonical wheel construction uses PyPA's isolated build interface and package-owned release projection inputs rather than the repository checkout as an installed runtime source.

The host Buildx invocation owns selected external-cache specifications and passes them as opaque values through the Docker transport. Docker and Buildx own backend syntax, credentials, compatibility, and cache transport. Cache selection is external execution state, so it remains outside the canonical planning, materialization, and evidence chain and carries no cdh replay or authentication claim.

The release projection deliberately contains the package/build metadata and runtime resources required for the canonical wheel. The root and release-projection project tables declare identical package-owned inline Markdown `project.readme.text`; repository README file bytes are not package or wheel inputs. Replacing the inline value with a repository file or otherwise changing this input boundary requires an explicit packaging decision rather than an incidental documentation edit.

## Python and PyTorch package-source ownership

User-authored `python.extra_packages`, `python.uv_tools`, and `pytorch.extra_packages` are standard named requirements admitted through `packaging.Requirement`. Target markers are evaluated against the fixed configured CPython/Linux `amd64` environment before package ownership. Checkout-owned ComfyUI requirements remain a separate content authority: they may constrain the protected PyTorch foundation or enter ordinary application installation, but they cannot contribute a user package source.

cdh-controlled ordinary Python resolution and installation use the typed `[python].index_url`. A target-active admitted named HTTP(S) or Git-over-HTTP(S) direct reference instead remains opaque source text in the canonical request and BuildPlan. Ambient pip or uv package configuration must not replace either authority. Target-active package references are public configuration that enters the rendered context; target-active configured direct uv-tool references additionally enter Dockerfile instruction text and may appear in image history. They reject URL userinfo and have no Secret-routing contract.

Target-active configured PyTorch members and target-active protected requirements from the exact ComfyUI checkout form one atomic direct group. Its members partition exactly by source: every index-backed member uses the CUDA-derived explicit PyTorch index, while a non-protected configured direct reference keeps its authored source and receives no index route. Ordinary transitive dependencies use the Python index. A missing index-backed direct member must fail rather than fall back to a same-named package from the ordinary source.

The exact direct results and any compatibility constraint derived from the selected torch wheel protect later cdh-controlled application mutations. Protected PyTorch foundation members cannot use a direct reference. Checkout-owned requirements, including Manager requirements, and cdh-admitted Git/local root requirements cannot change package sources or introduce direct URL, VCS, local, editable, or raw-option inputs. User-authored package direct references remain confined to their accepted owner and use the existing uv resolver/install path rather than a downloader or alternate installer.

Registry Manager and the Git/local root-install Python processes for root requirements and `install.py` receive the BuildPlan-owned ordinary and CUDA-derived PyTorch indexes. Runtime and isolated-build constraint projections both retain the exact PyTorch-group results, while only the runtime projection adds the selected torch wheel's setuptools compatibility; installer-specific propagation of ordinary runtime constraints into build isolation remains installer-owned. uv considers compatible candidates across both indexes, matching pip's merged-candidate behavior, and ambient pip or uv configuration cannot replace these plan-owned inputs. This environment governs available sources and protected versions without widening the [custom-node identity and trust contract](#custom-node-identity-order-and-trust).

### Package-build subprocess environment

cdh-controlled application and custom-node package mutations use a dedicated package-build subprocess environment. The [application installer](../../src/comfyui_docker_helper/container/build/application.py) owns the exact admitted names and constructs this environment from the narrow application process environment: it retains reviewed network proxy names and exact values owned by the invoking command, then copies only reviewed generic and BuildPlan-selected backend toolchain names that are present in the current container environment. It does not inherit an ambient `PATH`, package-manager configuration, or Python configuration. This constructor is a process boundary, not a second configuration or image-rendering authority.

Package-mutation owners additionally fix any uv cache location and link-materialization policy required by their operation. Those command-owned values are not admitted from ambient state or `[system.env]`, are not caller-overridable build arguments, and remain build-only rather than becoming persistent Docker `ENV` or runtime configuration. Production code and behavioral tests own their exact names and values.

Ordinary ComfyUI Git and observation subprocesses retain their narrow process environment, direct-Git Git operations retain their separate Git and credential contract, and trusted build hooks retain their full-environment contract. Persistent Docker `ENV` from the selected base image and `[system.env]` has a separate rendering and image owner; the package-build constructor neither re-emits nor revalidates it.

## Official ComfyUI source and requirements

cdh owns one fixed official ComfyUI repository. A user selector resolves to one exact commit. The container exclusively creates the required-absent final directory, materializes the detached checkout there, and proves its repository, HEAD, and ancestry from the immutable support-floor commit before package mutation.

The exact checkout owns the admitted root `requirements.txt` snapshot and, when Manager is enabled, `manager_requirements.txt`. Host acquisition, canonical lock, BuildPlan, and container installation must refer to that same source identity and content. cdh installs the checkout directly; comfy-cli is not an installer authority, and Manager has no independent floating source.

## Manager/cm-cli and comfy-cli

Manager and comfy-cli are independently controlled optional capabilities. Both are enabled when their configuration switches are omitted, and either can be disabled without disabling the other.

Manager belongs to the application environment and the exact ComfyUI checkout. cdh verifies its declared distributions, import bridge, and exclusive `/opt/venv/bin/cm-cli` command ownership. Registry custom nodes require this capability and use that absolute command as their control boundary.

comfy-cli is a separately resolved isolated user tool under `/opt/uv/tools`. Its public commands are not invoked during image construction, and it is not the Registry control path. Enabling Manager also does not imply a ComfyUI runtime activation argument; runtime arguments remain separately owned.

## Custom-node identity, order, and trust

One declaration-order, fail-fast orchestrator owns all node types:

| Source | Preparation | Installation |
| --- | --- | --- |
| Git | Pre-clone hooks → exclusive target creation → exact checkout and recursive submodule proof | Pre-install hooks → root requirements and `install.py` → post-install hooks |
| Registry | Manager and application preconditions | Pre-install hooks → Manager installation → post-install hooks |
| Local | Exclusive target creation → selected-source copy and proof | Same optional root installation files as Git, between pre/post hooks |

Registry identity is its exact locked resource/version, not arbitrary node-local metadata. Verified checkout-owned `cm-cli` controls installation; success requires proof of normalized installed project identity and version, not just exit zero. Direct Git retains its raw URL as locator, but the root commit and recursive gitlinks own content identity; transport rewrites remain Git-owned. Local nodes consume the Plan's selected inventory without reinterpreting ignore rules. Their exclusive copy applies image modes; locked copies prove size/digest and aggregate identity while transferring, without a second source scan or destination rehash. Unlocked copies do not hash.

The orchestrator keeps three states distinct: installed prefix, current prepared target and future absent targets. A current Git/local target joins Registry scan exclusions only after its source proof succeeds, and joins the installed prefix only after installation succeeds. Hook, acquisition and installation mutations invalidate the relevant application/Manager observations and require boundary proof. Adjacent identical checks with no intervening mutation may share a proof; final observation stays independent.

Root `requirements.txt` is read after pre-install hooks, followed by optional root `install.py`. Missing files do not suppress configured hooks. Hooks run from the ComfyUI root; `install.py` runs from the node root. Child environment changes do not propagate to the parent, and post-install hooks run only on success.

Trusted Manager/installers may choose dependencies or moving sources that cdh does not separately lock. Re-proving repository and target boundaries allows ordinary patches; it does not make script effects deterministic. Local final evidence checks only the real contained target root and emits Plan-bound `local-directory` / `direct-local` evidence, without importing code, hashing the final tree or rerunning installation. Build files apply afterward in their own evidence domain; node overlays do not reinstall dependencies or rerun hooks.

## Host Secret source and credential boundary

Change credential handling at the acquisition, consumer or transport owner below. A credential route selects where cdh supplies a value; it does not sandbox trusted code or attest a network endpoint.

### Acquisition and lifetime

The command-scoped [host session](../../src/comfyui_docker_helper/host/credentials/session.py) lazily reads exactly one env/file source per logical name and caches it once. Git-password and Bearer consumers validate the same raw snapshot independently. POSIX preserves environment bytes; Windows encodes its Unicode environment as UTF-8. Files use cooperative admission with a 65,525-byte bound.

Snapshots are private regular files protected by the native descriptor lock. POSIX may warn without content about permissive source modes; Windows source ACLs remain operator-owned, while snapshots use protected DACLs. Success, handled errors and interruption attempt cleanup. A cleanup failure becomes the command error only without a primary failure; otherwise warn without exposing the path. Uncatchable termination carries no deletion or secure-erase guarantee.

### Consumer grants

| Consumer | Acquisition and grant |
| --- | --- |
| Host Git provider | Fixed credential helper, inherited helpers reset, path-aware matching, no interactive prompts |
| Direct-Git image build | Every distinct secret referenced by effective routes, once under stable required BuildKit IDs, for the complete combined custom-node instruction |
| HTTPX image downloads | Every distinct downloader-route secret only when the Plan has an effective HTTPX file, granted only to the download instruction |
| Runtime HTTPX | Independent deployment sources; see [runtime configuration](#runtime-configuration-stays-separate-from-build-planning) |

The graph and Plan carry safe match context, username/type and logical IDs, never source kind/location or values. Image helpers re-admit the expected Plan and read only their fixed mounts. Windows helper executable paths use Git-for-Windows forward slashes before shell quoting. Host paths are adapter path values; Buildx Secret CSV fields use CSV encoding rather than shell escaping.

Root Git operations and recursive submodules share the helper policy. Git owns rewrites and redirects. HTTPX instead selects the longest scheme/host/effective-port/path-segment match before each actual request, with queries excluded from route selection. It removes a prior cdh Bearer value, replaces Authorization on a match, and otherwise leaves transport-owned authorization alone. Leaving all routes drops the cdh token; entering another switches it; HTTPS-to-HTTP requires a separately matching HTTP route. Initial protected aria2 requests are rejected before network activity.

### Persistence, trust and cache

cdh must not print or persist values, source locators, generated authorization or token hashes in its lock, Plan, context metadata, manifest, image metadata/history, final filesystem or diagnostics. Values must not enter URLs or command arguments. Safe route metadata remains visible to the builder and participates in build identity; downloader routes and secret sources do not project into runtime defaults.

Hooks and installers can read or disclose credentials granted to their instruction. This is a structural cdh non-persistence guarantee, not a sandbox or arbitrary-output redactor. BuildKit Secret contents do not normally invalidate an instruction cache; never add a value-derived cachebuster to serialized authority. Operators use BuildKit cache controls when they need a fresh authentication attempt.

### SSH invocation inputs

Opted-in `--ssh` forwarding applies only to a direct-Git plan. POSIX requires `SSH_AUTH_SOCK` and discovers default user/system known-hosts paths. Windows passes BuildKit's default agent selector without that precondition and discovers only user-profile trust paths. Docker owns agent compatibility.

These inputs bypass configuration, reconciliation, Plan, context and manifest identity. Rendering declares stable optional mounts and strict host-key checking with ambient container SSH configuration disabled. Private keys remain agent-owned; cdh does not automatically persist agent/trust mounts. Trusted code in the combined node instruction can use the agent or disclose trust content. Cache hits may skip current authentication, and Git/OpenSSH diagnostics remain external output.

## Executable input identity and opaque effects

Selected baked build hooks and baked runtime hooks enter the canonical lock and BuildPlan as a canonical relative path plus SHA-256 source identity. Host absolute source paths remain materialization-only data. Admission and materialization re-verify the selected source bytes, and final image observation re-verifies retained baked hook bytes. Build-hook execution also re-verifies the expected digest and executes sealed bytes. Runtime startup validates hook shape and order, but does not re-authenticate baked or mounted runtime-hook bytes against the BuildPlan.

That digest proves the executable bytes, not their filesystem, network, process, or package effects. Hooks and custom-node installers run as trusted code without a cdh sandbox. A checksum must never be described as effect reproducibility or isolation.

Verified build-hook source remains in the final image as audit input and can remain visible in image layers; it must not contain secrets. Baked runtime hooks are image inputs, while deployment-mounted runtime hooks remain trusted external inputs outside the image lock.

## Final observation and replay ceiling

The rendered instruction that consumes an immutable input owns its pre-use identity, integrity, and trust-boundary checks. Installed-state facts are observed after all build mutations by the final observer; installation steps do not maintain an equivalent second proof of managed-interpreter relationships, installed distributions and dependency health, or public-executable ownership. This separation keeps failure evidence authoritative without turning final observation into a planning input.

Local-tree final observation emits compact Plan-bound evidence after checking the selected image state; the complete root/member and digest rules are owned by [Image local-tree projection and final evidence](#image-local-tree-projection-and-final-evidence).

cdh verifies selected direct identities and their typed consumers and records the final observed state. The resulting evidence does not strengthen the identity guarantees of its inputs:

- exact direct Python package versions do not imply a complete transitive graph or wheel hashes;
- a target-active authored package URL or moving VCS ref plus an exact installed version does not identify fetched bytes, a redirect target, or a VCS commit; locked host reconciliation does not contact the source, but image execution may still need to fetch and install that active source;
- Registry `id@version` does not imply a cdh-verified archive or tree digest;
- a Git commit and gitlinks do not attest the retrieval endpoint, a clean post-script worktree, or arbitrary installer effects;
- local-node content identities bind selected installation inputs, not the final tree or installer effects;
- executable digests do not identify script effects;
- checksum-free downloads have no cdh authenticity claim; and
- runtime configuration, mounted hooks, environment overrides, and other deployment mutations are outside image replay.

Accordingly, the contract is not an offline-build, byte-identical-build, complete software-bill-of-materials, artifact-attestation, deterministic opaque-code, or production-health guarantee. The final manifest observes accepted outcomes; it must never feed resolution or retroactively promote an observed value into a replay identity.

## CLI observation and stream ownership

The root CLI owns one immutable detail setting per invocation. Quiet and verbose are mutually exclusive root-only options. They filter optional cdh presentation, never explicit results, warnings or controlled failures. `NO_COLOR` disables cdh color without changing content or stream ownership.

| Output | Owner and destination |
| --- | --- |
| Successful command results and explicit data | stdout; direct result/diagnostic and Typer owners remain in place |
| cdh progress, lifecycle facts, warnings and expected diagnostics | stderr |
| Host one-shot preparation | Terminal-aware presenter; plain flushed phase lines on redirected streams |
| Image one-shot helper events | Plain append-only stderr; directly interactive downloads may use progress display |
| Runtime lifecycle | Always durable plain stderr, even on a terminal |
| BuildKit library messages | Unstyled stdout lines after transient preparation stops |
| Inherited ComfyUI, hook, SSH or other child streams | Original producer-owned bytes and channels |

Inspect stdout and stderr capabilities independently. Only capable destinations admit transient layout or cursor controls. Validate may summarize ordered layers on interactive stdout; verbose exposes that semantic result on plain destinations. Host render/build preparation must remain observable without inventing a byte percentage, and stop transient output before the first BuildKit line. Normalize each yielded BuildKit message to one trailing newline, without prefix or detail filtering; this is not a byte-preserving inherited stream.

Workflow facts crossing a presentation boundary are immutable typed events. Producers own truthful phases, outcomes and admitted dynamic identities; presenters own filtering, layout and wording. Domain validators or controlled categories admit fields, and renderers handle control characters safely. Neither events nor diagnostics may reconstruct display text from raw configuration, exceptions, arguments, URLs, provider payloads, source locators or resolved secrets.

Explicit results need no event wrapper. Captured provider/protocol output stays adapter-owned and may become bounded diagnostics; inherited external output is never parsed, styled, sanitized or redacted by cdh. Teardown or delivery failure must not replace a business failure or prevent cleanup. Runtime background delivery follows its [broker contract](#original-container-output-remains-primary).

## Runtime, transfer, and process boundaries

For deployment instructions, supported overrides, file outcomes and timing, see the [runtime guide](../user/runtime.md). This section defines the cross-module ownership rules that implementations must preserve.

### Runtime configuration stays separate from build planning

[`RuntimeConfig`](../../src/comfyui_docker_helper/config/runtime/models.py) is the strict per-generation admission schema. [`runtime/config.py`](../../src/comfyui_docker_helper/config/runtime/config.py) owns the precedence of code defaults, baked defaults, mounted input, and supported environment input, then validates one effective runtime value for the initial generation and each manually restarted successor. Each admission rereads baked and mounted inputs but uses the environment snapshot captured when `runtime serve` began. Environment supplied only to an exec or SSH control client does not become generation input. Among file declarations, host configuration and the BuildPlan project only HTTP file defaults; local build-file sources (files or directory trees), host downloader routes and Secret sources, and their host execution policy remain build-only. Mounted runtime configuration may independently declare downloader routes and exactly-one env/file source per logical Secret. Generation admission does not rerun host resolution, installation planning, or image materialization.

The generation-owned runtime Secret session validates source structure and references during admission but reads content only when an actual protected request first selects the logical Secret. It caches one exact value or content-safe acquisition failure per name for that generation across synchronous/asynchronous work and redirects. Restart discards the session; a successor re-resolves projected files while retaining the process-start environment snapshot. Runtime file locators are absolute container paths and may traverse deployment-managed symlink projections, but the opened target must be a regular file and satisfy the same 65,525-byte bounded read. Deployment owns file mode, ACL, namespace delivery, and access by other code under the same UID.

The runtime loader deliberately distinguishes known host-only input from unknown runtime input: the former is ignored with a warning, while the latter fails validation. Broadly accepting host configuration at runtime would create a second, misleading build authority.

Controller logging uses the shared normalized [log settings](../../src/comfyui_docker_helper/config/logs.py), projected through the BuildPlan into baked runtime defaults. The runtime loader acquires one initial source-document snapshot, admits logging, and reuses those documents for full generation validation; early logging admission never exempts unrelated configuration from validation. Successors reread documents using the fixed environment. Valid changed log settings warn and defer until controller restart; invalid settings retain strict generation failure. A control client's environment or current view of mounted configuration is not a storage authority.

[`runtime/lifecycle.py`](../../src/comfyui_docker_helper/container/runtime/lifecycle.py) owns construction of the ComfyUI process arguments from structured runtime fields. Runtime validation must continue to reject extra arguments that would replace cdh-owned listen, port, or auto-launch controls. Mounted configuration and environment values are deployment inputs and therefore remain outside the baked image's verified replay boundary.

### Runtime control has one owner and no local fallback

One [`runtime/serve.py`](../../src/comfyui_docker_helper/container/runtime/serve.py) process remains Tini's direct child and owns the controller, control endpoint, log broker, and every serial runtime generation. A control client never starts a lifecycle locally or assumes ownership when the endpoint is missing, rejects it, or returns a malformed response. [`runtime/controller.py`](../../src/comfyui_docker_helper/container/runtime/controller.py) owns cross-generation arbitration, operation and generation identities, and the in-memory status snapshot; [`runtime/lifecycle.py`](../../src/comfyui_docker_helper/container/runtime/lifecycle.py) owns one generation and admits a pending restart only on the main lifecycle thread after child liveness has been resolved.

The private Linux Unix-domain endpoint admits only the same effective UID and uses bounded, strict typed frames. This is a container-local ownership boundary, not isolation from other code already running as that UID. A missing endpoint, rejected peer, malformed frame, or disconnected client fails that client without starting a second owner or changing runtime policy. Controller status, operation identities, and restart results remain in memory and must not enter runtime download state.

An accepted restart is a complete serial replacement. The old generation requests cancellation of its download and SSH owners, runs ordered stop hooks while ComfyUI remains available, terminates ComfyUI, and reaps all exact cdh-owned work before a successor can be admitted. Concurrent restart requests fail busy rather than queue, merge, or join an operation. A Docker shutdown outranks a pending or active restart, makes successor admission impossible, and cannot create a later deadline. Natural ComfyUI exit retains the ordinary container-exit contract rather than becoming an idle controller state.

An old-generation stop-hook failure prevents successor admission after exact owned cleanup and makes cdh exit nonzero. A successor admission failure starts no successor owners. A failure after successor admission cleans its exact hook, download, SSH, and ComfyUI owners under that successor's admitted timeout and does not run successor stop hooks. Both failure paths publish a failed operation result and make cdh exit nonzero. Terminal delivery to a directly waiting client may use a short bounded acknowledgement drain, but client loss must never prevent exit or extend an external shutdown deadline.

### Original container output remains primary

[`runtime/logging.py`](../../src/comfyui_docker_helper/container/runtime/logging.py) owns a controller-lifetime byte-preserving tee for stdout and stderr. It writes each stream to the saved original container descriptor before publishing it to live followers, so the original descriptors and the deployment logging backend remain the primary log authority. The broker and its pipe drains do not end at a generation boundary: a released hook or SSH descendant may retain a standard-output descriptor and continue producing unattributed output, as it could through ordinary container logs.

The broker publishes merged bytes in one observed order to the configured history owners and live subscribers. This order does not prove cross-process causal order or generation attribution. Each subscriber has bounded independent buffering and may be disconnected when it falls behind; no subscriber may backpressure primary output or change lifecycle policy. Failure of a primary drain is instead controller-fatal: it wakes the sole runtime owner, triggers exact managed cleanup, and ends cdh nonzero rather than silently discarding authoritative output.

Runtime semantic presentation lives inside the same broker lifetime and writes complete plain lines to its owned stderr. The main lifecycle emits directly on its serial owner. Genuine background download and SSH producers only offer typed facts to one controller-scoped delivery owner; offering is bounded and non-blocking, progress is coalesced by private scope, and informational saturation becomes one controlled aggregate after recovery. Controlled warning categories retain their existence under bounded pressure. Presentation or delivery failure remains secondary to lifecycle cleanup and primary-output authority, and all producer owners quiesce before delivery and the broker close.

### Retained logs are bounded optional copies

| Owner | Responsibility |
| --- | --- |
| [`log_history.py`](../../src/comfyui_docker_helper/container/runtime/log_history.py) | Bounded memory byte tail |
| [`log_storage.py`](../../src/comfyui_docker_helper/container/runtime/log_storage.py) | Independently bounded asynchronous queue, raw rotating-file writer and storage admission |
| [`log_query.py`](../../src/comfyui_docker_helper/container/runtime/log_query.py) | Fixed history ranges and subsequent live delivery |

File mode retains memory too. File acknowledgements track successful writes, including short writes, not power-loss durability. Queue ownership is independent of ring eviction; metadata, leases, buffers and peers have their own bounds. Disk I/O, sync and socket sends never run under the ordered publication lock.

Storage admits only the dedicated owned directory, exact regular files and non-inherited single-writer lock. Check descriptors, modes, links and replacement identities at the relevant boundary. Never repair mounts recursively or delete unrelated artifacts. Rotation preserves raw bytes and stable archives, including unfinished lines. Cooperative reads are allowed; concurrent mutation/shared writers are unsupported.

The first admission, write, rotation, sync or queue failure disables file recording for the controller lifetime. Memory/live output continues with warnings, without retry, alternate directory or backfill. Memory mode ignores old files; disabled recording may read safely admitted old files without mutation. Original container output remains unchanged.

Queries subscribe to later output atomically before replaying fixed history ranges. Use bounded reads and identity checks without pinning all payloads; emit file/memory overlap once. Gaps or eviction during replay make results incomplete, while ordinary eviction of an oldest prefix before a query is a retention limit. LF defines lines without decoding bytes; gaps cannot join fragments or prove that a missing interval contains no lines.

| Delivery event | Contract |
| --- | --- |
| Finite replay | Explicit completion required; incomplete replay never enters follow |
| Follow termination | Sole sender emits explicit live-end after final queued delivery |
| EOF, overflow or malformed/incomplete delivery | Client fails |
| Client interruption | Local only; application keeps running |
| Application replacement | Following continues |
| Controller exit | Following ends without automatic reconnect |

Payload goes to stdout, controlled diagnostics to stderr. Success covers the final stream cutoff, not future shutdown output or disk durability. Queries cannot fall back to Docker or become an offline storage owner; raw files cannot reconstruct unobservable volatile losses.

Lifecycle supplies a typed generation-scoped projection of the accepted monotonic shutdown deadline. External takeover preserves it; successful replacement clears it. Broker, writer, capture and control-server cleanup share a component deadline clipped to that budget. Natural/pre-admission exits use the bounded component allowance. Signals and the force latch remain active; no stage restarts the allowance or blocks the main thread on file sync. Forced termination or storage failure can lose pending bytes. Deployment persistence instructions belong in the [runtime guide](../user/runtime.md#persist-log-files).

### Transfer policy, mechanism, and state have different owners

The transfer path deliberately splits policy from mechanism:

| Owner | Owns | Must not become |
| --- | --- | --- |
| Build and runtime orchestrators | Ordering, attempt budgets, applicable backend and mode selection, failure policy, and cancellation | A second placement implementation |
| [`transfer/core.py`](../../src/comfyui_docker_helper/container/transfer/core.py) | Target admission, transfer identity, transport sinks, verification, atomic final placement, durability, and exact cleanup | A scheduler or runtime-policy interpreter |
| Transport adapters in [`transfer/httpx.py`](../../src/comfyui_docker_helper/container/transfer/httpx.py) and [`transfer/aria2.py`](../../src/comfyui_docker_helper/container/transfer/aria2.py) | Moving bytes through the sink supplied by the core | Owners of final paths, overwrite policy, verification, or cleanup |
| Runtime state modules | Minimal persisted recovery authority and its transition API | User configuration, history, telemetry, or a second retry policy |

Build orchestration lives in [`build/downloads.py`](../../src/comfyui_docker_helper/container/build/downloads.py), separate from the transport adapters. HTTP build-file declarations are authoritative final content: a matching checksum may avoid a transfer, but otherwise the core atomically replaces the target without a build-time conditional-overwrite branch. Runtime file orchestration is split across [`runtime/files/planning.py`](../../src/comfyui_docker_helper/container/runtime/files/planning.py), [`runtime/files/reconciliation.py`](../../src/comfyui_docker_helper/container/runtime/files/reconciliation.py), and [`runtime/files/download.py`](../../src/comfyui_docker_helper/container/runtime/files/download.py); [`runtime/downloads.py`](../../src/comfyui_docker_helper/container/runtime/downloads.py) separately owns queue orchestration. Together this runtime path retains its independent conditional-overwrite policy. Module placement does not weaken the ownership boundaries in the table.

Local build files and directory trees do not enter the transfer core or runtime state; their image projection and final evidence are owned by [Image local-tree projection and final evidence](#image-local-tree-projection-and-final-evidence). Runtime file planning admits only HTTP inputs, and only build HTTP declarations project into baked defaults.

The HTTPX adapter can write through a core-opened sink. aria2 instead requires an anchored directory/name interface and maintains its own control file. The core defends ordinary replacement races and unsafe filesystem shapes around that interface, but it cannot isolate aria2 artifacts from independently malicious code running with the same effective UID. Tests and documentation must not turn those defenses into a same-UID isolation claim.

One HTTPX credential policy is evaluated before each actual request for build and runtime. Runtime credential acquisition failure is a typed local failure at the file-policy seam, not an HTTP response and not a retryable transport attempt. Failure before the initial request records zero network attempts; redirect-time acquisition preserves earlier request-attempt evidence. The existing synchronous/asynchronous `continue` or `fail` policy decides queue behavior without creating a second retry engine. Route, reference, source locator, value, and value hash remain outside desired download identity and persisted runtime state, so credential rotation affects pending requests but does not make a completed target stale.

A configured checksum is trusted content intent supplied from outside the transport. Without it, completed transport and atomic placement do not prove content authenticity. Existing final content remains in place until a complete verified replacement commits through rename. Rename is the placement commit point: a later durability, final-verification, cleanup, or state-persistence failure remains fatal, but the complete new final may remain and is not rolled back. Containment, type, permission, identity, persistence, and durability failures fail closed and are never converted into ordinary runtime `continue` outcomes. The user guide owns the complete target-result matrix.

### Runtime state is minimal recovery authority

[`state.py`](../../src/comfyui_docker_helper/container/runtime/state.py) owns the strict serialized recovery model and durable store. [`runtime/download_state.py`](../../src/comfyui_docker_helper/container/runtime/download_state.py) owns current-run state transitions, while [`runtime/files/reconciliation.py`](../../src/comfyui_docker_helper/container/runtime/files/reconciliation.py) owns reconciliation against desired files.

The state records only the startup generation, desired download identity, actionable recovery status, and exact resume authority needed for safe reconciliation. It is not a durable attempt log, error history, progress feed, or user-editable control surface. Do not add telemetry fields and then make runtime policy depend on them.

Reconciliation acts only on desired input and state-backed transfer authority. It preserves completed final files when configuration changes and does not replace exact ownership with a broad filesystem scan. Invalid state or a required persistence failure stops reconciliation. The user runtime guide owns mounting and persistence instructions.

One active cdh instance owns the state file, its state-indexed transfer namespaces, and the declared targets governed by those entries. Container replacement must be serial: the old owner stops before its replacement takes ownership. Overlapping rolling instances, replicas sharing writable state, and different state files controlling the same target or staging namespace are not supported. The state `run_id` admits the synchronous-to-asynchronous handoff within one admitted runtime generation; it is not a lock or lease between instances, a controller operation identity, status authority, or logging channel.

### SSH environment, configuration, and lifecycle boundary

| Owner | Responsibility |
| --- | --- |
| [`runtime/serve.py`](../../src/comfyui_docker_helper/container/runtime/serve.py) | One immutable container-start environment snapshot, reused by serial generations |
| [`runtime/ssh/config.py`](../../src/comfyui_docker_helper/container/runtime/ssh/config.py) | Root credential preparation and complete generated sshd configuration |
| [`runtime/ssh/service.py`](../../src/comfyui_docker_helper/container/runtime/ssh/service.py) | Foreground child, monitoring, bounded shutdown and reap |
| Renderer/materializer | Static SSH-conditional workspace profile from the exact retained wheel |

The snapshot's text view serves config/hooks and its byte view serves SSH. Authenticated sessions inherit every representable value except per-connection `TERM` and `SSH_AUTH_SOCK`. Never filter by guessed secret names or silently alter unrepresentable values. Deployment owns same-root visibility; cdh protects only its own argv, diagnostics, events and temporary transport. Passwords reach credential tooling through stdin, not arguments.

The generation-owned root `0600` config in the private `0700` control directory is the sole sshd authority. Do not read distribution configuration, bake another template/Include chain or overlay values through `-o`. Foreground argv contains only the generated path and process-mode flags. Retain the file for the child lifetime and clean it by identity on failure/exit/replacement without deleting another generation's file. Image finalization removes package-generated host keys; activated runtime SSH generates its own.

For enabled credentialed SSH, projection, preparation, preflight or readiness failure fails the complete generation. Serial replacement has no rollback or partial SSH fallback. An sshd exit after ComfyUI starts is instead a warning and does not stop ComfyUI.

The static POSIX profile is consumed only when an SSH-associated login shell reads the system profile. It enters `WORKSPACE` or warns and falls back to `/root`; ordinary remote commands keep native OpenSSH `/root` behavior. Runtime does not mutate the profile. Do not introduce ForceCommand, PAM synchronization, interpreter-name gating or a downstream-shell compatibility layer to broaden this convenience.

### Baked and mounted hooks have different identity authority

Baked runtime hooks cross [`runtime_hooks.py`](../../src/comfyui_docker_helper/host/context/runtime_hooks.py), the [`canonical lock`](../../src/comfyui_docker_helper/config/planning/canonical_lock.py), the [`BuildPlan`](../../src/comfyui_docker_helper/config/planning/build_plan.py), and [`final materialization`](../../src/comfyui_docker_helper/rendering/final_materializer.py) as selected content-identified image inputs. Mounted hooks are external deployment inputs and never enter that identity chain.

[`runtime/hooks.py`](../../src/comfyui_docker_helper/container/runtime/hooks.py) discovers the two sources without turning them into an override overlay: baked hooks run first, followed by mounted hooks, with lexical ordering inside each source and phase. Both sources are trusted executable code. Content identity for baked bytes does not sandbox their effects or make their filesystem, network, package, or process behavior reproducible.

Host baking and container discovery select only direct regular `.sh` and `.py` files inside known phase directories. Ordinary unselected files and directories are ignored without recursion and reported through bounded source/phase warnings; symlinks, special files, inspection/read failures, invalid phase paths, and selected-hook failures remain hard boundaries.

### Readiness gates post-start hooks only

[`runtime/readiness.py`](../../src/comfyui_docker_helper/container/runtime/readiness.py) and the lifecycle owner invoke the loopback ComfyUI probe only when post-start hooks exist. A successful probe admits those hooks; process exit or timeout fails that startup path. It is not continuous monitoring, a container health check, or evidence that nodes, models, workflows, GPUs, or production workloads function correctly.

Do not move this probe into unconditional startup or give it wider health meaning without defining a new public lifecycle contract.

### Hook ownership ends when the leader is reaped

Each active hook runs as a new session leader. [`runtime/hooks.py`](../../src/comfyui_docker_helper/container/runtime/hooks.py) owns that leader and its process group through cancellation or deadline escalation, terminal result, and leader reap. [`process/control.py`](../../src/comfyui_docker_helper/container/process/control.py) provides the narrow spawn, signal, terminate, kill, wait, and reap mechanisms; it does not decide lifecycle policy.

After the hook leader's terminal result is accepted and reaped, cdh releases that execution's group authority. It does not enumerate descendants, discover or supervise deliberately backgrounded services, health-check them, or broadly signal them later. A process that detaches from the original group is also outside hook ownership.

A hook-created service therefore requires a paired stop hook that uses the service's control interface or a carefully validated process identity and waits for exit. A missing or failed stop hook, natural ComfyUI exit, external `SIGKILL`, or container teardown carries no graceful-shutdown guarantee. Tini reaping an orphan is process hygiene, not service supervision.

### One owner and one monotonic shutdown timeline

[`final_renderer.py`](../../src/comfyui_docker_helper/rendering/final_renderer.py) places Tini at PID 1 and cdh as its direct child. Tini forwards the container signal and reaps adopted orphans. [`runtime/controller.py`](../../src/comfyui_docker_helper/container/runtime/controller.py) owns irreversible cross-generation shutdown arbitration, while [`runtime/lifecycle.py`](../../src/comfyui_docker_helper/container/runtime/lifecycle.py) owns lifecycle and exact process policy for one generation. Neither listener threads nor control clients signal managed processes or run hooks.

Acceptance of a manual restart creates one monotonic shutdown timeline from the old generation's admitted `shutdown_timeout`. Without an active restart, the first accepted `SIGTERM` or `SIGINT` creates the same kind of timeline. Asynchronous downloads and SSH begin cancellation, ordered stop hooks run while ComfyUI is available, ComfyUI receives the restart's fixed `SIGTERM` or the original external signal, and cdh waits for the exact managed processes to terminate and be reaped. An external shutdown accepted while restart is in progress adopts the existing timeline and suppresses the successor; it cannot extend or restart the budget. Components may use narrower bounds inside that timeline, but they must not mint fresh deadlines that extend it. A repeated catchable external signal requests immediate force escalation.

Disabling the cdh outer and hook deadline does not disable component bounds or an orchestrator's independent hard limit. An external `SIGKILL` permits no further cleanup.

Natural ComfyUI exit follows a different path: cdh preserves its result, disables later signal admission, cleans up owned auxiliary work, and does not run signal-only stop hooks. Do not turn natural exit into synthetic signal shutdown or broadcast signals beyond exact cdh-owned processes. Operator-facing timeout values and external-grace configuration remain in the user runtime guide.
