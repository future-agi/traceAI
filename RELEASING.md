# Releasing traceAI

## The release loop

1. Send normal changes to **dev** using Conventional Commit PR titles (`fix(openai): ...`, `feat: ...`). Keep the actual commits conventional too when using merge commits; a PR title alone does not rewrite its commits. Do not hand-bump package versions for routine changes.
2. A maintainer merges dev into **main**. Release automation runs only on main. Nothing is published by a PR into dev or main.
3. The **Release** workflow inventories the current package versions and queries their registries. Unpublished versions are built, checked, uploaded and verified; versions already on a registry are never uploaded again. Python and npm core packages finish before their consumers.
4. At the end of a main push, release-please maintains **one combined release PR targeting main** with per-package versions and changelogs. Changes are scoped by package path. `fix` bumps patch; `feat` also bumps patch before 1.0 (`bump-patch-for-minor-pre-major`, matching [OpenInference](https://github.com/Arize-ai/openinference)). Breaking changes follow release-please's pre-major versioning rules. Java is one Maven reactor; `skip-snapshot` prevents a second SNAPSHOT PR. The Python core watches `python/` using the **non-recursive `simple` strategy** with explicit TOML/runtime updaters; this includes dependency-only fixes in `python/pyproject.toml` without discovering or overwriting sibling SDK `version.py` files. Its optional `version.txt` is absent and is not created; the manifest and marked runtime file remain authoritative. Sibling SDK paths have their own components and `python/frameworks`/`python/traceai` are excluded from core-only commit detection. All npm deliveries run sequentially in dependency-DAG order, including semantic conventions → fi-core and Vercel → Mastra, waiting for each published dependency version to be served before delivering its consumer.
5. Review and merge that release PR. Its version changes trigger the same build/verify/publish pipeline. The next ordinary changes accumulate in the next release PR. No automatic merging is configured.

This extends **Tanmay's [#300](https://github.com/future-agi/traceAI/pull/300)**: his planner, core-first publishing, clean-install checks and changelog-based GitHub releases are retained and extended to five languages. The branch contains a normal merge of his publishing branch, not a rewrite of it.

### Exactly one owner for tags

**Publishers own all tags and GitHub releases.** release-please runs with `skip-github-release: true`; it only authors the release PR. Tags are created after registry availability is verified (Go necessarily creates its tag first, then verifies the proxy). The publisher acknowledges completed merged release PRs by replacing `autorelease: pending` with `autorelease: tagged`. It does this only after every publishable current manifest version has a GitHub release, so missing credentials or a partial release cannot silently unblock the next release.

| Ecosystem | Version source | Tag | Delivery |
|---|---|---|---|
| Python | `pyproject.toml`, plus marked runtime `__version__` files | normalized PyPI name + `-vX.Y.Z` | PyPI wheel + sdist |
| npm | each `typescript/packages/*/package.json` | `npm-` + scope/name with `/` replaced by `-`, then `-vX.Y.Z` | exact smoke-tested tarball, public, provenance |
| Java | `java/pom.xml`; release-please updates reactor POMs together | `java-vX.Y.Z` | signed Maven Central reactor, `com.futureagi` |
| Go | `.release-please-manifest.json` entry per module | `go/<module>/vX.Y.Z` | source tag; request Go proxy `.info` afterwards |
| NuGet | `csharp/src/FIInstrumentation/FIInstrumentation.csproj` | `nuget-fi-instrumentation-otel-vX.Y.Z` | `dotnet pack`, verified local consumer, push with `--skip-duplicate` |

The Go proxy URL escapes uppercase letters: `traceAI` becomes `trace!a!i`. A module tag such as `go/openai/v0.1.0` is queried at `https://proxy.golang.org/github.com/future-agi/trace!a!i/go/openai/@v/v0.1.0.info`. No Go registry credentials are needed. JitPack remains a Java commit-preview route; Maven Central is the release route.

### Bootstrap and the dev → main PR

The manifest is seeded from the current versions on dev, **not** from old registry versions. Go starts at **0.1.0**. `bootstrap-sha` is the dev head `35f2e3dc364676fa5bdab1e71ae500daf9efc00c`: this deliberately excludes pre-bootstrap history from the first release PR. Keep that SHA as a fallback for components that have not yet acquired matching release tags; do not advance it on each release.

When [#287](https://github.com/future-agi/traceAI/pull/287) reaches main, existing unpublished versions can ship immediately once their ecosystem's setup is complete. **Already-merged fixes must not be stranded behind an unchanged registry version.** The committed [bootstrap audit](.github/release/bootstrap-audit.json) inventories all **68 Python and 47 npm manifests** at that baseline, reads the public upload timestamp of each current version, and checks first-parent package history after the upload. It uses the last wheel/sdist upload, not registry project-modification time; core excludes sibling SDK directories. The history filter uses **first-parent committer/merge times, not author dates**: a branch authored before an upload still counts when its merge lands afterwards. `--since-as-filter` also traverses backdated later commits instead of stopping early. A real temporary-git regression covers both cases. This is conservative: any package-directory change counts, including docs/version housekeeping.

The audit found **35 Python and 3 npm same-version packages with later merged changes**. This PR applies explicit, one-time patch bumps to those **38 packages**, including Python OpenAI **0.1.10 → 0.1.11** for the already-merged #249 fix. Package manifests, Python runtime versions, release-please's manifest and changelogs move together. This option deliberately avoids replaying all old conventional commits or repeatedly forcing `release-as`. There are also **25 Python and 12 npm current versions not yet uploaded**, which need no additional bootstrap bump. Six published Python and 32 published npm packages have no package history after their current-version upload and are left unchanged; two Python manifests are excluded below.

Thus the audited first run needs **60 Python + 15 npm new uploads**, five initial Go tags, and the Java reactor after its companion PR; NuGet 1.0.0 is already published. Registry state can change before the actual release, so the live planner remains authoritative. The workflow also repairs missing tags/releases for versions already on a registry, without re-uploading them or attaching newly built binaries to their historical version. Subsequent qualifying **post-bootstrap** changes use the normal release PR loop.

To reproduce the read-only audit or validate its one-time version floor:

```bash
python3 .github/release/bootstrap_audit.py --output /tmp/traceai-bootstrap-audit.json
python3 .github/release/bootstrap_audit.py --check
```

Reproducing history requires the baseline commit in the local clone. The offline check and CI tests need only the current checkout; they allow later versions rather than permanently pinning packages to the bootstrap patch.

Two manifests are tracked but intentionally **never published**:

- `python/traceai`: the PyPI name `traceai` belongs to another publisher. No rename or takeover is attempted.
- `python/frameworks/hermes-agent/hermes-plugin/futureagi`: a Hermes dependency manifest, explicitly not a buildable distribution.

Java publishing refuses legacy `ai.traceai` coordinates and SNAPSHOT versions. Merge the Java namespace/release-profile PR before expecting Central delivery. The release workflow will not claim that a legacy-coordinate build is a Central release.

## One-time maintainer setup

This is a checklist, not something the workflow changes for you. Never paste secret values into issues, PRs, logs or this file.

### GitHub release PRs

- [ ] Enable GitHub Actions for the repository and allow Actions to create/approve PRs in repository Actions settings (creation is needed; no automatic approval/merge is configured).
- [ ] Recommended: add **`RELEASE_PLEASE_TOKEN`**, a dedicated fine-grained token with access only to this repository, Contents read/write, Pull requests read/write and Issues read/write (labels). PRs created with this token trigger normal CI. A GitHub App installation token may be supplied instead by adapting the token step.
- [ ] The fallback `GITHUB_TOKEN` can create the PR when repository settings permit it, but GitHub suppresses most workflow events caused by that token: do not assume its generated PR will trigger all required checks. Supply `RELEASE_PLEASE_TOKEN` for unattended CI.
- [ ] Protect main and keep merge approval with a maintainer. The automation does not change branch protection or merge settings.

### PyPI: trusted publishing preferred

- [ ] For **each publishable Python project that already exists on PyPI**, add a PyPI Trusted Publisher: owner **`future-agi`**, repository **`traceAI`**, workflow filename **`release.yml`**, environment **blank** (blank accepts the per-project environment the publish job runs in).
- [ ] For **each first-time project**, add an organization **pending** publisher (futureagi organization → Publishing) with the same owner, repository and workflow, and environment **`pypi-<project name>`** exactly as the release plan spells the name (for example `pypi-traceAI-ag2`). PyPI accepts only one pending publisher per (repository, workflow, environment), so each new project needs its own environment; the `Publish python` job runs in `pypi-${{ matrix.name }}`.
- [ ] Set repository **variable** **`PYPI_TRUSTED_PUBLISHING=true`** after all intended project entries exist. No PyPI secret is needed for this mode.
- [ ] Important correction to #300: the trusted publisher filename is **`release.yml`, not `release-python.yml`**. The pinned PyPI action explicitly does not support Trusted Publishing inside reusable workflows. `release-python.yml` now only builds/checks/uploads artifacts with read-only repository access. OIDC upload jobs live in the top-level `release.yml`.
- [x] **First-release bootstrap (set up 2026-10-29).** PyPI allows only three pending trusted publishers per organization. `traceAI-a2a`, `traceAI-ag2-classic` and `traceAI-agent-framework` have org pending publishers. The other 16 first-time projects are listed in `.github/release/config.json` `pypi_bootstrap_token` and their first upload uses the **`PYPI_BOOTSTRAP_TOKEN`** secret (an account-scoped token, because only those can create projects). Nothing else uses it. **After the first release that creates them:** add each of the 16 projects to the futureagi organization, add its trusted publisher (owner `future-agi`, repository `traceAI`, workflow `release.yml`, blank environment), revoke the token on PyPI, delete the `PYPI_BOOTSTRAP_TOKEN` secret, and empty the list.
- [ ] Token fallback only: add secret **`PYPI_API_TOKEN`** with upload rights for the intended projects. The publisher uses it when present. Account-scoped tokens can create new projects; project-scoped tokens cannot create another project. Remove the fallback token once trusted publishing is working.

Sources: [PyPI publisher setup](https://docs.pypi.org/trusted-publishers/adding-a-publisher/) and [pinned PyPI action's reusable-workflow restriction](https://github.com/pypa/gh-action-pypi-publish/blob/7245906fb61fa33a13554f9d5fb856fe61758086/README.md#trusted-publishing).

### npm

- [x] **`NPM_TOKEN`** (set 2026-10-29): granular token `traceai-github-release`, read and write on the `@traceai` scope only, bypass 2FA, **expires 2027-01-27**. It publishes the first release, including first-time package names, which cannot have a trusted publisher until they exist.
- [ ] **Move to npm trusted publishing before January 2027.** npm removes direct publishing with 2FA-bypass tokens then. The workflow already installs npm 11.21.1 and has `id-token: write` in both the caller and the reusable job. npm tries OIDC first and falls back to the token.
  - **Timing:** a new npm trusted publisher expires if it is not used within **2 days**, so configure them right before a release, not in advance.
  - **Per package:** for each `@traceai` package, go to Settings → Trusted publisher → GitHub Actions and enter owner `future-agi`, repository `traceAI`, workflow **`release.yml`** (npm checks the calling workflow when `workflow_call` is used), with no environment. Allow `npm publish`.
  - **After a successful OIDC release:** set each package's publishing access to "Require two-factor authentication and disallow tokens", then revoke the token and delete the `NPM_TOKEN` secret.
- [ ] Publishing uses `npm publish <verified tarball> --access public --provenance` on a GitHub-hosted Linux runner. `package.json` `repository.url` must match the GitHub repository for trusted publishing. Already-published names are never renamed.

### Maven Central: primary Java distribution

- [ ] In [Central Portal](https://central.sonatype.com/), request namespace **`com.futureagi`**.
- [ ] Add a **DNS TXT record on the exact apex `futureagi.com`** (host `@`, depending on the DNS provider). Its value must be the **Verification Key generated by Central for this namespace request**; it is not a value this repository can pre-generate. Verify DNS propagation, then confirm namespace verification in Central. Do not use `traceai.ai`, `com.futureagi.com` or a `maven-central` subdomain.
- [ ] Generate a Central Portal user token and add **`MAVEN_CENTRAL_USERNAME`** and **`MAVEN_CENTRAL_PASSWORD`** as repository secrets. These are the Portal token's username/password, not your interactive login password.
- [ ] Create a release GPG signing key, publish its **public key** to a public keyserver accessible to Central, and verify the fingerprint. Export the **ASCII-armored private signing key** into **`MAVEN_GPG_PRIVATE_KEY`** and its passphrase into **`MAVEN_GPG_PASSPHRASE`**. Keep the private key out of git and command output.
- [ ] Merge the Java release-profile PR: groupId `com.futureagi`, unchanged artifactIds, sources/javadocs, signing, and `central-publishing-maven-plugin` server id **`central`**. setup-java provides Central credentials; the release profile's BC signer reads the private key and passphrase directly from the deployment step's environment (no persistent GnuPG keyring). The publisher runs **`mvn -B -ntp -P release deploy` from `java/`**.
- [ ] The planner skips a version only when **every reactor artifact** has its POM on Central. Central deployments are verified against canonical repository files, not the delayed search index. Check the Portal for a failed/incomplete deployment before retrying; do not replace immutable artifacts.

Sources: [namespace verification](https://central.sonatype.org/register/namespace/) and [Central publishing Maven plugin](https://central.sonatype.org/publish/publish-portal-maven/).

### NuGet

- [ ] Add **`NUGET_API_KEY`** with push rights for **`fi-instrumentation-otel`** and track its expiration. Grant new-package creation only if needed for a future package, not by default.
- [ ] The build targets **.NET 10** (`net10.0` in the current project), installs the exact packed nupkg into a fresh consumer, and uses `dotnet nuget push --skip-duplicate`. Registry verification precedes tagging.

Missing credentials disable uploads for that ecosystem and emit a warning; build/check jobs still run. Go can create its source tags on main without registry credentials. Setup does not retroactively start a failed/finished run: rerun Release after finishing the checklist.

## Checks and dry runs

From the repository root, using Python **3.11+**:

```bash
python3 -m unittest discover -s .github/release -p 'test_*.py'
python3 .github/release/release_plan.py --check
python3 .github/release/release_plan.py --offline
python3 .github/release/release_plan.py                    # read-only live registry queries
python3 .github/release/release_plan.py --only traceAI-openai @traceai/openai
```

For the real release-please strategy regression test (offline after installing the tool):

```bash
npm install --prefix /tmp/traceai-release-please --ignore-scripts --no-audit --no-fund release-please@17.6.0
NODE_PATH=/tmp/traceai-release-please/node_modules node .github/release/test_release_strategies.cjs
```

Once the config/manifest are present on the target branch, preview the actual PR without writing to GitHub:

```bash
/tmp/traceai-release-please/node_modules/.bin/release-please release-pr \
  --repo-url future-agi/traceAI --target-branch main --dry-run \
  --token "$(gh auth token)"
```

Use a token with read access for this dry run. Do not enable shell tracing around tokens. The initial development preview used real dev history with **only** the two proposed, not-yet-merged config files overlaid locally; it found zero post-bootstrap commits and would open zero PRs. The strategy test uses **release-please 17.6.0**, matching the pinned production action. A separate fully offline fake-history test exercises a qualifying feature across all five languages and asserts one combined PR plus correct version-file changes. It also proves a fix touching **only** `python/pyproject.toml` releases core, while sibling-only fixes leave core and unrelated SDK runtime versions untouched. It checks every internal Java parent POM, not just the reactor root; an additional local run over companion #302 also covers `traceai-bom`.

For a GitHub-hosted build-only run after the workflow lands on the default branch:

```bash
gh workflow run release.yml --repo future-agi/traceAI --ref main \
  -f dry_run=true -f packages='traceAI-openai @traceai/openai'
```

A dispatch defaults to **dry run**. It creates no release PR, upload, tag or release. npm clean-install verification supplies the target **and all transitive workspace dependency tarballs built locally**, including versions absent from npm. Only selected targets are published, never incidental dependency artifacts; a publish selection that omits an unpublished dependency fails before uploading the consumer (include that dependency in `packages`, or leave the selection empty). Builds/install scripts do not receive publishing credentials. Offline unit tests pack/install an unpublished four-package dependency graph with an empty npm cache and no network. PR runs validate scripts/configuration and display the plan; they do not invoke the publishing/build jobs.

## Manual release and recovery

After reviewing the plan and completing the applicable setup, a maintainer may deliberately dispatch `Release` on **main** with `dry_run=false`, optionally selecting package names. Dispatches from another branch cannot publish. A manual dispatch does not open/update a release PR; the main push path does that.

Rerun the same main version after a failure. Registry versions already present are marked `registry_published`; uploads are skipped, visibility checks still run, and absent GitHub releases are repaired. A package leaves the plan only after both its registry version and GitHub release exist. GitHub release/tag creation is idempotent. Never delete/repoint a released tag or overwrite a registry version to repair a failed build. Fix the package and let the next release PR bump it.

For a deliberate manual version bump, update both the package manifest and its `.release-please-manifest.json` entry, synchronize any Python runtime version strings, and provide the changelog. For Go, the release-please manifest is the version source. Keep release configuration coverage when adding a package; `--check` and the strategy tests run in CI.
