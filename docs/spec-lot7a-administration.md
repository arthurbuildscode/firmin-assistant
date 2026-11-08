# Spec lot 7a — Administration par la conversation

Objectif (décision du porteur du projet, 7 nov.) : tout réglage qui passe aujourd'hui par un fichier de configuration ou par une commande sur le serveur doit pouvoir se faire en parlant à Firmin, pour les propriétaires du foyer. Aucun code n'est écrit par l'assistant dans ce lot (c'est le lot 7b « atelier ») : il s'agit de **configuration**, avec journal et retour arrière.

## 1. Capacité `administration` (`capabilities/administration/`)

Contrat §9 (CONTRACT.md). `personnes: []` mais chaque outil vérifie que l'appelant a le rôle `proprietaire` (nouveau champ facultatif `role: proprietaire|membre|invite` dans `personnes[]` de config.yaml ; défaut `proprietaire` pour les adultes listés). `origine: humain`, `statut: actif`, `reseau: []`, `secrets: []`.

Outils (préfixe `admin_`), tous journalisés dans `actions_log` avec `kind=admin_<action>` et l'auteur :

| Outil | Effet | Avec effet ? |
|---|---|---|
| `admin_etat()` | état du système lisible : capacités (statut, dernier smoke, crons, validation), dépense et latence du mois, dernière sauvegarde et restauration testée, session Kitchen Daily, espace disque | non |
| `admin_capacite_activer(nom)` / `admin_capacite_desactiver(nom)` | `tools.capacites enable/disable` (+ régénération des timers via un script `scripts/appliquer-config.sh` lancé par systemd path/unit, voir §3) | oui (CONFIRMATION_REQUISE) |
| `admin_validation(nom, mode)` | `capacites.<nom>.validation: automatique|humaine` dans config.yaml | oui |
| `admin_cron(nom, cron, quand)` | heure d'un cron (`capacites.<nom>.crons.<cron>` ou `crons.<nom>` pour brief_jour/brief_hebdo), format « Wed 18:00 » ou « tous les jours 07:15 » (parser français simple → systemd OnCalendar) | oui |
| `admin_parametre(nom, cle, valeur)` | paramètre déclaré d'une capacité (= `capacites_parametrer`, alias pour cohérence) | non |
| `admin_reconnexion(nom)` | explique la procédure de reconnexion (Kitchen Daily : commande à lancer ; plus tard : lien vers la carte de l'appli) | non |
| `admin_pause()` / `admin_reprendre()` | équivalents de /stop /reprendre | oui |
| `admin_journal(n)` | n dernières actions d'administration (qui, quoi, quand, ancienne → nouvelle valeur) | non |
| `admin_annuler(id)` | retour arrière d'une action d'administration (valeur précédente restaurée) | oui |

Toute écriture dans `config.yaml` passe par `tools/config_edition.py` : lecture YAML, modification de la clé, **validation** (`bot.config.parse_config` doit réussir sur le résultat), écriture atomique avec sauvegarde `config.yaml.bak-<horodatage>` (10 conservées), journal `data/administration/journal.jsonl` (auteur, clé, avant, après). Jamais de secret dans config.yaml ; l'outil refuse toute clé hors liste blanche (`capacites.*.validation`, `capacites.*.crons.*`, `crons.*`, `capacites.*.parametres.*`).

## 2. Prise en compte à chaud

- `bot` : `Config` rechargée à chaque message si `config.yaml` a changé (mtime) ; `preparer_prompt_systeme` suit déjà.
- Timers systemd : `scripts/appliquer-config.sh` (root, idempotent) = la partie « timers » de `install.sh` extraite en fonction partagée ; déclenchée par une unité `homeos-appliquer-config.path` qui surveille `data/config.yaml` et `data/capacites/etat.json`. Le bot n'a donc jamais besoin de sudo pour ça.
- Capacité activée/désactivée : déjà à chaud (statut relu à chaque appel).

## 3. Comportement de Firmin (`capabilities/administration/CLAUDE.md`)

- Vocabulaire du foyer, jamais de nom de clé YAML : « rappel Kitchen Daily le mercredi à midi » → `admin_cron("kitchen_daily","rappel","Wed 12:00")`.
- Avant un changement avec effet, reformule en une ligne et passe par CONFIRMATION_REQUISE (sauf si `administration` est elle-même en validation automatique — déconseillé, à documenter).
- Après : « Fait. Le rappel Kitchen Daily est maintenant le mercredi à 12h (avant : mardi 18h). Tu peux annuler en me le disant. »
- « Qu'est-ce que tu sais faire ? / Où en est le système ? » → `admin_etat` résumé en langage courant.
- Demandes hors périmètre (« ajoute la gestion des desserts ») → « Ça demande un développement : je le note pour l'atelier (lot 7b) » + `feedback_noter(sujet="atelier", …)` ; ne jamais prétendre que c'est fait.
- Une personne non propriétaire → « Ce réglage est réservé aux propriétaires du foyer. »

## 4. Commande Telegram `/admin` → `admin_etat`. README : section « Administrer par la conversation » avec 6 exemples.

## 5. Tests

`tests/test_administration.py` : liste blanche des clés, validation refusée si config invalide, sauvegarde + retour arrière, rôle propriétaire, parser des horaires (« mercredi 12h », « tous les jours 7h15 », « Wed 18:00 »), rechargement à chaud de Config, journal. Suite complète verte, ruff, `bash -n`.

## 6. Hors périmètre (lot 7b)

Création ou modification de code par l'assistant, agent développeur, Playwright MCP, isolation `homeos-dev`.
