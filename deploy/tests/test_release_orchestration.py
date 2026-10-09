"""Local orchestration tests: no SSH server, registry, tailnet or production access."""

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
IMAGE = "ghcr.io/example-org/exampleapp@sha256:" + "b" * 64
spec = importlib.util.spec_from_file_location("receiver", ROOT / "deploy/receive-production-release.py")
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


def bundle(script=b"exit 0\n", extra=None):
    output = io.BytesIO()
    files = {name: b"# test fixture\n" for name in receiver.FILES}
    files["deploy/deploy-production.sh"] = script
    if extra:
        files.update(extra)
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    output.seek(0)
    return output


class ReceiverTests(unittest.TestCase):
    def test_rejects_invalid_command_or_mutable_image(self):
        for command in ("", "bash", f"deploy-production {SHA} 1-1 ghcr.io/example-org/exampleapp:latest",
                        f"deploy-production {SHA} 1-1 {IMAGE}; touch /tmp/example",
                        f"deploy-production ../escape 1-1 {IMAGE}"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                receiver.parse_command(command)

    def test_archive_rejects_extra_files_traversal_links_missing_and_oversize(self):
        for name in (".env.production", "../escape", "/absolute"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                receiver.read_bundle(bundle(extra={name: b"no"}))
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            entry = tarfile.TarInfo("deploy/deploy-production.sh")
            entry.type = tarfile.SYMTYPE
            entry.linkname = "/etc/passwd"
            archive.addfile(entry)
        output.seek(0)
        with self.assertRaises(ValueError):
            receiver.read_bundle(output)
        with self.assertRaises(ValueError):
            receiver.read_bundle(io.BytesIO(b"x" * (receiver.MAX_ARCHIVE + 1)))
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w"):
            pass
        output.seek(0)
        with self.assertRaises(ValueError):
            receiver.read_bundle(output)

    def test_receiver_preserves_host_files_captures_status_and_keeps_logs_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, releases = root / "app", root / "releases"
            app.mkdir()
            releases.mkdir()
            (app / ".env.production").write_text("host configuration stays here\n")
            (app / "compose.yml").write_text("host compose stays here\n")
            for status in (0, 17):
                script = (f'test "$JOINTOPS_DEPLOY_ROOT" = "$PWD" || exit 99\n'
                          f'test "$1" = "{IMAGE}" || exit 98\n'
                          'echo private-migration-output\n'
                          f'exit {status}\n').encode()
                output = io.StringIO()
                with redirect_stdout(output):
                    code = receiver.receive(f"deploy-production {SHA} 42-1 {IMAGE}", bundle(script), app, releases)
                self.assertEqual(status, code)
                self.assertNotIn("private-migration-output", output.getvalue())
                self.assertIn("succeeded" if status == 0 else "FAILED", output.getvalue())
            records = list(releases.glob("*/release.json"))
            self.assertEqual(2, len(records))
            self.assertEqual({0, 17}, {json.loads(p.read_text())["exit_code"] for p in records})
            for path in records:
                record = json.loads(path.read_text())
                self.assertEqual(SHA, record["commit"])
                self.assertEqual(IMAGE, record["image"])
                self.assertEqual(receiver.FILES, set(record["tooling_sha256"]))
                self.assertIn("private-migration-output", (path.parent / "deployment.log").read_text())
            self.assertEqual("host configuration stays here\n", (app / ".env.production").read_text())
            self.assertEqual("host compose stays here\n", (app / "compose.yml").read_text())


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "GITHUB_SHA": SHA,
                    "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
                    "GITHUB_REPOSITORY": "example-org/ExampleApp", "TEST_ROOT": str(self.root)}

    def executable(self, name, source):
        path = self.bin / name
        path.write_text("#!/usr/bin/env python3\n" + source)
        path.chmod(0o755)

    def test_ci_gate_requires_latest_success_for_exact_main_commit(self):
        self.executable("gh", 'import os\nfrom pathlib import Path\nprint((Path(os.environ["TEST_ROOT"]) / "runs.json").read_text())\n')
        good = dict(id=1, head_sha=SHA, head_branch="main", event="push", status="completed", conclusion="success")
        cases = [([good], True), ([], False),
                 ([{**good, "head_sha": "c"*40}], False),
                 ([{**good, "event": "pull_request"}], False),
                 ([good, {**good, "id": 2, "conclusion": "failure"}], False),
                 ([good, {**good, "id": 2, "status": "in_progress", "conclusion": None}], False)]
        for runs, accepted in cases:
            with self.subTest(runs=runs):
                (self.root / "runs.json").write_text(json.dumps({"workflow_runs": runs}))
                result = subprocess.run(["bash", str(ROOT / "deploy/check-release-ci.sh")], env=self.env, capture_output=True)
                self.assertEqual(accepted, result.returncode == 0, result.stderr)
        for key, value in (("GITHUB_EVENT_NAME", "push"), ("GITHUB_REF", "refs/heads/feature")):
            result = subprocess.run(["bash", str(ROOT / "deploy/check-release-ci.sh")],
                                    env={**self.env, key: value}, capture_output=True)
            self.assertNotEqual(0, result.returncode)

    def test_transport_pins_host_key_sends_only_tools_and_propagates_ssh_failure(self):
        key = self.root / "ephemeral-test-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        public = key.with_suffix(".pub").read_text().split()
        self.env.update(IMAGE_REFERENCE=IMAGE, GITHUB_RUN_ID="42", GITHUB_RUN_ATTEMPT="1",
                        PRODUCTION_SSH_HOST="100.100.100.100", PRODUCTION_SSH_USER="deploy",
                        PRODUCTION_SSH_PRIVATE_KEY=key.read_text(),
                        PRODUCTION_SSH_KNOWN_HOSTS=f"app-prod {public[0]} {public[1]}")
        self.executable("ssh", '''import json, os, sys
from pathlib import Path
root = Path(os.environ["TEST_ROOT"])
(root / "bundle.tar").write_bytes(sys.stdin.buffer.read())
(root / "ssh.json").write_text(json.dumps(sys.argv[1:]))
assert "PRODUCTION_SSH_PRIVATE_KEY" not in os.environ
sys.exit(int(os.environ.get("SSH_RESULT", "0")))
''')
        for status in (0, 17):
            result = subprocess.run(["bash", str(ROOT / "deploy/invoke-production-release.sh")],
                                    env={**self.env, "SSH_RESULT": str(status)}, capture_output=True)
            self.assertEqual(status, result.returncode, result.stderr)
            self.assertNotIn(key.read_bytes(), result.stdout + result.stderr)
            args = json.loads((self.root / "ssh.json").read_text())
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertIn("HostKeyAlias=app-prod", args)
            self.assertIn("IdentitiesOnly=yes", args)
            self.assertIn(f"deploy-production {SHA} 42-1 {IMAGE}", args)
            self.assertFalse(Path(args[args.index("-i") + 1]).exists())  # private key cleaned up
            with (self.root / "bundle.tar").open("rb") as stream:
                self.assertEqual(receiver.FILES, set(receiver.read_bundle(stream)))
        result = subprocess.run(["bash", str(ROOT / "deploy/invoke-production-release.sh")],
                                env={**self.env, "IMAGE_REFERENCE": "ghcr.io/example-org/exampleapp:latest"}, capture_output=True)
        self.assertNotEqual(0, result.returncode)


if __name__ == "__main__":
    unittest.main()
