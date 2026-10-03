#!/usr/bin/env python3
"""Pousse dans VictoriaMetrics la consommation d'Hermes et le quota du compte Codex.

Deux sources, lues sur la machine ou tourne Hermes :
- `~/.hermes/state.db`, ouverte en lecture seule : les tokens et appels par session ;
- https://chatgpt.com/backend-api/wham/usage, non documentee : le quota du compte.

Une serie par session, valeur = total de la ligne, horodatee a son `last_seen` :
Hermes supprime des sessions, un total recalcule redescendrait le jour d'une purge.

Modes :
- defaut (cron) : sessions vues dans les dernieres 24 h, plus le quota ;
- `--rattrapage` : toutes les sessions de la base, sans le quota ;
- `--sec` : imprime les lignes au lieu de les envoyer.

⛔ Le jeton n'est jamais renouvele ici : le renouvellement fait tourner le
refresh_token et deconnecterait Hermes. Jeton expire ou refuse = `collecte_ok 0`.

Sortie toujours 0, comme `quota.py`.
"""

from __future__ import annotations

import base64
import json
import pathlib
import sqlite3
import sys
import time
import urllib.error
import urllib.request

MAISON = pathlib.Path.home()
REGLAGES = MAISON / ".claude/settings.json"
BASE_HERMES = MAISON / ".hermes/state.db"
IDENTIFIANTS = MAISON / ".hermes/auth.json"
USAGE = "https://chatgpt.com/backend-api/wham/usage"
AGENT = "codex_cli_rs/0.50.0"
DESTINATION_PAR_DEFAUT = "http://localhost:8428"
FENETRE_RECENTE = 24 * 3600
DELAI_ENVOI = 5
DELAI_USAGE = 15
LOT_ENVOI = 5000

TYPES_TOKENS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_tokens": "cacheRead",
    "cache_write_tokens": "cacheCreation",
    "reasoning_tokens": "reasoning",
}
FENETRES_PRINCIPALES = {"primary_window": "5h", "secondary_window": "weekly"}


def destinations() -> list[str]:
    """Rend les URL d'import a essayer : reglages de la machine, sinon la base locale."""
    try:
        env = json.loads(REGLAGES.read_text(encoding="utf-8")).get("env", {})
    except (OSError, ValueError):
        env = {}
    bases = [b.strip().rstrip("/") for b in env.get("TELEMETRIE_ENDPOINTS", "").split(",") if b.strip()]
    if not bases:
        point = env.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "")
        bases = [point.split("/opentelemetry")[0].rstrip("/")] if point else [DESTINATION_PAR_DEFAUT]
    return [f"{b}/api/v1/import/prometheus" for b in bases]


def echapper(valeur: str) -> str:
    """Echappe une valeur d'etiquette Prometheus."""
    return str(valeur).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def etiquettes(**paires: str) -> str:
    """Rend le bloc d'etiquettes Prometheus des paires donnees, dans leur ordre."""
    return ",".join(f'{cle}="{echapper(valeur)}"' for cle, valeur in paires.items())


def sessions(depuis: float | None) -> list[sqlite3.Row]:
    """Rend l'usage par (session, modele, tache), vu depuis `depuis` (epoch s), ou tout si None.

    La base est ouverte en lecture seule : aucune ecriture possible dans `~/.hermes/`.
    """
    connexion = sqlite3.connect(f"file:{BASE_HERMES}?mode=ro", uri=True, timeout=5)
    connexion.row_factory = sqlite3.Row
    try:
        requete = """
            SELECT u.session_id, u.model, COALESCE(u.task, '') AS task,
                   COALESCE(s.source, 'inconnue') AS source,
                   SUM(u.api_call_count) AS api_call_count,
                   SUM(u.input_tokens) AS input_tokens,
                   SUM(u.output_tokens) AS output_tokens,
                   SUM(u.cache_read_tokens) AS cache_read_tokens,
                   SUM(u.cache_write_tokens) AS cache_write_tokens,
                   SUM(u.reasoning_tokens) AS reasoning_tokens,
                   MAX(u.last_seen) AS last_seen
            FROM session_model_usage u
            LEFT JOIN sessions s ON s.id = u.session_id
            GROUP BY u.session_id, u.model, COALESCE(u.task, '')
        """
        parametres: tuple = ()
        if depuis is not None:
            requete += " HAVING MAX(u.last_seen) >= ?"
            parametres = (depuis,)
        return connexion.execute(requete, parametres).fetchall()
    finally:
        connexion.close()


def lignes_conso(rangees: list[sqlite3.Row]) -> list[str]:
    """Rend les series de tokens et d'appels, horodatees au `last_seen` de chaque ligne (ms)."""
    sortie: list[str] = []
    for r in rangees:
        horodatage = int((r["last_seen"] or time.time()) * 1000)
        commun = dict(
            session=r["session_id"],
            model=r["model"] or "inconnu",
            source=r["source"],
            task=r["task"] or "travail",
        )
        for colonne, genre in TYPES_TOKENS.items():
            sortie.append(
                f"hermes_tokens{{{etiquettes(**commun, type=genre)}}} {int(r[colonne] or 0)} {horodatage}"
            )
        sortie.append(f"hermes_api_calls{{{etiquettes(**commun)}}} {int(r['api_call_count'] or 0)} {horodatage}")
    return sortie


def charge_jwt(jeton: str) -> dict:
    """Decode la charge utile d'un JWT, sans en verifier la signature."""
    partie = jeton.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(partie + "=" * (-len(partie) % 4)))


