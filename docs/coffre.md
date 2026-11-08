# Coffre à secrets (lot 5b)

Le coffre garde les identifiants des services que l'assistant utilise pour le foyer (Planity, cantine,
plus tard d'autres capacités). Règle de base : **l'assistant ne voit jamais un secret**. Les capacités
déclarent des références `secret://<cle>` dans leur manifeste ; seul `tools/coffre.py` lit les valeurs,
et toute sortie d'outil est masquée (`•••`) avant d'atteindre le modèle.

Kitchen Daily n'est pas concerné : il conserve son état de session dans `/var/lib/homeos/kd/state.json` (lot 5d ; `data/.kd/` avant)
(cookies, pas d'identifiant) et ses identifiants ne sont jamais stockés.

## Utilisation

Sur le serveur, en `homeos`, depuis la racine du dépôt :

```bash
alias coffre='sudo -u homeos homeos-coffre'   # lot 5c : wrapper installé dans /usr/local/bin par install.sh
# (équivalent : cd /srv/home-os && sudo -u homeos HOMEOS_DATA=/srv/home-os/data .venv/bin/python -m tools.coffre)
coffre set planity/alex                     # demande identifiant puis mot_de_passe (masqués, deux fois)
coffre set cantine --champs identifiant,mot_de_passe,code_famille
coffre set planity/alex --champs mot_de_passe   # change un seul champ, les autres sont conservés
coffre ls                                     # clés et noms de champs, jamais les valeurs
coffre get planity/alex                     # noms des champs, date et auteur de mise à jour
coffre get planity/alex --revele            # valeurs en clair : à éviter (écran, historique du terminal)
coffre rm planity/alex                      # demande « OUI »
coffre statut                                 # backend, fichier, clé présente, nombre de secrets
```

- Aucune valeur ne passe en argument de commande (elle finirait dans l'historique du shell et dans
  `ps`) : les valeurs se saisissent masquées. Sans terminal (tube, script), une valeur par ligne est
  lue sur l'entrée standard, sans jamais être réécrite.
- Clés : minuscules, `<capacite>` ou `<capacite>/<suite>` ; par convention `<capacite>/<linux_user>`
  pour un compte personnel (`planity/camille`), `<capacite>` seul pour un compte commun au foyer.
- Champs : `[a-z][a-z0-9_]*`, par défaut `identifiant` et `mot_de_passe`.

Dans Telegram, `/identifiants planity` répond avec la commande exacte à lancer sur le serveur ; la saisie
ne se fait **jamais** dans Telegram. Si quelqu'un colle un mot de passe après la commande
(`/identifiants planity monmotdepasse`), le bot ne l'enregistre pas, efface le message quand il le peut
(toujours en privé, dans le groupe seulement s'il est administrateur), ne le journalise pas, et conseille
de changer ce mot de passe. Un mot de passe collé dans un message ordinaire (hors commande) part, lui,
à l'assistant comme n'importe quel texte : c'est une limite connue (voir plus bas).

## Gérer ses identifiants en parlant (lot 5c)

Le foyer gère ses identifiants **en conversation**, sans que l'assistant voie jamais une valeur : il ne manipule que
des noms d'entrées, des types, des noms de champs et, pour une carte, les 4 derniers chiffres.

| On dit | L'assistant | Effet |
| --- | --- | --- |
| « Quels identifiants sont enregistrés ? » | `identifiants_lister` | entrées visibles (les siennes et celles du foyer ; tout pour un propriétaire du foyer), champs manquants, et ce qui manque aux capacités actives |
| « Enregistre le compte de la cantine, c'est pour le foyer » | `identifiants_preparer(libelle, type, proprietaire, capacite?)` | entrée créée **sans valeur**, puis la marche à suivre : `sudo -u homeos homeos-coffre set <cle>` ; dans l'appli, une carte sécurisée (`identifiants`, step-up) |
| « Change le mot de passe de Planity » | le même outil sur l'entrée existante | les valeurs saisies ensuite remplacent les anciennes |
| « Renomme… », « utilise cette carte pour la cantine », « supprime… » | `identifiants_renommer` / `_attribuer` / `_supprimer` | bouton Valider ; propriétaire de l'entrée ou propriétaire du foyer ; journalisé (`who`) |

Types et champs : `identifiants` (identifiant, mot_de_passe, url facultative), `carte_bancaire` (titulaire, numero,
expiration MM/AA, cvc ; numéro contrôlé par Luhn, carte échue refusée), `code` (code), `cle_api` (cle), `autre`
(champs libres). Une erreur de saisie cite le champ, jamais la valeur. Les entrées hors capacité vivent sous
`libre/<slug>` ; `identifiants_attribuer` les range sous la clé déclarée par la capacité (`cantine`,
`planity/<personne>`) après avoir vérifié le type et les champs requis.

CLI enrichie : `coffre set libre/netflix` demande les champs du type de l'entrée préparée en conversation ;
`coffre set libre/carte --type carte --virtuelle --libelle "Carte cantine" --proprietaire foyer` ; `coffre ls` affiche
type, libellé, propriétaire, « ••• 1234 » et capacités, jamais une valeur.

**Cartes bancaires** : seule une carte **virtuelle à plafond** est acceptée (`--virtuelle`, ou `virtuelle=true` après
la question « c'est bien une carte virtuelle à plafond ? ») ; une capacité qui déclare un secret
`type: carte_bancaire` doit lister son action de paiement dans `confirmation_toujours`, et `ctx.secret()` ne rend la
carte que pendant l'exécution de cette action après le clic humain ; le paiement est confirmé par la personne
(3-D Secure). L'assistant ne déclenche jamais un paiement seul.

Un mot de passe collé dans un argument d'outil (ou une valeur déjà dans le coffre, ou un numéro de carte) est refusé
(« je ne l'enregistre pas, change-le ») et journalisé comme `[valeur refusée, non journalisée]`.

### Filtre de données sensibles

Un message qui contient une donnée sensible (carte, IBAN, numéro de sécurité sociale, mot de passe ou code annoncé,
clé d'API) est masqué **avant** d'atteindre le modèle, sur tous les canaux (`coeur/filtre_sensible.py`) : le modèle
reçoit `[carte bancaire ••1234]`, `[IBAN ••7890]`, `[numéro de sécurité sociale]`, `[mot de passe masqué]` ou
`[clé masquée]`, plus une ligne `[donnée sensible détectée : …, quarantaine q7f3]`. La personne voit ce qui a été
transmis (« 🔒 Transmis à Firmin : … ») ; l'historique et les journaux ne gardent que la version masquée. La valeur
reste 10 minutes en quarantaine chiffrée (`quarantaine/<id>`, propriétaire = la personne) : si elle accepte de la
ranger (« range-la comme carte de la cantine »), `identifiants_ranger_quarantaine` l'écrit dans l'entrée après le
bouton Valider, sans qu'elle passe par le modèle ; sinon elle est effacée (cœur, écritures du coffre, healthcheck :
`coffre purger`). Détection volontairement conservatrice : un numéro de téléphone, une date, un montant, un code
postal ou un numéro de commande ne sont jamais masqués ; en cas de doute on laisse passer, et le masquage des
sorties d'outils reste la deuxième barrière. Limite : Telegram (et l'opérateur du bot) a vu passer le message ;
l'appli, elle, propose la saisie dans une carte sécurisée.

## API pour les capacités

```python
from tools import coffre

coffre.lire("secret://planity/alex", champs=["identifiant", "mot_de_passe"])  # -> dict
# lève coffre.SecretManquant : « Identifiants Planity à renseigner : /identifiants planity »
coffre.ecrire(cle, {"identifiant": ..., "mot_de_passe": ...}, fusion=False)
coffre.supprimer(cle) ; coffre.lister() ; coffre.decrire()   # decrire() : métadonnées sans valeur
coffre.valeurs_connues()   # set des valeurs (≥ 4 caractères), ne lève jamais
coffre.masquer(texte)      # remplace toute valeur connue par « ••• »
# lot 5c : métadonnées et quarantaine (jamais de valeur en sortie)
coffre.ecrire(cle, champs, fusion=False, meta={"type": "carte_bancaire", "libelle": "…", "proprietaire": "foyer",
                                              "virtuelle": True})   # validation selon le type
coffre.preparer_entree(cle, meta) ; coffre.modifier_meta(cle, libelle=…) ; coffre.deplacer(cle, nouvelle)
coffre.meta(cle) ; coffre.decrire()        # métadonnées + noms des champs
coffre.mettre_en_quarantaine(valeur, personne, nature, apercu) -> "q7f3" ; coffre.ranger_quarantaine(id, cle, champ, personne)
```

`tools.capacites` (lot 5a) s'appuie dessus : `Contexte.secret(cle)` → `coffre.lire(cle)` (l'exception
`SecretManquant` est définie ici et peut être réexportée), `masquer_secrets()` → `coffre.masquer()`.

## Backend `fichier` (par défaut)

Sélection : `data/config.yaml` → `coffre: {backend: fichier}` (valeur par défaut si absent).

- Fichier `/var/lib/homeos/coffre.json` (lot 5d ; `data/coffre.json` en développement), mode 640
  `homeos:homeos` : lu et écrit sous `homeos` seulement (CLI, crons, bot, serveur d'outils). Depuis le lot 7
  (outils en http, `homeos-outils.service` sous homeos), `ctx.secret()` fonctionne en conversation : la
  personne qui parle est celle du jeton présenté par sa session, une clé `<capacite>/<autre personne>` est
  refusée. En repli stdio (outils sous la personne), `ctx.secret()` échoue en conversation comme au lot 5d.
  Hors des dossiers lisibles par la session Claude ; inclus dans restic.
- Chiffrement **AES-256-GCM** (bibliothèque `cryptography`) de tout le contenu, y compris les noms de
  clés. Clé AES dérivée de `COFFRE_CLE` par **scrypt** (n=2¹⁵, r=8, p=1) avec un sel neuf à chaque
  écriture ; nonce de 96 bits neuf à chaque écriture. Les en-têtes (format, version, paramètres de
  dérivation) sont authentifiés comme données associées : toute altération est détectée.
- `COFFRE_CLE` (64 caractères hexadécimaux) est générée par `install.sh` dans `/etc/homeos/coffre.env`
  (640 root:homeos, jamais sauvegardé), comme
  `RESTIC_PASSWORD`. Elle peut aussi venir de l'environnement. **À conserver hors du VPS** avec
  `RESTIC_PASSWORD` : sans elle, `coffre.json` est irrécupérable, sauvegarde comprise.
- Écriture atomique (fichier temporaire, `fsync`, `rename`) sous verrou exclusif `data/.coffre.lock` :
  deux `set` simultanés ne perdent rien (verrou à côté du fichier, dans `/var/lib/homeos`). Un coffre illisible (mauvaise clé) n'est jamais écrasé.

Format sur disque :

```json
{"format": "homeos-coffre", "version": 1, "chiffrement": "AES-256-GCM",
 "kdf": {"nom": "scrypt", "n": 32768, "r": 8, "p": 1, "sel": "<base64>"},
 "nonce": "<base64>", "donnees": "<base64 : chiffré + tag>"}
```

Contenu déchiffré (lot 5c) : `{"secrets": {"planity/alex": {"champs": {...}, "type": "identifiants",
"libelle": "Planity", "proprietaire": "alex", "utilise_par": ["planity"], "cree": "<iso>", "maj": "<iso>",
"par": "<qui>"}}}` ; une carte ajoute `"virtuelle": true, "fin": "1234"` ; une quarantaine
`"quarantaine": {"nature", "apercu", "expire"}`. Les entrées du lot 5b (sans métadonnées) sont complétées à la
lecture et réécrites à la prochaine écriture.

### Ce que ça protège, et ce que ça ne protège pas

Protégé : une copie de `coffre.json` seule (sauvegarde, transfert, `git add` par erreur), la lecture
du fichier par l'assistant (outil `Read` sur `data/`) qui ne voit que du chiffré, les `grep`/`cat`
accidentels, les journaux (aucune valeur n'est journalisée, les erreurs n'en contiennent jamais).

Non protégé : quiconque lit à la fois `coffre.json` et `coffre.env`, c'est-à-dire root et `homeos` (bot,
crons). Depuis le lot 5d, les utilisateurs des personnes (et donc leurs sessions Claude) ne lisent ni l'un ni
l'autre, et l'outil `Read` est de toute façon limité aux dossiers de données non sensibles (CONTRACT §12).
La sauvegarde restic contient `coffre.json` mais jamais `COFFRE_CLE`.

### Rotation de `COFFRE_CLE`

Pas de commande dédiée dans ce lot. Procédure : noter `coffre ls`, déplacer `/var/lib/homeos/coffre.json`
ailleurs, remplacer `COFFRE_CLE` dans `/etc/homeos/coffre.env` (`openssl rand -hex 32`), puis ressaisir chaque
secret avec `coffre set`. (Une commande `rechiffrer` est une évolution simple si le besoin se présente.)

## Vaultwarden

### Pourquoi

Vaultwarden est une implémentation open source (AGPL) du serveur Bitwarden, légère (Rust, SQLite,
quelques dizaines de Mo de RAM), auto-hébergée sur le VPS : pas de service tiers payant. Le foyer y gagne
un vrai gestionnaire de mots de passe utilisable au quotidien avec les **apps Bitwarden officielles**
(iOS, Android, extensions navigateur), et l'assistant pourrait, à terme, y lire les identifiants d'un
dossier partagé « Firmin Assistant » au lieu du fichier local : une seule source de vérité, saisie depuis le
téléphone (sans passer par SSH).

### Installation (optionnelle)

```bash
sudo bash install/vaultwarden.sh --domaine coffre.exemple.fr   # nom DNS public du VPS (pas de défaut)
sudo bash install/vaultwarden.sh --domaine coffre.exemple.fr --inscriptions   # création des comptes
sudo bash install/vaultwarden.sh --domaine coffre.exemple.fr                 # inscriptions refermées
```

Le script (idempotent) installe `podman`, `caddy` et `sqlite3` ; crée un conteneur Podman via Quadlet
(`/etc/containers/systemd/homeos-vaultwarden.container` → `homeos-vaultwarden.service`), processus sous
l'uid `homeos`, écoute **uniquement** sur `127.0.0.1:8082` ; publie le site via Caddy
(`/etc/caddy/sites/homeos-vaultwarden.caddy`, TLS Let's Encrypt automatique) ; ouvre 443/tcp dans ufw
(80 avec `--ouvrir-80`) ; garde les données dans `data/vaultwarden` (homeos, 700 — invisible des
sessions `alex`/`camille`) ; ajoute à `homeos-backup.service` une copie cohérente de la base
(`sqlite3 .backup`) juste avant restic. La page `/admin` reste désactivée (pas d'`ADMIN_TOKEN`), les
inscriptions sont fermées hors `--inscriptions`, les invitations désactivées. Version de l'image épinglée
dans `install/versions.env` (`VW_IMAGE`, lot 5d). Exposition prévue via Tailscale uniquement (hors lot 5d).

### Compte de service « Firmin Assistant »

Un compte Vaultwarden dédié (adresse du type `homeos@<domaine>`), qui n'a accès qu'aux identifiants que
le foyer lui partage (organisation « Foyer », collection « Firmin Assistant »). Ses identifiants vont dans
`/etc/homeos/coffre.env` (640 root:homeos) : `VAULTWARDEN_URL`, `VAULTWARDEN_CLIENT_ID` / `VAULTWARDEN_CLIENT_SECRET` (clé API du
compte : Paramètres → Sécurité → Clés → Clé API) et `VAULTWARDEN_MOT_DE_PASSE_MAITRE` (indispensable :
le chiffrement Bitwarden est côté client, la clé API ne sert qu'à s'authentifier).

Rotation : changer le mot de passe maître et régénérer la clé API depuis le coffre web du compte de
service, reporter les trois valeurs dans `/etc/homeos/coffre.env`, redémarrer le bot. À faire aussi si
ce fichier a pu fuiter. Les comptes personnels des membres du foyer n'y sont jamais.

### Backend `vaultwarden` : pas encore implémenté

`coffre: {backend: vaultwarden}` lève aujourd'hui `NotImplementedError` avec un message explicite.
Raison : le chiffrement Bitwarden est entièrement côté client ; il faut réimplémenter la partie
cryptographique d'un client, et ce lot ne disposait d'aucune instance Vaultwarden pour valider
l'interopérabilité. Une écriture mal formée produirait des éléments illisibles dans les apps du foyer :
la priorité est allée à un backend fichier solide et testé. Ce qu'il faut faire (tout est faisable avec
`cryptography` ≥ 44, qui fournit aussi Argon2id) :

1. **Paramètres KDF** : `POST {url}/identity/accounts/prelogin` `{"email"}` → `kdf` (0 = PBKDF2-SHA256,
   1 = Argon2id), `kdfIterations`, `kdfMemory` (Mio), `kdfParallelism`.
2. **Clé maître** : PBKDF2-SHA256(mot de passe maître, sel = e-mail en minuscules, itérations, 32 octets),
   ou Argon2id(mot de passe, sel = SHA-256(e-mail), mémoire, itérations, parallélisme, 32 octets).
   Clé étirée : HKDF-Expand-SHA256(clé maître, info `enc`, 32) ‖ HKDF-Expand(info `mac`, 32).
3. **Jeton** : `POST {url}/identity/connect/token` (formulaire) `grant_type=client_credentials`,
   `scope=api`, `client_id=user.<uuid>`, `client_secret`, `deviceType`, `deviceIdentifier` (UUID stable,
   stocké dans `data/`), `deviceName=homeos`. La réponse contient `access_token`, `Key` (clé de
   l'utilisateur chiffrée) et `PrivateKey`.
4. **Déchiffrement** des EncString de type 2 `2.<iv>|<chiffré>|<mac>` : vérifier HMAC-SHA256(clé mac,
   iv ‖ chiffré) en temps constant, puis AES-256-CBC/PKCS7. La clé utilisateur déchiffrée fait 64 octets
   (chiffrement ‖ mac). Clé privée RSA (PKCS#8 DER) = EncString chiffrée par la clé utilisateur ; clés
   d'organisation = EncString de type 4 (RSA-OAEP-SHA1) dans `profile.organizations[].key` ; un élément
   peut aussi porter sa propre clé (`cipher.key`), à déchiffrer d'abord.
5. **Lecture** : `GET {url}/api/sync?excludeDomains=true` → éléments ; convention de correspondance :
   nom d'élément `homeos:<cle>` dans la collection « Firmin Assistant » ; `identifiant` ↔ `login.username`,
   `mot_de_passe` ↔ `login.password`, autres champs ↔ champs personnalisés masqués (type 1).
6. **Écriture** : chiffrer de la même façon (IV aléatoire, MAC) puis `POST /api/ciphers/create` (élément
   d'organisation, avec `collectionIds`) ou `PUT /api/ciphers/<id>`.
7. **Cache et repli** : chaque lecture réussie met à jour le backend fichier (même API) ; si Vaultwarden
   ne répond pas, on lit le cache. Le jeton est mis en cache jusqu'à son expiration.
8. **Tests** : vecteurs de test officiels du SDK Bitwarden (`bitwarden/sdk-internal`,
   `crates/bitwarden-crypto` : dérivation de clé maître, déchiffrement de clé utilisateur, EncString),
   puis un test d'intégration marqué `@pytest.mark.reseau` contre un conteneur Vaultwarden jetable.

Alternative plus rapide mais écartée par la spec : le CLI officiel `bw` en service (`bw serve`, API REST
locale sur 127.0.0.1, déverrouillage par `BW_SESSION`). Il évite toute cryptographie maison mais ajoute
une dépendance Node lourde et une session déverrouillée en permanence.
