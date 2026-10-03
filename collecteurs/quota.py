#!/usr/bin/env python3
"""Pousse dans VictoriaMetrics ce qu'il reste de quota Claude, et quand il repart.

Le reste de la pile mesure ce qui a ete consomme ; celui-ci lit le chiffre qui fait
autorite, celui de l'ecran /usage. Il tourne sur la machine qui heberge la base, par
cron, parce que le quota est au niveau du compte et non de la machine.

Source : https://api.anthropic.com/api/oauth/usage — non documentee, donc on ne lit
que les cles qu'on connait, et leur disparition se voit au lieu de passer pour un
quota a zero.

Sortie toujours 0 : un cron qui echoue bruyamment tous les quarts d'heure finit par
etre ignore, et c'est comme ca qu'on rate la vraie panne.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

MAISON = pathlib.Path.home()
REGLAGES = MAISON / ".claude/settings.json"
IDENTIFIANTS = MAISON / ".claude/.credentials.json"
CLAUDE = MAISON / ".local/bin/claude"
USAGE = "https://api.anthropic.com/api/oauth/usage"
ENTETE_BETA = "oauth-2025-04-20"
MARGE_RENOUVELLEMENT = 3600
# Ecartes : `extra_usage` et `spend` sont des credits, desactives sur
# ce compte. Deux series qui ne bougeraient jamais et qui feraient croire a une mesure.
ECARTES = {"extra_usage", "spend"}
DELAI_ENVOI = 5
DELAI_USAGE = 15
DELAI_RENOUVELLEMENT = 60


def destinations() -> list[str]:
    """Rend les URL d'import a essayer, lues dans les reglages de la machine."""
    env = json.loads(REGLAGES.read_text(encoding="utf-8")).get("env", {})
    bases = [b.strip().rstrip("/") for b in env.get("TELEMETRIE_ENDPOINTS", "").split(",") if b.strip()]
    if not bases:
        point = env.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "")
        if not point:
            raise RuntimeError("ni TELEMETRIE_ENDPOINTS ni OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
        bases = [point.split("/opentelemetry")[0].rstrip("/")]
    return [f"{b}/api/v1/import/prometheus" for b in bases]


def jeton() -> tuple[str, int]:
    """Rend le jeton d'acces et le nombre de secondes avant son expiration."""
    o = json.loads(IDENTIFIANTS.read_text(encoding="utf-8"))["claudeAiOauth"]
    return o["accessToken"], int(o.get("expiresAt", 0) / 1000 - time.time())


def renouveler() -> None:
    """Renouvelle le jeton sans consommer de quota.

    `claude mcp list` suffit : il renouvelle au demarrage quand le jeton est expire,
    sans aucune inference. Mesure du 24/09 — `claude auth status`, lui, ne renouvelle
    pas. ⛔ Ne jamais sauvegarder le fichier d'identifiants avant : le renouvellement
    fait tourner le refreshToken, et une copie restauree deconnecterait la machine.
    """
    subprocess.run(
        [str(CLAUDE), "mcp", "list"],
        capture_output=True, timeout=DELAI_RENOUVELLEMENT, check=False,
    )


def interroger(acces: str) -> dict:
    """Rend le payload de l'ecran /usage."""
    requete = urllib.request.Request(
        USAGE,
        headers={"Authorization": f"Bearer {acces}", "anthropic-beta": ENTETE_BETA},
    )
    with urllib.request.urlopen(requete, timeout=DELAI_USAGE) as reponse:
        return json.load(reponse)


def epoch(horodatage: str | None) -> float | None:
    """Convertit un horodatage ISO de l'API en secondes depuis l'epoch."""
    if not horodatage:
        return None
    return datetime.fromisoformat(horodatage.replace("Z", "+00:00")).timestamp()


def echapper(valeur: str) -> str:
    """Echappe une valeur d'etiquette Prometheus."""
    return str(valeur).replace("\\", "\\\\").replace('"', '\\"')


def fenetres(payload: dict):
    """Rend les fenetres du payload qui portent une utilisation, quel que soit leur nom.

    Volontairement sans liste en dur : le payload porte une quinzaine d'emplacements
    nuls -- `seven_day_cowork`, `seven_day_opus`, et d'autres a noms codes. Le jour ou
    l'un d'eux se remplit, il prend la forme d'une fenetre et se met donc a etre pousse
    tout seul, sans qu'il faille modifier ce fichier.
    """
    for cle, valeur in payload.items():
        if cle in ECARTES:
            continue
        if isinstance(valeur, dict) and isinstance(valeur.get("utilization"), (int, float)):
            yield cle, valeur


def lignes(payload: dict, reste_jeton: int) -> list[str]:
    """Rend les lignes d'exposition, en ne lisant que les cles connues."""
    sortie: list[str] = []

    # `utilization` est un flottant la ou `limits[].percent` est un entier : sur une
    # fenetre a peine entamee, l'entier arrondit a zero ce que le flottant montre encore.
    for nom, fenetre in fenetres(payload):
        sortie.append(f'quota_utilisation{{fenetre="{echapper(nom)}"}} {fenetre["utilization"]}')
        remise = epoch(fenetre.get("resets_at"))
        if remise:
            sortie.append(f'quota_utilisation_resets_at{{fenetre="{echapper(nom)}"}} {remise:.0f}')

    # Combien d'emplacements restent nuls. Le nombre baisse le jour ou Anthropic en
    # remplit un : c'est le signal qu'une mesure plus fine est devenue disponible.
    nuls = sum(1 for v in payload.values() if v is None)
    sortie.append(f"quota_champs_nuls {nuls}")

    for limite in payload.get("limits") or []:
        genre = limite.get("kind")
        if not genre:
            continue
        etiquettes = f'fenetre="{echapper(genre)}",severite="{echapper(limite.get("severity", "inconnue"))}"'
        modele = ((limite.get("scope") or {}).get("model") or {}).get("display_name")
        if modele:
            etiquettes += f',modele="{echapper(modele)}"'
        sortie.append(f"quota_percent{{{etiquettes}}} {limite.get('percent', 0)}")
        sortie.append(f'quota_active{{fenetre="{echapper(genre)}"}} {1 if limite.get("is_active") else 0}')
        remise = epoch(limite.get("resets_at"))
        if remise:
            sortie.append(f'quota_resets_at{{fenetre="{echapper(genre)}"}} {remise:.0f}')

    # `percent` est une part de ce qui a ete consomme : les surfaces somment a 100. Pour
    # savoir ce qu'une surface a pris de la FENETRE, il faut la rapporter au remplissage
    # de celle-ci -- ce qui donne au passage une resolution de 0,01 point au lieu de 1.
    remplissage = (payload.get("seven_day") or {}).get("utilization")
    for ligne in (payload.get("seven_day_breakdown") or {}).get("rows") or []:
        if not ligne.get("key"):
            continue
        surface = echapper(ligne["key"])
        part = ligne.get("percent", 0)
        sortie.append(f'quota_surface_percent{{surface="{surface}"}} {part}')
        if isinstance(remplissage, (int, float)):
            sortie.append(
                f'quota_surface_fenetre_percent{{surface="{surface}"}} '
                f"{part * remplissage / 100:.4f}"
            )

    sortie.append(f"quota_jeton_expire_dans {reste_jeton}")
    return sortie


def pousser(corps: list[str], urls: list[str]) -> bool:
    """Essaie les adresses dans l'ordre et dit si l'une a accepte les lignes."""
    donnees = ("\n".join(corps) + "\n").encode("utf-8")
    for url in urls:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, data=donnees, method="POST"), timeout=DELAI_ENVOI
            ) as reponse:
                if 200 <= reponse.status < 300:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return False


