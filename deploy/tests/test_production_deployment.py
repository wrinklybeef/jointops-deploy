"""Production control flow against a recording Docker CLI, no daemon or secrets."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "ghcr.io/example-org/exampleapp@sha256:" + "a" * 64
OLD = "sha256:" + "c" * 64
NEW = "sha256:" + "b" * 64
DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
a = sys.argv[1:]
root = Path(os.environ['TEST_ROOT'])
with (root / 'calls.jsonl').open('a') as log:
    log.write(json.dumps(a) + '\n')
f = json.loads((root / 'fixture.json').read_text())
stage = os.environ.get('FAIL_STAGE')
if (stage == 'pull' and a[0] == 'pull'
    or stage == 'backup' and 'pg_dump' in ' '.join(a)
    or stage == 'archive' and 'pg_restore' in a and '--file=/dev/null' in a
    or stage == 'migration' and 'upgrade' in a
    or stage == 'schema' and '--check-heads' in a
    or stage == 'start' and '--force-recreate' in a
    or stage == 'health' and '/app/deploy/healthcheck.py' in a):
    sys.exit(17)
if stage in ('inspect_stderr', 'config_stderr', 'database_stderr') and (
    stage == 'inspect_stderr' and a[0] == 'ps'
    or stage == 'config_stderr' and 'config' in a
    or stage == 'database_stderr' and 'psql' in ' '.join(a)):
    print('SYNTHETIC_SENSITIVE_CONFIGURATION', file=sys.stderr)
    sys.exit(17)
if a[0] == 'ps':
    print('db' if any('service=postgres' in v for v in a) else 'app')
elif a[0] == 'inspect':
    if '--format' not in a:
        print(json.dumps([f[a[-1]]]))
    elif a[2] == '{{.Image}}':
        print('wrong-image' if stage == 'identity' else f['new_id'])
    elif a[2] == '{{.Config.Image}}':
        print(os.environ['JOINTOPS_PRODUCTION_IMAGE'])
    elif a[2] == '{{.State.Health.Status}}':
        print('unhealthy' if stage == 'health_status' else 'healthy')
elif a[:2] == ['image', 'inspect']:
    print(f['new_id'] if '--format' in a else json.dumps([f['old_image']]))
elif a[0] == 'compose':
    if 'config' in a:
        print(json.dumps(f['config']))
    elif 'ps' in a:
        print('app')
    elif 'pg_dump' in ' '.join(a) and stage != 'empty_backup':
        print('synthetic archive')
    elif 'psql' in ' '.join(a):
        print(json.dumps({'owner': 'jointops', 'heads': ['existing_head']}))
'''


class ProductionDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / "deploy", self.root / "deploy", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        for file in ("compose.yml", "compose.production-image.yml"):
            shutil.copyfile(ROOT / file, self.root / file)
        (self.root / ".env.production").write_text("# synthetic fixture only\n")
        (self.root / "proxy.yml").write_text("services: {}\n")
        self.state = self.root / "state"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(DOCKER)
        (self.bin / "docker").chmod(0o755)
        self.fixture = self.make_fixture()
        self.log = self.root / "calls.jsonl"
        self.script = self.root / "deploy/deploy-production.sh"

    def make_fixture(self):
        app_env = {"JOINTOPS_DATABASE_URL": "postgresql+psycopg://jointops:synthetic@postgres:5432/jointops"}
        db_env = {"POSTGRES_DB": "jointops", "POSTGRES_USER": "admin", "POSTGRES_PASSWORD": "synthetic"}
        config = {"services": {
            "jointops-app": {"image": IMAGE, "pull_policy": "never", "environment": app_env,
                             "read_only": True, "cap_drop": ["ALL"],
                             "security_opt": ["no-new-privileges:true"],
                             "networks": {"database": {}, "outbound": {}},
                             "ports": [{"target": 8000, "published": "8000", "host_ip": "127.0.0.1"}]},
            "postgres": {"environment": db_env, "networks": {"database": {}},
                         "volumes": [{"type": "volume", "source": "postgres-data", "target": "/var/lib/postgresql/data"}]}},
            "networks": {key: {"name": "existing-prod_" + key} for key in ("database", "outbound")},
            "volumes": {"postgres-data": {"name": "existing-prod_postgres-data"}}}
        app = {"Id": "app", "Image": OLD, "Config": {"Image": "jointops365:old-source-build", "User": "10001:10001",
            "Env": [f"{k}={v}" for k, v in app_env.items()], "Labels": {
                "com.docker.compose.project": "existing-prod",
                "com.docker.compose.project.working_dir": str(self.root),
                "com.docker.compose.project.config_files": f"{self.root}/compose.yml,{self.root}/proxy.yml"}},
            "NetworkSettings": {"Networks": {"existing-prod_database": {}, "existing-prod_outbound": {}}},
            "Mounts": [], "HostConfig": {"ReadonlyRootfs": True, "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "PortBindings": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]}}}
        db = {"Id": "db", "Config": {"Env": [f"{k}={v}" for k, v in db_env.items()]},
              "State": {"Running": True, "Health": {"Status": "healthy"}},
              "NetworkSettings": {"Networks": {"existing-prod_database": {}}},
              "Mounts": [{"Type": "volume", "Name": "existing-prod_postgres-data", "Destination": "/var/lib/postgresql/data", "RW": True}],
              "HostConfig": {"PortBindings": {}}}
        return dict(app=app, db=db, config=config, new_id=NEW,
                    old_image={"Config": {"Env": [], "User": "10001:10001"}, "RepoDigests": ["old-repo@sha256:old"]})

    def run_script(self, fail="", image=IMAGE):
        (self.root / "fixture.json").write_text(json.dumps(self.fixture))
        self.log.unlink(missing_ok=True)
        result = subprocess.run(["bash", str(self.script), image],
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "TEST_ROOT": str(self.root),
                 "JOINTOPS_DEPLOY_STATE_DIR": str(self.state), "FAIL_STAGE": fail,
                 **getattr(self, "extra_env", {})},
            text=True, capture_output=True, timeout=20)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, calls

    def test_success_order_context_previous_metadata_and_backup(self):
        result, calls = self.run_script()
        self.assertEqual(0, result.returncode, result.stderr)
        operations = [next(i for i, a in enumerate(calls) if predicate(a)) for predicate in (
            lambda a: a[0] == "pull", lambda a: a[:2] == ["image", "tag"], lambda a: "stop" in a,
            lambda a: "pg_dump" in " ".join(a), lambda a: "pg_restore" in a,
            lambda a: "upgrade" in a, lambda a: "--check-heads" in a,
            lambda a: "--force-recreate" in a, lambda a: "/app/deploy/healthcheck.py" in a)]
        self.assertEqual(sorted(operations), operations)
        archive_check = next(a for a in calls if "pg_restore" in a)
        self.assertIn("--file=/dev/null", archive_check)
        self.assertNotIn("--list", archive_check)
        for a in calls:
            self.assertNotIn("build", a)
            self.assertNotIn("--build", a)
            self.assertNotIn("down", a)
            if a[0] == "compose":
                self.assertEqual("existing-prod", a[a.index("--project-name") + 1])
                self.assertEqual(str(self.root / ".env.production"), a[a.index("--env-file") + 1])
                self.assertEqual([str(self.root / n) for n in ("compose.yml", "proxy.yml", "compose.production-image.yml")],
                                 [a[i+1] for i, v in enumerate(a) if v == "-f"])
                if "up" in a or "run" in a:
                    self.assertEqual("jointops-app", a[a.index("jointops-app")])
                    self.assertNotIn("postgres", a)
                    self.assertIn("--no-deps", a)
                    self.assertEqual("never", a[a.index("--pull") + 1])
                if "up" in a:
                    self.assertIn("--no-build", a)
        attempt = next(self.state.glob("*/metadata.json")).parent
        metadata = json.loads((attempt / "metadata.json").read_text())
        self.assertEqual(OLD, metadata["previous_image_id"])
        self.assertEqual("jointops365:old-source-build", metadata["previous_image"])
        self.assertEqual(["existing-prod_postgres-data"], metadata["postgres_volumes"])
        self.assertNotIn("synthetic", (attempt / "metadata.json").read_text())
        self.assertTrue((attempt / "database.dump.sha256").is_file())
        self.assertIn("result=healthy", (attempt / "result.txt").read_text())
        self.assertIn(IMAGE, result.stdout)

    def test_invalid_reference_before_any_docker_or_state_changes(self):
        for image in ("ghcr.io/example-org/exampleapp:latest", "ghcr.io/other/app@sha256:" + "a"*64, IMAGE[:-1], IMAGE.upper()):
            with self.subTest(image=image):
                result, calls = self.run_script(image=image)
                self.assertEqual(2, result.returncode)
                self.assertEqual([], calls)
                self.assertFalse(self.state.exists())

    def test_pull_failure_leaves_app_running_and_retains_previous_metadata(self):
        result, calls = self.run_script("pull")
        self.assertEqual(17, result.returncode)
        self.assertFalse(any("stop" in a or "pg_dump" in " ".join(a) for a in calls))
        self.assertEqual(OLD, json.loads(next(self.state.glob("*/metadata.json")).read_text())["previous_image_id"])

    def test_backup_and_migration_failures_never_start_candidate(self):
        for failure in ("backup", "archive", "empty_backup", "migration", "schema"):
            with self.subTest(failure=failure):
                result, calls = self.run_script(failure)
                self.assertNotEqual(0, result.returncode)
                self.assertTrue(any("stop" in a for a in calls))
                self.assertFalse(any("--force-recreate" in a for a in calls))
                if failure in ("backup", "archive", "empty_backup"):
                    self.assertFalse(any("upgrade" in a for a in calls))

    def test_start_health_or_identity_failure_stops_candidate(self):
        for failure in ("start", "health", "identity", "health_status"):
            with self.subTest(failure=failure):
                result, calls = self.run_script(failure)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(["stop", "jointops-app"], calls[-1][-2:])

    def test_preflight_rejects_runtime_drift_and_wrong_database_before_pull(self):
        for failure in ("project", "network", "volume", "port", "environment", "database", "unhealthy", "override"):
            with self.subTest(failure=failure):
                self.fixture = self.make_fixture()
                f = self.fixture
                if failure == "project":
                    f["app"]["Config"]["Labels"]["com.docker.compose.project.working_dir"] = "/another-checkout"
                elif failure == "network":
                    f["app"]["NetworkSettings"]["Networks"] = {}
                elif failure == "volume":
                    f["db"]["Mounts"][0]["Name"] = "other-volume"
                elif failure == "port":
                    f["app"]["HostConfig"]["PortBindings"]["8000/tcp"][0]["HostPort"] = "9999"
                elif failure == "environment":
                    f["config"]["services"]["jointops-app"]["environment"]["SECRET"] = "changed"
                elif failure == "database":
                    f["db"]["Config"]["Env"][0] = "POSTGRES_DB=other"
                elif failure == "unhealthy":
                    f["db"]["State"]["Health"]["Status"] = "unhealthy"
                else:
                    f["app"]["Config"]["Labels"]["com.docker.compose.project.config_files"] += ",/missing.yml"
                result, calls = self.run_script()
                self.assertNotEqual(0, result.returncode)
                self.assertFalse(any(a[0] == "pull" or "stop" in a for a in calls))

    def test_security_drift_is_rejected_before_pull_or_stop(self):
        for name in ("jointops-app", "postgres"):
            for setting, value in (("read_only", name == "postgres"), ("privileged", True),
                                   ("cap_drop", [] if name == "jointops-app" else ["ALL"]),
                                   ("cap_add", ["SYS_ADMIN"]),
                                   ("security_opt", [] if name == "jointops-app" else ["no-new-privileges:true"]),
                                   ("user", "unexpected-user")):
                with self.subTest(service=name, setting=setting):
                    self.fixture = self.make_fixture()
                    self.fixture["config"]["services"][name][setting] = value
                    result, calls = self.run_script()
                    self.assertNotEqual(0, result.returncode)
                    self.assertFalse(any(a[0] == "pull" or "stop" in a for a in calls))

    def test_existing_runtime_user_override_cannot_be_silently_removed(self):
        self.fixture["app"]["Config"]["User"] = "other-user"
        result, calls = self.run_script()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("runtime user override", result.stderr)
        self.assertFalse(any(a[0] == "pull" for a in calls))

    def test_equivalent_security_options_and_explicit_user_are_accepted(self):
        definition = self.fixture["config"]["services"]["jointops-app"]
        definition.update(user="10001:10001", security_opt=["no-new-privileges=true"],
                          cap_add=["NET_BIND_SERVICE"])
        self.fixture["app"]["HostConfig"]["CapAdd"] = ["CAP_NET_BIND_SERVICE"]
        result, _ = self.run_script()
        self.assertEqual(0, result.returncode, result.stderr)

    def test_subprocess_diagnostics_never_escape_preflight(self):
        for failure in ("inspect_stderr", "config_stderr", "database_stderr"):
            with self.subTest(failure=failure):
                result, calls = self.run_script(failure)
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn("SYNTHETIC_SENSITIVE_CONFIGURATION", result.stdout + result.stderr)
                self.assertIn("Unable to inspect", result.stderr)
                self.assertFalse(any(a[0] == "pull" or "stop" in a for a in calls))

    def test_parser_errors_do_not_expose_database_url_values(self):
        url = "postgresql://user:password@postgres:SYNTHETIC_SENSITIVE_CONFIGURATION/jointops"
        self.fixture["config"]["services"]["jointops-app"]["environment"]["JOINTOPS_DATABASE_URL"] = url
        self.fixture["app"]["Config"]["Env"] = ["JOINTOPS_DATABASE_URL=" + url]
        result, calls = self.run_script()
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("SYNTHETIC_SENSITIVE_CONFIGURATION", result.stdout + result.stderr)
        self.assertIn("Unable to inspect", result.stderr)
        self.assertFalse(any(a[0] == "pull" for a in calls))

    def test_repeated_execution_keeps_each_attempt_and_existing_overlay_once(self):
        self.fixture["app"]["Config"]["Labels"]["com.docker.compose.project.config_files"] += f",{self.root}/compose.production-image.yml"
        for _ in range(2):
            result, calls = self.run_script()
            self.assertEqual(0, result.returncode, result.stderr)
            for a in calls:
                if a[0] == "compose":
                    self.assertEqual(1, a.count(str(self.root / "compose.production-image.yml")))
        self.assertEqual(2, len(list(self.state.glob("*/metadata.json"))))

    def test_lock_rejects_second_operator_before_docker(self):
        import fcntl
        self.state.mkdir()
        with (self.state / "deploy.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result, calls = self.run_script()
            self.assertNotEqual(0, result.returncode)
            self.assertEqual([], calls)

    def test_versioned_tools_use_existing_compose_checkout_without_git_pull(self):
        bundle = self.root / "release-tools"
        shutil.move(str(self.root / "deploy"), bundle)
        self.script = bundle / "deploy-production.sh"
        self.extra_env = {"JOINTOPS_DEPLOY_ROOT": str(self.root)}
        result, calls = self.run_script()
        self.assertEqual(0, result.returncode, result.stderr)
        for args in calls:
            if args[0] == "compose":
                self.assertEqual(str(self.root), args[args.index("--project-directory") + 1])


if __name__ == "__main__":
    unittest.main()
