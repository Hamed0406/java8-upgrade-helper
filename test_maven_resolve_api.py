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


def post_json(url, payload):
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


class MavenResolveApiTest(unittest.TestCase):
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
        while time.time() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{cls.port}/index.html", timeout=2):
                    return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("server.py did not start in time")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait(timeout=5)

    def test_resolves_parent_dependency_management_version(self):
        parent_pom = """<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>acme</groupId>
  <artifactId>root</artifactId>
  <version>1.0</version>
  <properties><junit.version>4.13.2</junit.version></properties>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>junit</groupId><artifactId>junit</artifactId><version>${junit.version}</version>
      </dependency>
    </dependencies>
  </dependencyManagement>
</project>"""
        child_pom = """<project>
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>acme</groupId><artifactId>root</artifactId><version>1.0</version><relativePath>../pom.xml</relativePath>
  </parent>
  <artifactId>child</artifactId>
  <dependencies>
    <dependency><groupId>junit</groupId><artifactId>junit</artifactId></dependency>
  </dependencies>
</project>"""
        payload = {
            "poms": [
                {"name": "root/pom.xml", "content": parent_pom},
                {"name": "root/child/pom.xml", "content": child_pom},
            ],
            "gradles": [],
        }
        data = post_json(f"http://127.0.0.1:{self.port}/api/maven-resolve", payload)
        deps = {(d["groupId"], d["artifactId"]): d for d in data.get("dependencies", [])}
        self.assertIn(("junit", "junit"), deps)
        self.assertEqual(deps[("junit", "junit")]["version"], "4.13.2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
