# Versioning and releases

Lab Tracker follows [Semantic Versioning 2.0.0](https://semver.org/) for the
single `lab-tracker` Python distribution, which contains the server, the
`lab_tracker_client` package, and the `lab-tracker`, `lab_tracker`, `lt`, and
`lt-mcp` commands.

## Version contract

`project.version` in `pyproject.toml` is the only editable version source.
`uv.lock`, built wheel/sdist metadata, `lab_tracker.__version__`,
`lab_tracker_client.__version__`, `lab_tracker --version`, and `lt --version`
must agree with it. Release tags have the exact form `vX.Y.Z`; the `v` belongs
to the Git tag and is not part of the package version.

The automated release path currently accepts stable releases only. Do not use
pre-release or build suffixes until their mapping between SemVer and Python's
package-version rules is designed and added to `scripts/verify_release.py`.
Adopting this policy does not itself publish or tag a release; a release exists
only once a maintainer completes the steps below. `0.1.0` was the untagged
baseline, and `v0.2.0` is the first tagged release.

The public compatibility surface is:

- documented REST and MCP request/response contracts;
- public names in `lab_tracker_client`;
- documented CLI commands, flags, output contracts, and exit behavior;
- documented `LAB_TRACKER_*` configuration names and meanings; and
- database and deployment upgrade behavior documented for operators.

Internal Python modules and uncommitted/deferred design documents are not public
API. `docs/retained-v1-surface.md` remains the authority on which product
capabilities ship.

## Choosing the next version

For `1.0.0` and later:

- **MAJOR**: an incompatible change to the public compatibility surface, or an
  upgrade that requires coordinated consumer/operator changes.
- **MINOR**: backward-compatible functionality, a new public endpoint/command,
  or a deprecation. Additive, automatically applied database migrations normally
  belong here.
- **PATCH**: backward-compatible bug, security, documentation, packaging, or
  internal maintenance fixes.

While the project remains on `0.y.z`, increment **MINOR** for features and any
intentional incompatibility, and **PATCH** only for backward-compatible fixes.
Every incompatible `0.y.0` release must call out its migration impact in the
release notes. Move to `1.0.0` when the declared public surface is ready for the
standard MAJOR/MINOR/PATCH compatibility promise.

When a release contains several kinds of change, use the largest required bump.
A released version is immutable; corrections get a new PATCH release rather
than a moved or rebuilt tag.

Clients are told about every release, not only feature releases: a client whose
release is older than its server's, a PATCH release included, gets an update
notice from `lt setup status`, `lt doctor`, `lt-mcp` and the coverage read. Cut
a PATCH release for a fix that consumers should take, such as a dependency
bound that breaks `lt-mcp`, and a MINOR release for features. The same release
at a different commit is reported but never suggested, so unreleased commits
notify nobody. See
[setup.md](setup.md#know-when-a-client-install-is-broken-or-behind-its-server).

## Preparing and publishing a release

1. Start from a clean branch based on `main`, with CI green. Review merged work
   and choose the bump from the policy above.
2. Preview the version change, for example:

   ```bash
   uv version --bump patch --dry-run
   ```

3. Apply it while updating `pyproject.toml` and `uv.lock` without an unnecessary
   environment sync:

   ```bash
   uv version --bump patch --no-sync
   ```

   Use `--bump minor`, `--bump major`, or an exact version such as
   `uv version 1.0.0 --no-sync` when appropriate.
4. Validate the version and release build:

   ```bash
   uv run python scripts/verify_release.py
   uv run ruff check .
   uv run pytest -q
   uv build --no-sources
   ```

5. Commit the version preparation as `chore(release): prepare vX.Y.Z` and merge
   it to `main`. Merging is the release: nothing else needs pushing.

The `auto-release` GitHub Actions workflow runs after each `ci` run on `main`.
When that run passed and its commit's `project.version` has no `vX.Y.Z` tag
yet, it calls the `release` workflow for that commit. So the first `main`
commit to pass CI after a version bump is the one released, and a version
that already has its tag is never released again. A red `main` releases
nothing until a later commit passes.

The `release` workflow rejects a tag that does not exactly match
`project.version`, reruns the Python quality gates, builds a wheel and source
distribution, and checks the installed wheel's runtime version. Only then does
it create the annotated tag and a GitHub Release with generated notes and both
artifacts. If a check fails, no tag is created, and the next green `main` run
tries again. If only the GitHub Release step fails, the tag already exists, so
no later run retries it: re-run that failed job from the Actions page, and it
keeps the tag when it names the same commit. It deliberately does not publish
to PyPI; adding package-index publication requires a separate decision and
trusted-publisher configuration.

A maintainer can still release by hand. Push an annotated tag on a commit
whose CI is green, and the `release` workflow runs for that tag directly:

```bash
git tag -a vX.Y.Z -m "Lab Tracker vX.Y.Z"
git push origin vX.Y.Z
```
