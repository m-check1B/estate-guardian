#!/usr/bin/env python3
"""Every remote probe embeds Python in a shell heredoc. A quoting slip there fails only on the remote host, so the
guard sees "no data" instead of a crash (11 Oct: a refactor broke PROD_PM2_PROBE and hid three matrix rows).
This test parses every embedded `python3 - <<'PY'` body and shell-checks the rest. Run: python3 -m unittest test_probes"""
import ast, os, re, subprocess, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "guardian.py")).read()
PROBES = dict(re.findall(r"^([A-Z0-9_]+_PROBE) = r'''(.*?)'''", SRC, re.S | re.M))


class Probes(unittest.TestCase):
    def test_found(self):
        self.assertGreaterEqual(len(PROBES), 3, list(PROBES))

    def test_embedded_python_parses(self):
        for name, body in PROBES.items():
            for i, py in enumerate(re.findall(r"python3 - <<'PY'\n(.*?)\nPY\b", body, re.S)):
                with self.subTest(probe=name, block=i):
                    ast.parse(py)

    def test_shell_parses(self):
        for name, body in PROBES.items():
            with self.subTest(probe=name):
                r = subprocess.run(["sh", "-n"], input=body, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
