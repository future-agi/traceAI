# traceAI Kubernetes injection

An opt-in Pod mutating admission webhook, a payload init container, and a Helm
chart. Module: `github.com/future-agi/traceAI/kubernetes`. No operator framework,
CRDs, application command wrapping, package installation at startup, or Secret
read permission is needed by the webhook. Cert-manager CRDs are needed only in
cert-manager mode. This is a new component, not a claim of production certification
or an already-published registry image. The Dockerfile-specific context allowlist
excludes local caches, `.git`, env files and unused Go source payloads.

## Admission and payload flow

1. The chart registers an HTTPS `admission.k8s.io/v1` webhook for **CREATE Pods
   only**, `failurePolicy: Ignore`, `sideEffects: None`, and a fixed 3-second
   timeout. The release namespace and system namespaces are excluded.
2. A Pod opts in with annotation `traceai.futureagi.com/inject: "true"`, or its
   namespace with label `traceai.futureagi.com/inject=true`. A Pod annotation of
   `"false"` (or any value other than exactly `"true"`) overrides the namespace.
   Annotation opt-in/out requires no API request. Namespace opt-in uses a
   token-rotation-aware, verified-TLS GET capped at 300 ms; lookup failure means
   **no mutation**. Namespace labels are not cached, avoiding stale opt-ins.
   Each replica admits at most 32 mutations and 8 namespace lookups concurrently
   (configurable); nonblocking semaphores skip injection immediately on overload.
   Admission work/body reads have a 1-second deadline, below the API's 3 seconds.
   A timed-out lookup retains its slot until its worker exits, so even a broken
   lookup implementation cannot create unbounded background workers.
3. Supported, eligible application containers get a read-only `/opt/traceai`
   mount and merge-only environment patches. One `traceai-agent-init` container
   copies the image payload to a shared, disk-backed `traceai-agent` emptyDir.
   Application images, commands, arguments, probes, resources, security contexts,
   init containers, existing mounts, and unknown API fields are preserved.
4. The init image is **the same image/digest as the webhook**. The image-build
   command `stage-payload` invokes the launcher's `env --format json` with a
   clean, credential-free environment. It copies the launcher's own guarded
   Node/Python/Ruby shims, the embedded Java jar, the complete .NET hook directory,
   and their content-addressed fallback trees into a fixed layout. No shim is
   independently reimplemented here. Unused Go build sources are not extracted
   or included in the deployment image.
5. At init, `copy-payload` authenticates every source file against an image-local
   SHA-256 inventory, rejects missing/extra/symlinked files, copies with bounded
   streaming I/O, and verifies the destination. Retries repair interrupted copies
   of known files, never overwrite unknown files. The bounds are 256 MiB, 4,096
   files, 8,193 tree entries and 32 path separators; emptyDir is limited to 512 MiB.
   Files are readable by arbitrary application UIDs. The init runs as UID/GID
   65532, drops all capabilities, and has a read-only root filesystem.

**Availability boundary:** admission exceptions, invalid configuration, malformed
requests, namespace lookup errors, webhook outages and TLS errors do not reject
Pods. However, `failurePolicy: Ignore` cannot undo an already-applied patch. An
image-pull failure, failed init, quota/validation policy rejection, or incompatible
agent can still prevent an opted-in workload from starting. The copier deliberately
fails rather than starting a JVM with a missing `-javaagent`. This is not a promise
of runtime fail-open for unavailable payload images. Canary and pre-pull the image
before rollout; disable injection and recreate affected Pods to roll back.

## Opt in and select containers

Put annotations on the **Pod template**, not Deployment metadata:

```yaml
spec:
  template:
    metadata:
      annotations:
        traceai.futureagi.com/inject: "true"
        traceai.futureagi.com/languages: '{"api":"node","worker":"python"}'
        traceai.futureagi.com/skip-containers: "proxy,metrics"
        traceai.futureagi.com/credentials-secret: "team-traceai-credentials"
```

