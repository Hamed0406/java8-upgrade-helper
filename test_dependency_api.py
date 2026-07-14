#!/usr/bin/env python3
import json
import os
import socket
import subprocess
import sys
import time
import unittest
from urllib.request import Request, urlopen


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_post_json(url, payload):
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


class DependencyApiIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        env = os.environ.copy()
        env["PORT"] = str(cls.port)
        cls.proc = subprocess.Popen(
            [sys.executable, "server.py"],
            cwd=os.path.dirname(__file__),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 10
        last_err = None
        while time.time() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{cls.port}/index.html", timeout=2):
                    return
            except Exception as err:
                last_err = err
                time.sleep(0.2)
        raise RuntimeError(f"server.py did not start in time: {last_err}")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait(timeout=5)

    def test_resolves_from_maven_central(self):
        payload = {
            "targetJava": 17,
            "dependencies": [{"groupId": "junit", "artifactId": "junit", "version": "4.12"}],
        }
        data = http_post_json(f"http://127.0.0.1:{self.port}/api/dependency-check", payload)
        self.assertGreaterEqual(data.get("checked", 0), 1)
        results = {r["ga"]: r for r in data.get("results", [])}
        self.assertIn("junit:junit", results)
        junit = results["junit:junit"]
        self.assertEqual(junit.get("status"), "ok")
        self.assertTrue(junit.get("latestVersion"))
        self.assertIn("repo1.maven.org", junit.get("source", ""))

    def test_unresolved_dependency_returns_unresolved(self):
        payload = {
            "targetJava": 17,
            "dependencies": [{"groupId": "no.such.group", "artifactId": "no-such-artifact", "version": "1.0"}],
        }
        data = http_post_json(f"http://127.0.0.1:{self.port}/api/dependency-check", payload)
        results = {r["ga"]: r for r in data.get("results", [])}
        self.assertIn("no.such.group:no-such-artifact", results)
        item = results["no.such.group:no-such-artifact"]
        self.assertEqual(item.get("status"), "unresolved")


if __name__ == "__main__":
    unittest.main(verbosity=2)
