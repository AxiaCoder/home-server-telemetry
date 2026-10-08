# home-server-telemetry

> 🇫🇷 Où part le quota de Claude Code — et de Hermès —, sur toutes vos machines, en graphes. Deux
> conteneurs sur un serveur maison : VictoriaMetrics reçoit l'OpenTelemetry natif de Claude Code,
> Grafana l'affiche. Les dashboards mesurent ce qui compte vraiment sur un abonnement : le **cache
> relu**, pas seulement les tokens produits. Et vmalert + Alertmanager préviennent sur Slack
> quand une collecte échoue ou qu'une tâche planifiée se tait.
>
> 🇬🇧 Where your Claude Code — and Hermès — quota goes, across all your machines, as graphs. Two
> containers on a home server: VictoriaMetrics receives Claude Code's native OpenTelemetry, Grafana
> displays it. The dashboards measure what actually matters on a subscription: **cache reads**, not
> just output tokens. vmalert + Alertmanager post to Slack when a collector fails or a scheduled
> job goes silent. *The content is written in French.*

| Pièce | Port | Rôle |
|---|---|---|
| VictoriaMetrics | 8428 | reçoit l'OTLP, stocke (12 mois), répond à l'API de requête Prometheus |
| Grafana | 3000 | lit VictoriaMetrics, affiche les dashboards du dépôt |
| vmalert | — | évalue les règles de `alertes.yml` chaque minute |
| Alertmanager | 9093 | groupe les alertes et les poste sur Slack |

🔴 **VictoriaMetrics n'a aucune authentification.** Quiconque atteint le port 8428 peut lire vos
métriques — **dont l'adresse e-mail et les identifiants de votre compte Claude**, que l'OpenTelemetry
de Claude Code joint à chaque série —, en écrire de fausses, et en effacer. N'exposez jamais 8428 ni
3000 sur Internet — ni 9093 : **Alertmanager non plus n'a aucune authentification**, et
quiconque l'atteint pose des silences qui font taire vos alertes. Les ports n'écoutent par défaut que sur la machine (`127.0.0.1`) ; pour que vos
postes émettent, `BIND_ADDR` dans `.env` :

| `BIND_ADDR` | Qui atteint les ports | Quand |
|---|---|---|
| *(absent)* | la machine seule — ⚠️ pas un conteneur du serveur, qui la joint par la passerelle de Docker | tout émetteur tourne sur le serveur, hors conteneur |
| `0.0.0.0` | toutes les interfaces : machine, réseau local, VPN, conteneurs | un serveur maison **derrière un routeur sans redirection de port**. ⛔ Jamais sur un VPS : **Docker contourne `ufw`**, un pare-feu d'hôte ne protège pas |
| l'IP d'une interface | ce réseau seul — ⚠️ **`localhost` ne répond plus** : les émetteurs du serveur lui-même doivent viser cette IP | un hôte exposé, en ne publiant que sur l'interface du VPN — ⚠️ elle doit exister quand Docker démarre, sinon la publication échoue au redémarrage |

Le nom porte `home-server` parce qu'une télémétrie de projets en ligne n'aurait ni les mêmes
contraintes ni la même pile.

---

## Ce dépôt reçoit, il n'émet pas

