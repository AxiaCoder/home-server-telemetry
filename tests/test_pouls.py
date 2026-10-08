"""Tests of the `pouls` script, run as a subprocess against a local fake VictoriaMetrics."""

from __future__ import annotations

import http.server
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.dont_write_bytecode = True
RACINE = pathlib.Path(__file__).resolve().parent.parent
POULS = RACINE / "pouls"
CHEMIN_IMPORT = "/api/v1/import/prometheus"


class FauxVictoria:
    """Local HTTP server recording each POST (raw request path, body) and answering a fixed status."""

    def __init__(self, statut: int = 204) -> None:
        self.recus: list[tuple[str, str]] = []
        faux = self

        class Gestionnaire(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                longueur = int(self.headers.get("Content-Length", 0))
                chemin_brut = self.requestline.split(" ")[1]
                faux.recus.append((chemin_brut, self.rfile.read(longueur).decode()))
                self.send_response(statut)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args) -> None:
                pass

        self.serveur = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Gestionnaire)
        self.base = f"http://127.0.0.1:{self.serveur.server_address[1]}"
        threading.Thread(target=self.serveur.serve_forever, daemon=True).start()

    def fermer(self) -> None:
        """Stop the server and release its port."""
        self.serveur.shutdown()
        self.serveur.server_close()


