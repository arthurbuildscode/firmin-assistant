# Installer Firmin Assistant pour votre foyer

Ce guide suit l'installation de bout en bout, écran par écran. Compter **30 minutes** : 5 minutes de questions,
10 à 20 minutes d'installation automatique, puis quelques gestes avec votre téléphone. Aucune connaissance
technique n'est nécessaire : il suffit de copier les commandes indiquées.

Les « captures » ci-dessous sont des copies de ce qui s'affiche dans le terminal ; ce que vous tapez est après
le `:` des questions. Les prénoms, la commune et les identifiants sont des exemples.

## Avant de commencer

| Il vous faut | Pourquoi | Où l'obtenir |
| --- | --- | --- |
| Un serveur **Ubuntu 24.04** (VPS), 2 processeurs, 4 Go de mémoire | l'assistant tourne chez vous, pas chez un tiers | un hébergeur (5 à 10 € par mois) ; choisir l'image « Ubuntu 24.04 » |
| Un compte d'administration avec `sudo` (ex. `admin`) et une connexion SSH par clé | pour lancer l'installation | proposé par l'hébergeur à la création du serveur |
| **Telegram** sur le téléphone de chaque adulte | c'est par là qu'on parle à l'assistant | App Store / Play Store |
| Un **abonnement Claude** (Pro ou Max) par adulte, **ou** une clé API Anthropic | c'est l'intelligence de l'assistant | claude.ai ; clé API : console.anthropic.com |
| Facultatif : un compte **Tailscale** (gratuit) | appli web et coffre, sans rien ouvrir sur Internet | tailscale.com |

Ce que ça coûte chaque mois : le serveur (5 à 10 €), l'abonnement Claude de chaque adulte (ou la clé API, payée
à l'usage), et quelques euros de clé API pour les briefs automatiques.

## Étape 1 — Préparer Telegram (5 minutes, sur le téléphone)

