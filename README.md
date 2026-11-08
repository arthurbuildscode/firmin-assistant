# Firmin Assistant

**Pour qui.** Une famille (ou toute maison partagée) qui veut un assistant commun, sans confier sa vie
quotidienne à une application tierce : il s'installe sur **votre** serveur et ne parle qu'aux personnes que
vous déclarez.

**Ce que ça fait.** On lui parle dans Telegram ou dans une appli web privée, par écrit ou en vocal. Il tient
l'agenda, la liste de courses, les échéances (contrôle technique, vaccins, assurances) et les anniversaires ;
il range les photos et papiers qu'on lui envoie ; il envoie un brief chaque matin et chaque dimanche ; il retient
les goûts et les règles de la maison. Des capacités s'ajoutent (plats livrés, transports, administration en
conversation). Il propose, vous validez : rien d'engageant ne part sans un bouton « Valider ».

**Ce que ça coûte.** Un petit serveur (VPS Ubuntu 24.04, 2 processeurs, 4 Go : 5 à 10 € par mois chez un
hébergeur) et **l'abonnement Claude** de chaque adulte (ou une clé API Anthropic payée à l'usage), plus quelques
euros de clé API par mois pour les briefs automatiques. Le logiciel s'achète une fois ; son prénom par défaut,
Firmin, se change à l'installation.

**30 minutes d'installation.** Sur un Ubuntu 24.04 vierge :

```bash
sudo bash install/installer-un-foyer.sh
```

Le script pose les questions en français (prénom de l'assistant, adultes, enfants, animaux, commune, Telegram,
compte Claude de chacun, Tailscale, coffre, sauvegardes), installe et vérifie tout, puis donne la liste de ce qui
reste à faire avec un téléphone. Guide pas à pas avec les écrans : [docs/installation.md](docs/installation.md).
Nouveautés : [CHANGELOG.md](CHANGELOG.md). Architecture détaillée : [CONTRACT.md](CONTRACT.md).

La suite de ce document s'adresse à la personne qui exploite le serveur (installation manuelle, mises à jour,
dépannage).

## Installation manuelle, étape par étape

### 0. Prérequis

- Un VPS Ubuntu 24.04 (2 vCPU, 4 Go RAM) avec un compte d'administration **`admin`** (groupe `sudo`, connexion
  SSH par clé ed25519 avec phrase de passe). Root SSH sera fermé à la fin (étape 9) : toute l'installation se
  fait depuis `admin`, avec `sudo`. Secours : la console de l'hébergeur garde l'accès root.