def jeton() -> tuple[str, str, int]:
    """Rend le jeton d'acces, l'identifiant de compte ChatGPT et les secondes avant expiration."""
    identifiants = json.loads(IDENTIFIANTS.read_text(encoding="utf-8"))
    acces = identifiants["providers"]["openai-codex"]["tokens"]["access_token"]
    charge = charge_jwt(acces)
    compte = charge["https://api.openai.com/auth"]["chatgpt_account_id"]
    return acces, compte, int(charge.get("exp", 0) - time.time())


def interroger(acces: str, compte: str) -> dict:
    """Rend le payload d'usage du compte Codex. Leve HTTPError sur un refus."""
    requete = urllib.request.Request(
        USAGE,
        headers={
            "Authorization": f"Bearer {acces}",
            "ChatGPT-Account-Id": compte,
            "Accept": "application/json",
            "User-Agent": AGENT,
        },
    )
    with urllib.request.urlopen(requete, timeout=DELAI_USAGE) as reponse:
        return json.load(reponse)


def lignes_fenetre(nom: str, fenetre: dict | None) -> list[str]:
    """Rend le pourcentage et la remise a zero (epoch s) d'une fenetre, si elle en porte."""
    if not isinstance(fenetre, dict) or not isinstance(fenetre.get("used_percent"), (int, float)):
        return []
    sortie = [f'hermes_quota_percent{{fenetre="{echapper(nom)}"}} {fenetre["used_percent"]}']
    if isinstance(fenetre.get("reset_at"), (int, float)):
        sortie.append(f'hermes_quota_resets_at{{fenetre="{echapper(nom)}"}} {fenetre["reset_at"]:.0f}')
    return sortie


def lignes_quota(payload: dict) -> list[str]:
    """Rend les series de quota, en ne lisant que les cles connues."""
    limite = payload.get("rate_limit")
    if not isinstance(limite, dict):
        raise RuntimeError(f"pas de rate_limit dans le payload — cles recues : {sorted(payload)[:8]}")
    sortie: list[str] = []
    for cle, nom in FENETRES_PRINCIPALES.items():
        sortie += lignes_fenetre(nom, limite.get(cle))
    for additionnelle in payload.get("additional_rate_limits") or []:
        nom = additionnelle.get("limit_name")
        if nom:
            sortie += lignes_fenetre(nom, (additionnelle.get("rate_limit") or {}).get("primary_window"))
    sortie.append(f"hermes_quota_limit_reached {1 if limite.get('limit_reached') else 0}")
    return sortie


def collecter_quota() -> tuple[list[str], str]:
    """Rend les series de quota et un resume ; `collecte_ok 0` sur toute erreur, sans renouveler."""
    try:
        acces, compte, reste = jeton()
    except Exception as erreur:  # noqa: BLE001 - auth.json absent ou illisible
        return ["hermes_quota_collecte_ok 0"], f"jeton illisible ({type(erreur).__name__})"
    expiration = f"hermes_jeton_expire_dans {reste}"
    if reste <= 0:
        return [expiration, "hermes_quota_collecte_ok 0"], "jeton expire"
    try:
        payload = interroger(acces, compte)
        corps = lignes_quota(payload)
        cinq_heures = ((payload.get("rate_limit") or {}).get("primary_window") or {}).get("used_percent")
    except urllib.error.HTTPError as erreur:
        return [expiration, "hermes_quota_collecte_ok 0"], f"usage refuse (HTTP {erreur.code})"
    except Exception as erreur:  # noqa: BLE001 - le cron ne doit jamais crier
        return [expiration, "hermes_quota_collecte_ok 0"], f"usage illisible ({erreur})"
    return corps + [expiration, "hermes_quota_collecte_ok 1"], f"5h a {cinq_heures} %"


def pousser(corps: list[str], urls: list[str]) -> bool:
    """Envoie les lignes par lots ; dit si chaque lot a ete accepte par l'une des adresses."""
    for debut in range(0, len(corps), LOT_ENVOI):
        donnees = ("\n".join(corps[debut:debut + LOT_ENVOI]) + "\n").encode("utf-8")
        accepte = False
        for url in urls:
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(url, data=donnees, method="POST"), timeout=DELAI_ENVOI
                ) as reponse:
                    if 200 <= reponse.status < 300:
                        accepte = True
                        break
            except (urllib.error.URLError, OSError, ValueError):
                continue
        if not accepte:
            return False
    return True


def main() -> int:
    """Point d'entree : collecte puis pousse ou imprime ; rend toujours 0, erreur imprevue sur stderr."""
    try:
        return collecter()
    except Exception as erreur:  # noqa: BLE001 - le cron ne doit jamais crier
        print(f"collecteur hermes : erreur imprevue ({type(erreur).__name__}: {erreur})", file=sys.stderr)
        return 0


def collecter() -> int:
    """Lit la base et le quota selon le mode, puis pousse ou imprime. Rend 0."""
    rattrapage = "--rattrapage" in sys.argv
    sec = "--sec" in sys.argv

    try:
        rangees = sessions(None if rattrapage else time.time() - FENETRE_RECENTE)
        corps = lignes_conso(rangees) + ["hermes_conso_collecte_ok 1"]
        resume = f"{len({r['session_id'] for r in rangees})} sessions, {len(rangees)} lignes"
    except Exception as erreur:  # noqa: BLE001 - le cron ne doit jamais crier
        print(f"collecteur hermes : base illisible ({erreur})", file=sys.stderr)
        corps, resume = ["hermes_conso_collecte_ok 0"], "base illisible"

    if not rattrapage:
        quota, etat_quota = collecter_quota()
        corps += quota
        resume += f", quota : {etat_quota}"

    if sec:
        print("\n".join(corps))
        print(f"hermes : {resume}", file=sys.stderr)
        return 0

    if pousser(corps, destinations()):
        print(f"hermes : {resume}")
    else:
        print("collecteur hermes : base injoignable", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