1. Dans Telegram, écrire à **@BotFather**, envoyer `/newbot`, choisir un nom (par exemple « Firmin de la
   maison ») puis un identifiant qui finit par `bot`. BotFather répond avec un **token** :

   ```
   Done! Congratulations on your new bot. […]
   Use this token to access the HTTP API:
   123456789:AAE…(une quarantaine de caractères)
   ```

   Le garder sous la main (c'est un secret : ne pas le partager).
2. Toujours dans @BotFather : `/setprivacy` → choisir le bot → **Disable** (le bot pourra lire le groupe).
3. Chaque adulte écrit à **@userinfobot** : il répond avec son identifiant (`Id: 111111111`). Les noter.
4. Créer un **groupe** Telegram avec les adultes et le bot. Y ajouter @userinfobot : il affiche l'identifiant
   du groupe (négatif, commence par `-100`). Le noter, puis retirer @userinfobot du groupe.

## Étape 2 — Copier le kit sur le serveur

Depuis votre ordinateur (remplacer `VPS` par l'adresse du serveur) :

```bash
scp firmin-assistant-0.6.0.tar.gz admin@VPS:~
ssh admin@VPS
tar -xzf firmin-assistant-0.6.0.tar.gz && cd firmin-assistant-0.6.0
```

## Étape 3 — Lancer l'installation guidée

```bash
sudo bash install/installer-un-foyer.sh
```

```
  Firmin Assistant — installation d'un nouveau foyer (0.6.0)
  Compter 30 minutes, dont 5 de questions. Les réponses restent sur ce serveur.

━━ 1/8 Questions ━━

── Bienvenue ──
   Je vais vous poser une vingtaine de questions pour installer votre assistant de famille.
   […]

── 1. L'assistant ──
 ? Quel prénom voulez-vous donner à l'assistant ? [Firmin] :
 ? Il vous tutoie ? (oui/non) [oui] :

── 2. Le foyer ──
   La commune sert à la météo du jour. Jamais d'adresse : la commune suffit.
 ? Dans quelle commune habitez-vous ? (Entrée pour passer) : Villeneuve
 ? Combien d'adultes utiliseront l'assistant ? (1 à 6) [2] : 2

   Adulte 1
 ? Prénom : Alex
 ? Identifiant technique (compte sur le serveur) [alex] :
   Rôle : propriétaire = peut régler le système en parlant à l'assistant ; membre = l'utilise seulement.
 ? Rôle (proprietaire/membre) [proprietaire] :
 ? Identifiant Telegram de Alex : 111111111
   Compte Claude : « abonnement » = son propre abonnement Claude (Pro ou Max), connecté après
   l'installation ; « cle_api » = les conversations passent par la clé API du foyer (payée à l'usage).
 ? Compte Claude (abonnement/cle_api) [abonnement] :

   Adulte 2
 ? Prénom : Camille
 […]
   Les enfants : seulement le prénom et un âge approximatif ou la classe (« 8 ans », « CE2 »).
   Pas de date de naissance, pas d'école, rien de médical.
 ? Ajouter un enfant ? (oui/non) [non] : oui
 ? Prénom de l'enfant : Nina
 ? Âge approximatif ou classe (Entrée pour passer) : CE2
 ? Ajouter un enfant ? (oui/non) [non] :
 ? Ajouter un animal ? (oui/non) [non] : oui
 ? Nom de l'animal : Rex
 ? Espèce (chien, chat…) (Entrée pour passer) : chien
 ? Ajouter un animal ? (oui/non) [non] :

── 3. Telegram ──
 ? Token du bot (Entrée pour passer) :            ← collé, rien ne s'affiche (normal)
 ? Identifiant du groupe du foyer (Entrée pour passer) : -1001234567890

── 4. Claude pour les messages programmés ──
 ? Clé API Anthropic (Entrée pour passer) :
 ? Heure du brief du matin [7h15] :
 ? Jour et heure du brief de la semaine [dim 19h] :

── 5. Agendas (facultatif) ──
 ? Agenda commun du foyer (Entrée pour passer) :
 […]
── 6. Capacités ──
 ? Êtes-vous abonnés aux plats livrés Kitchen Daily ? (oui/non) [non] :
 ? Activer les transports ? (oui/non) [oui] :
 ? Réseau local utilisé (nom, ou Entrée si aucun de ceux-là) (Entrée pour passer) :

── 7. Appli web, coffre et sauvegarde ──
 ? Installer Tailscale et l'appli web ? (oui/non) [oui] :
 ? Clé Tailscale (tskey-…) (Entrée pour passer) :
 ? Installer aussi le coffre à mots de passe Vaultwarden ? (oui/non) [non] :
 ? Configurer la copie hors du serveur à la fin ? (oui/non) [oui] :

── Récapitulatif ──
   Assistant      : Firmin (tutoiement)
   Commune        : Villeneuve
   Adulte         : Alex — compte « alex », proprietaire, Telegram 111111111, Claude abonnement
   Adulte         : Camille — compte « camille », proprietaire, Telegram 222222222, Claude abonnement
   Enfants        : Nina
   Animaux        : Rex
   Telegram       : token saisi, groupe -1001234567890
   Clé API        : à faire (pas de brief automatique sans elle)
   […]
 ? C'est bon, on installe ? (oui/non) [oui] :
```

Tout ce qui est « (Entrée pour passer) » peut être fait plus tard : l'installation le note dans la liste finale.
Ctrl+C pendant les questions arrête tout sans rien écrire.

## Étape 4 — L'installation travaille seule (10 à 20 minutes)

```
━━ 2/8 Configuration du foyer ━━
   configuration écrite : /srv/home-os/data/config.yaml
   fiche « Identité du foyer » pré-remplie (complétée avec l'assistant à la première séance)
   capacités : Kitchen Daily non, transports oui, administration oui
━━ 3/8 Réseau privé Tailscale ━━
   ouvre l'URL ci-dessous sur ton téléphone ou ton PC et connecte-toi avec le compte du foyer :
   https://login.tailscale.com/a/…
━━ 4/8 Installation du système (10 à 20 minutes) ━━
==> Paquets système
==> Node.js 22 (dépôt NodeSource ajouté explicitement, sans curl | bash)
==> Claude Code … (version épinglée, mise à jour automatique coupée)
[…]
━━ 5/8 Secrets (/etc/homeos) ━━
   secrets posés : TELEGRAM_BOT_TOKEN
   bot Telegram démarré
━━ 6/8 Comptes Claude ━━
 ? Connecter maintenant le compte Claude de « alex » (il faut son téléphone) ? (oui/non) [oui] :
```

Pour chaque adulte en « abonnement », l'installation propose de connecter son compte Claude : un lien
`https://claude.ai/oauth/authorize?…` s'affiche ; l'ouvrir **sur le téléphone de cette personne** (connecté à
SON compte Claude), accepter, puis coller le code affiché dans le terminal. Si le navigateur est déjà connecté
au compte d'un autre adulte, se déconnecter d'abord.

## Étape 5 — Vérification et liste de ce qui reste

```
━━ 8/8 Vérification ━━
==> pytest
==> ruff
[…]
==> Résumé
  pytest : OK
  ruff : OK
  bash -n : OK
  administration : OK
  serveur d'outils : OK
  appli web : non installée
  secrets : OK
━━ Terminé ━━
Firmin Assistant — installation du 08/11/2026 10:42 (0.6.0)
Assistant : Firmin ; vérification : vert

À faire par le foyer :
  1. Connecter le compte Claude de camille (avec SON téléphone) : sudo -u camille -H claude auth login
  2. Clé API Anthropic (briefs automatiques, personnes en mode clé API) : sudo nano /etc/homeos/anthropic.env
  3. Recopier RESTIC_PASSWORD (/etc/homeos/restic.env) et COFFRE_CLE (/etc/homeos/coffre.env) dans un
     gestionnaire de mots de passe : sans eux, pas de restauration
  4. Quand les comptes Claude sont connectés : sudo bash /srv/home-os/scripts/verifier.sh (test complet),
     puis écrire /start au bot

Ensuite, dans Telegram : écrire /start au bot ; l'assistant propose la première séance (20 minutes).
```

La liste est gardée dans `/root/firmin-a-faire.txt`. Chaque ligne se fait indépendamment, dans n'importe quel
ordre ; relancer `sudo bash /srv/home-os/scripts/verifier.sh` à la fin.

## Étape 6 — Première conversation

Dans le groupe Telegram, écrire `/start`. L'assistant se présente et propose la première séance : cinq
questions (qui vit ici, commune et rythme de la semaine, courses, règles de la maison, comment l'appeler).
Ce que l'installation sait déjà (prénoms, enfants, animaux, commune) n'est pas redemandé, seulement confirmé.

## Plus tard

- **Modifier un réglage** (heure d'un brief, activer une capacité) : le demander à l'assistant (propriétaires).
- **Ajouter un adulte** : relancer `sudo bash install/installer-un-foyer.sh` (la configuration actuelle est
  sauvegardée avant d'être remplacée), ou modifier `/srv/home-os/data/config.yaml` puis relancer
  `sudo bash /srv/home-os/install/install.sh`.
- **Mise à jour** : copier la nouvelle archive, puis `sudo bash install/install.sh` depuis son dossier (les
  données dans `/srv/home-os/data` sont conservées). Voir `CHANGELOG.md`.
- **Durcissement final** (après avoir vérifié la connexion SSH par clé) : `sudo bash /srv/home-os/install/install.sh --fermer-root`.

## Installation sans question (réinstallation, hébergeur, tests)

Les réponses peuvent être données dans un fichier JSON (mêmes règles de validation que les questions) :

```bash
sudo bash install/installer-un-foyer.sh --reponses reponses.json --oui
```

```json
{
  "assistant": "Firmin",
  "tutoiement": true,
  "commune": "Villeneuve",
  "adultes": [
    {"prenom": "Alex", "telegram_id": 111111111, "role": "proprietaire", "claude": "abonnement"},
    {"prenom": "Camille", "telegram_id": 222222222, "role": "proprietaire", "claude": "cle_api"}
  ],
  "enfants": [{"prenom": "Nina", "age": "CE2"}],
  "animaux": [{"nom": "Rex", "espece": "chien"}],
  "telegram_token": "",
  "groupe_telegram_id": null,
  "cle_api": "",
  "brief_jour": "7h15",
  "brief_hebdo": "dim 19h",
  "kitchen_daily": false,
  "transports": true,
  "reseaux": "",
  "tailscale": false,
  "tailscale_cle": "",
  "vaultwarden": false,
  "hors_site": false
}
```

Avec `--oui`, rien n'est demandé : ce qui demande un téléphone (comptes Claude, Tailscale sans clé, copie hors
site) part directement dans la liste « à faire par le foyer ». Le fichier contient des secrets s'il porte le
token ou la clé API : le supprimer après usage.

## En cas de problème

| Symptôme | Que faire |
| --- | --- |
| « à lancer avec sudo » | relancer la commande en la précédant de `sudo` |
| Une étape réseau échoue (paquets, téléchargements) | relancer `sudo bash install/installer-un-foyer.sh` : tout est reprenable |
| Le bot ne répond pas dans Telegram | `sudo journalctl -u homeos-bot -n 50` ; vérifier le token dans `/etc/homeos/telegram.env` et les identifiants dans `config.yaml` |
| « Vérification : ÉCHEC » | lire la ligne en échec du résumé ; `sudo bash /srv/home-os/scripts/verifier.sh --sans-claude` pour relancer |
| Perte d'accès SSH après `--fermer-root` | console de l'hébergeur (accès root de secours) |

Détails techniques : `README.md` (exploitation) et `CONTRACT.md` (architecture).