def main() -> int:
    """Point d'entree : renouvelle si besoin, interroge, pousse."""
    try:
        urls = destinations()
    except (OSError, ValueError, RuntimeError) as erreur:
        print(f"collecteur de quota : {erreur}", file=sys.stderr)
        return 0

    try:
        acces, reste = jeton()
        if reste < MARGE_RENOUVELLEMENT:
            renouveler()
            acces, reste = jeton()

        try:
            payload = interroger(acces)
        except urllib.error.HTTPError as erreur:
            if erreur.code != 401:
                raise
            renouveler()
            acces, reste = jeton()
            payload = interroger(acces)

        if not payload.get("limits"):
            raise RuntimeError(f"aucune limite dans le payload — cles recues : {sorted(payload)[:8]}")

        corps = lignes(payload, reste) + ["quota_collecte_ok 1"]
        if pousser(corps, urls):
            hebdo = next((l for l in payload["limits"] if l.get("kind") == "weekly_all"), {})
            print(f"quota : hebdomadaire a {hebdo.get('percent')} % ({hebdo.get('severity')}), "
                  f"jeton valide {reste // 3600} h")
        else:
            print("collecteur de quota : base injoignable", file=sys.stderr)
        return 0

    except Exception as erreur:  # noqa: BLE001 - le cron ne doit jamais crier
        print(f"collecteur de quota : {erreur}", file=sys.stderr)
        pousser(["quota_collecte_ok 0"], urls)
        return 0


if __name__ == "__main__":
    sys.exit(main())
