# Atelier — l'assistant fabrique ses propres capacités (lot 7b)

« Ajoute Planity pour prendre les rendez-vous coiffeur » : un propriétaire du foyer le demande en conversation,
l'assistant pose trois questions, reformule, et après le bouton Valider un **sous-agent Claude Code** écrit la
capacité en arrière-plan. Elle est contrôlée, testée, relue par un second agent, puis installée **en essai** ; sa
première action avec effet est validée à la main par un propriétaire, et seulement alors elle devient active.

Code : `tools/atelier.py` (file, fabrication, contrôles, relecture, installation ; capacités et fournisseurs de
transports), `tools/navigation.py` (navigation
sûre des connecteurs), `capabilities/atelier/` (outils `atelier_*`), `content/atelier/SYSTEM.md` (contrat donné au
sous-agent), `content/atelier/RELECTURE.md` (grille du relecteur), mode atelier du wrapper `install/bin/homeos-claude`,
unités `homeos-atelier{,-installer}.{path,service}`. Contrat : CONTRACT.md §9 « Atelier ».

## Parcours en conversation

1. « Ajoute Planity pour prendre les rendez-vous coiffeur. »
2. Questions de découverte, **une à la fois**, en sautant ce qui est déjà connu : quel site, quel compte (le sien ou
   celui du foyer), quelle action attendue.
3. Reformulation et bouton Valider (`atelier_creer` → `CONFIRMATION_REQUISE`, kind `atelier_creer`, toujours par
   bouton, propriétaires seulement) : « Fabriquer la capacité « Planity » : réserver une coupe sur www.planity.com
   (compte d'Alex). Planity parlera à www.planity.com. Elle restera en essai jusqu'à sa première action validée à la
   main. » Valider, c'est aussi **approuver le site**.
4. « C'est parti : je fabrique « Planity » en arrière-plan. » Dans l'appli, la ligne de réflexion (événement
   `etape`) affiche « Je prépare la demande pour l'atelier… » ; ensuite `atelier_etat` dit où on en est (« je la
   fais relire »).
5. Fin, par un message Telegram privé au demandeur (et une ligne au journal du foyer) :
   - « Capacité « Planity » prête à l'essai. Il me faut tes identifiants Planity : dis-moi « mes identifiants
     Planity » et je t'ouvre la carte sécurisée. Sa première action attendra ta validation. » → l'assistant appelle
     `identifiants_preparer(…, capacite="planity")`, la carte sécurisée s'ouvre dans l'appli ;
   - ou « Ça n'a pas marché pour « Planity » : Je n'ai pas réussi à ouvrir le site. Je garde ce que j'ai appris pour
     l'atelier. » Rien n'est jamais présenté comme fait.
