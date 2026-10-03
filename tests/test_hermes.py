"""Tests of the Hermes collector: prometheus lines from state.db, quota parsing, token guard."""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

sys.dont_write_bytecode = True
RACINE = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("hermes", RACINE / "collecteurs" / "hermes.py")
hermes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hermes)

SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT);
CREATE TABLE session_model_usage (
    session_id TEXT, model TEXT, task TEXT,
    api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,
    cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
    last_seen REAL
);
"""


def faire_jwt(charge: dict) -> str:
    """Build an unsigned JWT carrying `charge`."""
    corps = base64.urlsafe_b64encode(json.dumps(charge).encode()).decode().rstrip("=")
    return f"e30.{corps}.signature"


def ecrire_auth(chemin: pathlib.Path, exp: float) -> None:
    """Write a minimal Hermes auth.json whose access token expires at `exp` (epoch s)."""
    acces = faire_jwt({"exp": exp, "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1"}})
    chemin.write_text(
        json.dumps({"providers": {"openai-codex": {"tokens": {"access_token": acces, "refresh_token": "r"}}}}),
        encoding="utf-8",
    )


class Reponse:
    """Minimal urlopen response usable as a context manager."""

    def __init__(self, payload: dict, status: int = 200) -> None:
        self._flux = io.BytesIO(json.dumps(payload).encode())
        self.status = status

    def read(self, *args):
        return self._flux.read(*args)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


PAYLOAD = {
    "rate_limit": {
        "limit_reached": False,
        "primary_window": {"used_percent": 42, "reset_at": 1759200000.4},
        "secondary_window": {"used_percent": 7.5, "reset_at": 1759700000},
    },
    "additional_rate_limits": [
        {"limit_name": "gpt-5-codex", "rate_limit": {"primary_window": {"used_percent": 3}}},
        {"limit_name": None, "rate_limit": {"primary_window": {"used_percent": 99}}},
    ],
}


class Base(unittest.TestCase):
    """Fake HOME with a state.db and auth.json; module paths redirected there."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.maison = pathlib.Path(self._tmp.name)
        (self.maison / ".hermes").mkdir()
        self.base = self.maison / ".hermes/state.db"
        self.auth = self.maison / ".hermes/auth.json"
        self.maintenant = time.time()
        connexion = sqlite3.connect(self.base)
        connexion.executescript(SCHEMA)
        connexion.executemany(
            "INSERT INTO sessions VALUES (?, ?)", [("s-recente", "slack"), ("s-vieille", "cli")]
        )
        recent = self.maintenant - 3600
        self.recent = recent
        connexion.executemany(
            "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("s-recente", "gpt-5", None, 2, 100, 20, 50, 5, 7, recent - 10),
                ("s-recente", "gpt-5", None, 1, 1, 2, 3, 4, 5, recent),
                ("s-recente", "gpt-5", "resume", 1, 9, 9, 9, 9, 9, recent),
                ("s-orpheline", None, "", 1, 1, 1, 1, 1, 1, recent),
                ("s-vieille", "gpt-5", None, 4, 10, 10, 10, 10, 10, self.maintenant - 3 * 86400),
            ],
        )
        connexion.commit()
        connexion.close()
        ecrire_auth(self.auth, self.maintenant + 3600)
        self._patches = [
            mock.patch.object(hermes, "BASE_HERMES", self.base),
            mock.patch.object(hermes, "IDENTIFIANTS", self.auth),
            mock.patch.object(hermes, "REGLAGES", self.maison / ".claude/settings.json"),
            mock.patch.dict(os.environ, {"HOME": str(self.maison)}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def lancer(self, *arguments: str, urlopen=None) -> tuple[int, list[str]]:
        """Run main() with argv, return (exit code, stdout lines)."""
        sortie = io.StringIO()
        ouvrir = urlopen or mock.Mock(side_effect=AssertionError("network not expected"))
        with mock.patch.object(sys, "argv", ["hermes.py", *arguments]), \
                mock.patch.object(hermes.urllib.request, "urlopen", ouvrir), \
                contextlib.redirect_stdout(sortie), contextlib.redirect_stderr(io.StringIO()):
            code = hermes.main()
        return code, sortie.getvalue().splitlines()


class LignesConso(Base):
    def test_emits_five_token_types_and_api_calls_per_session_model_task(self):
        code, lignes = self.lancer("--sec", "--rattrapage")
        self.assertEqual(code, 0)
        attendu = int(self.recent * 1000)
        pref = 'session="s-recente",model="gpt-5",source="slack",task="travail"'
        self.assertIn(f"hermes_tokens{{{pref},type=\"input\"}} 101 {attendu}", lignes)
        self.assertIn(f"hermes_tokens{{{pref},type=\"output\"}} 22 {attendu}", lignes)
        self.assertIn(f"hermes_tokens{{{pref},type=\"cacheRead\"}} 53 {attendu}", lignes)
        self.assertIn(f"hermes_tokens{{{pref},type=\"cacheCreation\"}} 9 {attendu}", lignes)
        self.assertIn(f"hermes_tokens{{{pref},type=\"reasoning\"}} 12 {attendu}", lignes)
        self.assertIn(f"hermes_api_calls{{{pref}}} 3 {attendu}", lignes)

    def test_null_or_empty_task_becomes_travail_and_named_task_is_kept(self):
        _, lignes = self.lancer("--sec", "--rattrapage")
        self.assertTrue(any('session="s-recente",model="gpt-5",source="slack",task="resume"' in l for l in lignes))
        self.assertFalse(any('task=""' in l for l in lignes))

    def test_session_missing_from_sessions_table_gets_source_inconnue_and_model_inconnu(self):
        _, lignes = self.lancer("--sec", "--rattrapage")
        self.assertIn(
            f'hermes_api_calls{{session="s-orpheline",model="inconnu",source="inconnue",task="travail"}} 1 '
            f"{int(self.recent * 1000)}",
            lignes,
        )

    def test_default_mode_keeps_only_sessions_seen_in_last_24h(self):
        _, lignes = self.lancer("--sec", urlopen=mock.Mock(return_value=Reponse(PAYLOAD)))
        self.assertTrue(any('session="s-recente"' in l for l in lignes))
        self.assertFalse(any('session="s-vieille"' in l for l in lignes))
        self.assertIn("hermes_conso_collecte_ok 1", lignes)

    def test_rattrapage_includes_old_sessions_and_skips_quota(self):
        ouvrir = mock.Mock(side_effect=AssertionError("quota must not be fetched"))
        _, lignes = self.lancer("--sec", "--rattrapage", urlopen=ouvrir)
        self.assertTrue(any('session="s-vieille"' in l and 'source="cli"' in l for l in lignes))
        self.assertFalse(any(l.startswith(("hermes_quota", "hermes_jeton")) for l in lignes))
        ouvrir.assert_not_called()

    def test_label_values_are_escaped(self):
        self.assertEqual(hermes.etiquettes(a='x"y\\z\nw'), 'a="x\\"y\\\\z\\nw"')

    def test_unreadable_base_reports_conso_ko_and_exits_zero(self):
        self.base.unlink()
        code, lignes = self.lancer("--sec", "--rattrapage")
        self.assertEqual(code, 0)
        self.assertEqual(lignes, ["hermes_conso_collecte_ok 0"])


class Quota(Base):
    def test_parses_5h_weekly_and_named_additional_windows(self):
        lignes = hermes.lignes_quota(PAYLOAD)
        self.assertEqual(
            lignes,
            [
                'hermes_quota_percent{fenetre="5h"} 42',
                'hermes_quota_resets_at{fenetre="5h"} 1759200000',
                'hermes_quota_percent{fenetre="weekly"} 7.5',
                'hermes_quota_resets_at{fenetre="weekly"} 1759700000',
                'hermes_quota_percent{fenetre="gpt-5-codex"} 3',
                "hermes_quota_limit_reached 0",
            ],
        )

    def test_limit_reached_and_missing_windows(self):
        lignes = hermes.lignes_quota({"rate_limit": {"limit_reached": True, "primary_window": None}})
        self.assertEqual(lignes, ["hermes_quota_limit_reached 1"])

    def test_payload_without_rate_limit_raises(self):
        with self.assertRaises(RuntimeError):
            hermes.lignes_quota({"autre": 1})

    def test_valid_token_queries_usage_with_account_header_and_reports_ok(self):
        ouvrir = mock.Mock(return_value=Reponse(PAYLOAD))
        code, lignes = self.lancer("--sec", urlopen=ouvrir)
        self.assertEqual(code, 0)
        requete = ouvrir.call_args.args[0]
        self.assertEqual(requete.full_url, hermes.USAGE)
        self.assertEqual(requete.get_header("Chatgpt-account-id"), "acct-1")
        self.assertIn("hermes_quota_collecte_ok 1", lignes)
        self.assertIn('hermes_quota_percent{fenetre="5h"} 42', lignes)
        self.assertTrue(any(l.startswith("hermes_jeton_expire_dans 3") for l in lignes))

    def test_missing_auth_json_reports_ko_without_network(self):
        self.auth.unlink()
        ouvrir = mock.Mock(return_value=Reponse(PAYLOAD))
        code, lignes = self.lancer("--sec", urlopen=ouvrir)
        ouvrir.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(lignes[-1], "hermes_quota_collecte_ok 0")
        self.assertFalse(any(l.startswith("hermes_jeton") for l in lignes))

    def test_expired_token_reports_ko_without_network_nor_refresh(self):
        ecrire_auth(self.auth, self.maintenant - 60)
        avant = self.auth.read_bytes()
        ouvrir = mock.Mock(return_value=Reponse(PAYLOAD))
        code, lignes = self.lancer("--sec", urlopen=ouvrir)
        ouvrir.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(lignes[-1], "hermes_quota_collecte_ok 0")
        self.assertTrue(any(l.startswith("hermes_jeton_expire_dans -") for l in lignes))
        self.assertEqual(self.auth.read_bytes(), avant)

    def test_http_refusal_reports_ko_and_exits_zero(self):
        erreur = urllib.error.HTTPError(hermes.USAGE, 401, "Unauthorized", {}, None)
        code, lignes = self.lancer("--sec", urlopen=mock.Mock(side_effect=erreur))
        self.assertEqual(code, 0)
        self.assertEqual(lignes[-1], "hermes_quota_collecte_ok 0")
        self.assertFalse(any(l.startswith("hermes_quota_percent") for l in lignes))

    def test_malformed_payloads_report_ko_and_exit_zero(self):
        malformes = [
            {"rate_limit": {"primary_window": [1]}},
            {"rate_limit": [1]},
            {"rate_limit": {"primary_window": {"used_percent": 1}}, "additional_rate_limits": {"a": 1}},
            {"rate_limit": {"primary_window": {"used_percent": 1}}, "additional_rate_limits": 5},
        ]
        for payload in malformes:
            with self.subTest(payload=payload):
                code, lignes = self.lancer("--sec", urlopen=mock.Mock(return_value=Reponse(payload)))
                self.assertEqual(code, 0)
                self.assertIn("hermes_quota_collecte_ok 0", lignes)
                self.assertNotIn("hermes_quota_collecte_ok 1", lignes)

    def test_unexpected_error_anywhere_exits_zero(self):
        with mock.patch.object(hermes, "collecter_quota", side_effect=AttributeError("boom")):
            code, _ = self.lancer("--sec")
        self.assertEqual(code, 0)


class AucuneEcriture(Base):
    def test_run_leaves_home_untouched(self):
        def etat():
            return {
                str(p.relative_to(self.maison)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.maison.rglob("*")
                if p.is_file()
            }

        avant = etat()
        self.lancer("--sec", urlopen=mock.Mock(return_value=Reponse(PAYLOAD)))
        self.lancer("--sec", "--rattrapage")
        self.assertEqual(etat(), avant)

    def test_base_is_opened_read_only(self):
        with mock.patch.object(hermes, "BASE_HERMES", self.maison / "absente.db"):
            with self.assertRaises(sqlite3.OperationalError):
                hermes.sessions(None)
        self.assertFalse((self.maison / "absente.db").exists())


if __name__ == "__main__":
    unittest.main()
