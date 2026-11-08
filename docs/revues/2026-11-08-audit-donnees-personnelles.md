# Audit « zéro donnée personnelle » — lot 6 (8 nov. 2026)

Objectif : que Firmin Assistant soit installable par un autre foyer sans aucune donnée des premiers utilisateurs, en achat
unique, open source. Ce rapport ne recopie volontairement aucune des valeurs retirées : elles sont désignées par
leur catégorie.

## Méthode

1. Recherche exhaustive dans tous les fichiers suivis par git (hors `data/`, jamais versionné) : prénoms et nom de
   famille du foyer, prénoms d'enfants, nom de l'animal, commune et communes voisines, arrêts et ligne de bus du
   trajet quotidien, nom d'hôte et adresse IP du VPS, nom du réseau Tailscale, adresses mail, numéros, employeur,
   compte GitHub, numéro de livraison Kitchen Daily, et l'ancien nom du produit (qui contenait la commune).
2. Recherche de motifs génériques : identifiants Telegram numériques, noms de machine Tailscale, noms d'hôte d'hébergeur, mails de
   messageries grand public, téléphones, IPv4 publiques.
3. Remplacement dans le code, les prompts, les tests, les exemples et la documentation ; déplacement des
   documents datés ; puis contrôle automatique permanent (`tests/test_generique.py`).

## Occurrences trouvées et corrigées

Comptage sur `main` (61d00c8), hors documents datés, maquette (lot 4b) et page de présentation, puis après le lot 6.

