"""The cluster scripts spend money, so their guards are tested with STUB doctl and kubectl
binaries that only record how they were called. No cloud API is contacted."""
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest

from ._util import REPO

SCRIPTS = os.path.join(REPO, "serving", "cluster")

STUB_DOCTL = textwrap.dedent("""\
    #!/usr/bin/env bash
    echo "doctl $*" >> "$STUB_LOG"
    case "$*" in
      "kubernetes cluster list"*) [ -f "$STUB_DIR/cluster-exists" ] && echo spot-drill-serving ;;
      "compute size list --format"*) printf 'gpu-test-spot 9.99\\ncpu-test 0.11\\n' ;;
      "kubernetes cluster create"*) touch "$STUB_DIR/cluster-exists" ;;
      "kubernetes cluster get"*) echo cid-123 ;;
      "kubernetes cluster delete"*) rm -f "$STUB_DIR/cluster-exists" ;;
    esac
    exit 0
    """)

STUB_KUBECTL = textwrap.dedent("""\
    #!/usr/bin/env bash
    echo "kubectl $*" >> "$STUB_LOG"
    case "$*" in
      *"--no-headers"*) echo "gpu-node-1 Ready <none> 1m v1" ;;
      *"allocatable"*) echo 1 ;;
      "config current-context") echo "${STUB_CONTEXT:-do-xyz-spot-drill-serving}" ;;
    esac
    exit 0
    """)


class Scripts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.bin = os.path.join(d, "bin")
        os.mkdir(self.bin)
        for name, body in (("doctl", STUB_DOCTL), ("kubectl", STUB_KUBECTL)):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self.log = os.path.join(d, "calls.log")
        open(self.log, "w").close()
        self.envfile = os.path.join(d, "env")
        self.write_env("DO_TOKEN=stub-not-a-token\nREGION=tst1\nSPOT_SIZE_SLUG=gpu-test-spot\n"
                       "DRILL_TAG=spot-drill\nSERVING_CPU_SIZE=cpu-test\n")
        self.env = {**os.environ, "PATH": self.bin + os.pathsep + os.environ["PATH"],
                    "STUB_LOG": self.log, "STUB_DIR": d, "SERVING_ENV_FILE": self.envfile,
                    "SERVING_STATE_DIR": os.path.join(d, "state"), "SERVING_CONFIRM_SLEEP": "0"}

    def tearDown(self):
        self.tmp.cleanup()

    def write_env(self, text):
        with open(self.envfile, "w") as f:
            f.write(text)

    def run_script(self, name, *args, **env):
        return subprocess.run(["bash", os.path.join(SCRIPTS, name), *args], capture_output=True,
                              text=True, env={**self.env, **env}, timeout=60)

    def calls(self):
        return self.read(self.log).splitlines()

    @staticmethod
    def read(path):
        with open(path) as f:
            return f.read()

    def test_create_refuses_without_confirm(self):
        r = self.run_script("01_create_cluster.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Refusing to create billable cloud resources without --confirm", r.stdout)
        self.assertFalse(any("create" in c for c in self.calls()))

    def test_create_refuses_without_cpu_size(self):
        self.write_env("DO_TOKEN=stub-not-a-token\nREGION=tst1\nSPOT_SIZE_SLUG=gpu-test-spot\n")
        r = self.run_script("01_create_cluster.sh", "--confirm")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("SERVING_CPU_SIZE", r.stderr)
        self.assertFalse(any("create" in c for c in self.calls()))

    def test_create_refuses_duplicate(self):
        open(os.path.join(self.tmp.name, "cluster-exists"), "w").close()
        r = self.run_script("01_create_cluster.sh", "--confirm")
        self.assertEqual(r.returncode, 1)
        self.assertIn("already exists", r.stdout)
        self.assertFalse(any("cluster create" in c for c in self.calls()))

    def test_create_command_and_recorded_price(self):
        r = self.run_script("01_create_cluster.sh", "--confirm")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("!!! COST: up to 2 x gpu-test-spot at ~$9.99/hr", r.stdout)
        create = [c for c in self.calls() if "kubernetes cluster create" in c]
        self.assertEqual(len(create), 1)
        c = create[0]
        self.assertIn("spot-drill-serving --region tst1 --version latest", c)
        self.assertIn("--ha=false", c)
        self.assertIn("--enable-amd-gpu-device-plugin=true", c)
        self.assertIn("--tag spot-drill-serving", c)
        self.assertIn("name=cpu;size=cpu-test;count=1", c)
        # the autoscaling range is fixed at creation: a spot pool's max cannot be raised later
        self.assertIn("name=gpu-spot;size=gpu-test-spot;count=1;auto-scale=true;min-nodes=1;max-nodes=2", c)
        state = self.env["SERVING_STATE_DIR"]
        self.assertEqual(self.read(os.path.join(state, "serving-gpu-price-hourly")).strip(), "9.99")
        self.assertEqual(self.read(os.path.join(state, "serving-cpu-price-hourly")).strip(), "0.11")
        self.assertIn('"event":"serving_cluster_created"',
                      self.read(os.path.join(state, "serving-events.jsonl")))

    def test_destroy_refuses_without_confirm(self):
        r = self.run_script("03_destroy_cluster.sh")
        self.assertEqual(r.returncode, 1)
        self.assertFalse(any("delete" in c for c in self.calls()))

    def test_destroy_deletes_then_checks_for_leftovers(self):
        open(os.path.join(self.tmp.name, "cluster-exists"), "w").close()
        os.mkdir(self.env["SERVING_STATE_DIR"])
        r = self.run_script("03_destroy_cluster.sh", "--confirm")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.calls()
        delete = [i for i, c in enumerate(calls) if "kubernetes cluster delete" in c]
        self.assertEqual(len(delete), 1)
        self.assertIn("spot-drill-serving --dangerous --force", calls[delete[0]])
        after = calls[delete[0] + 1:]
        self.assertTrue(any("kubernetes cluster list" in c for c in after))
        self.assertTrue(any("droplet list --tag-name k8s:cid-123" in c for c in after))
        self.assertIn("are gone", r.stdout)

    def test_install_refuses_wrong_kubectl_context(self):
        r = self.run_script("02_install.sh", STUB_CONTEXT="production-cluster")
        self.assertEqual(r.returncode, 1)
        self.assertIn("REFUSING", r.stdout)
        self.assertFalse(any("apply" in c for c in self.calls()))


if __name__ == "__main__":
    unittest.main()
