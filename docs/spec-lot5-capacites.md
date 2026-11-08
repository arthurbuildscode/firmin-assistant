# Spec lot 5 — Système de capacités, coffre, bus

Dépôt : le dépôt (déployé en `/srv/home-os` sur le VPS, utilisateurs `homeos`/`alex`/`camille`, groupe `foyer`, umask 002). Lire `CONTRACT.md`, `tools/server.py`, `tools/kitchen_daily.py`, `tools/runner.py`, `scripts/healthcheck.sh`, `install/install.sh`, `content/CLAUDE.md` avant de commencer. Python 3.12, tests pytest dans `tests/`, ruff. Tout en français (code commenté en français, identifiants en français comme le reste du dépôt).

Principe directeur (décision du porteur du projet, 7 nov.) : **une capacité = un plugin** (skill + connecteur + commandes + crons + smoke test), déclarative, activable/désactivable, et à terme **fabriquée par l'assistant lui-même** à la demande (ex. Planity). L'utilisateur ne voit jamais la technique. Les secrets ne sont jamais dans le code ni dans le prompt : les capacités déclarent ce dont elles ont besoin, le coffre fournit.

## A. Contrat d'une capacité (interface commune — NE PAS dévier)

Dossier `capabilities/<nom>/` (nom en minuscules, sans accent, `[a-z0-9_]`) :

```
capabilities/<nom>/
  capability.yaml      # manifeste (schéma ci-dessous)
  tools.py             # def enregistrer(mcp, ctx) -> None : ajoute les outils MCP via @mcp.tool()
  CLAUDE.md            # fragment injecté dans le prompt système quand la capacité est active
  onboarding.md        # (optionnel) questionnaire de découverte, même format que content/onboarding/*.md
  smoke.py             # def verifier(ctx) -> dict(ok: bool, message: str) ; exécuté par le healthcheck
  crons.py             # (optionnel) def executer(nom_cron, ctx) -> None
  tests/               # pytest, lancés par la suite globale
  README.md            # (optionnel) notes d'exploration du site, décisions
```

`capability.yaml` :

```yaml
nom: bus
titre: Prochain bus
description: Horaires et retards en temps réel des bus du réseau configuré (ligne 12…).
version: 1
statut: actif            # actif | inactif | essai   (essai = lecture seule, actions avec effet refusées)
origine: humain          # humain | assistant        (qui a écrit la capacité)
personnes: [alex, camille]   # qui peut l'utiliser ; [] = tout le foyer
secrets:                 # références de secrets requis, résolues par le coffre (jamais de valeur ici)
  - cle: planity/alex  # -> secret://planity/alex ; champs attendus
    champs: [identifiant, mot_de_passe]
    par_personne: true   # un secret par personne (planity/alex, planity/camille)
crons:
  - nom: rafraichir_gtfs
    quand: "Mon 04:00"   # même format que config.yaml crons
actions_avec_effet: [planity_reserver]   # outils qui nécessitent CONFIRMATION_REQUISE et sont bloqués en statut essai
reseau: ["siri.exemple.fr"]   # hôtes autorisés (documentaire pour l'instant, utilisé par l'isolation plus tard)
commandes_telegram:
  - nom: bus
    aide: Prochains bus sur ton trajet habituel
```

`ctx` (objet `tools.capacites.Contexte`) expose : `donnees` (Path de `data/`), `config` (config.yaml chargé), `personne` (slug de la personne courante ou None pour les crons), `secret(cle) -> dict` (via le coffre, lève `SecretManquant` avec un message utilisateur clair : « Identifiants Planity à renseigner : /identifiants planity »), `journal(texte)` (ajout au journal du foyer), `memoire_lire(chemin)` / `memoire_ecrire(chemin, texte)`.

Règles communes :
- Les outils d'une capacité sont préfixés par son nom (`bus_prochains`, `kitchen_daily_menu`).
- Un outil listé dans `actions_avec_effet` doit renvoyer `CONFIRMATION_REQUISE` (mécanisme existant dans `bot/confirmations.py`) tant que l'utilisateur n'a pas validé, et refuser avec un message clair si `statut: essai`.
- Toute sortie d'outil est passée par `tools.capacites.masquer_secrets()` avant retour (remplace toute valeur de secret connue par `•••`).
- Les données d'une capacité vont dans `data/capacites/<nom>/` (créé à l'activation, jamais supprimé par `disable`, supprimé par `remove --donnees`).

## B. Lot 5a — Cadre `tools/capacites.py` + CLI + migration Kitchen Daily (agent 1)

