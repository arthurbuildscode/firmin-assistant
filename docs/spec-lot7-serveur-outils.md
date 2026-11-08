# Spécification lot 7 — serveur d'outils MCP persistant sous `homeos`

Origine : audit de sécurité du 7 novembre 2026, point 9 ; revue d'architecture R1, R3, P1. Hors du lot 5d
(trop gros), dont il est la suite logique : le lot 5d rend la lecture des secrets **difficile** (droits Unix,
dossiers de travail, règles de refus, wrapper) ; le lot 7 la rend **impossible** par construction.

## 1. Problème

Aujourd'hui, chaque message lance `claude -p` sous l'utilisateur de la personne, qui démarre son propre serveur
MCP stdio (`python -m tools.server`) **sous le même utilisateur**. Conséquences :

- tout ce que les outils doivent lire (URLs iCal, session Kitchen Daily, demain les identifiants du coffre) doit
  être lisible par l'utilisateur Linux de la personne, donc atteignable par sa session Claude si une règle saute ;
- le coffre (`COFFRE_CLE`) est réservé à `homeos` depuis le lot 5d : les capacités qui ont besoin
  d'identifiants ne fonctionnent pas en conversation, seulement dans les crons ;
- l'identité de l'appelant est l'uid du processus : correcte, mais chaque outil la redécouvre à sa façon ;
- coût : un démarrage Python (+ Chromium pour Kitchen Daily) à chaque message ;
- Chromium tourne sous l'utilisateur de la personne, avec accès à `data/`.

## 2. Cible

```
Telegram ─► bot (homeos) ─► sudo -u <personne> homeos-claude ─► claude -p
                                                                   │  MCP HTTP (127.0.0.1:8090, jeton de la personne)
                                                                   ▼
                                  homeos-outils.service (homeos) ── seul détenteur des secrets et de l'état
                                       │  Playwright (socket)            /etc/homeos/*.env, /var/lib/homeos
                                       ▼
                                  homeos-web.service (uid homeos-web) ── Chromium, aucun accès à data/
```

### 2.1 Service `homeos-outils`

- Unité systemd `homeos-outils.service`, `User=homeos`, profil strict du lot 5d (sans sudo : `NoNewPrivileges`,
  `ProtectSystem=strict`, `PrivateDevices`…), `MemoryMax=1G`.
