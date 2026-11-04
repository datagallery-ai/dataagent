"""CLI restart/failed-turn allocation must never overwrite Context run files."""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dataagent.interface.cli.session_runs import reserve_run_id


class CLISessionRunTests(unittest.TestCase):
    def test_restart_bootstraps_existing_dumps_and_reserves_failed_turns(self):
        with TemporaryDirectory() as tmp:
            config = {"WORKSPACE": {"path": tmp}}
            context = Path(tmp) / ".context"
            context.mkdir()
            (context / "Run7_Sub0.json").write_text('{}')
            self.assertEqual(reserve_run_id(config, user_id="u", session_id="s"), 8)
            # No Run8 dump: a failed turn still keeps its reservation.
            self.assertEqual(reserve_run_id(config, user_id="u", session_id="s"), 9)
            program = '''
import json, sys
from dataagent.interface.cli.session_runs import reserve_run_id
print(reserve_run_id(json.loads(sys.argv[1]), user_id="u", session_id="s"))
'''
            result = subprocess.run([sys.executable, "-c", program, json.dumps(config)],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "10")

    def test_corrupt_counter_fails_instead_of_resetting_to_zero(self):
        with TemporaryDirectory() as tmp:
            memory = Path(tmp) / ".memory"
            memory.mkdir()
            (memory / "cli_runs.json").write_text('{broken')
            with self.assertRaises(ValueError):
                reserve_run_id({"WORKSPACE": {"path": tmp}}, user_id="u", session_id="s")


if __name__ == "__main__":
    unittest.main()