For a single-runtime workload, use `traceai.futureagi.com/language: "node"`.
Per-container `languages` entries override this default. An unknown language
(including `none`) skips that container. Invalid JSON or an unknown container name
in the hints skips the entire mutation. Unknown executable/image contents are
never guessed. Without hints, an explicit first `command` of `node`, `nodejs`,
`java`, `dotnet`, `python`/supported `python3.X`, or `ruby` is recognized. Image
ENTRYPOINTs and shell/npm/bundle/uvicorn wrappers require a language hint. Prefer
per-container hints with sidecars rather than a Pod-wide default.

For namespace opt-in, add language hints to the workload and label its namespace:

```sh
kubectl label namespace applications traceai.futureagi.com/inject=true
# Pod-template annotation inject: "false" still wins.
```

### Runtime environment

| Hint (aliases accepted) | Injected value, prepended to existing literal value |
| --- | --- |
| `node` (`nodejs`, `javascript`, `typescript`, `js`, `ts`) | `NODE_OPTIONS=--require /opt/traceai/node/preload.cjs …` |
| `java` (`jvm`) | `JAVA_TOOL_OPTIONS=-javaagent:/opt/traceai/java/traceai-javaagent.jar …` |
| `dotnet` (`csharp`, `c#`) | `DOTNET_STARTUP_HOOKS=/opt/traceai/dotnet/TraceAI.AutoInstrumentation.dll:…` |
| `python` (`py`) | `PYTHONPATH=/opt/traceai/python:…`, plus `FI_AUTO_INSTRUMENT=true` |
| `ruby` (`rb`) | `RUBYOPT=-r/opt/traceai/ruby/preload.rb …` |
| `go` | No runtime injection. Build with the agent import; see `../go/launcher`. |

Original option spelling, quoted paths, path-list trailing separators, and env
ordering are preserved. Newly supplied settings are escaped for Kubernetes env
expansion. Existing explicit FI settings win, even empty values or `valueFrom`
references. Credentials are pair-aware as described below. Each successfully injected container gets
`TRACEAI_KUBERNETES_INJECTED=v1`. Existing traceAI runtime options, this marker,
`FI_AUTO_INSTRUMENT=true`, or `OTEL_SDK_DISABLED=true` skip the container entirely.
Any explicit `FI_AUTO_INSTRUMENT` value also skips Python (including a disable).

Conservative skips protect workloads: containers with `envFrom`, duplicate env
names, a selected runtime option from `valueFrom`, or overlapping `/opt/traceai`
mounts are not mutated. An existing `traceai-agent` volume or a `traceai-agent-init`
name in either application or init containers skips the entire Pod, even when the
colliding application container is opted out. Init/ephemeral containers and Windows
Pods are not instrumented. Re-admission is idempotent; this is not a continuously
reconciling controller and does not instrument already-running Pods.

## Credentials, settings and privacy

Provision `traceai-credentials` in **each workload namespace**, keys `FI_API_KEY`
and `FI_SECRET_KEY` (names configurable via chart values). A per-Pod
`credentials-secret` annotation may reference a different Secret in the **same**
namespace, never another namespace. The webhook injects only `secretKeyRef` with
`optional: true`: missing Secret/keys do not create a kubelet credential-config
error. The webhook never checks Secret existence or contents and cannot certify
credential availability. Agents must resolve a complete pair atomically; a missing
optional key must not be combined with another credential source.

- A container declaring **both** `FI_API_KEY` and `FI_SECRET_KEY` keeps both
  declarations untouched (literal, empty, `valueFrom`, or mixed).
- When **neither** is declared, the operator's two optional references are added
  together, both pointing to the same workload-namespace Secret.
- A **partial** application pair is preserved and is **never completed** with
  the operator's missing half. Pod annotation
  `traceai.futureagi.com/degraded-reason` includes
  `partial-application-credential-pair`; fix the application pair at its source.