**Les postes émettent, ce dépôt collecte et affiche.** Ce qui tourne sur les postes — l'activation
de l'OpenTelemetry de Claude Code, et le hook qui rend aux sous-agents leur nom — vit dans
[**claude-config**](https://github.com/AxiaCoder/claude-config). Les deux dépôts sont
indépendants : n'importe quel client peut écrire ici, en OTLP (`/opentelemetry/v1/metrics`) comme
Claude Code, ou par l'API d'import de VictoriaMetrics (`/api/v1/import/prometheus`) comme le hook
`collecteur-agents` et les collecteurs de ce dépôt.

Ce qu'un poste doit poser dans son environnement (le bloc `env` de `~/.claude/settings.json`) :

```bash
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_METRICS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://<hôte>:8428/opentelemetry/v1/metrics
OTEL_LOGS_EXPORTER=none
OTEL_RESOURCE_ATTRIBUTES=machine=<nom-du-poste>
```

⚠️ **C'est bien `..._METRICS_ENDPOINT`, avec le chemin complet.** La variable générique
`OTEL_EXPORTER_OTLP_ENDPOINT` est une *base* à laquelle le SDK ajoute `/v1/metrics` : lui
donner le chemin entier ferait poster sur `/opentelemetry/v1/metrics/v1/metrics`.

⚠️ **`machine=<nom>` est ce qui sépare vos postes** dans les dashboards. `os.type` ne suffit pas :
deux machines Linux tombent sous la même valeur.

⛔ **Un client dont la variable d'endpoint est absente n'émet rien et ne tombe pas.**

⚠️ **Métriques seulement.** `OTEL_LOGS_EXPORTER` reste à `none` : les événements portent du
texte de prompt, les métriques non.

📌 **Une mesure fraîche met une trentaine de secondes à apparaître** dans une requête :
VictoriaMetrics écarte les dernières secondes par défaut (`-search.latencyOffset`). Ce n'est
pas une perte, c'est un décalage de lecture.

---

## Installer

**Prérequis** : Docker avec Compose sur le serveur ; Python 3 (bibliothèque standard seulement)
pour les collecteurs.

```bash
git clone https://github.com/AxiaCoder/home-server-telemetry.git
cd home-server-telemetry
cp .env.example .env        # mot de passe admin de Grafana, et BIND_ADDR
docker compose up -d
```

📌 **Les dashboards sont réglés sur un parc précis**, gardé comme exemple : quatre machines
étiquetées `mac`, `windows`, un serveur et `worker` (un worker de développement en conteneur).
Plusieurs panneaux nomment `mac`, `windows` ou `worker` dans leurs requêtes, et certaines descriptions citent des relevés de ce
parc. Pour le vôtre : reprendre ces noms dans `OTEL_RESOURCE_ATTRIBUTES`, ou les remplacer dans les
JSON.

Grafana est sur `http://<hôte>:3000`, les dashboards sont déjà provisionnés. Pour suivre le dépôt :
`git pull`, Grafana recharge les fichiers tout seul.

---

## Les collecteurs

Deux scripts qui lisent ce que l'OpenTelemetry ne donne pas, et le poussent par l'API d'import de
VictoriaMetrics (`/api/v1/import/prometheus`). Ils sortent toujours en 0 et signalent leur échec
par une métrique `*_collecte_ok` : un cron qui échoue bruyamment finit par être ignoré.

🟠 **Les deux appellent des endpoints non documentés, avec le jeton OAuth de votre compte.**
`api.anthropic.com/api/oauth/usage` (Claude) et `chatgpt.com/backend-api/wham/usage` (Codex) ne
sont garantis par personne : ils peuvent changer ou disparaître sans préavis, et leur usage hors
des clients officiels n'est pas couvert par les conditions des fournisseurs. Une requête toutes les
cinq minutes, sans aucune inférence. Mais ce n'est pas de la lecture passive :

- `quota.py`, quand le jeton approche de l'expiration, lance `claude mcp list` pour le faire
  renouveler : le `refreshToken` tourne, `~/.claude/.credentials.json` est réécrit, et les serveurs
  MCP configurés démarrent le temps de la commande. ⛔ Ne restaurez jamais une copie ancienne de ce
  fichier : elle déconnecterait la machine.
- `hermes.py` se présente avec l'en-tête `User-Agent` du client Codex (`codex_cli_rs`).

**À utiliser sur votre propre compte, à vos risques.**

| Collecteur | Ce qu'il lit | Où le lancer |
|---|---|---|
| `collecteurs/quota.py` | le quota Claude restant et sa remise à zéro — le chiffre de l'écran `/usage` | une machine **Linux** où Claude Code est connecté : il lit `~/.claude/.credentials.json` (sous macOS, les identifiants sont dans le trousseau) et appelle `~/.local/bin/claude`, chemin de l'installateur officiel. Par cron |
| `collecteurs/hermes.py` | la consommation de Hermès (`~/.hermes/state.db`) et le quota du compte Codex | la machine où tourne Hermès, par cron |

**L'adresse de VictoriaMetrics** est lue dans le bloc `env` de `~/.claude/settings.json` :
`TELEMETRIE_ENDPOINTS`, une ou plusieurs bases séparées par des virgules
(`http://<hôte>:8428,http://<autre>:8428`, essayées dans l'ordre), sinon la base tirée de
`OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`. Faute des deux, `quota.py` n'envoie rien et le dit sur sa sortie
d'erreur, et `hermes.py` pousse sur `http://localhost:8428` — qui ne répond pas si `BIND_ADDR`
désigne une seule interface.

```
*/5 * * * * cd <clone> && python3 collecteurs/quota.py  >> <log>/quota.log 2>&1 && ./pouls collecte-quota 30m alertes
*/5 * * * * cd <clone> && python3 collecteurs/hermes.py >> <log>/hermes.log 2>&1 && ./pouls collecte-hermes 30m alertes
```

Le `pouls` en bout de ligne dit que le cron tourne : un collecteur sort toujours en 0, il pulse donc
même quand sa collecte échoue — c'est `*_collecte_ok` qui dit qu'elle réussit. Sans lui, un
collecteur arrêté n'alerte pas. Voir [`pouls`](#surveiller-une-tâche--pouls).

Pour `hermes.py`, une première passe `python3 collecteurs/hermes.py --rattrapage` pousse toutes
les sessions de la base, sans le quota.

---

## Les alertes

vmalert évalue `alertes.yml` chaque minute contre VictoriaMetrics et passe ce qui se déclenche à
Alertmanager, qui groupe et poste sur Slack — une ligne par alerte :

```
🟠 le quota Claude ne se collecte plus → lire le log de quota.py — jeton expiré ou endpoint changé
🔴 tâche sauvegarde muette depuis 2d 3h 0m 0s → lire <dossier>/backup.log, relancer backup.sh
```

🟠 part sur le canal `alertes` (rappel toutes les 24 h), 🔴 sur `urgent` (toutes les 12 h), ✅ quand
l'alerte se résout. Les alertes d'une même `famille` partent ensemble, après 2 min d'attente.

### Brancher Slack

Une application Slack avec un bot, scope `chat:write`, invité dans les deux canaux. Ce qui est
propre à votre installation vit dans `alertmanager/local/`, **ignoré par git** :

```bash
mkdir -p alertmanager/local
cp alertmanager/local.tmpl.example alertmanager/local/local.tmpl   # canaux et gestes
printf '%s' 'xoxb-…' > alertmanager/local/slack-token              # le jeton du bot
sudo chown 65534 alertmanager/local/slack-token && chmod 400 alertmanager/local/slack-token
docker compose up -d
```

⚠️ **Alertmanager tourne sous l'utilisateur `nobody` (65534)** : un jeton lisible par vous seul lui
est illisible, et Slack ne reçoit rien. Le `chown` ci-dessus le lui donne sans l'ouvrir à tous.

Sans `local.tmpl`, les messages partent vers `#alertes` et `#urgent` par leur nom, avec un geste
générique.

### Surveiller une tâche : `pouls`

Une tâche planifiée qui se tait ne prévient personne. `pouls` signale chaque succès ; la règle
`TacheMuette` alerte quand le dernier date de plus que le délai donné :

```
pouls <tâche> <délai> <canal> [<délai2> <canal2>]
```

Une ligne de cron suffit, après le `&&` : sans succès, pas de pouls.

```
0 3 * * * <dossier>/backup.sh && <clone>/pouls sauvegarde 26h alertes 48h urgent
```

Ici : 🟠 si la sauvegarde n'a pas réussi depuis 26 h, 🔴 depuis 48 h. Délai en `s`, `m`, `h` ou
`d` ; noms de tâche et de canal en lettres, chiffres, `_` et `-`. L'adresse est celle des
collecteurs (`TELEMETRIE_ENDPOINTS`, sinon la base de `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`, sinon
`http://localhost:8428`). Il ne demande que `sh` et
`curl`, et sort toujours en 0.

Le geste affiché pour une tâche muette se règle dans `alertmanager/local/local.tmpl`, une branche
par nom de tâche (`geste.tache`), puis `docker compose restart alertmanager`.

📌 **Une tâche qui n'a jamais pulsé n'alerte pas** : la règle compare un âge à un délai, il faut
les deux. Et un pouls qui n'arrive plus depuis 45 jours sort de la fenêtre de la règle — l'alerte se
résout faute de donnée. ⇒ Pour retirer une tâche, la retirer du cron et laisser passer 45 jours, ou
effacer ses séries (`/api/v1/admin/tsdb/delete_series`).

`collecteurs/hermes.py` pulse pour Hermès : il lit `~/.hermes/cron/ticker_last_success` et pousse
`tache="hermes"`, 🟠 au-delà de 30 min.

### Ajouter une alerte seuil

Une règle dans `alertes.yml`, sur le modèle des autres : deux étiquettes, `famille` (le groupe du
message) et `canal` (`alertes` ou `urgent`), deux annotations, `resume` et `geste`. Puis
`docker compose restart vmalert`. Vérifier le fichier avant :

```bash
docker run --rm -v "$PWD":/r:ro victoriametrics/vmalert:v1.152.0 -dryRun -rule=/r/alertes.yml
docker run --rm -v "$PWD/alertmanager.yml":/etc/alertmanager/alertmanager.yml:ro \
  -v "$PWD/alertmanager":/etc/alertmanager/maison:ro --entrypoint amtool prom/alertmanager:v0.34.1 \
  check-config /etc/alertmanager/alertmanager.yml
```

**Et la tester** : chaque règle a ses cas dans `tests/regles/alertes_test.yml` — une série
d'échantillons, le moment où l'alerte doit (ou ne doit pas) se déclencher. Ajouter une règle, c'est
ajouter son cas. La CI (`.github/workflows/tests.yml`) les joue à chaque pull request avec
`vmalert-tool`, à la même version que l'image ; en local :

```bash
VMALERT_TOOL=/chemin/vers/vmalert-tool-prod python3 -m unittest discover -s tests -v
```

Sans `vmalert-tool`, ce test est **sauté** : en local, c'est voulu ; en CI, il est forcé.

### Faire taire une alerte

Un silence, le temps d'une intervention : la page d'Alertmanager (`http://<hôte>:9093`, onglet
*Silences*), ou en ligne de commande sur le serveur —

```bash
docker exec alertmanager amtool --alertmanager.url=http://localhost:9093 \
  silence add alertname=TacheMuette tache=sauvegarde --duration=6h --comment="restauration en cours"
docker exec alertmanager amtool --alertmanager.url=http://localhost:9093 silence query
```

Les silences survivent à un redémarrage (volume `alertmanager-data`).

---

## Les cinq dashboards

| Dashboard | Ce qu'il répond |
|---|---|
| **Claude Code — où part le quota** (`claude-code-quota`) | où part la consommation *maintenant* : par type de token, par machine, principal contre sous-agents, par modèle |
| **Claude Code — une semaine contre l'autre** (`claude-code-semaines`) | est-ce que ça a bougé depuis la semaine dernière |
| **Claude Code — ce que coûtent les sous-agents** (`claude-code-agents`) | quel agent pèse, combien de fois il part, et ce qu'il coûte à chaque lancement — nourri par le hook `collecteur-agents` de claude-config |
| **Claude Code — est-ce qu'on optimise sur la durée** (`claude-code-tendance`) | la dépense ramenée à une unité de travail — par session, par lancement d'agent —, semaine par semaine sur douze mois : un total suit le volume de travail, ce ratio suit l'efficacité |
| **Hermès — consommation et quota** (`hermes`) | ce que consomme Hermès, par origine et par tâche, et où en est le quota du compte Codex |

🔑 **La grandeur qui compte est le cache relu.** Sur un usage réel, il pèse plusieurs centaines de
fois les tokens produits : une pile qui compte `input + output` mesure le mauvais nombre.

🔑 **L'unité est le token.** Le « coût pondéré » des dashboards applique la grille publique (sortie
×5, cache écrit ×1,25, cache relu ×0,1) : c'est un indice de comparaison, pas un prix — un
abonnement est fixe.

🔑 **La comparaison se fait à durée écoulée égale.** `offset 7d` décale la *même* fenêtre de sept
jours : on compare le même nombre d'heures depuis jeudi. Comparer la semaine en cours, partielle, à
une semaine complète donnerait toujours une baisse — et on conclurait à un gain qui n'existe pas.

⛔ **Le dashboard des sous-agents se lit avec `last_over_time`, jamais `sum_over_time`.** Ses
séries viennent du collecteur : un échantillon = un lancement, et `sum_over_time` compterait deux
fois un lancement renvoyé après une panne de réseau.

⚠️ **Un panneau « semaine -1 » vide n'est pas une panne** : il n'y a pas encore de donnée sept
jours plus tôt.

---

## Ouvrir un dashboard sans se faire piéger par l'état retenu

Grafana garde dans le navigateur la dernière fenêtre de temps et les dernières valeurs de
variables. **Le défaut écrit dans le JSON ne s'applique donc qu'à la première visite** — après,
on retombe sur ce qu'on regardait la fois précédente, et on peut lire de travers sans s'en
apercevoir.

⇒ **Les liens à mettre en favori**, qui imposent l'état à chaque ouverture :

```
http://<hôte>:3000/d/claude-code-quota/?from=now-3d%2Fw%2B3d&to=now&var-machine=%24__all&refresh=5m
http://<hôte>:3000/d/claude-code-semaines/?from=now-3d%2Fw%2B3d&to=now&var-machine=%24__all&refresh=5m
http://<hôte>:3000/d/claude-code-agents/?from=now-30d&to=now&var-machine=%24__all&var-agent=%24__all&refresh=5m
http://<hôte>:3000/d/hermes/?from=now-7d&to=now&refresh=5m
http://<hôte>:3000/d/claude-code-tendance/?from=now-12M&to=now&var-machine=%24__all&var-agent=%24__all&refresh=1h
```

⚠️ **`/` et `+` doivent être encodés** (`%2F`, `%2B`) : `now-3d/w+3d` passé tel quel dans une URL
donne une autre date, sans erreur visible.

📌 **Pourquoi cette fenêtre** : le quota Claude court du **jeudi 00:00 au jeudi 00:00**, et
`now-3d/w+3d` résout au dernier jeudi quel que soit le jour d'ouverture. Une fenêtre `now-7d`
glissante mélangerait deux semaines de quota. Le début de semaine est figé à lundi côté serveur
(`GF_DATE_FORMATS_DEFAULT_WEEK_START`), sinon le calage `/w` suivrait la locale du navigateur et
deux postes liraient deux fenêtres différentes.

---

## Modifier un dashboard

Ils vivent dans `grafana/dashboards/*.json` et sont montés dans Grafana en lecture seule
(`allowUiUpdates: false`). La base de Grafana n'est donc jamais la source de vérité.

**Pour en modifier un :** l'éditer dans l'UI, exporter le JSON (*Dashboard settings → JSON
Model*), écraser le fichier, commiter, `git pull` sur le serveur. Grafana recharge les fichiers
toutes les 30 secondes.

---

## Ce que cette pile ne fait pas

- **Elle ne surveille pas l'hôte.** CPU, RAM, disque, état des conteneurs : c'est le travail d'un
  autre outil, qu'on gagne à garder séparé — pour pouvoir casser celle-ci sans perdre la vue sur
  la machine.
- **Elle n'instrumente rien.** Ce sont les clients qui émettent.

## Inspiration

Ce dépôt doit beaucoup à [**S.C.R.O.O.G.E.**](https://github.com/blegouge/S.C.R.O.O.G.E) —
*Smart Context Reducer & Optimized Observability Governance Engine* —, une pile de télémétrie et
d'optimisation pour IDE assistés par IA, dont il a tiré l'idée de mesurer la consommation des
agents.

## Licence

[MIT](./LICENSE).
