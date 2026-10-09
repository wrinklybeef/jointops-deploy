"""Read-only production discovery and guards. Never print runtime secrets."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit


class GuardError(ValueError):
    """A deliberately safe diagnostic, never provider or parser output."""


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.PIPE)


def require(condition, message):
    if not condition:
        raise GuardError(message)


def environment(container):
    return dict(item.split("=", 1) for item in container["Config"]["Env"])


def containers(service, project=None):
    filters = ["--filter", f"label=com.docker.compose.service={service}",
               "--filter", "label=com.docker.compose.oneoff=False"]
    if project:
        filters += ["--filter", f"label=com.docker.compose.project={project}"]
    ids = docker("ps", "-a", "-q", *filters).split()
    return json.loads(docker("inspect", *ids)) if ids else []


def main(root, attempt):
    apps = [app for app in containers("jointops-app")
            if Path(app["Config"]["Labels"].get(
                "com.docker.compose.project.working_dir", "/missing")).resolve() == root]
    require(len(apps) == 1, "Expected exactly one existing app for this checkout; refusing to guess a project.")
    app = apps[0]
    labels = app["Config"]["Labels"]
    project = labels["com.docker.compose.project"]
    require(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project), "Invalid existing project name.")
    databases = containers("postgres", project)
    require(len(databases) == 1, "Expected exactly one existing PostgreSQL container.")
    database = databases[0]
    require(database["State"].get("Health", {}).get("Status") == "healthy"
            and database["State"]["Running"], "Existing PostgreSQL must be running and healthy.")
    files = [Path(name).resolve() for name in labels.get(
        "com.docker.compose.project.config_files", "").split(",") if name]
    overlay = root / "compose.production-image.yml"
    files = [path for path in files if path != overlay]
    require(files and files[0] == root / "compose.yml", "Existing Compose base must be this checkout's compose.yml.")
    require(all(path.is_file() for path in files), "An existing Compose override is missing; restore it before deploying.")
    require(root / "compose.staging.yml" not in files, "Refusing a staging Compose context for production.")
    files.append(overlay)
    compose = ["docker", "compose", "--project-directory", str(root),
               "--project-name", project, "--env-file", str(root / ".env.production")]
    for path in files:
        compose += ["-f", str(path)]
    # Capture potentially secret expanded config in memory only, never in records.
    config = json.loads(subprocess.check_output(
        [*compose, "config", "--format", "json"], text=True, stderr=subprocess.PIPE))
    service = config["services"]["jointops-app"]
    require(not service.get("build"), "Application build configuration must be absent.")
    require(service["image"] == os.environ["JOINTOPS_PRODUCTION_IMAGE"], "Candidate image mismatch.")
    require(service.get("pull_policy") == "never", "Automatic pulls must be disabled.")
    live_env = environment(app)
    desired_env = service.get("environment", {})
    require(all(live_env.get(key) == value for key, value in desired_env.items()),
            "Runtime environment differs from .env.production/Compose; reconcile it before image deployment.")
    old_image = json.loads(docker("image", "inspect", app["Image"]))[0]
    old_defaults = environment(old_image)
    require(all(key in desired_env or old_defaults.get(key) == value for key, value in live_env.items()),
            "An existing runtime environment override would be removed.")
    db_env = environment(database)
    require(all(db_env.get(key) == config["services"]["postgres"]["environment"].get(key)
                for key in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")),
            "PostgreSQL environment differs from existing container.")
    url = urlsplit(desired_env["JOINTOPS_DATABASE_URL"])
    require(url.scheme.startswith("postgresql") and url.hostname == "postgres"
            and url.port in (None, 5432) and unquote(url.path.lstrip("/")) == db_env["POSTGRES_DB"]
            and not url.query, "App must use the local PostgreSQL database being backed up, without URL routing overrides.")

    for name, container in (("jointops-app", app), ("postgres", database)):
        definition = config["services"][name]
        host = container["HostConfig"]
        require(bool(definition.get("read_only", False)) == bool(host.get("ReadonlyRootfs", False))
                and bool(definition.get("privileged", False)) == bool(host.get("Privileged", False)),
                f"{name} filesystem or privilege controls differ from existing container.")
        for setting, field in (("cap_drop", "CapDrop"), ("cap_add", "CapAdd")):
            desired = {cap.upper().removeprefix("CAP_") for cap in definition.get(setting, [])}
            live = {cap.upper().removeprefix("CAP_") for cap in host.get(field) or []}
            require(desired == live, f"{name} capability controls differ from existing container.")
        # Docker accepts both ':' and '='; a bare no-new-privileges means true.
        desired_options = {option.replace("=", ":") for option in definition.get("security_opt", [])}
        live_options = {option.replace("=", ":") for option in host.get("SecurityOpt") or []}
        desired_options = {"no-new-privileges:true" if option == "no-new-privileges" else option
                           for option in desired_options}
        live_options = {"no-new-privileges:true" if option == "no-new-privileges" else option
                        for option in live_options}
        require(desired_options == live_options, f"{name} security options differ from existing container.")
        if "user" in definition:
            require(str(definition["user"]) == container["Config"].get("User", ""),
                    f"{name} runtime user differs from existing container.")
        elif name == "jointops-app":
            require(container["Config"].get("User", "") == old_image["Config"].get("User", ""),
                    "An existing application runtime user override would be removed.")
        networks = {config["networks"][key]["name"] for key in definition["networks"]}
        require(networks == set(container["NetworkSettings"]["Networks"]), f"{name} networks differ from existing container.")
        mounts = set()
        for mount in definition.get("volumes", []):
            require(not (name == "jointops-app" and (mount["target"] == "/app" or mount["target"].startswith("/app/"))),
                    "Application source mounts are not allowed for image deployment.")
            source = (config["volumes"][mount["source"]]["name"] if mount["type"] == "volume" else mount["source"])
            mounts.add((mount["type"], source, mount["target"], not mount.get("read_only", False)))
        live_mounts = {(m["Type"], m.get("Name") if m["Type"] == "volume" else m["Source"], m["Destination"], m["RW"])
                       for m in container["Mounts"] if m["Type"] != "tmpfs"}
        require(mounts == live_mounts, f"{name} persistent mounts differ from existing container.")
        ports = {(f'{p["target"]}/{p.get("protocol", "tcp")}', p.get("host_ip", ""), str(p["published"]))
                 for p in definition.get("ports", [])}
        live_ports = {(port, bind["HostIp"], bind["HostPort"])
                      for port, bindings in (container["HostConfig"].get("PortBindings") or {}).items()
                      for bind in bindings or []}
        require(ports == live_ports, f"{name} published ports differ from existing container.")
    require(any(m["Type"] == "volume" and m["Destination"] == "/var/lib/postgresql/data"
                for m in database["Mounts"]), "Expected existing named PostgreSQL data volume.")
    database_state = json.loads(subprocess.check_output([
        *compose, "exec", "-T", "postgres", "sh", "-c",
        'exec psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1 -c "$1"',
        "sh", "SELECT json_build_object('owner', pg_get_userbyid(datdba), "
        "'heads', (SELECT json_agg(version_num) FROM alembic_version)) "
        "FROM pg_database WHERE datname=current_database()"], text=True, stderr=subprocess.PIPE))
    metadata = dict(project=project, compose_files=[str(p) for p in files],
                    env_file=str(root / ".env.production"), previous_container=app["Id"],
                    previous_image=app["Config"]["Image"], previous_image_id=app["Image"],
                    previous_repo_digests=json.loads(docker("image", "inspect", app["Image"]))[0].get("RepoDigests", []),
                    requested_image=os.environ["JOINTOPS_PRODUCTION_IMAGE"],
                    database_name=db_env["POSTGRES_DB"], database_owner=database_state["owner"],
                    previous_schema_heads=database_state["heads"],
                    postgres_container=database["Id"],
                    postgres_volumes=[m["Name"] for m in database["Mounts"] if m["Type"] == "volume"],
                    database_backup=str(attempt / "database.dump"))
    (attempt / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (attempt / "compose.args").write_bytes(b"\0".join(arg.encode() for arg in compose) + b"\0")
    print(f"Verified existing Compose project: {project}; PostgreSQL will remain untouched.")


if __name__ == "__main__":
    try:
        main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]))
    except (ValueError, KeyError, TypeError, IndexError, OSError, subprocess.CalledProcessError) as error:
        # Driver/config output can contain secrets; report only our guard messages.
        message = str(error) if isinstance(error, GuardError) else "Unable to inspect existing Docker/Compose configuration."
        print(f"Production preflight failed: {message}", file=sys.stderr)
        sys.exit(1)