- Optional credential references also report
  `optional-credential-secret-unverified` in that annotation. This is an honest
  admission-time uncertainty marker, not a claim that a Secret is missing. Missing
  optional Secret/keys cannot be diagnosed without granting Secret-read access,
  which the webhook deliberately does not have. Check the agent's safe startup
  diagnostics separately. Reasons contain only fixed codes, never values/names.

Application init containers remain wholly unchanged (including credential pairs);
when an application sibling is injected, their partial pairs are also reported.
The payload-copy init never receives credentials. Set `credentials.secretName: ""`
to omit new credential references entirely when workloads supply their own.
Secret rotation requires new Pods because these are environment variables, not
projected files.

The chart never creates credentials and the server's ClusterRole has only `get`
on `namespaces`, not Secrets/ConfigMaps/Pods. Application containers alone receive
credential references. No requests, patches, env values, collector responses or
raw errors are logged. Normal application/agent stdout is not rewritten. Protect
Kubernetes audit logs and access to Pod specs as usual.

Chart `settings` accepts string values for `FI_BASE_URL`, `FI_PROJECT_NAME`,
`FI_PROJECT_TYPE`, `FI_INSTRUMENTATIONS`, `FI_DISABLED_INSTRUMENTATIONS`,
`FI_WIRE_CAPTURE`, `FI_WIRE_MAX_BODY_BYTES`, `FI_AUTO_DEBUG`, `FI_SAMPLE_RATE` and
`FI_HIDE_*`. Inline credentials, runtime options, agent-path overrides, and
`FI_CONFIG_FILE` are rejected by schema/server validation. Existing application env
wins over chart settings. Collector selection is administrator-controlled, not a
new Pod annotation. Agent capture/export privacy remains governed by
`../spec/auto-instrumentation/README.md`; this webhook does not parse AI traffic.

## Webhook control-plane security

**Serving TLS is not caller authentication.** For clusters where you control the
API server, configure its admission webhook kubeconfig (via
`AdmissionConfiguration` / `--admission-control-config-file`) with a client
certificate/key for this webhook Service. Use a **dedicated API-server client
CA**, issue only API-server certificates with `clientAuth` usage, and distribute
the public CA PEM in a ConfigMap in the webhook namespace. Set
`tls.clientCAConfigMap` and optionally `tls.clientCAKey` (default `ca.crt`). The
server's `--tls-client-ca` then requires and verifies a client certificate on
**every** TLS connection; certificates from other CAs, wrong usages and anonymous
callers are rejected before HTTP/namespace lookup. Do not reuse a broad cluster
CA that also issues workload certificates. No identity header is trusted.

The CA volume and serving certificates reload on new handshakes (no `subPath`);
invalid rotated trust fails closed. TLS session tickets are disabled in mTLS
mode so new connections reauthenticate. Already-established connections are not
revoked by CA rotation; drain/restart replicas if immediate revocation is needed.
In mTLS mode, chart health probes use TCP because kubelet HTTPS probes cannot
supply the API server's certificate; TCP probes establish listener availability,
not a successful authenticated admission. No unauthenticated health listener is
exposed. `failurePolicy: Ignore` remains unchanged for all handshake failures.

Managed clusters often **cannot configure webhook client certificates**. Leave
`tls.clientCAConfigMap: ""` there; this is explicitly server-only TLS, not
cryptographic caller authentication. Use the provider/CNI's enforceable private
control-plane ingress restrictions and private webhook endpoint routing. If your
provider offers an authenticated webhook proxy/mesh, terminate its verified API
server identity there and forward to the webhook with a dedicated trusted client
certificate; never assume forwarded headers alone prove caller identity. Qualify
that integration with the provider before claiming authentication support.

