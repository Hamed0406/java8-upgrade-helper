import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class ScanCliTest(unittest.TestCase):
    def test_headless_scan_generates_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "A.java").write_text(
                "import sun.misc.Unsafe;\nclass A { void finalize(){} }\n",
                encoding="utf-8",
            )
            (root / "pom.xml").write_text(
                "<project><properties><java.version>8</java.version><spring-boot.version>2.7.18</spring-boot.version></properties></project>",
                encoding="utf-8",
            )
            out = root / "report.json"
            subprocess.run(
                [
                    "python3",
                    "scan.py",
                    "--path",
                    str(root),
                    "--target",
                    "17",
                    "--skip-deps",
                    "--out",
                    str(out),
                ],
                cwd="/opt/java-upgreaer",
                check=True,
                capture_output=True,
                text=True,
            )
            data = json.loads(out.read_text(encoding="utf-8"))
            titles = [f.get("title", "") for f in data.get("findings", [])]
            self.assertIn("Internal JDK API usage (sun.* / com.sun.*)", titles)
            self.assertTrue(any("Build is pinned to Java 8" in t for t in titles))


if __name__ == "__main__":
    unittest.main()
