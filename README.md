# jointops-deploy

Deployment scripts for Joint Ops, a web app running beside PostgreSQL on one
Docker host. The application and image-build workflow are private; this repository
contains the deployment tooling and tests. It requires an existing installation.
`example-org/exampleapp` and `app-prod` are placeholders.

## Release flow

1. [`check-release-ci.sh`](deploy/check-release-ci.sh) requires a manual dispatch
   from `main` and the newest successful push CI run for the exact release commit.
   The private workflow then builds that commit and publishes its image by digest.
2. [`invoke-production-release.sh`](deploy/invoke-production-release.sh) sends
   three tool files over SSH with a pinned Ed25519 host key. Credentials live in a
   private temporary directory, are removed from the environment, and are deleted
   on exit. SSH ignores local configuration and propagates the remote exit status.
3. [`receive-production-release.py`](deploy/receive-production-release.py) checks
   the command, commit, run identity and image allowlist. It accepts an uncompressed
   archive of at most 1 MiB containing only `deploy-production.sh`,
   `production_context.py` and `lib/image-deployment.sh` under `deploy/`. Missing,
   duplicate or unexpected files, links and traversal paths are rejected.
   Tools go into a new release directory; the app checkout and secrets stay put.
4. [`deploy-production.sh`](deploy/deploy-production.sh) takes a non-blocking
   host-wide lock and runs [preflight](#preflight) before pulling the digest.
   It records the new image ID and tags the previous image for manual recovery.
5. The app stops. [`image-deployment.sh`](deploy/lib/image-deployment.sh) runs
   `pg_dump -Fc`, requires a nonempty archive, reads it through
   `pg_restore --file=/dev/null`, and saves a SHA-256 checksum. Full validation
   adds one archive read and decompression during downtime; it creates neither a
   second database nor a SQL file. It catches unreadable data that `--list` misses,
   but does not prove restorability. Size the maintenance window using real backups.
6. A one-off container runs `alembic upgrade head` followed by
   `alembic current --check-heads`. The app is recreated with
   `--no-build --pull never --wait`.
7. [`healthcheck.py`](deploy/healthcheck.py) checks HTTP, database connectivity and
   migration heads. Deployment also verifies the running image ID, digest reference
   and Docker health status. A start or health failure stops the candidate.

The receiver uses a minimal environment and the host's Docker login. It records
commit, image, tool hashes and exit status in `release.json`; deployment output
stays in the private `deployment.log`. Attempt records contain the failed phase,
backup location and previous image/schema metadata. Preflight errors are redacted;
migration logs may contain SQL or data and should not be posted publicly.

## Preflight

[`production_context.py`](deploy/production_context.py) reconstructs the Compose
context from the existing container labels. It requires one app for this checkout,
one healthy running PostgreSQL container, the original base file and all existing
overrides. It rejects the staging overlay and adds
[`compose.production-image.yml`](compose.production-image.yml) last.

Before pulling or stopping anything, it checks:

- The candidate is the requested digest, with no build configuration or automatic pulls.
- App environment values and PostgreSQL credentials match the live containers;
  no live-only app setting is dropped. The app uses the local database being backed up.
- Networks, persistent mounts and published ports match. App source mounts are
  refused and PostgreSQL must use a named data volume.
- Read-only root filesystems, privileged mode, capabilities and security options
  match. Explicit Compose users match; a live app user override cannot silently
  revert to an image default.

These guards cover specific deployment assumptions, not every Docker setting.
Resource limits and tmpfs options are not compared. Review changes to image-defined
user/default settings as part of the candidate build. Expanded configuration stays
in memory; metadata excludes environment values.

## Requirements

**Host:** Linux, Bash, Python 3, Docker Engine, Compose v2, `flock` and GNU coreutils
(`realpath`, `sha256sum`, `mktemp`). Compose must support `!reset`, JSON `config`,
`--wait`, `--wait-timeout` and `--pull never`. Test your chosen versions on an
isolated host; the fake tests do not establish a minimum Docker/Compose version.
Images target `linux/amd64`.

Keep the existing Compose project, files and overrides in `/opt/jointops/app`,
with a private `.env.production`, an existing app container, and healthy PostgreSQL
with its named volume and initialized Alembic state. This tooling does not bootstrap
an empty host. [compose.yml](compose.yml) declares required variables with `:?`:
session/database settings, technician sign-in settings, separate M365 app/certificate
settings and callback URLs. The certificate at `JOINTOPS_CERT_HOST_PATH` must exist.
The host needs its own registry login with pull access and space for images,
backups and logs. Retention and off-host backups are operator responsibilities.

**Image:** include the application, its dependencies, `/app/alembic.ini`, migrations
and `/app/deploy/healthcheck.py`. Python, SQLAlchemy, the configured PostgreSQL driver
and Alembic with `current --check-heads` must be available. The entrypoint must
execute supplied migration commands and propagate failure. Normal startup serves
port 8000; the image's Docker `HEALTHCHECK` uses the included probe. Preserve its
intended non-root user and compatibility with Compose's read-only filesystem,
temporary mounts and dropped capabilities.

**Runner:** Bash, Python 3, `gh`, OpenSSH and `tar`, with read access to the private
application's Actions runs and access to the SSH endpoint through the existing
private network policy. Required inputs are:

| Purpose | Environment variables |
| --- | --- |
| CI gate | `GITHUB_EVENT_NAME`, `GITHUB_REF`, `GITHUB_REPOSITORY`, `GITHUB_SHA` |
| Release | `IMAGE_REFERENCE`, `GITHUB_SHA`, `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT` |
| SSH | `PRODUCTION_SSH_HOST`, `PRODUCTION_SSH_USER`, `PRODUCTION_SSH_PRIVATE_KEY`, `PRODUCTION_SSH_KNOWN_HOSTS` |

The private workflow must gate, check out and build the same commit, then pass the
registry's published digest. The receiver records that association but does not
independently verify image provenance. Adapt fixed repository/image allowlists and
their tests deliberately; do not replace them with unrestricted input.

## SSH setup and trust

Use a dedicated account/key and install the receiver at a fixed,
administrator-controlled path. Example authorized-keys entry:

```text
restrict,command="/usr/bin/python3 -I /opt/jointops/deploy-tools/receive-production-release.py" ssh-ed25519 <DEPLOY_PUBLIC_KEY_BASE64>
```

OpenSSH's [`restrict`](https://man.openbsd.org/sshd.8#restrict) disables port,
agent and X11 forwarding, PTYs and user SSH startup scripts. A forced command alone
does not disable forwarding. Use a root-managed `AuthorizedKeysFile`, disable
password/keyboard-interactive authentication, and provide no unrestricted alternate
key. Keep receiver/key files root-owned (0644) and parent directories unwritable by
the deploy account (0755). Python `-I` excludes caller-controlled import paths.

Create `/opt/jointops/deploy-tools/releases` and `/opt/jointops/backups/deployments`
owned by the deploy account with mode 0700. The account needs Docker access and read
access to the app configuration and its Docker login. Protect secret/login files
with mode 0600 or equivalent restricted access; grant the container user only the
certificate access it needs. Keep dumps and logs private.

Obtain the server's Ed25519 host key through a trusted administrative channel and
pin it under the exact `app-prod` alias. Do not learn it during deployment. Restrict
network access and test wrong-host-key, shell and forwarding refusals on an isolated
SSH host before enabling a release key.

The runner is trusted: the receiver validates the bundle's shape, then executes its
contents. Tool hashes support auditing, not authorization. Docker access gives the
deploy account effective root authority, including the ability to bypass file
permissions. Treat the deploy key and workflow write access as production admin
access; the forced command does not sandbox malicious tooling.

## Failures and recovery

The app remains stopped from backup until the candidate is healthy. There is no
automatic rollback: migrations may already have committed, making the previous
image incompatible with the database.

Read `release.json`, `deployment.log`, and the attempt's `result.txt`/`metadata.json`.
Confirm live container and database state before acting:

- **Preflight, pull or image-retention failure:** the app has not intentionally
  been stopped or migrated. Correct the problem before retrying.
- **Stop or backup failure:** if migrations never began and the database/configuration
  are unchanged, an operator can restart the retained image. A partial dump is not usable.
- **Migration, schema, start or health failure:** keep traffic stopped, determine
  what committed, and choose a reviewed forward repair or database/image restore.
  Do not assume migrations rolled back or can safely be rerun.

For rollback, verify the dump's saved checksum and rehearse restoration into a
separate compatible database with no production connection. Check schema heads,
roles/extensions and representative data/application reads. `pg_dump` covers one
database, not cluster roles, certificates or environment files. Preserve the current
database where possible and account for writes after the backup that would be lost.
An operator must authorize the live restore and start its matching retained image
with the original Compose context. Verify HTTP, database, schema and image identity
before reopening traffic. Local rollback tags are deliberately not accepted by the
normal digest-only release command.

There is no overall execution deadline. Pulls, database inspection, backup validation
and migrations can block; `--wait-timeout` bounds startup waiting only. The host lock
does not bound PostgreSQL lock waits. Database lock-timeout policy belongs with the
private application's migration connections and transaction handling; none is injected
here. The receiver ignores `SIGHUP`, but hard kills, reboots and disk failures can
leave incomplete records. After disconnection, check the receiver and migration
container before retrying. Never delete the lock to bypass a running deployment.

## Tests and supporting files

On Linux or WSL with Bash, Python 3, `ssh-keygen`, `flock` and ShellCheck:

```sh
python3 -m unittest discover -s deploy/tests -v
shellcheck -x deploy/*.sh deploy/lib/*.sh
```

[CI](.github/workflows/ci.yml) runs both. Tests use fake `docker`, `ssh` and `gh` to
check ordering, failure handling, drift rejection and redaction. They do not verify
a real host, registry, SSH server, restore or connection-loss recovery.

[`deploy-staging.sh`](deploy/deploy-staging.sh) shares the image/backup/migration
steps and applies [compose.staging.yml](compose.staging.yml) last.
[`gunicorn.conf.py`](deploy/gunicorn.conf.py) and
[`requirements-wsgi.txt`](deploy/requirements-wsgi.txt) provide app server settings.

## License

[MIT](LICENSE)