- Transport MCP **Streamable HTTP** du SDK `mcp` (même code d'outils que `tools/server.py`), écoute
  `127.0.0.1:8090` uniquement (et/ou socket Unix `/run/homeos/outils.sock` 660 `homeos:foyer`).
- Charge les capacités une fois ; recharge à chaud sur `SIGHUP` (après `enable`/`disable`).
- Secrets : `EnvironmentFile=/etc/homeos/{ical,coffre}.env` ; `ical.env` repasse en `640 root:homeos`,
  `ical-<personne>.env` aussi (l'outil filtre par identité, §2.2) : plus aucun secret lisible par les personnes.
- État : `/var/lib/homeos` passe en `700 homeos:homeos` (plus besoin du groupe foyer pour `kd/`).

### 2.2 Identité de l'appelant : un jeton par personne

- `install.sh` génère pour chaque personne un jeton aléatoire (32 octets) stocké :
  - côté serveur : `/etc/homeos/outils-jetons.env` (`640 root:homeos`, `JETON_<linux_user>=…`) ;
  - côté personne : `/home/<personne>/.config/homeos/jeton` (`600 <personne>`), jamais dans `data/`.
- `tools/mcp.json` devient une config HTTP par personne générée à l'exécution par le wrapper :
  `{"mcpServers":{"homeos":{"type":"http","url":"http://127.0.0.1:8090/mcp","headers":{"Authorization":"Bearer ${HOMEOS_JETON}"}}}}`.
  Le wrapper `homeos-claude` lit le jeton de la personne (il tourne sous elle) et l'exporte dans
  l'environnement de `claude` uniquement (`HOMEOS_JETON`), jamais en argument.
- Le serveur résout `jeton → linux_user` (comparaison à temps constant) et place la personne dans un
  `contextvars.ContextVar` lu par `tools.capacites.personne_courante()`, `tools.agenda.utilisateur_courant()`,
  la journalisation (`actions_log.who`) et `ctx.secret()` (clés `<capacite>/<personne>`).
- Jeton absent ou inconnu → 401. Les crons et le bot (`homeos`) utilisent un jeton `homeos` qui n'ouvre ni
  les agendas privés ni les dossiers `prive/`.
- Risque résiduel accepté : la session Claude de la personne peut lire son propre jeton si une règle saute ;
  il ne lui donne que ses propres droits.

### 2.3 Chromium sous `homeos-web`

- Nouvel utilisateur système `homeos-web` (sans shell, sans groupe foyer), `homeos-web.service` : serveur
  Playwright (`playwright run-server` ou `launchServer`) sur socket Unix `/run/homeos-web/pw.sock`
  (`660 homeos-web:homeos`), `ProtectSystem=strict`, `ReadWritePaths=/var/lib/homeos-web`, `PrivateTmp`,
  `MemoryMax=1500M`, AppArmor `homeos-chromium` (sandbox activé).
- `tools.kitchen_daily` se connecte au navigateur distant (`chromium.connect`) et lui passe le
  `storage_state` en mémoire : le navigateur ne voit jamais `data/` ni `/var/lib/homeos`.
- Les captures reviennent au serveur d'outils (octets), qui les écrit dans `/var/lib/homeos/kd/captures`.
- Filtrage sortant (lot 5d) : `homeos-web` ajouté aux uid filtrés ; le proxy reste le seul chemin.

## 3. Migration des outils

| Étape | Contenu | Test |
| --- | --- | --- |
| 1 | `tools/server.py` : `main(transport="stdio"|"http")`, middleware d'authentification, `ContextVar` personne | tests unitaires du middleware (jeton valide, absent, d'une autre personne) |
| 2 | Outils : remplacer `getpass`/`HOMEOS_PERSONNE`/uid par la `ContextVar` | suite actuelle + tests « Alex ne voit pas l'agenda privé de Camille » via HTTP |
| 3 | `homeos-outils.service` + jetons (`install.sh`) ; wrapper : génération de la config MCP HTTP | `scripts/verifier.sh` : `claude -p "liste les courses"` par personne |
| 4 | Basculer `tools/mcp.json` (stdio → http) ; garder `mcp.dev.json` en stdio pour le dev | bot : message texte, vocal, PDF, Kitchen Daily |
| 5 | `homeos-web` + Playwright distant | `kd statut`, `kd menu`, application à blanc (`--dry-run`) |
| 6 | Resserrer les droits : `ical*.env` et `/var/lib/homeos` réservés à homeos ; retirer le groupe foyer de `kd/` | `scripts/verifier-secrets.sh` étendu : la personne ne lit plus `ical.env` |
| 7 | `LoadCredentialEncrypted=` pour le serveur d'outils et le bot (audit point 14) | `/proc/<pid>/environ` sans secret |

## 4. Impact sur le lot 5d

- `bot/claude_runner.construire_commande` : inchangé côté options ; `--mcp-config` pointe vers la config HTTP
  générée par le wrapper (le wrapper accepte alors `--mcp-config` absent et injecte la sienne).
- `install/bin/homeos-claude` : lit `~/.config/homeos/jeton`, écrit une config MCP temporaire dans
  `/var/lib/homeos/cwd/<personne>` ou la passe en JSON inline (`--mcp-config '{…}'` accepte du JSON) ;
  `HOMEOS_JETON` exporté seulement vers `claude`.
- `tools/securite.reglages_session` : ajouter `Read(//home/*/.config/homeos/**)` aux refus.
- Le mode `cle_api` (claude sous homeos) utilise le jeton de la personne concernée, lu par le bot dans
  `outils-jetons.env` : la personne en clé API retrouve ses agendas privés et ses identifiants.
- Le proxy filtrant ne change pas.

## 5. Plan de test

1. Unitaires (pytest, sans réseau) : middleware jeton, `ContextVar`, outils agenda/mémoire/capacités sous
   chaque identité, refus 401.
2. Intégration locale : serveur HTTP sur un port libre, client MCP du SDK, appels d'outils avec deux jetons.
3. Sur le VPS (`scripts/verifier.sh`) : `claude -p` par personne ; `verifier-secrets.sh` (la personne ne lit
   plus aucun `.env`) ; `reseau-test.sh` (homeos-web filtré) ; Kitchen Daily (`kd statut`, dry-run) ; un vocal,
   un PDF ; `systemctl kill homeos-outils` → redémarrage, message suivant OK.
4. Performance : latence médiane (`/statut`, revue P2) avant/après ; attendu −1 à −3 s par message.
5. Retour arrière : `tools/mcp.json` en stdio (une ligne), `systemctl disable --now homeos-outils homeos-web`.

## 6. Hors périmètre

Vaultwarden (exposé plus tard via Tailscale), appli web et ré-authentification « step-up » (CONTRACT §12),
auditd (audit point 13).

## 7. Réalisation (7 novembre 2026) et écarts

Livré : étapes 1 à 6 du §3 (l'étape 7, `LoadCredentialEncrypted=`, reste à faire ; le serveur d'outils lit déjà ses
secrets dans des fichiers, rien dans son environnement). Détail contraignant : CONTRACT.md §6 et §12 ; procédure :
README, section « Serveur d'outils ».

- Code : `tools/identite.py` (ContextVar de l'appelant, exécution des outils synchrones dans un fil avec copie du
  contexte), `tools/serveur_http.py` (jetons à temps constant, intergiciel 401, application Streamable HTTP sans
  état du SDK, SIGHUP, sonde), `tools/server.py` (`--transport stdio|http`, `recharger_capacites`),
  `tools/capacites.py` (`ctx.personne` et `ctx.config` dynamiques, `decharger`), `tools/kitchen_daily.py`
  (navigateur distant), `bot/claude_runner.py` (clé API), `install/bin/homeos-claude` (config HTTP générée),
  `scripts/jetons-outils.sh`, unités `homeos-outils.service` et `homeos-web.service`.
- Compatibilité : `python -m tools.server` reste en stdio ; `HOMEOS_OUTILS=stdio|http` dans
  `/etc/default/homeos-outils` choisit le chemin (wrapper, bot) ; `install.sh --outils=stdio` est le retour arrière.
  SDK `mcp` 1.x (testé 1.30) et 2.x (testé 2.3).

Écarts par rapport au §2 :

1. **Jeton dans la config MCP plutôt qu'expansé** : le wrapper écrit `~/.config/homeos/mcp.json` (600) avec le jeton
   en clair au lieu d'exporter `HOMEOS_JETON` et d'écrire `${HOMEOS_JETON}`. La documentation de Claude Code décrit
   l'expansion `${VAR}` dans `url` et `headers` pour `.mcp.json` sans la garantir pour `--mcp-config` ; ce choix ne
   dépend pas de ce détail et protège le jeton de la même façon (fichier 600 de la personne, jamais en argument).
   `tools/mcp.http.json` garde la forme `${HOMEOS_JETON}` pour les essais à la main.
2. **`/var/lib/homeos` en 700 : dossiers de travail déplacés** dans `/var/lib/homeos-cwd/<personne>` (750
   root:foyer), sinon les sessions Claude ne pourraient plus entrer dans leur `cwd`. Les sessions Claude repartent de
   zéro une fois (le bot gère la reprise impossible).
3. **Pas de socket Unix** : Claude Code joint un serveur MCP HTTP par URL TCP ; écoute `127.0.0.1:8090` seulement,
   le jeton fait la frontière. De même `playwright run-server` n'écoute qu'en TCP : `127.0.0.1:8091` avec un chemin
   secret de 48 caractères hexadécimaux (`/etc/homeos/navigateur.env`).
4. **Sandbox Chromium** : `playwright run-server` refuse `chromiumSandbox` venant du client sans `--unsafe` (qui
   permettrait aussi `executablePath`) : Chromium tourne sans son sandbox interne, l'isolation est celle de l'unité
   (`homeos-web` sans données, `ProtectSystem=strict`, `ProtectHome`, `InaccessiblePaths`, filtrage sortant).
5. **Outils des capacités réservées** : le serveur sert tout le foyer, les outils d'une capacité `personnes: [x]`
   restent listés pour les autres ; l'appel est refusé (« n'est pas ouverte à cette personne »).
6. **SIGHUP** recharge jetons et capacités ; un changement du code des outils de base demande
   `systemctl restart homeos-outils`. `homeos-appliquer-config` envoie le SIGHUP à chaque changement de
   `config.yaml` ou `etat.json`.
7. **`MemoryMax`** : 1 Go pour `homeos-outils` (Chromium dans `homeos-web`, 1,5 Go) ; en repli
   `HOMEOS_NAVIGATEUR=local`, prévoir 2 Go. Un seul Chromium à la fois par processus.
