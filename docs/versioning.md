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
Adopting this policy does not itself publish or tag a release; `0.1.0` remains
the initial baseline until a maintainer intentionally completes the release
steps below.

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

5. Commit the version preparation as `chore(release): prepare vX.Y.Z`, merge it
   to `main`, and confirm CI is green on that exact commit.
6. Create and push an annotated tag on the merged commit:

   ```bash
   git tag -a vX.Y.Z -m "Lab Tracker vX.Y.Z"
   git push origin vX.Y.Z
   ```

The `release` GitHub Actions workflow rejects a tag that does not exactly match
`project.version`, reruns the Python quality gates, builds a wheel and source
distribution, checks the installed wheel's runtime version, and creates a
GitHub Release with generated notes and both artifacts. It deliberately does
not publish to PyPI; adding package-index publication requires a separate
decision and trusted-publisher configuration.