1. `tools/capacites.py` : découverte des dossiers `capabilities/*`, validation du manifeste (pydantic ou validation manuelle, pas de nouvelle dépendance lourde), `Contexte`, `charger_actives(mcp)` appelé par `tools/server.py` après les outils de base, `fragments_prompt()` qui concatène les `CLAUDE.md` actifs (utilisé par `bot/claude_runner.py` via le fichier passé à `--append-system-prompt-file` : construire un fichier temporaire = `content/CLAUDE.md` rendu + fragments), `smoke_tous()` pour le healthcheck, `executer_cron(nom_capacite, nom_cron)`, `masquer_secrets()`.
2. Outils MCP de base ajoutés à `server.py` : `capacites_lister()` (titre, statut, origine, dernier smoke test, personnes) et `capacites_statut(nom)`.
3. CLI `python -m tools.capacites list|enable <nom>|disable <nom>|remove <nom> [--donnees]|smoke [nom]|new <nom> --titre "…"` (`new` génère le squelette complet avec un outil d'exemple et un test).
4. Commande Telegram `/capacites` (liste lisible) dans `bot/commands.py` ; les `commandes_telegram` des manifestes sont enregistrées dynamiquement (le texte de la commande est envoyé à l'assistant comme un message « /bus » → il appelle l'outil adapté ; pas de logique métier côté bot).
5. `scripts/healthcheck.sh` → appelle `python -m tools.capacites smoke` et alerte par capacité en échec (même anti-spam 6 h).
6. Timers systemd : `install.sh` génère un timer `homeos-cap-<nom>-<cron>` par cron déclaré dans les manifestes actifs (comme pour `crons:` de config.yaml), qui lance `tools.capacites cron <nom> <cron>`.
7. **Migration de Kitchen Daily** en `capabilities/kitchen_daily/` sans régression : outils `kitchen_daily_*` (garder les noms existants s'ils sont déjà préfixés, sinon alias), le rappel du mercredi devient un cron du manifeste, la section Kitchen Daily de `content/CLAUDE.md` devient le `CLAUDE.md` du module, `onboarding/10-alimentation.md` reste commun, smoke = `statut` de session, la CLI `python -m tools.kitchen_daily …` continue de fonctionner (README inchangé côté commandes). `actions_avec_effet: [kitchen_daily_appliquer]`.
8. `CONTRACT.md` : nouvelle section « §9 Capacités » décrivant A, la CLI, le cycle de vie (créer/essai/actif/inactif/supprimé), la règle « origine: assistant ⇒ statut essai jusqu'à première validation humaine d'une action avec effet », et un paragraphe « Identifiants » : la carte de saisie dans l'appli web (lot 4) envoie les valeurs directement au coffre, l'assistant ne manipule que des références `secret://`.
9. README : section « Capacités » (lister, activer, désactiver, ajouter).
10. Tests : manifeste valide/invalide, chargement, masquage des secrets, statut essai bloque une action avec effet, CLI `new` produit un module qui charge, Kitchen Daily migré (les 22 tests existants passent toujours). Suite complète verte + ruff.

## C. Lot 5b — Coffre `tools/coffre.py` + Vaultwarden (agent 2)

1. `tools/coffre.py` : API unique `lire(cle) -> dict`, `ecrire(cle, champs: dict)`, `supprimer(cle)`, `lister() -> [cle]`, `valeurs_connues()` (pour le masquage). Deux backends :
   - `fichier` (par défaut, immédiat) : `data/coffre.json` chiffré avec une clé dérivée de `COFFRE_CLE` dans `secrets.env` (AES-GCM via `cryptography`, déjà dispo ? sinon l'ajouter à `tools/requirements.txt`), mode 640 `homeos:foyer`. Ce backend est le repli et sert aussi de cache local.
   - `vaultwarden` : via l'API Bitwarden (client `bw` CLI n'est pas souhaité ; utiliser l'API REST de Vaultwarden avec un compte de service dédié « Firmin Assistant », identifiants dans `secrets.env` : `VAULTWARDEN_URL`, `VAULTWARDEN_CLIENT_ID`, `VAULTWARDEN_CLIENT_SECRET`, `VAULTWARDEN_MOT_DE_PASSE_MAITRE` — le chiffrement Bitwarden se fait côté client, donc il faut implémenter le déchiffrement des cipher items : dérivation PBKDF2/Argon2 de la clé maître, déchiffrement de la clé symétrique, puis des items. Si c'est trop lourd/risqué pour ce lot : implémenter proprement le backend `fichier`, documenter dans `docs/coffre.md` ce qu'il faudrait pour Vaultwarden (ou l'alternative `bw` CLI en service) et laisser `backend: vaultwarden` lever `NotImplementedError` avec un message clair. **Priorité à un backend fichier solide et testé.**
   - Sélection via `config.yaml` → `coffre: { backend: fichier }`.
2. CLI `python -m tools.coffre set <cle> [--champs identifiant,mot_de_passe]` (saisie masquée `getpass`, jamais d'argument de ligne de commande contenant une valeur), `get <cle> --champs` (affiche seulement les noms de champs présents, jamais les valeurs sauf `--revele` explicite), `rm`, `ls`.
3. Commande Telegram `/identifiants <capacite>` dans `bot/commands.py` : répond « Pour ta sécurité, les identifiants se saisissent sur le serveur (ou dans l'appli web à venir) : `sudo -u homeos … tools.coffre set <cle>` », jamais de saisie dans Telegram (si quelqu'un colle un mot de passe dans Telegram, le bot répond qu'il ne l'enregistre pas et conseille de le changer).
4. `install/vaultwarden.sh` (optionnel, idempotent) : installe Vaultwarden en conteneur Podman ou binaire (open source, pas Docker Desktop), derrière Caddy avec TLS (Let's Encrypt sur un domaine à fournir), ouverture ufw 443, sauvegarde `data/vaultwarden` incluse dans restic. Documenter dans `docs/coffre.md` : pourquoi Vaultwarden (open source, auto-hébergé, apps Bitwarden sur téléphone pour le foyer), ce que fait le compte de service, rotation.
5. Migration douce : `tools/kitchen_daily.py` continue d'utiliser `data/.kd/state.json` (état de session, pas un identifiant) — ne pas toucher. Le coffre servira aux capacités futures (Planity, cantine).
6. Tests : chiffrement/déchiffrement, mauvaise clé, masquage, CLI set/get sans fuite (vérifier qu'aucune valeur n'apparaît dans stdout). Ruff.

## D. Lot 5c — Capacité `bus` (agent 3)

Source : le projet d'origine (app Node sans dépendance : `app.mjs` = logique GTFS + SIRI temps réel, `server.mjs` serveur local, `data/reseau.json` + `data/horaires.json` 22 Mo, `prepare.py` régénère la grille GTFS, `config.json`). Lire son README.

1. Intégration sans réécriture : copier `app.mjs`, `server.mjs`, `prepare.py`, `config.json`, `data/` dans `capabilities/bus/service/` (git-lfs non disponible : vérifier que 22 Mo en JSON est acceptable dans le dépôt ; sinon exclure `data/` du git, le générer à l'installation via `prepare.py` dans un cron/`install`). Service systemd `homeos-bus.service` (User=homeos, `node server.mjs`, PORT 8081, bind 127.0.0.1 uniquement, Restart=always) ajouté par `install.sh` (ou par un script `capabilities/bus/install.sh` appelé par install.sh — choisir et documenter).
2. Suivre le contrat §A : `capability.yaml` (statut actif, origine humain, cron `rafraichir_gtfs` hebdo lundi 04:00 qui relance `prepare.py` et redémarre le service), `tools.py` avec :
   - `bus_lignes()` : lignes disponibles (titre, sens).
   - `bus_arrets(ligne, recherche)` : arrêts d'une ligne filtrés par texte (accent-insensible).
   - `bus_prochains(ligne, sens, arret, nombre=3)` : prochains passages avec heure théorique, estimée, retard en minutes, source (`temps_reel`/`theorique`), bus déjà passés récents. Appelle `http://127.0.0.1:8081/api/...` (regarder `gerer()` dans `app.mjs` pour les routes exactes). Erreur réseau → message clair « Service bus indisponible ».
   - `bus_trajet_habituel(personne)` : lit `data/capacites/bus/trajets.yaml` (par personne : ligne, sens, arrêt, heure habituelle, jours) et renvoie les prochains passages correspondants ; vide → inviter à l'onboarding.
   - `bus_definir_trajet(personne, ligne, sens, arret, heure, jours)` : écrit `trajets.yaml`.
   - `smoke.py` : `GET /api/diagnostic` ou un arrêt connu répond en < 3 s.
   - `CLAUDE.md` : quand utiliser (« il est où mon bus », brief du jour : si un trajet habituel part dans l'heure et qu'un retard ≥ 5 min est annoncé, le mentionner ; ne jamais alerter de façon proactive pour un retard < 5 min — filtre « ça vaut le coup »), formuler en minutes restantes (« dans 7 min, retard 3 min »).
   - `onboarding.md` : qui prend le bus, ligne/arrêt/sens, horaires habituels, jours, seuil d'alerte souhaité.
   - `commandes_telegram: [{nom: bus, aide: Prochains bus sur ton trajet habituel}]`.
3. Le module doit fonctionner même si le lot 5a (cadre) n'est pas encore fusionné : `tools.py` expose `enregistrer(mcp, ctx)` et un `ctx` minimal est accepté (duck typing : seulement `ctx.donnees` utilisé). Ne pas modifier `tools/server.py` ni `bot/` — l'agent 1 branche le chargement.
4. Tests (pytest, sans réseau : mocker les réponses HTTP du service local avec des JSON fixes issus d'un vrai appel si possible, sinon construits à partir de `app.mjs`) + un test d'intégration optionnel marqué `@pytest.mark.reseau` qui lance `node server.mjs` et interroge un arrêt réel (donné par l'environnement, jamais en dur). Ruff.
5. `capabilities/bus/README.md` : routes de l'API utilisées, quota SIRI, décision sur `data/`.

## E. Vérification de fin de lot (moi, après fusion)

Suite complète verte, `ruff` propre, déploiement sur le VPS (`git pull`, `install.sh`, restart), `python -m tools.capacites list` montre kitchen_daily et bus actifs, `/capacites` et `/bus` dans Telegram, healthcheck avec smoke tests, `tools.coffre set` fonctionne en `homeos`.
