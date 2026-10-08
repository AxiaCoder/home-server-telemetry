"""Runs the vmalert-tool unit tests of alertes.yml; skipped when vmalert-tool is not installed."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import unittest

sys.dont_write_bytecode = True
RACINE = pathlib.Path(__file__).resolve().parent.parent
TESTS_REGLES = RACINE / "tests" / "regles" / "alertes_test.yml"


def trouver_vmalert_tool() -> str | None:
    """Return the vmalert-tool binary from VMALERT_TOOL, else from PATH, else None."""
    explicite = os.environ.get("VMALERT_TOOL")
    if explicite:
        return explicite
    return shutil.which("vmalert-tool") or shutil.which("vmalert-tool-prod")


@unittest.skipUnless(trouver_vmalert_tool(), "vmalert-tool absent (PATH or VMALERT_TOOL)")
class TestRegles(unittest.TestCase):
    def test_regles_d_alerte_passent_leurs_tests_vmalert(self) -> None:
        sortie = subprocess.run(
            [trouver_vmalert_tool(), "unittest", f"--files={TESTS_REGLES}"],
            cwd=RACINE, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(sortie.returncode, 0, sortie.stdout + sortie.stderr)


if __name__ == "__main__":
    unittest.main()