The chart ships an ingress-only **NetworkPolicy enabled by default**. It selects
only webhook Pods and allows TCP 8443 only from `networkPolicy.apiServerCIDRs`
(the API server's actual source addresses as seen by your CNI), and/or explicit
`networkPolicy.controlPlanePeers` pairs of nonempty namespace and Pod
`matchLabels` selectors (AND in each peer). **Empty allowlists deny all ingress**:
configure them before installation or injection will be skipped. Example for an
operator-provided values file (replace the documentation-only address):

```yaml
networkPolicy:
  enabled: true
  apiServerCIDRs: ["192.0.2.10/32"]
  controlPlanePeers: []
admission:
  maxConcurrentAdmissions: 32 # 1..1024, per replica
  maxConcurrentLookups: 8     # 1..maxConcurrentAdmissions
```

Never assume the Service IP is the source CIDR. Source NAT, host-network control
planes and node-origin traffic depend on the CNI; standard NetworkPolicy may not
filter host/node traffic and does nothing without an enforcing CNI. Verify with
provider flow logs and positive/negative cloud tests. Set
`networkPolicy.enabled=false` only when equivalent provider firewall/CNI/mesh
controls are in place (or for an isolated test environment); no broad fallback
CIDR is silently supplied. The policy does not restrict egress: retain any
existing egress policy and permit the verified-TLS namespace GET to the API.

Namespace enrollment labels belong to namespace administrators. Pod creators can
opt their Pods in/out and select a same-namespace credentials Secret; existing
Pod-creation authority generally already permits projecting namespace Secrets.
Use cluster validation policy to restrict those annotations/Secret choices if
needed. This webhook is **not** an authorization gate or a Secret broker. The
ClusterRole stays `get namespaces` only: no Secret read/list/watch is added.

## Init resources and QoS

`initResources.requests` / `initResources.limits` configure CPU and memory
(default 10m/16Mi requests, 100m/128Mi limits). The server also accepts the JSON
`--init-resources` flag. Both resources require positive requests no greater than
limits; other resources/claims are rejected. The original application and init
resource requirements are never modified. For container-level **Guaranteed**
Pods the injected init uses the configured **limits for both requests and
limits**, preserving Guaranteed QoS. For **BestEffort** Pods it omits CPU/memory
resources entirely, preserving BestEffort QoS; configure application resources
first if the copy must have limits. **Burstable** Pods use the configured values.
Existing Pod-level resource requirements are left intact. Preserving QoS does
not guarantee identical scheduling/quotas: the added init's peak reservation can
still affect admission, scheduling and copy completion; canary representative
payload sizes and namespace policies.

## Build and install

The repository currently assumes no published Kubernetes image. On a **cloud
builder**, first build the sibling Java, .NET **net8.0**, and Node agents using
their READMEs, then stage the current artifacts. Ruby's `lib` must be available:

```sh
# Repository root, cloud builder only. Keep sibling outputs stable throughout.
python3 -I go/launcher/scripts/stage.py "$PWD" "$PWD/go/launcher/embedded"
docker build -f kubernetes/Dockerfile -t "$IMAGE" .
docker push "$IMAGE"
```

The Dockerfile uses `-tags embedagents` with only deployable runtime payloads;
missing Java/.NET/Node/Ruby payloads fail the image build. The launcher is not
required inside application images. There is no runtime dependency download:
Python must already have compatible `fi-instrumentation-otel` installed in its
interpreter; Ruby needs compatible OpenTelemetry gems (Bundler must allow them).
Node can prefer the application's installed package and otherwise uses the bundle.
Java/.NET need compatible target runtimes; the staged .NET 8 hook supports 8/10.
Other versions require their own verified compatibility test before rollout.

With preinstalled cert-manager **and cainjector**, install the chart using your
published repository and immutable digest. Create `control-plane-values.yaml`
with your verified ingress allowlist from the security section first:

```sh
helm upgrade --install traceai kubernetes/chart \
  --namespace traceai-system --create-namespace \
  --set image.repository="$IMAGE_REPOSITORY" --set image.digest="$IMAGE_DIGEST" \
  -f control-plane-values.yaml \
  --set-string settings.FI_HIDE_INPUTS=true \
  --set-string settings.FI_HIDE_OUTPUTS=true --wait
kubectl -n traceai-system wait --for=condition=Ready \
  certificate/traceai-traceai-injector --timeout=120s
```

Default TLS creates a namespaced self-signed Issuer and Certificate; cainjector
fills the webhook CA bundle. `certManager.issuerRef` can select your Issuer or
ClusterIssuer. To manage certificates externally, set `certManager.enabled=false`,
`tls.existingSecret`, and `tls.caBundle` (base64 PEM CA) together. The certificate
must cover `<service>.<release-namespace>.svc`. Mounted certificate/key pairs reload
on each TLS handshake; do not use `subPath` mounts. External CA rotation requires
updating `caBundle` as well. Never configure `failurePolicy: Fail` to compensate
for certificate issues.

Two replicas, preferred anti-affinity, health/readiness probes, resource limits,
non-root execution and a PDB are supplied. Permit control-plane traffic to service
443/container 8443 and webhook egress to the API server. Private registries require
appropriate `imagePullSecrets` in workload Pods too: the chart's `imagePullSecrets`
configures only the webhook Deployment. No registry credentials are copied across
namespaces.

Roll out to one canary Deployment first. Check init success and payload mount,
agent startup status, privacy and spans against an uninstrumented baseline; then
expand. To roll back, remove namespace opt-in/set Pod-template `inject: "false"`
and roll the workload. Uninstalling the chart does not remove env/mounts from
existing Pods.

## Local verification (no Docker or cluster)

Go 1.26 with auto toolchain, Helm 3/4 for rendered-chart tests. Keep scratch under
`/tmp/traceai-*` and remove it afterward; Go's shared module/build caches stay at
their defaults. Every suite/build/child launcher execution must hold the shared
host safety slot, with Go parallelism capped:

```sh
cd /Volumes/nikm4/futureagi/traceai-auto/kubernetes
SCRATCH=$(mktemp -d /tmp/traceai-k8s.XXXXXX)
mkdir -p "$SCRATCH/tmp"
trap 'rm -rf "$SCRATCH"' EXIT
export GOTOOLCHAIN=auto GOMAXPROCS=4
export GOTMPDIR="$SCRATCH/tmp" TMPDIR="$SCRATCH/tmp"
HEAVY=/Volumes/nikm4/futureagi/tmp/traceai-auto-review/heavy.sh
bash "$HEAVY" go test -p 2 -count=1 ./...
bash "$HEAVY" go test -p 2 -race -count=1 ./...
bash "$HEAVY" go build -p 2 -o "$SCRATCH/traceai-webhook" ./cmd/traceai-webhook
bash "$HEAVY" helm lint chart --strict
bash "$HEAVY" helm template traceai chart --namespace traceai-system --kubeconfig=/dev/null
# Optional: real launcher staging/copy, no application runtime or cluster needed.
TRACEAI_TEST_FULL_LAUNCHER="$PWD/../go/launcher/bin/traceai-full" \
  bash "$HEAVY" go test -p 2 -count=1 -run '^TestFullLauncherStageAndCopy$' ./internal/payload
```

The tests apply real RFC 6902 patches for every runtime with/without existing
options, credentials/settings precedence, namespace selection, per-container
hints/opt-out, idempotence, unresolved env, name/path collisions, unknown/null
fields, token rotation, malformed/oversized requests, panic recovery, certificate
reload, actual in-process HTTPS AdmissionReview exchange, and payload integrity/
retry/limits. Credential admission cases cover complete/partial literal/valueFrom
pairs, empty declarations, simulated missing optional Secret keys, unchanged
application init containers and a credential-free copier. TLS tests exercise
anonymous/untrusted/wrong-EKU rejection, verified client acceptance and client-CA
rotation. Saturation and uncancellable-lookup tests assert allowed/no mutation
and bounded workers. QoS tests cover defaults/custom resources and all three
container-level classes. Chart tests render both TLS modes, mTLS probes/mounts,
custom issuers, image digests, deny-all/allowed-peer/disabled NetworkPolicy,
and assert safe webhook/RBAC/Deployment wiring and rejection of unsafe values.
They skip if Helm is absent; the full-launcher test skips unless explicitly enabled.

## Cloud lane: kind end-to-end recipe

**Cloud only; never run Docker, kind, VMs or clusters on the shared laptop.** Use
approved pinned kind/node, application image, cert-manager and builder versions in
CI. The following admission smoke recipe uses disposable credentials/no collector;
it is not proof of successful tracing. From the repository root on that runner:

```sh
# Build current sibling agents + stage.py first, as above.
docker build -f kubernetes/Dockerfile -t traceai-injector:e2e .
kind create cluster --name traceai-injection
kind load docker-image traceai-injector:e2e --name traceai-injection
helm repo add jetstack https://charts.jetstack.io
helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace --version "$CERT_MANAGER_VERSION" \
  --set crds.enabled=true --wait
helm upgrade --install traceai kubernetes/chart --namespace traceai-system \
  --create-namespace --set image.repository=traceai-injector --set image.tag=e2e \
  --set image.pullPolicy=Never -f control-plane-values.yaml --wait
kubectl -n traceai-system wait --for=condition=Ready \
  certificate/traceai-traceai-injector --timeout=120s
kubectl wait --for=jsonpath='{.webhooks[0].clientConfig.caBundle}' \
  mutatingwebhookconfiguration -l app.kubernetes.io/instance=traceai --timeout=120s
kubectl create namespace applications
kubectl -n applications run baseline --image=node:22-bookworm-slim \
  --restart=Never --command -- node -e 'console.log("ok")'
kubectl -n applications run injected --image=node:22-bookworm-slim \
  --restart=Never \
  --annotations='traceai.futureagi.com/inject=true,traceai.futureagi.com/language=node' \
  --command -- node -e 'console.log("ok")'
kubectl -n applications wait --for=jsonpath='{.status.phase}'=Succeeded \
  pod/baseline pod/injected --timeout=180s
kubectl -n applications get pod injected -o json | jq -e \
  'any(.spec.initContainers[]?; .name == "traceai-agent-init")'
kubectl -n applications get pod baseline -o json | jq -e \
  'any(.spec.initContainers[]?; .name == "traceai-agent-init") | not'
# Namespace opt-in, explicit Pod opt-out, sidecars and env merges: repeat with
# Pod templates above and assert all original options/sidecars are unchanged.
# Outage: opted-in Pods must still be admitted WITHOUT injection.
kubectl -n traceai-system scale deployment traceai-traceai-injector --replicas=0
kubectl -n traceai-system wait --for=delete pod \
  -l app.kubernetes.io/instance=traceai --timeout=60s
kubectl -n applications run outage --image=node:22-bookworm-slim \
  --restart=Never \
  --annotations='traceai.futureagi.com/inject=true,traceai.futureagi.com/language=node' \
  --command -- node -e 'console.log("ok")'
kubectl -n applications wait --for=jsonpath='{.status.phase}'=Succeeded \
  pod/outage --timeout=180s
kubectl -n applications get pod outage -o json | jq -e \
  'any(.spec.initContainers[]?; .name == "traceai-agent-init") | not'
kind delete cluster --name traceai-injection
```

Release-gating cloud matrix (not run locally): test supported Python/Node/JVM/
.NET 8 and 10/Ruby images against uninstrumented twins. Include preexisting
preloads/agents/sitecustomize, env refs and sidecars, restricted Pod Security,
private pulls, quota failures, init restart, projected TLS rotation, namespace
lookup denial, unavailable webhook and missing credentials. With real SDK
fixtures plus a local mock collector, require exact exit/output/stream behavior,
AI spans without duplicates, expected request timing, and no leaked credentials
or hidden content under the shared contract. A smoke Pod's success alone does not
establish these guarantees.

## Known limitations

As of **2026-10-30**:

- Caller mTLS is qualified only by loopback tests here; managed API-server client
  certificate/proxy integration and CNI enforcement need cloud qualification.
  Without a configured dedicated client CA there is no cryptographic caller
  authentication. Default empty NetworkPolicy allowlists deny all ingress;
  control-plane addresses/host-network exceptions are cluster-specific. A plain
  TCP health probe in mTLS mode verifies only the listener, not admission health.
- Admission does not resolve image-defined credential ENV or optional Secret
  contents. Partial declared pairs are never spliced; optional references are
  annotated as unverified. Agents must reject/fall back atomically on incomplete
  resolved pairs. Missing-key tests use an in-memory kubelet projection model,
  not a real kubelet or admission-to-running-agent certification.
- Windows constrained **only by required node affinity** is not detected by the
  current OS guard and can receive Linux injection. Set `inject: "false"` on such
  workloads (or explicitly set Windows `spec.os`/`nodeSelector`); Windows is not
  a supported injection target.
- Chart names (fixed 2026-10-30): label values are quoted, so release names such as
  `on` or `true` stay strings; a name that does not start with a letter is prefixed
  with `traceai-` (release `123` -> `traceai-123-traceai-injector`); cluster-scoped
  names hash the namespace and the untruncated name, so long overrides sharing a
  54-character prefix no longer collide. Names longer than 63 characters are still
  truncated, so overrides that differ only after 63 characters still collide.
- No Kubernetes API-server/envtest, real image build, kind or cluster end-to-end
  execution has been performed in the local lane. In-process HTTPS, rendered Helm
  contracts, unit tests and an opt-in real full-launcher stage/copy test do not
  replace the cloud matrix above. Production qualification remains outstanding.
- Admission is fail-open; injected init/image/policy/runtime startup is not.
  An unavailable image or incompatible JVM/.NET hook cannot be repaired by the
  webhook after creation. Payloads require a trusted, prevalidated image/digest.
  Image-local hashes detect corruption, not malicious publisher substitution;
  use registry signature/admission controls for supply-chain trust.
- Image-defined ENV options cannot be inspected/merged by admission. Surface
  all runtime options explicitly in Pod `env` before opting in. `envFrom`,
  selected runtime `valueFrom`, duplicate env, and ambiguous entrypoints are
  deliberately skipped, not silently overwritten. One language hint per
  application container; polyglot child-runtime injection is not implemented.
- Python dependencies and Ruby gems must already exist in the application.
  Python's requested `FI_AUTO_INSTRUMENT=true` can activate an installed `.pth`
  before the guarded sitecustomize (unlike the launcher's normal default).
  Validate that environment's installed bootstrap/dependencies; unsupported
  third-party `.pth` startup code cannot be guarded by this webhook.
- Linux only; Go requires build-time instrumentation. Init/ephemeral containers,
  Windows, existing Pods, dynamic reinjection and unsupported runtimes are out of
  scope. Namespace opt-in does not discover image ENTRYPOINTs. No label watch/
  cache or admission metrics backend; namespace-mode requests add a bounded API
  read per new Pod, and silent conservative skips currently require Pod inspection.
- cert-manager/cainjector or externally managed TLS is required. CA rotation can
  briefly admit uninstrumented Pods while bundles/volumes converge. An already
  mutated Pod retains its old payload until recreated; Secret env rotation also
  requires recreation. Registry access and cluster-specific security/quota policy
  compatibility must be provisioned and tested by the cluster operator.
