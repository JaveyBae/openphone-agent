import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class SmsControlPolicyTest(unittest.TestCase):
    def test_exact_save_banner_close_and_rejected_neighbors(self):
        jdk = Path(os.environ.get("JAVA_HOME", "/Applications/Android Studio.app/Contents/jbr/Contents/Home"))
        javac = str(jdk / "bin/javac") if (jdk / "bin/javac").is_file() else shutil.which("javac")
        java = str(jdk / "bin/java") if (jdk / "bin/java").is_file() else shutil.which("java")
        if not javac or not java:
            self.skipTest("Java runtime is needed for the pure SMS policy regression")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run([javac, "-d", directory,
                            str(root / "tools/workspace/src/ai/openphone/workspace/SmsControlPolicy.java"),
                            str(root / "tests/java/SmsControlPolicyTest.java")], check=True, capture_output=True)
            subprocess.run([java, "-cp", directory, "SmsControlPolicyTest"], check=True, capture_output=True)
