"""Run on Linux: python -m unittest discover -s deploy/tests -v.

Exercise the real Bash script with a recording Docker executable; no daemon,
credentials, application database, or network is used by these tests.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
IMAGE = "ghcr.io/example-org/exampleapp@sha256:" + "a" * 64
DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
with open(os.environ["DOCKER_CALLS"], "a") as log:
    log.write(json.dumps({"args": a, "image": os.environ.get("JOINTOPS_STAGING_IMAGE")}) + "\n")
stage = os.environ.get("FAIL_STAGE")
if stage == "config" and "config" in a:
    print("SYNTHETIC_SENSITIVE_CONFIGURATION", file=sys.stderr)
    print("SYNTHETIC_SENSITIVE_CONFIGURATION")
    sys.exit(17)
if (stage == "pull" and a[0] == "pull"
    or stage == "backup" and "pg_dump" in " ".join(a)
    or stage == "archive" and "pg_restore" in a and "--file=/dev/null" in a
    or stage == "migration" and "upgrade" in a
    or stage == "schema" and "--check-heads" in a
    or stage == "start" and "--force-recreate" in a
    or stage == "health" and "/app/deploy/healthcheck.py" in a):
    sys.exit(17)
if a[:2] == ["image", "inspect"] or a[0] == "inspect" and a[2] == "{{.Image}}":
    print("sha256:" + ("c" if stage == "identity" and a[0] == "inspect" else "b") * 64)
elif a[0] == "inspect" and a[2] == "{{.Config.Image}}":
    print(os.environ["JOINTOPS_STAGING_IMAGE"])
elif a[0] == "inspect" and a[2] == "{{.State.Health.Status}}":
    print("healthy")
elif a[0] == "inspect":
    print("previous_image=old-image\nprevious_image_id=sha256:old")
elif "ps" in a:
    print("test-app-container")
elif "pg_dump" in " ".join(a):
    print("synthetic database archive")
'''


class StagingDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "deploy").mkdir()
        shutil.copytree(ROOT / "deploy/lib", self.root / "deploy/lib")
        shutil.copyfile(ROOT / "deploy/deploy-staging.sh", self.root / "deploy/deploy-staging.sh")
        for name in ("compose.yml", "compose.staging.yml"):
            shutil.copyfile(ROOT / name, self.root / name)
        self.envfile = self.root / "staging.env"
        self.envfile.write_text("# synthetic only\n")
        self.override = self.root / "existing-proxy.yml"
        self.override.write_text("services: {}\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(DOCKER)
        docker.chmod(0o755)
        self.log = self.root / "calls.jsonl"

    def run_script(self, fail="", image=IMAGE):
        environment = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                       "DOCKER_CALLS": str(self.log), "FAIL_STAGE": fail}
        result = subprocess.run(
            ["bash", str(self.root / "deploy/deploy-staging.sh"), image,
             "existing-stage", str(self.envfile), str(self.override)],
            env=environment, text=True, capture_output=True, timeout=20,
        )
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, calls

    def test_success_preserves_context_and_orders_pull_stop_backup_migrate_start_health(self):
        result, calls = self.run_script()
        self.assertEqual(0, result.returncode, result.stderr)
        args = [row["args"] for row in calls]
        def index(predicate):
            return next(i for i, a in enumerate(args) if predicate(a))
        phases = [index(lambda a: a[0] == "pull"), index(lambda a: "stop" in a),
                  index(lambda a: "pg_dump" in " ".join(a)), index(lambda a: "upgrade" in a),
                  index(lambda a: "--check-heads" in a), index(lambda a: "--force-recreate" in a),
                  index(lambda a: "/app/deploy/healthcheck.py" in a)]
        self.assertEqual(sorted(phases), phases)
        archive_check = next(a for a in args if "pg_restore" in a)
        self.assertIn("--file=/dev/null", archive_check)
        self.assertLess(index(lambda a: "pg_restore" in a), index(lambda a: "upgrade" in a))
        for row in calls:
            a = row["args"]
            self.assertEqual(IMAGE, row["image"])
            self.assertNotIn("build", a)
            self.assertNotIn("--build", a)
            self.assertNotIn("down", a)
            if a[0] == "compose":
                self.assertEqual("existing-stage", a[a.index("--project-name") + 1])
                self.assertEqual(str(self.envfile), a[a.index("--env-file") + 1])
                self.assertEqual([str(self.root / "compose.yml"), str(self.override), str(self.root / "compose.staging.yml")],
                                 [a[i + 1] for i, value in enumerate(a) if value == "-f"])
                if "up" in a:
                    self.assertIn("--no-build", a)
                    self.assertIn("--no-deps", a)
                    if a[-1] == "postgres":
                        self.assertIn("--no-recreate", a)
                if "run" in a or "--force-recreate" in a:
                    self.assertEqual("never", a[a.index("--pull") + 1])
        record = next((self.root / ".staging-deploy/existing-stage").glob("*.txt")).read_text()
        self.assertIn("previous_image=old-image", record)
        self.assertIn("result=healthy", record)

    def test_pull_failure_does_not_stop_existing_app(self):
        result, calls = self.run_script("pull")
        self.assertEqual(17, result.returncode)
        self.assertFalse(any("stop" in row["args"] or "upgrade" in row["args"] for row in calls))

    def test_config_failure_is_redacted_and_stops_before_pull(self):
        result, calls = self.run_script("config")
        self.assertEqual(17, result.returncode)
        self.assertNotIn("SYNTHETIC_SENSITIVE_CONFIGURATION", result.stdout + result.stderr)
        self.assertIn("Unable to validate staging Compose configuration", result.stderr)
        self.assertFalse(any(row["args"][0] == "pull" for row in calls))
        record = next((self.root / ".staging-deploy/existing-stage").glob("*.txt")).read_text()
        self.assertIn("failed_phase=preflight", record)

    def test_backup_migration_or_schema_failure_never_starts_candidate(self):
        for failure in ("backup", "archive", "migration", "schema"):
            with self.subTest(failure=failure):
                self.log.unlink(missing_ok=True)
                result, calls = self.run_script(failure)
                self.assertEqual(17, result.returncode)
                self.assertTrue(any("stop" in row["args"] for row in calls))
                self.assertFalse(any("--force-recreate" in row["args"] for row in calls))
                if failure in ("backup", "archive"):
                    self.assertFalse(any("upgrade" in row["args"] for row in calls))

    def test_startup_or_health_failure_stops_candidate_without_automatic_rollback(self):
        for failure in ("start", "health", "identity"):
            with self.subTest(failure=failure):
                self.log.unlink(missing_ok=True)
                result, calls = self.run_script(failure)
                self.assertEqual(1 if failure == "identity" else 17, result.returncode)
                self.assertEqual(["stop", "jointops-app"], calls[-1]["args"][-2:])
                self.assertEqual(1, sum("--force-recreate" in row["args"] for row in calls))

    def test_deployment_lock_rejects_a_second_operator_before_docker(self):
        import fcntl
        state = self.root / ".staging-deploy/existing-stage"
        state.mkdir(parents=True)
        with (state / "deploy.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result, calls = self.run_script()
            self.assertEqual(1, result.returncode)
            self.assertIn("already running", result.stderr)
            self.assertEqual([], calls)

    def test_mutable_tags_and_other_registries_are_rejected_before_docker(self):
        for image in ("ghcr.io/example-org/exampleapp:latest", "example.com/app@sha256:" + "a" * 64):
            with self.subTest(image=image):
                result, calls = self.run_script(image=image)
                self.assertEqual(2, result.returncode)
                self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