def port_ferme() -> str:
    """Return a base URL on a local port nobody listens on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


class TestPouls(unittest.TestCase):
    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self._home.name)
        self.serveurs: list[FauxVictoria] = []

    def tearDown(self) -> None:
        for serveur in self.serveurs:
            serveur.fermer()
        self._home.cleanup()

    def faux(self, statut: int = 204) -> FauxVictoria:
        serveur = FauxVictoria(statut)
        self.serveurs.append(serveur)
        return serveur

    def regler(self, *bases: str) -> None:
        """Write a settings.json whose env block lists `bases` as TELEMETRIE_ENDPOINTS."""
        dossier = self.home / ".claude"
        dossier.mkdir(exist_ok=True)
        reglages = {"env": {"TELEMETRIE_ENDPOINTS": ",".join(bases)}}
        (dossier / "settings.json").write_text(json.dumps(reglages, indent=2))

    def lancer(self, *args: str) -> subprocess.CompletedProcess:
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        return subprocess.run(
            ["sh", str(POULS), *args], env=env, capture_output=True, text=True, timeout=30
        )

    def test_pousse_le_succes_et_un_delai_par_canal(self) -> None:
        serveur = self.faux()
        self.regler(serveur.base)
        avant = int(time.time())
        sortie = self.lancer("sauvegarde", "26h", "alertes", "1d", "urgent")
        apres = int(time.time())

        self.assertEqual(sortie.returncode, 0)
        self.assertEqual(sortie.stderr, "")
        self.assertEqual(len(serveur.recus), 1)
        chemin, corps = serveur.recus[0]
        self.assertEqual(chemin, CHEMIN_IMPORT)
        lignes = corps.splitlines()
        self.assertEqual(len(lignes), 3)
        nom, valeur = lignes[0].rsplit(" ", 1)
        self.assertEqual(nom, 'tache_dernier_succes{tache="sauvegarde"}')
        self.assertTrue(avant <= int(valeur) <= apres)
        self.assertEqual(
            lignes[1:],
            [
                'tache_delai_max_secondes{tache="sauvegarde",canal="alertes"} 93600',
                'tache_delai_max_secondes{tache="sauvegarde",canal="urgent"} 86400',
            ],
        )

    def test_convertit_chaque_unite_en_secondes(self) -> None:
        serveur = self.faux()
        self.regler(serveur.base)
        cas = {"45s": 45, "90m": 5400, "26h": 93600, "1d": 86400, "2d": 172800, "090m": 5400}
        for delai, attendu in cas.items():
            with self.subTest(delai=delai):
                serveur.recus.clear()
                self.assertEqual(self.lancer("t", delai, "alertes").returncode, 0)
                ligne = serveur.recus[0][1].splitlines()[1]
                self.assertEqual(ligne, f'tache_delai_max_secondes{{tache="t",canal="alertes"}} {attendu}')

    def test_retire_la_barre_finale_et_les_espaces_des_bases(self) -> None:
        serveur = self.faux()
        self.regler(f" {serveur.base}/ ")
        self.assertEqual(self.lancer("t", "1h", "alertes").returncode, 0)
        self.assertEqual([chemin for chemin, _ in serveur.recus], [CHEMIN_IMPORT])

    def test_se_replie_sur_la_deuxieme_base_si_la_premiere_est_fermee(self) -> None:
        serveur = self.faux()
        self.regler(port_ferme(), serveur.base)
        sortie = self.lancer("t", "1h", "alertes")
        self.assertEqual(sortie.returncode, 0)
        self.assertEqual(sortie.stderr, "")
        self.assertEqual(len(serveur.recus), 1)

    def test_se_replie_sur_la_deuxieme_base_si_la_premiere_repond_une_erreur(self) -> None:
        en_panne, serveur = self.faux(503), self.faux()
        self.regler(en_panne.base, serveur.base)
        self.assertEqual(self.lancer("t", "1h", "alertes").returncode, 0)
        self.assertEqual(len(en_panne.recus), 1)
        self.assertEqual(len(serveur.recus), 1)

    def test_s_arrete_a_la_premiere_base_qui_accepte(self) -> None:
        premier, second = self.faux(), self.faux()
        self.regler(premier.base, second.base)
        self.assertEqual(self.lancer("t", "1h", "alertes").returncode, 0)
        self.assertEqual(len(premier.recus), 1)
        self.assertEqual(second.recus, [])

    def test_sort_en_zero_quand_toutes_les_bases_sont_injoignables(self) -> None:
        en_panne = self.faux(500)
        self.regler(port_ferme(), en_panne.base)
        sortie = self.lancer("sauvegarde", "1h", "alertes")
        self.assertEqual(sortie.returncode, 0)
        self.assertIn("base injoignable, pouls de sauvegarde perdu", sortie.stderr)

    def test_arguments_invalides_ne_poussent_rien_et_sortent_en_zero(self) -> None:
        serveur = self.faux()
        self.regler(serveur.base)
        cas = {
            "trop peu d'arguments": (["t", "1h"], "usage"),
            "delai sans canal": (["t", "1h", "alertes", "2h"], "chaque delai attend son canal"),
            "nom de tache avec un point": (["t.x", "1h", "alertes"], "nom de tache refuse"),
            "nom de tache avec une accolade": (['t"}', "1h", "alertes"], "nom de tache refuse"),
            "nom de tache vide": (["", "1h", "alertes"], "nom de tache refuse"),
            "nom de canal avec un espace": (["t", "1h", "a b"], "nom de canal refuse"),
            "delai nul": (["t", "0m", "alertes"], "delai illisible"),
            "delai nul a plusieurs zeros": (["t", "000h", "alertes"], "delai illisible"),
            "unite inconnue": (["t", "5x", "alertes"], "delai illisible"),
            "unite absente": (["t", "90", "alertes"], "delai illisible"),
            "nombre absent": (["t", "m", "alertes"], "delai illisible"),
            "nombre negatif": (["t", "-1h", "alertes"], "delai illisible"),
            "deux unites": (["t", "1hm", "alertes"], "delai illisible"),
            "second delai invalide": (["t", "1h", "alertes", "0d", "urgent"], "delai illisible"),
        }
        for libelle, (args, message) in cas.items():
            with self.subTest(libelle):
                sortie = self.lancer(*args)
                self.assertEqual(sortie.returncode, 0)
                self.assertIn(message, sortie.stderr)
        self.assertEqual(serveur.recus, [])


if __name__ == "__main__":
    unittest.main()
