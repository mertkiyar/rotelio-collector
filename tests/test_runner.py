import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cloud_runner import collect_process


class RunnerTests(unittest.TestCase):
    def test_source_collector_does_not_receive_repository_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            process = Mock()
            process.wait.return_value = 0
            with patch.dict(os.environ, {
                "DATA_VAULT_TOKEN": "unit-test-vault-placeholder",
                "GITHUB_TOKEN": "unit-test-checkout-placeholder",
            }), patch("cloud_runner.subprocess.Popen", return_value=process) as start:
                self.assertEqual(collect_process(1, data, data / "collection.log"), 0)
                child = start.call_args.kwargs["env"]
            self.assertNotIn("DATA_VAULT_TOKEN", child)
            self.assertNotIn("GITHUB_TOKEN", child)
            self.assertEqual(child["ROTELIO_DATA_DIR"], str(data))
            self.assertEqual(child["FLIGHT_BROWSER_CHANNEL"], "chromium")


if __name__ == "__main__":
    unittest.main()