6. « Réserve-moi la coupe de mardi 10h » → `planity_reserver` : en essai, le **cadre** crée lui-même l'action à
   valider (payload = arguments de l'outil, l'outil n'est pas appelé) ; seul un **propriétaire** peut la valider
   (step-up dans l'appli) ; si elle réussit, la capacité passe **active** (`validee_par`). Un échec la laisse en essai.
7. Sites supplémentaires (un CDN, une API) : « Elle voudrait aussi parler à cdn.planity.com » → accord →
   `atelier_approuver_reseau`.
8. Retrait : « enlève Planity » → `admin_capacite_retirer` (code archivé, données conservées, pas d'annulation).

## Chaîne de fabrication

```
atelier_creer ──Valider──► runner : etat.json (site approuvé, en cours) + _file/<nom>.json
                                  │ homeos-atelier.path
                                  ▼
homeos-atelier.service (bac à sable, homeos)                         homeos-atelier-installer.service (homeos)
  1. exploration publique (tools.navigation.explorer) :               5. copie figée des seuls fichiers autorisés
     robots.txt, CGU, HTML sans scripts, aria, capture, réseau           (atelier-relecture/<nom>)
  2. sous-agent : homeos-claude --homeos-atelier <nom>                6. contrôles statiques + ruff refaits
     --homeos-atelier-mode generer (cwd atelier/<nom>)                7. relecteur : homeos-claude … --homeos-atelier-mode
  3. contrôles statiques, ruff, tests (pytest), smoke test              relire (lecture seule) → {"verdict": "ok"}
     (sous-processus isolés) ; un tour de correction au plus          8. installation en essai (capacites-atelier/<nom>),
  4. _resultats/<nom>.json ──homeos-atelier-installer.path──►            paramètre `site`, journal, message
```

Sans systemd (développement) : `python -m tools.atelier traiter` est lancé en processus détaché ;
`HOMEOS_ATELIER_LANCEMENT=synchrone` fait tout dans l'appel (tests).

## Ce que produit le sous-agent

Le contrat du lot 5 (`CONTRACT.md` §9) sans écart : `capability.yaml`, `tools.py` (les « outils » : lecture d'abord,
actions ensuite), `connecteur.py` (navigation via `tools.navigation.Session`), `smoke.py`, `CLAUDE.md` (les consignes
données au prompt quand la capacité est chargée — le « skill » de la capacité), `README.md` (exploration, robots.txt,
CGU), `tests/`. Exemple complet : `tests/fixtures/atelier/salon_demo/` (ce que le faux sous-agent des tests recopie).

Règles imposées (prompt `content/atelier/SYSTEM.md`, puis vérifiées par `verifier_candidat`) :

- `origine: assistant`, `statut: essai`, `personnes: []`, nom et préfixe = ceux de la demande ;
- secrets typés, clé `<nom>/…`, `par_personne: true` pour un compte personnel ; aucune valeur, e-mail, chemin système
  ni donnée personnelle dans le code ;
- **toute action avec effet est en `confirmation_toujours`** ; un paiement sans `confirmation_toujours` est refusé ;
  l'outil d'une action ne fait que `ctx.demander_confirmation(...)` (payload = ses arguments), l'effet est dans
  `executer` et n'est annoncé qu'une fois confirmé par le site ;
- paramètre obligatoire `site` (rempli à l'installation) ; une routine (`crons`) déclare son seuil « ça vaut le
  coup » (`parametres: seuil_…`) ;
- imports limités (bibliothèque standard utile, `bs4`, `tools.navigation`) : ni `os`, `subprocess`, `socket`,
  `requests`, `urllib.request`, `open()`, `eval`, ni module interne (`tools.coffre`, `tools.db`…) ;
- README qui dit ce que permettent robots.txt et les conditions d'utilisation ;
- demande ou description qui vise l'espace professionnel d'une personne : refusée (à la demande et à la vérification).

## Navigation : `tools/navigation.py`

`Session.pour(ctx)` : navigateur isolé `homeos-web` (ou Chromium local en développement), un seul à la fois ;
**hôtes du manifeste approuvés seulement** (toute autre requête est bloquée et journalisée) ; https hors poste local ;
**robots.txt** lu et respecté ; **pas de contournement anti-bot** (page de vérification humaine → arrêt, « je te rends
la main ») ; bannières de cookies refusées ; attente d'hydratation ; écoute des réponses JSON (`ecouter_json`) ;
captures, arbre d'accessibilité et journal (`<etat>/capacites-etat/<nom>/`, secrets masqués) ; `echec()` range
capture + instantané + requêtes dans `incidents/` (« ce que l'atelier a appris ») ; état de session par personne
(600).

## Sécurité

| Risque | Parade |
| --- | --- |
| Le code généré lit les secrets ou les données du foyer | Il ne s'exécute que dans `homeos-atelier.service` (`/srv/home-os/data` inaccessible, `/var/lib/homeos` réduit à `atelier/`, `/etc/homeos` réduit à la clé API et à l'adresse du navigateur) puis, une fois installé, dans le serveur d'outils sous homeos avec le profil strict du lot 5d ; il n'importe que `tools.navigation`. |
| Le sous-agent sort de son dossier | Wrapper root : utilisateur homeos, cwd imposé et possédé par homeos, outils `Read,Glob,Grep,Edit(./**),Write(./**),Bash(python -m pytest:*),Bash(ruff check:*),Bash(python -m tools.navigation explorer:*)`, `--bare`, `dontAsk`, lecture bloquée hors du dossier, refus explicites, budget et tours plafonnés ; bac à sable systemd. |
| Le code généré falsifie son verdict | La relecture et l'installation se font dans un autre service, sur une copie figée des seuls fichiers autorisés, contrôles refaits ; le verdict vient de la sortie du relecteur, jamais d'un fichier. |
| La capacité joint un autre site | `reseau:` approuvé par un propriétaire (demande, puis `atelier_approuver_reseau`) ; liste blanche du proxy régénérée par `homeos-appliquer-config` ; `Session` bloque tout autre hôte. |
| Une action part sans humain | Essai : l'outil n'est même pas appelé, le cadre crée la validation ; premier clic réservé aux propriétaires (step-up), jamais automatique ; ensuite `confirmation_toujours`. |
| Le code installé est modifié après coup | `<etat>/capacites-atelier/` (homeos, 750) : jamais modifiable par les personnes ni par le bac à sable de fabrication. |
| Prétendre que c'est fait | Messages de fin écrits par le pipeline, pas par le modèle ; échec dit simplement ; effet annoncé seulement après confirmation du site. |

Journal : `actions_log` (`atelier_creer`, `atelier_pret`, `atelier_echec`, `capacite_validee_<nom>`, chaque appel
d'outil), journal du foyer, `<etat>/atelier/_suivi/<nom>/` (progression, apprentissages, verdict du relecteur).

Risques résiduels acceptés : la clé API est visible du code généré pendant la fabrication (nécessaire à claude ;
sortie limitée par le proxy filtrant) ; les captures d'écran peuvent montrer des données du compte (dossier 700 de
homeos, rotation à 60) ; un site sans robots.txt est considéré comme autorisé.

## Second type de fabrication : un fournisseur de transports

« Ajoute le métro de Lyon » : la capacité Transports (`docs/transports.md`) ne connaît pas ce réseau. Si la personne a
les adresses de ses données ouvertes (GTFS, GTFS-RT), `transports_reseau_ajouter` suffit, sans atelier. Sinon l'atelier
fabrique un **fournisseur** — un réseau de la capacité transports, pas une capacité entière :

1. Découverte, une question à la fois : le site du réseau (ou de ses données ouvertes), les modes (bus, métro, tram…),
   la ville si utile. Pas de question de compte : un fournisseur ne fait que lire.
2. `atelier_creer(demande, site, action, titre, fabrication="fournisseur_transports", modes, zone)` →
   `CONFIRMATION_REQUISE` : « Ajouter le réseau « Métro de Lyon » (metro, tram) aux transports : … Métro de Lyon
   parlera à data.exemple.fr, en lecture seule. » Valider approuve l'hôte **pour la capacité transports**
   (`etat.json` : `transports.reseau_approuve`, `transports.fournisseurs_atelier.<nom>`).
3. Fabrication : exploration publique comme pour une capacité, puis le dossier de travail est **amorcé avec le gabarit**
   `content/atelier/gabarits/fournisseur_transports/` (`fournisseur.yaml`, `source.py` qui implémente l'interface
   commune `capabilities/transports/interface.py`, `README.md`, `tests/test_source.py` + une réponse capturée) ; le
   sous-agent l'adapte au réseau (section « Second type » de `content/atelier/SYSTEM.md`) : de préférence
   `class Source(interface.SourceGtfs)` si le réseau publie du GTFS, sinon une API publique via `self.client.json`, en
   dernier recours des pages publiques via `self.client.navigation()`.
4. Contrôles (`verifier_fournisseur`) : fichiers autorisés seulement (`fournisseur.yaml`, `source.py`, `README.md`,
   `tests/`), manifeste valide avec `origine: assistant`, source `api_ouverte` ou `page_web`, **ni secret ni action
   avec effet**, `class Source`, hôte approuvé déclaré (tout autre hôte attend `atelier_approuver_reseau`), README
   sur robots.txt et les conditions d'utilisation ou la licence, imports d'une capacité plus
   `capabilities.transports.interface` ; puis ruff, tests, smoke test isolé (`smoke-candidat --fournisseur`, hôte
   approuvé seulement), relecteur (section dédiée de `RELECTURE.md`).
5. Installation dans `<etat>/fournisseurs-atelier/<nom>/` (`HOMEOS_FOURNISSEURS_ATELIER`) : lecture seule, utilisable
   tout de suite (pas de « première action » : il n'y en a pas). Message : « Réseau « Métro de Lyon » prêt : dis-moi
   ton trajet (d'où, vers où, à quelle heure) et je le note. »
6. Retrait : `python -m tools.atelier retirer-fournisseur <nom>` (archivé dans `<etat>/atelier/retirees/`, hôte retiré,
   trajets conservés sans suivi).

Test de bout en bout : `tests/test_atelier_transports.py` (faux sous-agent en mode `garder` : le gabarit amorcé passe
tel quel tous les contrôles, ruff, ses tests et son smoke test contre un faux site local).

## Démonstration (faux site local)

`tests/fixtures/faux_rdv/serveur.py` simule une prise de rendez-vous (connexion, créneaux hydratés en JavaScript
depuis une API JSON, réservation, robots.txt, page anti-robot). `tests/test_atelier.py::test_demonstration_de_bout_en_bout`
fait tout le parcours avec un vrai Chromium : demande → fabrication (faux sous-agent qui recopie le modèle) →
exploration, contrôles, ruff, tests, smoke test → relecture → installation en essai → carte d'identifiants →
créneaux lus sur le site → réservation en `CONFIRMATION_REQUISE` → refus pour un membre → validation par un
propriétaire → réservation effective, capacité active → retrait. Le vrai Planity n'est jamais touché.

À la main : `python tests/fixtures/faux_rdv/serveur.py --port 8765`, puis demander à l'assistant d'ajouter le
« salon démo » sur `http://127.0.0.1:8765`.

## Exploitation

- `python -m tools.atelier etat [nom]` : progression ; `journalctl -u homeos-atelier -u homeos-atelier-installer`.
- Un travail bloqué plus de 2 h fait échouer le smoke test de la capacité `atelier` (healthcheck).
- Relancer : redemander en conversation (les apprentissages du précédent essai sont donnés au sous-agent).
- Retirer : `admin_capacite_retirer` (archive dans `<etat>/atelier/retirees/`).

## Hors périmètre

Réparation automatique d'une capacité cassée (boucle « incident → correctif → validation », étude interne
sur la navigation web par IA, §3.3), exploration connectée avant la première version (la première
version part des pages publiques ; l'échec de la première action laisse un incident exploitable), signature des
capacités.