- Un **bot Telegram** : dans Telegram, ouvrir `@BotFather` → `/newbot` → noter le token (`123456789:AAE...`).
- Le **telegram_id** de chaque personne : écrire à `@userinfobot`, il répond avec l'`Id`.
- Un **groupe Telegram** du foyer : le créer, y ajouter le bot et `@userinfobot` (qui donne l'ID du groupe, négatif, du type `-100…`), puis retirer `@userinfobot`. Dans BotFather, `/setprivacy` → `Disable` pour que le bot lise les messages du groupe.
- Les **URLs iCal secrètes** des agendas Google : Google Agenda → Paramètres de l'agenda → « Adresse secrète au format iCal » (une par agenda).
- Une **clé API Anthropic** (console.anthropic.com → API keys) pour les tâches programmées.
- Les identifiants des comptes Claude (claude.ai) de chaque personne, sur son téléphone.

### 1. Copier le kit sur le VPS

Depuis ton ordinateur, au choix :

```bash
scp -r firmin-assistant/ admin@VPS:~/firmin-assistant
# ou
ssh admin@VPS git clone https://github.com/<toi>/firmin-assistant.git ~/firmin-assistant
```

### 2. Installer

```bash
ssh admin@VPS
sudo bash ~/firmin-assistant/install/install.sh
```

Le script est idempotent (relançable). Il installe les paquets, Node (dépôt NodeSource ajouté explicitement),
Claude Code et Playwright **aux versions épinglées** de `install/versions.env`, crée l'utilisateur de service
`homeos` et un utilisateur Linux par personne de `data/config.yaml` (groupe `foyer`, sans shell de connexion),
copie le kit dans `/srv/home-os`, prépare le `.venv`, range les secrets dans **`/etc/homeos`** (un fichier par
usage) et l'état sensible dans **`/var/lib/homeos`**, installe le garde-fou `/usr/local/sbin/homeos-claude` et
le sudoers restreint, les services systemd durcis, ufw (SSH limité), fail2ban, les mises à jour automatiques
(redémarrage à 04:40 si nécessaire), zram, le dépôt restic, la clé du coffre, puis vérifie l'étanchéité des
secrets. À la fin il affiche les étapes manuelles restantes.

Mise à niveau d'une installation antérieure au lot 5d : la même commande migre automatiquement l'ancien
`data/secrets.env` vers `/etc/homeos/*.env` (copie conservée en `data/secrets.env.migre`, 600 root, à supprimer
quand tout fonctionne) et déplace `.kd/`, `coffre.json`, `sessions.json` dans `/var/lib/homeos` (les
conversations repartent de zéro une fois). Voir « Mise à jour » plus bas pour l'ordre exact.

### 3. Remplir les secrets et la configuration

```bash
sudo nano /srv/home-os/data/config.yaml     # telegram_id des personnes, groupe_telegram_id, agendas
sudo bash /srv/home-os/install/install.sh   # si des personnes ou des agendas ont été ajoutés
sudo nano /etc/homeos/telegram.env          # TELEGRAM_BOT_TOKEN
sudo nano /etc/homeos/anthropic.env         # ANTHROPIC_API_KEY
sudo nano /etc/homeos/ical.env              # URLs iCal des agendas communs (ICAL_FOYER, …)
```

Un agenda marqué `prive: <utilisateur>` dans `config.yaml` a son URL dans `/etc/homeos/ical-<utilisateur>.env` :
seule cette personne le voit (jamais l'autre, jamais les briefs du groupe).

Ne pas toucher à `RESTIC_PASSWORD` (`/etc/homeos/restic.env`) ni à `COFFRE_CLE` (`/etc/homeos/coffre.env`),
générés : les **copier dans vos gestionnaires de mots de passe**. Ils ne sont jamais sauvegardés et sont
indispensables pour restaurer (`docs/sauvegardes.md`). Modèle commenté de tous les fichiers : `install/env.example`.

### 4. Connecter les comptes Claude (une section par personne)

Chaque personne se connecte **avec son propre compte Claude**, dans son utilisateur Linux. La session est
stockée dans `~/.claude` de cet utilisateur et sert au bot pour répondre à cette personne. Les utilisateurs des
personnes n'ont pas de shell : on passe par `sudo -u <utilisateur> -H` (et non `sudo -iu`, refusé).

**Alex**

```bash
sudo -u alex -H claude auth login
```

La commande affiche une URL `https://claude.ai/oauth/authorize?...`. L'ouvrir **sur le téléphone d'Alex** (ou tout navigateur connecté au compte Claude d'Alex), accepter, puis coller le code renvoyé dans le terminal si demandé. Vérifier :

```bash
sudo -u alex -H claude auth status
```

**Camille**

```bash
sudo -u camille -H claude auth login
sudo -u camille -H claude auth status
```

Même procédure, avec le téléphone et le compte Claude de **Camille**. Attention : si le navigateur est déjà connecté au compte d'Alex, se déconnecter d'abord, sinon les deux utilisateurs partageront le même compte.

### 5. Démarrer le bot

```bash
sudo systemctl restart homeos-bot
sudo systemctl status homeos-bot --no-pager
```

### 6. Premier test

Dans Telegram, en privé avec le bot : `/start`. L'assistant se présente et lance l'onboarding (séance 1 à faire dans le groupe, à deux). Puis `/aide` pour la liste des commandes.

### 7. Vérifier

```bash
sudo bash /srv/home-os/scripts/verifier.sh  # pytest, ruff, étanchéité des secrets, claude -p "ok" par personne
journalctl -u homeos-bot -f                  # logs du bot en direct
systemctl list-timers 'homeos-*'             # briefs, sauvegarde, healthcheck, restauration, vérifications, capacités
sudo systemctl start homeos-brief-jour       # forcer un brief du jour
journalctl -u homeos-healthcheck -n 20       # dernier healthcheck
systemctl status homeos-transports-cartreize # service local du réseau Cartreize (transports, s'il est utilisé)
systemctl status homeos-outils homeos-web    # serveur d'outils (127.0.0.1:8090) et navigateur isolé (lot 7)
```

Dans Telegram : `/statut` (sessions Claude, dernier brief, disque, sauvegarde, copie hors site, restauration testée, dépense et latence du mois), `/brief`, `/journal`.

Le healthcheck tourne toutes les 10 min et poste dans le groupe uniquement en cas de problème (bot arrêté, session Claude déconnectée, disque > 85 %, sauvegarde locale ou hors site trop ancienne, test de restauration en échec, service en échec, redémarrage en attente depuis 48 h, rafale d'erreurs dans le journal, proxy sortant arrêté, vérification automatique d'une capacité en échec), au plus une alerte par problème toutes les 6 h. Chaque lundi, `homeos-verifier-secrets` revérifie que les sessions Claude ne peuvent lire aucun secret.

### 8. Sauvegarde hors site (Google Drive)

Les sauvegardes locales (`/var/backups/homeos`) ne survivent pas à la perte du VPS. Une fois :

```bash
sudo bash /srv/home-os/scripts/configurer-hors-site.sh
```

Procédure pas à pas (rclone, autorisation Google, mot de passe à ranger) : **[docs/sauvegardes.md](docs/sauvegardes.md)**.
Ensuite chaque sauvegarde nocturne est recopiée, chiffrée, sur le Drive du foyer, et une restauration complète
est testée automatiquement le 3 de chaque mois.

### 9. Fermer root en SSH (après vérification manuelle)

1. Depuis ton ordinateur, vérifier que `ssh admin@VPS` fonctionne **par clé** et que `sudo -v` marche.
2. Vérifier à qui appartiennent les clés de root et d'admin : `sudo ssh-keygen -lf /root/.ssh/authorized_keys`,
   `ssh-keygen -lf ~/.ssh/authorized_keys`.
3. Depuis cette session `admin` :

   ```bash
   sudo bash /srv/home-os/install/install.sh --fermer-root
   ```

   Le script vérifie le compte `admin` (groupe sudo, clé, session en cours), applique
   `install/ssh/10-homeos.conf` (`PermitRootLogin no`, `AllowUsers admin`, pas de mot de passe, 3 essais),
   contrôle avec `sshd -t` puis recharge SSH. **Garder la session ouverte** et tester `ssh admin@VPS` dans un
   autre terminal. En cas de problème : console de l'hébergeur (root), supprimer
   `/etc/ssh/sshd_config.d/10-homeos.conf`, `systemctl reload ssh`.

### 10. Filtrage sortant (optionnel, après validation)

Par défaut le VPS peut joindre n'importe quel site. Le filtrage limite `homeos` et les personnes à une liste
blanche (`install/reseau-autorise.txt` + hôtes déclarés par les capacités) via un proxy local :

```bash
sudo bash /srv/home-os/install/install.sh --filtrer-sortant
sudo bash /srv/home-os/scripts/reseau-test.sh          # un hôte non listé bloqué, api.anthropic.com joignable
```

Puis tester : un message texte, un vocal, un PDF, `kd statut`, `/transports`, `/statut`. Retour arrière :
`sudo bash /srv/home-os/install/install.sh --ne-plus-filtrer`. Ajouter un site : une ligne dans
`install/reseau-autorise.txt`, puis relancer `install.sh`.

### Mise à jour

```bash
ssh admin@VPS
cd /srv/home-os && sudo -u homeos git pull       # ou recopier le kit dans ~/home-os
sudo bash /srv/home-os/install/install.sh        # (ou ~/home-os/install/install.sh) : idempotent, migre si besoin
sudo bash /srv/home-os/scripts/verifier.sh
```

`install.sh` met à jour les dépendances Python, Playwright/Chromium et Claude Code **aux versions de
`install/versions.env`** et redémarre le bot. Claude Code ne se met plus à jour tout seul
(`DISABLE_AUTOUPDATER=1`) : pour monter de version, modifier `CLAUDE_CODE` dans `install/versions.env`, relancer
`install.sh`, puis `scripts/verifier.sh` (qui lance `claude -p "ok"` pour chaque personne). Le navigateur est
réinstallé le 2 de chaque mois (`homeos-maj-navigateur`, alerte Telegram si Kitchen Daily ne répond plus).

### Restauration

Les données (`/srv/home-os/data` et `/var/lib/homeos`) sont sauvegardées chaque nuit à 03:30 dans
`/var/backups/homeos` (30 jours) et, si configuré, sur Google Drive. Procédures (un fichier, tout le serveur,
nouveau VPS depuis le Drive) : [docs/sauvegardes.md](docs/sauvegardes.md).

## Sécurité en bref

- Les sessions Claude ne lisent que les dossiers de données utiles (mémoire, fichiers reçus, photos) : ni les
  secrets (`/etc/homeos`), ni l'état sensible (`/var/lib/homeos`), ni la base, ni les notes privées de l'autre
  personne. Le bot ne peut lancer Claude que via le garde-fou `homeos-claude`, qui impose les options sûres.
- Depuis le lot 7, les outils tournent dans un service à part (`homeos-outils`, sous `homeos`) que chaque session
  joint avec le jeton de sa personne : les personnes ne peuvent plus lire aucun secret, même par leurs outils.
  Chromium tourne sous un utilisateur dédié (`homeos-web`) qui ne voit pas les données.
- Détail et limites : CONTRACT.md §12 ; audit de sécurité du VPS (notes internes, hors paquet) ; serveur
  d'outils : `docs/spec-lot7-serveur-outils.md` et la section « Serveur d'outils » ci-dessous.

## Kitchen Daily (lots 2 et 2b)

L'assistant lit les livraisons et le menu sur kitchen-daily.fr, propose 18 plats selon vos goûts et, **après « Valider » dans Telegram**, enregistre la sélection sur le site (Chromium headless piloté par Playwright). Une capture d'écran du résultat est envoyée dans le groupe. Rappel automatique le mercredi à 18:00 (cron `rappel` de la capacité `kitchen_daily`, timer `homeos-cap-kitchen_daily-rappel` ; horaire réglable dans `config.yaml` → `capacites.kitchen_daily.crons.rappel`), avec une proposition qui n'est jamais appliquée sans validation — sauf si vous confiez le choix des plats à l'assistant (`capacites.kitchen_daily.validation: automatique`). Depuis le lot 5, Kitchen Daily est une capacité (`capabilities/kitchen_daily/`, voir « Capacités ») ; les commandes ci-dessous sont inchangées.

Depuis le lot 2b, tout se règle aussi en langage naturel :

- **Extras** (desserts, entrées-soupes-apéros, jus et shots) : « qu'est-ce qu'on a en desserts le 19 ? », « ajoute deux crèmes au chocolat pour le 19 », « enlève les jus ». Un extra payant ou un retrait affiche les boutons Valider / Annuler avec le supplément estimé ; le **dessert offert** (un par livraison, parmi les desserts marqués 🎁) est pris directement, sans frais, après vérification que le montant ne bouge pas.
- **Semaine sautée** : « pas de livraison la semaine du 26 », « finalement on la veut » → interrupteur « Je veux ma livraison » du site, toujours avec Valider / Annuler.
- Ces deux actions demandent **toujours** un clic sur Valider, même en validation automatique des plats (champ `confirmation_toujours` du manifeste). Enregistrer les plats conserve les extras déjà pris (ils sont relus avant, comparés à chaque étape du site et réappliqués si besoin).

### Connexion (une fois, puis à chaque expiration de session)

Le compte est celui de la personne du foyer abonnée. Les identifiants sont tapés dans le terminal du VPS (saisie masquée) et **ne sont enregistrés nulle part** : seul l'état de session (cookies + localStorage) est conservé dans `/var/lib/homeos/kd/state.json` (réservé à `homeos` depuis le lot 7, hors des dossiers lisibles par la session Claude).

```bash
cd /srv/home-os
sudo -u homeos HOMEOS_DATA=/srv/home-os/data HOMEOS_ETAT=/var/lib/homeos .venv/bin/python -m tools.kitchen_daily login
```

Toujours lancer la commande en `homeos` (le serveur d'outils et le runner tournent sous `homeos`). Chromium tourne dans le navigateur isolé `homeos-web` quand il est installé (lot 7) ; avec le filtrage sortant actif, il passe automatiquement par le proxy (`/etc/default/homeos-proxy`).

### Tester

```bash
alias kd='sudo -u homeos HOMEOS_DATA=/srv/home-os/data HOMEOS_ETAT=/var/lib/homeos /srv/home-os/.venv/bin/python -m tools.kitchen_daily'
cd /srv/home-os
kd statut                       # « Session Kitchen Daily valide. »
kd livraisons                   # JSON : id, date, modifiable_jusqua, modifiable…
kd menu <id>                    # JSON : formule_plats, plats (titre, badges, selectionne), detection_selection
kd appliquer <id> --dry-run '{"Tagliatelles carbonara": 2, "Dahl de lentilles corail au lait de coco": 16}'
                                # remise à zéro + clics, capture /var/lib/homeos/kd/captures/*-avant.png, SANS « Je valide »
kd extras <id>                  # JSON : desserts, entrees, jus (prix, quantités, offert_possible), selection, dessert_offert
kd extras-definir <id> '{"desserts": {"Crème au chocolat": 2}, "jus": {"Pack 3 Jus détox": 0}}' --dry-run
                                # quantités FINALES (0 retire) réglées étape par étape, capture *-extras-avant.png,
                                # SANS « Je valide ma commande » ; sans --dry-run : demande OUI, enregistre et relit
kd livraison <id> --pas-voulue --dry-run   # lit l'interrupteur « Je veux ma livraison », capture, sans cliquer
kd livraison <id> --voulue                 # réactive (demande OUI), confirme la modale éventuelle, relit l'état
```

`kd livraisons` lit désormais aussi l'interrupteur de chaque livraison modifiable (`livraison_voulue`), en ouvrant sa page de détail : quelques secondes de plus par livraison.

Attention : le `--dry-run` clique « Remise à zéro » et les « + » sans cliquer « Je valide ». Si le site enregistre chaque clic immédiatement (à vérifier au premier essai), la sélection en ligne est modifiée : faire le premier essai avec la sélection réellement voulue, puis relancer `kd menu <id>` (ou ouvrir le site) pour contrôler.

Dans Telegram : « choisis les plats de la semaine » → l'assistant lit le menu et vos préférences, propose une liste, puis affiche les boutons Valider / Annuler. « Ajoute un dessert offert pour le 19 », « pas de livraison la semaine du 26 » fonctionnent de la même façon.

Si la session a expiré, les outils et le rappel le disent explicitement (« Session Kitchen Daily expirée… ») : relancer `login`.

## Serveur d'outils (lot 7)

Les outils de l'assistant (agenda, courses, mémoire, capacités…) tournent dans un service permanent,
`homeos-outils` (utilisateur `homeos`, `127.0.0.1:8090`), au lieu d'être relancés sous la personne à chaque message.
Chaque session Claude le joint avec le **jeton de sa personne** (`~/.config/homeos/jeton`, généré par `install.sh`) :
le serveur sait qui parle, sans que la personne puisse lire le moindre secret. Chromium (Kitchen Daily) tourne à part,
sous l'utilisateur `homeos-web`, sans accès aux données. Spécification : `docs/spec-lot7-serveur-outils.md`.

### Déploiement (VPS déjà installé)

```bash
ssh admin@VPS
cd /srv/home-os && sudo -u homeos git pull                     # (ou recopier le kit)
sudo bash /srv/home-os/install/install.sh                       # http par défaut : jetons, homeos-web, homeos-outils,
                                                                # droits resserrés, puis redémarrage du bot
sudo bash /srv/home-os/scripts/verifier.sh                      # pytest, ruff, serveur d'outils (sonde par personne),
                                                                # étanchéité des secrets, claude -p par personne
```

`install.sh` fait, dans l'ordre : utilisateur `homeos-web` ; `/etc/default/homeos-outils` (`HOMEOS_OUTILS=http`) ;
`migrer-secrets.sh` (`ical*.env` en 640 root:homeos, `/var/lib/homeos` en 700 homeos, dossiers de travail déplacés
dans `/var/lib/homeos-cwd`) ; `/etc/homeos/navigateur.env` (adresse secrète du navigateur) ; jetons
(`scripts/jetons-outils.sh`) ; pip (`mcp>=1.10`, `uvicorn`) ; unités ; démarrage de `homeos-web` puis
`homeos-outils`, sonde avec le jeton homeos ; redémarrage du bot. Les conversations en cours repartent de zéro une
fois (le dossier de travail de Claude a changé : le bot rouvre une session tout seul).

Puis dans Telegram : un message texte (« qu'est-ce qu'on a sur la liste de courses ? »), un vocal, un PDF, « quels
sont mes rendez-vous cette semaine ? » (agenda privé), `kd statut` sur le serveur, `/statut`.

### Vérifier et diagnostiquer

```bash
systemctl status homeos-outils homeos-web --no-pager
journalctl -u homeos-outils -n 50                               # démarrage, jetons chargés, refus 401, rechargements
curl -s http://127.0.0.1:8090/sante                             # {"ok": true, …} (sans jeton)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8090/mcp   # 401 attendu sans jeton
cd /srv/home-os && sudo -u homeos .venv/bin/python -m tools.serveur_http sonde --appel capacites_lister
cd /srv/home-os && sudo -u homeos .venv/bin/python -m tools.serveur_http sonde --utilisateur <personne> --appel courses_lister
sudo systemctl reload homeos-outils                             # relit les jetons et recharge les capacités (SIGHUP)
```

Dépannage :

- **« Claude n'a plus d'outils » / réponses sans données** : `systemctl status homeos-outils` ; s'il est arrêté,
  `journalctl -u homeos-outils -n 100` (erreur Python au démarrage, port 8090 occupé : `ss -ltnp | grep 8090`).
  Le healthcheck alerte dans le groupe si le service ne répond plus.
- **401 dans le journal du service** : jeton absent ou désynchronisé. `sudo bash /srv/home-os/scripts/jetons-outils.sh
  homeos <personnes…>` (ou relancer `install.sh`) puis `sudo systemctl reload homeos-outils`.
- **« homeos-claude: refus : jeton du serveur d'outils absent »** : même remède (le wrapper exige
  `~/.config/homeos/jeton` en 600, propriété de la personne).
- **Une capacité activée n'apparaît pas** : `homeos-appliquer-config` recharge le serveur à chaque changement de
  `etat.json` ; à la main : `sudo systemctl reload homeos-outils`. Après un `git pull` qui modifie `tools/` :
  `sudo systemctl restart homeos-outils`.
- **Kitchen Daily : « Navigateur distant indisponible »** : `systemctl status homeos-web`, `journalctl -u homeos-web`.
  Après une montée de Playwright, `homeos-web` et `homeos-outils` doivent être redémarrés ensemble (fait par
  `install.sh` et `maj-navigateur.sh`). Contournement : `HOMEOS_NAVIGATEUR=local` (drop-in de `homeos-outils`, avec
  `MemoryMax=2G`).
- **Mémoire** : `homeos-outils` est limité à 1 Go, `homeos-web` à 1,5 Go (`systemctl status` affiche la
  consommation).

### Retour arrière (outils en stdio, comme au lot 5d)

```bash
sudo bash /srv/home-os/install/install.sh --outils=stdio        # mode stdio, droits du lot 5d réappliqués,
                                                                # homeos-outils et homeos-web arrêtés
```

Le wrapper et le bot relisent `/etc/default/homeos-outils` à chaque message : le message suivant utilise
`tools/mcp.json` (stdio). En urgence, sans relancer install.sh : `sudo sed -i 's/^HOMEOS_OUTILS=.*/HOMEOS_OUTILS=stdio/'
/etc/default/homeos-outils && sudo bash /srv/home-os/scripts/migrer-secrets.sh && sudo systemctl disable --now
homeos-outils homeos-web`. Revenir au serveur persistant : `sudo bash /srv/home-os/install/install.sh --outils=http`.

## Appli web (lots 4a et 4b)

La conversation existe aussi dans une **appli web** auto-hébergée, joignable seulement depuis vos appareils
Tailscale : même assistant, même mémoire, même conversation que Telegram (une réponse donnée d'un côté s'affiche de
l'autre), avec des cartes (agenda, courses, propositions Kitchen Daily…), la voix et une saisie sécurisée des
identifiants. Connexion par **passkey** (empreinte ou visage du téléphone, Windows Hello sur PC). Le lot 4a a livré
le backend ; le lot 4b le vrai front (maquette v1.2), installable comme une appli sur Android et iPhone, avec des
notifications. La page de démonstration du lot 4a reste sur `/demo`. Contrat : `CONTRACT.md` §12, §14, §15.

Choix réseau : l'appli écoute `127.0.0.1:8095` ; `tailscale serve --https=8443` la publie sur le réseau privé, à
l'adresse `https://<nom MagicDNS du serveur>:8443` (Vaultwarden garde `https://<nom MagicDNS>/`, port 443). Aucun
port public, pas de Funnel.

### Déploiement (VPS déjà installé, Tailscale connecté)

```bash
ssh admin@VPS
cd /srv/home-os && sudo -u homeos git pull                      # (ou recopier le kit)
tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"])'   # nom MagicDNS
sudo bash /srv/home-os/install/install.sh                        # pip (fastapi, sse-starlette, webauthn), Piper +
                                                                 # voix française, /etc/default/homeos-web-app,
                                                                 # tailscale serve --https=8443, homeos-web-app
sudo bash /srv/home-os/scripts/verifier.sh                       # inclut « appli web » : /api/sante, 401, CSRF, écoute
                                                                 # locale, tailscale serve, pas de Funnel
```

Prérequis Tailscale : HTTPS activé pour le tailnet (console Tailscale → DNS → « HTTPS Certificates »), sinon
`tailscale serve --https` échoue (install.sh le signale). Pour publier à la main :
`sudo tailscale serve --bg --https=8443 http://127.0.0.1:8095` ; vérifier : `tailscale serve status`.

`/etc/default/homeos-web-app` (sans secret) : `HOMEOS_WEB_ORIGINE=https://<nom MagicDNS>:8443` (origine et identifiant
WebAuthn : les passkeys sont liées à ce nom ; le changer oblige à réenrôler), `HOMEOS_WEB_HOTE=127.0.0.1`,
`HOMEOS_WEB_PORT=8095`, `HOMEOS_PIPER`, `HOMEOS_PIPER_VOIX`. Après modification : `sudo systemctl restart homeos-web-app`.

### Le front (lot 4b) : sources, build, mise à jour

- Sources : `web/front/` (Vite + React + TypeScript + Tailwind + shadcn/ui, tout open source ; versions épinglées
  dans `web/front/package-lock.json`). Build statique **commité** dans `web/static/app/`, servi tel quel par FastAPI :
  le déploiement n'a pas besoin de npm.
- `install.sh` reconstruit le front dans un dossier temporaire si npm est présent (`npm ci && npm run build`) et
  vérifie qu'il est identique au build commité ; `install.sh --construire-front` remplace le build servi par celui
  du serveur (sources modifiées sur place).
- Modifier le front (poste de développement) :
  ```bash
  cd web/front && npm ci
  npm run dev        # http://localhost:5173, API mandatée vers 127.0.0.1:8095 (HOMEOS_WEB_ORIGINE=http://localhost:5173)
  npm test           # Vitest : cartes, lecture SSE, échange
  npm run build      # tsc + vite build → web/static/app (à commiter avec les sources)
  ```
- Test de bout en bout dans Chromium contre le backend réel et un faux `claude` (passkey virtuelle, message, cartes,
  confirmation, masquage, carte identifiants, courses) : `pytest -m navigateur tests/front` ; captures 390 px et
  desktop : `python -m tests.front.captures` (→ `docs/maquette/captures/front-*.png`, notes internes hors paquet).
- Aucune donnée du foyer dans le code : prénoms, nom de l'assistant (`assistant.nom`) et commune viennent de l'API.

### Installer l'appli sur le téléphone (PWA) et les notifications

- **Android (Chrome)** : ouvrir `https://<nom MagicDNS>:8443`, menu ⋮ → « Installer l'application ».
- **iPhone (Safari, iOS 16.4+)** : Partager → « Sur l'écran d'accueil ». Les notifications ne fonctionnent sur iPhone
  qu'une fois l'appli ajoutée à l'écran d'accueil.
- Dans l'appli : Réglages → « Notifications sur cet appareil ». Le serveur envoie une notification (Web Push, chiffrée
  de bout en bout : le service de push d'Apple, Google ou Mozilla ne voit que du chiffré) pour un message spontané
  (brief, routine, résultat d'une action automatique) quand aucun onglet de l'appli n'est ouvert sur aucun appareil.
- Clés VAPID : générées une fois par `install.sh` dans `/etc/homeos/push.env` (640 root:homeos, jamais dans le
  dépôt). Les régénérer (`sudo rm /etc/homeos/push.env && sudo bash install/install.sh`) oblige chaque appareil à
  réactiver ses notifications. Avec le filtrage sortant, les services de push sont dans `install/reseau-autorise.txt`.
- Hors ligne : l'appli s'ouvre depuis le cache (service worker) et l'historique déjà vu reste lisible ; rien n'est
  envoyé avant le retour du réseau. Le cache est vidé à la déconnexion.

### Enrôler une passkey (une fois par appareil)

1. Sur l'appareil, installer Tailscale et se connecter au compte du foyer (le téléphone doit voir le serveur).
2. Sur le serveur, générer le lien (10 minutes, une seule fois) :
   ```bash
   cd /srv/home-os && sudo -u homeos HOMEOS_DATA=/srv/home-os/data .venv/bin/python -m web.invitation <prénom>             # affiché
   cd /srv/home-os && sudo -u homeos HOMEOS_DATA=/srv/home-os/data .venv/bin/python -m web.invitation <prénom> --telegram  # envoyé en privé
   ```
3. Ouvrir le lien sur l'appareil → « Activer cet appareil » → empreinte / visage. La session dure 30 jours (renouvelée
   à chaque visite). Ensuite, « Se connecter » suffit, sans rien taper.
4. Autre appareil : nouveau lien, ou, depuis un appareil déjà connecté, « Ajouter une passkey » (confirmation
   d'identité demandée). Secours sans passkey : « Recevoir un code sur Telegram » (code à 6 chiffres, 10 min).

**Connexion de secours (code Telegram).** Elle dure 24 heures (jamais prolongée) et sert à converser, lire et
**ajouter une passkey** — c'est son but : un bandeau « Connexion de secours · ajoute une passkey » le rappelle, et
l'ajout transforme la session en session complète (30 jours). Le coffre, les documents sensibles, l'administration
et les routines, la suppression d'une passkey sont refusés (« connecte-toi par empreinte pour ça »).

**Session liée au compte Tailscale.** `tailscale serve` indique à l'appli quel compte Tailscale fait chaque requête
(en-tête `Tailscale-User-Login`). La session retient ce compte à la connexion ; une requête venue d'un autre compte
(cookie copié sur un autre appareil du tailnet) est refusée et notée dans le journal (`web_tailscale`). Réglages →
appareils affiche « vu depuis <compte> » pour chaque session. Pour n'accepter qu'un compte par personne, l'écrire dans
`config.yaml` (facultatif ; sinon le compte est appris à la connexion) :

```yaml
personnes:
  - prenom: Alex
    linux_user: alex
    tailscale_login: alex@example.org   # comme affiché dans Réglages → appareils, ou `tailscale status`
```

Cet en-tête est fiable parce que l'appli n'écoute que sur `127.0.0.1` : seul `tailscale serve` la joint depuis le
réseau, et il retire tout en-tête `Tailscale-User-*` envoyé par le client avant de poser le sien. Ne jamais la faire
écouter ailleurs (ni `funnel`). Un appareil « tagué » n'a pas d'identité : il ne peut ni utiliser une session liée à
un compte ni se connecter pour une personne qui a un `tailscale_login`. En accès local direct (`curl` sur
`127.0.0.1:8095`, tests, `verifier.sh`), il n'y a aucun en-tête : le contrôle est ignoré (noté une fois au journal).

Révoquer un appareil : Réglages de l'appli (« Appareils et passkeys »), ou tout couper pour une personne :
`cd /srv/home-os && sudo -u homeos HOMEOS_DATA=/srv/home-os/data .venv/bin/python -m web.invitation <prénom> --revoquer`.

### Ce qui demande une confirmation d'identité (step-up)

Saisir des identifiants (carte « Identifiants »), valider un réglage d'administration ou une action toujours
confirmée (extra payant…), supprimer une passkey, ajouter un appareil : l'appli redemande l'empreinte (passkey avec
vérification de l'utilisateur), valable 5 minutes pour cette session seulement. Pour le coffre (saisie
d'identifiants, suppression ou rangement d'une donnée), la confirmation ne sert **qu'une fois** : deux cartes
d'affilée demandent deux fois l'empreinte (une saisie refusée, par exemple un numéro de carte invalide, ne la
consomme pas).

### Modèles Claude et sessions (`ia:` dans config.yaml)

Chaque message choisit son modèle : Haiku pour les commandes, confirmations et demandes courtes (« ajoute du lait »),
Opus pour choisir ou comparer, Sonnet sinon (un appel Haiku de moins de 3 s tranche quand les règles ne suffisent pas ;
`routeur: false` pour toujours Sonnet). Les crons : briefs en Sonnet, sélection Kitchen Daily en Opus. La session
Claude repart de zéro après 20 réponses ou 2 h de pause (l'historique reste affiché ; un court rappel des derniers
échanges accompagne le message suivant). Le modèle utilisé est dans `actions_log` (`message_sortant`, `modele`).

### Diagnostiquer

```bash
systemctl status homeos-web-app --no-pager ; journalctl -u homeos-web-app -n 50
curl -s http://127.0.0.1:8095/api/sante                          # {"ok":true}
tailscale serve status                                           # https://<nom>:8443 → http://127.0.0.1:8095
```

- **« Passkey refusée »** à l'enrôlement : l'adresse ouverte doit être exactement `HOMEOS_WEB_ORIGINE` (nom MagicDNS +
  `:8443`), en https.
- **Pas de voix** : Piper absent (`ls /opt/piper/venv/bin/piper /opt/piper/voix/`) ; relancer install.sh. L'appli se
  replie sur la voix du navigateur.
- **Appli arrêtée** : Telegram continue de fonctionner (même cœur, service distinct) ; le healthcheck alerte.
- **Page de démonstration au lieu de l'appli** : `web/static/app/index.html` absent (build non commité) ;
  `sudo bash install/install.sh --construire-front`.
- **Pas de notification** : `ls -l /etc/homeos/push.env` (640 root:homeos) ; l'appareil doit être abonné (Réglages) ;
  sur iPhone, l'appli doit être installée sur l'écran d'accueil ; aucune notification n'est envoyée tant qu'un onglet
  est ouvert.

## Capacités

Chaque fonction « en plus » (Kitchen Daily, transports…) est une **capacité** : un dossier `capabilities/<nom>/` avec son manifeste, ses outils, ses consignes pour l'assistant, sa vérification automatique et ses tâches programmées. Contrat complet : `CONTRACT.md` §9.

Capacités livrées :

- **Kitchen Daily** (`kitchen_daily`) : plats livrés, extras, semaines sautées, rappel du mercredi (voir plus haut).
- **Transports** (`transports`, remplace « Prochain bus ») : bus, car, métro, tram, train, avion ; trajets habituels de chacun notés en parlant, prochains départs et retards en direct quand le réseau les publie, état d'un train ou d'un vol par son numéro, rappel la veille des voyages notés à l'agenda, `/transports` et `/bus` dans Telegram. Train SNCF partout en France (données ouvertes, sans clé) ; réseaux locaux au choix du foyer (Cartreize fourni, avec son service local `homeos-transports-cartreize` installé seulement s'il est utilisé) ; tout autre réseau s'ajoute en conversation, au besoin fabriqué par l'atelier. Détails : `docs/transports.md`.
- **Atelier** (`atelier`, lot 7b) : un propriétaire demande en conversation une capacité qui manque (« ajoute Planity pour prendre les rendez-vous coiffeur ») ; l'assistant la fait fabriquer par un sous-agent Claude Code en arrière-plan (services `homeos-atelier` et `homeos-atelier-installer`), contrôlée, testée, relue puis installée en essai dans `/var/lib/homeos/capacites-atelier/`. Suivi : `sudo -u homeos … python -m tools.atelier etat`. Détails : `docs/atelier.md`.

```bash
alias cap='sudo -u homeos HOMEOS_DATA=/srv/home-os/data HOMEOS_ETAT=/var/lib/homeos /srv/home-os/.venv/bin/python -m tools.capacites'
cd /srv/home-os
cap list                         # nom, statut (actif / essai / inactif / invalide), origine, dernière vérification
cap statut kitchen_daily         # détail JSON : crons, actions à valider, identifiants requis, réglages
cap smoke                        # vérifie toutes les capacités chargées (code 1 si un échec)
cap disable kitchen_daily        # désactive (outils refusés aussitôt, retirés du serveur d'outils dans la minute ; données conservées)
cap enable kitchen_daily         # (ré)active ; vaut validation humaine d'une capacité écrite par l'assistant
# les timers des crons sont créés ou retirés tout seuls (homeos-appliquer-config.path) ; à la main : sudo homeos-appliquer-config
```

Dans Telegram : `/capacites` liste ce que l'assistant sait faire ; les commandes des capacités (ex. `/transports`) apparaissent dans `/aide`.

**Ajouter une capacité** :

1. `cap new planity --titre "Planity" --description "Rendez-vous coiffeur"` crée le squelette en **essai** (lecture seule : rien ne peut agir) avec un outil d'exemple et un test.
2. Écrire les outils (`tools.py`, préfixe `planity_`), les consignes (`CLAUDE.md`), la vérification (`smoke.py`), les tests ; déclarer dans `capability.yaml` les identifiants nécessaires (références au coffre, jamais de valeur), les réglages propres au foyer (`parametres`, demandés pendant l'onboarding), les actions à valider par bouton, les crons.
3. `pytest capabilities/planity` puis `cap enable planity`.

**Partager une capacité** : `cap export planity --sortie /tmp` produit `planity-v1.tar.gz` et son empreinte `.sha256` (sans données ni secrets). Chez quelqu'un d'autre : `cap install planity-v1.tar.gz` affiche ce que la capacité demande (identifiants, accès réseau, actions avec effet) et l'installe en essai après confirmation. Signature de l'auteur : à venir.

Une capacité ne contient aucune donnée personnelle (prénoms, adresse, identifiants Telegram) ; à vérifier avant un partage :

```bash
HOMEOS_MOTIFS_INTERDITS="Nom de famille,Ville,Rue" .venv/bin/pytest tests/test_capacites_portables.py
```

## Administrer par la conversation

Les **propriétaires** du foyer (`role: proprietaire` dans `config.yaml`, valeur par défaut d'une personne listée ;
`membre` et `invite` ne peuvent pas) règlent le système en parlant à l'assistant, sans terminal. Il reformule en
une ligne, affiche le bouton **Valider**, applique, puis répond « Fait. … (avant : …). Tu peux annuler en me le
disant. » Exemples :

1. « Mets le rappel Kitchen Daily le mercredi à midi. »
2. « Le brief du matin à 7h30 en semaine. »
3. « Laisse-toi choisir les plats Kitchen Daily tout seul, tu me préviens après. » (validation automatique)
4. « Coupe les transports pour l'instant. » / « Réactive les transports. »
5. « Où en est le système ? » (ou `/admin`) : capacités, vérifications, dépense du mois, sauvegardes, disque.
6. « Annule le dernier changement. » (« qui a changé le rappel ? » montre le journal)

Aussi : « mets tout en pause » / « reprends », « Kitchen Daily ne marche plus, comment je reconnecte ? » (la
marche à suivre, jamais d'identifiant en conversation). Une demande qui demande du développement (« ajoute la
gestion des desserts ») est notée pour l'atelier, jamais présentée comme faite.

Sous le capot : chaque écriture de `data/config.yaml` passe par `tools/config_edition.py` (liste blanche de
réglages, validation, sauvegarde `config.yaml.bak-<horodatage>` — 10 gardées —, journal
`data/administration/journal.jsonl`) ; le bot relit la configuration à chaque message et
`homeos-appliquer-config.path` régénère les timers dans la minute. Détails : `CONTRACT.md` §13.

```bash
sudo systemctl status homeos-appliquer-config.path          # surveillance active
cat /srv/home-os/data/administration/application.json      # dernière application des horaires (ok, message)
tail -n 5 /srv/home-os/data/administration/journal.jsonl   # derniers réglages (auteur, avant, après)
sudo homeos-appliquer-config                                # forcer l'application (idempotent)
```

Valider automatiquement l'administration elle-même (`capacites.administration.validation: automatique`) est
possible mais déconseillé : un réglage mal compris serait appliqué sans relecture (il reste annulable).

## Identifiants des services (coffre)

Les identifiants dont une capacité a besoin (Planity, cantine…) sont rangés dans un coffre chiffré (`/var/lib/homeos/coffre.json`, clé `COFFRE_CLE` générée par `install.sh` dans `/etc/homeos/coffre.env` : **la sauvegarder hors du VPS avec `RESTIC_PASSWORD`**). Ils ne passent jamais par Telegram ni par l'assistant.

```bash
alias coffre='cd /srv/home-os && sudo -u homeos HOMEOS_DATA=/srv/home-os/data HOMEOS_ETAT=/var/lib/homeos HOMEOS_SECRETS=/etc/homeos .venv/bin/python -m tools.coffre'
coffre set planity/alex        # saisie masquée (identifiant, mot_de_passe)
coffre ls                        # clés et noms de champs, jamais les valeurs
coffre statut                    # backend, fichier, clé présente
```

Dans Telegram, `/identifiants planity` rappelle la commande à lancer. Vaultwarden (optionnel, apps Bitwarden sur téléphone) : `sudo bash install/vaultwarden.sh`. Tout est détaillé dans `docs/coffre.md`.

## Pause et arrêt

- `/stop` dans Telegram crée `data/PAUSE` : briefs, rappels et validations sont suspendus. `/reprendre` le retire.
- Arrêt complet : `sudo systemctl stop homeos-bot` et `sudo systemctl stop 'homeos-*.timer'`.

## Licence

Firmin Assistant est publié sous licence **Apache 2.0** (fichiers `LICENSE` et `NOTICE`) : libre d'utilisation, de modification et de redistribution, avec protection sur les brevets. L'achat unique couvre l'installation guidée, le paquet prêt à l'emploi et le support ; le code est le même pour tous.