| Catégorie | Avant | Après | Traitement |
| --- | --- | --- | --- |
| Prénoms des deux adultes (affichage et identifiants Linux) | 251 dans 36 fichiers | 7 (tous dans `tools/coffre.py`, lot 5c) | prompts : `{{foyer.adultes}}` rendu depuis `config.yaml` ; exemples, tests, fixtures : « Alex », « Camille » ; `tools/memoire.py` : en-têtes des fiches tirés de la configuration |
| Nom de famille | 4 dans 3 fichiers | 0 | spécifications anonymisées (« premiers utilisateurs ») |
| Commune, communes voisines, arrêts du trajet quotidien, ancien nom du produit | 346 dans 63 fichiers | 3 (définition du réseau de bus, voir plus bas) | produit renommé « Firmin Assistant » (unités systemd, en-têtes, CONTRACT, README) ; captures du service bus anonymisées (« Villeneuve », « Grandport », « Pont du Moulin », noms d'arrêts et de lignes fictifs) ; exemples des prompts neutres |
| Nom de l'animal | 3 dans 2 fichiers | 0 | « Rex » |
| Nom d'hôte du VPS et hébergeur | 6 dans 5 fichiers | 0 | `install/vaultwarden.sh` n'a plus de domaine par défaut (`--tailscale` ou `--domaine` obligatoire) ; « console de l'hébergeur » |
| Adresses mail (exemples d'URL iCal) | 4 dans 3 fichiers | 0 | domaine `exemple.fr` (les 5 « gmail » restants sont le nom d'un connecteur et les motifs du contrôle) |
| Numéro de livraison Kitchen Daily | 7 dans 3 fichiers | 0 | numéros factices |
| Trajet de bus du foyer (ligne, sens, arrêt) dans les prompts, docstrings et le test réseau | 13 fichiers | 0 | exemples « ligne 12, Mairie, vers la gare » ; le test réseau prend sa ligne dans l'environnement ou dans la grille |
| Rythme réel d'un adulte cité en exemple dans `content/CLAUDE.md` | 2 | 0 | exemple neutre |
| Identifiants Telegram réels, IP, nom Tailscale | 0 | 0 | seules des valeurs factices (111111111, -100123456789) existent ; le contrôle les tolère et refuse toute autre |

Prénom de l'assistant : il n'est plus écrit nulle part dans le code. Défaut unique `NOM_ASSISTANT_DEFAUT = "Firmin"`
(`tools/common.py`, utilisé par `bot/config.py` et `tools/runner.py`) ;
prompts, bot, appli web et cartes passent par `assistant.nom` (`{{assistant.nom}}` ou `cfg.assistant_nom`). Le
contrôle refuse « Firmin » écrit en dur dans un prompt. Restent légitimes : le nom du produit « Firmin Assistant »
et le nom de machine Tailscale par défaut de `install/tailscale.sh` (l'installateur passe le prénom choisi).

Arrivé de `main` pendant le lot 6 (lot 5c et revue de sécurité de l'appli web) et traité au rebase : code postal
de la commune dans un test du filtre de données sensibles (remplacé), prénoms dans `CONTRACT.md`, `docs/coffre.md`
et `tests/test_identifiants.py`, revue datée de l'appli web (nom Tailscale du serveur, prénom) rangée dans les notes internes, hors paquet.

## Documents déplacés

Les revues, retours et études datés qui citent les premiers utilisateurs sont rangés dans des notes internes,
exclues du paquet (`scripts/empaqueter.sh`) et du contrôle. Les nouvelles revues anonymes (comme celle-ci) restent dans `docs/revues/`.

## Contrôle permanent : `tests/test_generique.py`

- chaque prompt (`content/CLAUDE.md`, `content/prompts/`, `content/onboarding/`, fragments et onboarding des
  capacités) est rendu avec une configuration fictive : plus aucune variable `{{…}}`, pas de prénom d'assistant
  en dur ;
- tout le dépôt (ou l'archive extraite, hors git) est parcouru : motifs génériques toujours, plus les motifs propres
  au foyer lus **hors dépôt** (`HOMEOS_MOTIFS_INTERDITS="a,b"` ou `HOMEOS_MOTIFS_INTERDITS_FICHIER=~/motifs.txt`,
  un motif par ligne). Vérifié vert avec la liste des premiers utilisateurs (prénoms, nom, commune, code postal, enfants,
  hôte, IP, tailnet, employeur, arrêts). Choisir des motifs qui ne sont pas des mots courants : le nom de l'animal
  est un mot courant, il ne peut pas figurer dans la liste.
- `scripts/empaqueter.sh` relance ce test sur le contenu exact de l'archive et refuse de la produire en cas d'échec.

## Ce qui reste (exclusions temporaires, à lever)

| Élément | Pourquoi pas corrigé ici | Action |
| --- | --- | --- |
| Maquette de travail et sa spécification (`docs/spec-lot4b-maquette-v2.md`) : ~110 occurrences | front en cours sur la branche lot4b | exclues du paquet ; à anonymiser (données de démo « Alex/Camille ») ou à ranger dans les notes internes, puis retirer de `EXCLUS_TEMPORAIRES` |
| `tools/coffre.py` : exemples `planity/<prénom>` dans les docstrings et l'aide (7) | fichier réservé au lot 5c | remplacer par `planity/alex` à la fusion du lot 5c |
| `capabilities/transports/fournisseurs/cartreize/` (définition du réseau) | le seul réseau pris en charge est régional : c'est la définition du connecteur, pas une donnée du foyer, mais il situe le département | décision ci-dessous |
| Historique git | les anciens commits contiennent les données retirées (messages, maquette, captures) | le paquet est fait par `git archive` (aucun historique) ; un dépôt public doit repartir d'un historique neuf |
| Page de présentation (`docs/landing/`) | aucune donnée personnelle (prénoms fictifs) ; mais elle annonce « licence propriétaire » et « 15 minutes » | à aligner sur les décisions de licence et sur les 30 minutes du README |
| Catégorie de rangement `chien` (`tools/fichiers.py`) | générique mais supposait un chien | fait : renommée `animaux`, migration automatique (`python -m tools.fichiers migrer`, lancée par `install.sh` et `appliquer-config`) |

## Décisions à prendre (porteur du projet)

1. **Licence.** Aucun fichier de licence dans le dépôt. La spécification du lot 6 prévoit une licence propriétaire
   (un foyer, pas de redistribution) ; la demande actuelle dit « open source, achat unique ». Proposition :
   **AGPL-3.0** (code ouvert, toute version modifiée mise à disposition, y compris en service hébergé), vendue en
   achat unique pour l'accompagnement et les mises à jour. Non posée : `scripts/empaqueter.sh` signale l'absence
   de licence. La page de présentation dit encore « licence propriétaire ».
2. **Prix** du paquet (achat unique) : non fixé ; la page de présentation l'affiche à compléter.
3. **Nom par défaut** de l'assistant : « Firmin » (une constante) ; le nom du produit « Firmin Assistant » est
   distinct. À confirmer, ainsi que le nom de machine Tailscale par défaut.
4. **Réseau de bus** : garder Cartreize comme réseau fourni (et l'assumer dans la description) ou rendre le réseau
   paramétrable par foyer (`data/capacites/bus/`) et ne livrer aucun réseau par défaut.
5. **Dépôt public** : créer un nouveau dépôt à partir de l'archive (historique neuf), le dépôt actuel restant celui
   des premiers utilisateurs.

## Annexe — installation à blanc (8 nov. 2026)

Environnement : conteneur Docker `ubuntu:24.04` avec systemd en PID 1 (privilégié, cgroups de l'hôte), complété
comme une image d'hébergeur (openssh-server, sudo, curl, python3, ufw, compte `admin`). Particularité de
l'environnement de test, pas du produit : la sortie réseau passe par un proxy HTTPS seulement, d'où des dépôts
Ubuntu en `https://` et les variables `HTTPS_PROXY` / certificat du proxy pour les téléchargements.

Réponses factices (`--reponses`, `--oui`) : deux adultes (Alex en abonnement, Camille en clé API), un enfant, un
animal, commune « Villeneuve », faux token Telegram, faux groupe, pas de clé API, ni Kitchen Daily, ni bus, ni
Tailscale.

| Scénario | Résultat |
| --- | --- |
| Serveur vierge, kit installé depuis l'archive `firmin-assistant-0.6.0.tar.gz` produite par `scripts/empaqueter.sh` | installation complète, `scripts/verifier.sh --sans-claude` **vert** (pytest 377 tests sous `homeos`, ruff, bash -n, administration, serveur d'outils avec jetons de chaque personne, étanchéité des secrets) |
| Relance sur un serveur déjà installé, réponses modifiées (autre prénom d'assistant, autre heure de brief) | `config.yaml` sauvegardé puis remplacé, fiche d'identité conservée, capacités non choisies restées inactives (service bus arrêté), secrets en 640 root:homeos, dossier temporaire des réponses effacé ; vérification **verte** |
| Première passe (avant correctif) | seul échec : le test générique trouvait ses propres exemples ; corrigé |
| Questionnaire au clavier | parcours complet et abandon (Ctrl+D) testés par `tests/test_installateur.py` (entrées tapées une à une) ; validation des réponses incohérentes testée |

Non testé ici, « à faire par le foyer » (demande un compte réel ou un téléphone) : connexion des comptes Claude
(`claude auth login`), bot Telegram avec un vrai token (le faux token est bien signalé : « le bot ne démarre
pas »), `/start` et première séance, briefs (clé API), Tailscale et donc appli web, Piper servi, Vaultwarden,
copie hors site (rclone), `--fermer-root`. Non testé non plus : les confirmations interactives du script shell
lui-même (connexion Claude proposée, remplacement de configuration) hors mode `--oui`, et une vraie VM (zram,
AppArmor et le redémarrage automatique ne s'appliquent pas dans un conteneur).
