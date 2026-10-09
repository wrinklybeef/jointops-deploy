"""One-time installed OpenSSH forced command. Transport only, never migrate here."""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile

APP_ROOT = Path("/opt/jointops/app")
RELEASES = Path("/opt/jointops/deploy-tools/releases")
FILES = {"deploy/deploy-production.sh", "deploy/production_context.py", "deploy/lib/image-deployment.sh"}
MAX_ARCHIVE = 1024 * 1024


def parse_command(command):
    args = shlex.split(command)
    if not (len(args) == 4 and args[0] == "deploy-production"
            and re.fullmatch(r"[a-f0-9]{40}", args[1])
            and re.fullmatch(r"[0-9]+-[0-9]+", args[2])
            and re.fullmatch(r"ghcr\.io/example-org/exampleapp@sha256:[a-f0-9]{64}", args[3])):
        raise ValueError("Expected a production release command with commit, run identity and immutable image.")
    return args[1:]


def read_bundle(stream):
    data = stream.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE:
        raise ValueError("Deployment tooling archive exceeds size limit.")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive:
            if member.name not in FILES or member.name in files or not member.isfile():
                raise ValueError("Only the three expected regular deployment files are allowed.")
            if member.size > MAX_ARCHIVE:
                raise ValueError("Deployment tooling member exceeds size limit.")
            files[member.name] = archive.extractfile(member).read()
    if set(files) != FILES:
        raise ValueError("Deployment tooling bundle is incomplete.")
    return files


def receive(command, stream, app_root=APP_ROOT, releases=RELEASES):
    commit, run, image = parse_command(command)
    files = read_bundle(stream)
    if not (app_root / ".env.production").is_file():
        raise ValueError("Existing production environment is missing.")
    # Never overwrite a running release's tooling, application checkout or secrets.
    release = Path(tempfile.mkdtemp(prefix=f"{commit}-{run}-", dir=releases))
    for name, data in files.items():
        path = release / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    record = {"commit": commit, "github_run": run, "image": image,
              "tooling_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    manifest = release / "release.json"
    manifest.write_text(json.dumps(record, indent=2) + "\n")
    log = release / "deployment.log"
    # Keep the host operator's Docker login, but do not inherit remote Docker or
    # Compose routing overrides. Runtime configuration comes from .env.production.
    environment = {key: os.environ[key] for key in ("HOME", "USER", "LOGNAME", "DOCKER_CONFIG") if key in os.environ}
    environment.update(PATH="/usr/local/bin:/usr/bin:/bin", JOINTOPS_DEPLOY_ROOT=str(app_root),
                       JOINTOPS_DEPLOY_STATE_DIR="/opt/jointops/backups/deployments")
    print(f"Starting {image}; release record: {manifest}", flush=True)
    # Logs may include migration SQL/data; retain them privately on the VM rather
    # than streaming application output into GitHub Actions logs.
    with log.open("wb") as output:
        result = subprocess.run(["bash", str(release / "deploy/deploy-production.sh"), image],
                                cwd=app_root, env=environment, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT, check=False)
    record["exit_code"] = result.returncode
    manifest.write_text(json.dumps(record, indent=2) + "\n")
    print(f"Deployment {'succeeded' if result.returncode == 0 else 'FAILED'}: {image}; exit={result.returncode}; log={log}", flush=True)
    return result.returncode if result.returncode >= 0 else 1


if __name__ == "__main__":
    os.umask(0o077)
    # A lost SSH connection must not interrupt an in-flight schema migration.
    # The existing host deployment lock remains held until the script finishes.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        sys.exit(receive(os.environ.get("SSH_ORIGINAL_COMMAND", ""), sys.stdin.buffer))
    except (ValueError, OSError, tarfile.TarError):
        print("Release receiver failed; check the command, bundle, host setup and private release records.", file=sys.stderr)
        sys.exit(1)
