#!/usr/bin/env python3
import base64
import os
import unittest
from unittest.mock import patch

import server


class ServerAuthHeaderTest(unittest.TestCase):
    def test_no_auth_header_for_public_repo(self):
        with patch.dict(os.environ, {}, clear=False):
            headers = server.build_request_headers(
                "https://repo1.maven.org/maven2",
                ["https://nexus.example.com/repository/maven-public"],
            )
        self.assertEqual(headers.get("User-Agent"), "java-upgrade-helper/1.0")
        self.assertNotIn("Authorization", headers)

    def test_basic_auth_header_for_internal_repo(self):
        with patch.dict(
            os.environ,
            {"INTERNAL_MAVEN_USERNAME": "alice", "INTERNAL_MAVEN_PASSWORD": "secret"},
            clear=False,
        ):
            headers = server.build_request_headers(
                "https://nexus.example.com/repository/maven-public",
                ["https://nexus.example.com/repository/maven-public"],
            )
        expected = "Basic " + base64.b64encode(b"alice:secret").decode("ascii")
        self.assertEqual(headers.get("Authorization"), expected)

    def test_explicit_auth_header_wins(self):
        with patch.dict(
            os.environ,
            {
                "INTERNAL_MAVEN_AUTH_HEADER": "Bearer token-123",
                "INTERNAL_MAVEN_USERNAME": "alice",
                "INTERNAL_MAVEN_PASSWORD": "secret",
            },
            clear=False,
        ):
            headers = server.build_request_headers(
                "https://nexus.example.com/repository/maven-public",
                ["https://nexus.example.com/repository/maven-public"],
            )
        self.assertEqual(headers.get("Authorization"), "Bearer token-123")


if __name__ == "__main__":
    unittest.main(verbosity=2)
