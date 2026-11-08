# Sauvegardes et restauration (lot 5d)

Ce document explique, sans prérequis technique, comment les données du foyer sont sauvegardées, comment
mettre en place la copie **hors site** sur Google Drive, et comment restaurer. Tout repose sur des logiciels
libres : [restic](https://restic.net) (sauvegarde chiffrée) et [rclone](https://rclone.org) (accès à Google Drive).
Aucun autre service tiers.

## 1. Ce qui est sauvegardé, où et quand

| Quoi | Où | Quand |
| --- | --- | --- |
| Données du foyer (`/srv/home-os/data` : mémoire, agenda, courses, fichiers reçus, photos, base) | dépôt restic local `/var/backups/homeos` | chaque nuit à 03:30 (30 jours conservés) |
| État sensible (`/var/lib/homeos` : coffre chiffré, session Kitchen Daily, sessions du bot) | même dépôt | idem |
| Copie hors site des mêmes sauvegardes | Google Drive du foyer, dossier `homeos-sauvegardes` | juste après chaque sauvegarde locale (30 jours + 12 mois) |
| Test de restauration | restauration complète dans un dossier temporaire, vérifiée puis effacée | le 3 de chaque mois à 05:10 |

Tout est **chiffré avant de quitter le serveur** : Google ne voit que des fichiers illisibles.

Les **documents sensibles** de l'appli (`data/fichiers/sensibles/`) sont sauvegardés avec le reste, mais ils sont
déjà chiffrés dans le navigateur avec une clé que le serveur n'a pas : après une restauration, chacun les rouvre avec
sa passkey ou sa **phrase de récupération** (à garder sur papier, elle ne figure dans aucune sauvegarde ;
`docs/documents-sensibles.md`).

**Ce qui n'est jamais sauvegardé** : les secrets de `/etc/homeos` (mots de passe de sauvegarde, clé du coffre,
token Telegram, clé API). Sans eux, une sauvegarde est illisible : ils se gardent **dans vos gestionnaires de
mots de passe**, hors du serveur. À y ranger (une fiche « Firmin Assistant » partagée entre vous deux) :

- `RESTIC_PASSWORD` (dans `/etc/homeos/restic.env`) — sauvegarde locale ;
- `RESTIC_PASSWORD_DISTANT` (affiché une seule fois par le script de mise en place) — sauvegarde Google Drive ;
- `COFFRE_CLE` (dans `/etc/homeos/coffre.env`) — identifiants des services rangés dans le coffre ;
- `TELEGRAM_BOT_TOKEN` et `ANTHROPIC_API_KEY` (récupérables auprès de BotFather et de la console Anthropic,
  mais plus simple à recopier) ; les URLs iCal se régénèrent dans Google Agenda.

Pour lire une valeur : `sudo cat /etc/homeos/restic.env` (sur le serveur, depuis le compte `admin`).

## 2. Mettre en place la copie sur Google Drive (une fois, 15 minutes)

Il faut : le compte `admin` du serveur, un ordinateur avec un navigateur, et le compte Google qui recevra
les sauvegardes (le Drive du foyer).

### 2.1 Installer rclone sur l'ordinateur

Google demande une autorisation dans un navigateur ; le serveur n'en a pas. On fait donc cette étape sur
l'ordinateur, avec rclone :

- Mac : `brew install rclone` (ou télécharger depuis <https://rclone.org/downloads/>) ;
- Windows : télécharger l'archive sur <https://rclone.org/downloads/>, la décompresser, ouvrir un terminal
  dans le dossier.

### 2.2 Lancer le script sur le serveur

```bash
ssh admin@<VPS>
sudo bash /srv/home-os/scripts/configurer-hors-site.sh
```

Le script installe rclone sur le serveur s'il manque, puis ouvre l'assistant `rclone config`. Répondre ainsi
(appuyer sur Entrée = accepter la valeur proposée) :

| Question affichée | Réponse |
| --- | --- |
| `e/n/d/r/c/s/q>` | `n` (nouveau) |
| `name>` | `gdrive` |
| `Storage>` | `drive` (Google Drive) |
| `client_id>` | Entrée (vide) |
| `client_secret>` | Entrée (vide) |
| `scope>` | `drive.file` — rclone ne verra **que** les fichiers qu'il crée, jamais le reste du Drive |
| `service_account_file>` | Entrée (vide) |
| `Edit advanced config?` | `n` |
| `Use web browser to automatically authenticate…?` | `n` (le serveur n'a pas de navigateur) |

rclone affiche alors une commande du type `rclone authorize "drive" "eyJzY29wZSI6…"`. **Sur l'ordinateur**, dans
un terminal, coller cette commande exacte : le navigateur s'ouvre, choisir le compte Google du foyer,
accepter. Le terminal de l'ordinateur affiche un long jeton entre deux lignes `--->` et `<---End paste` :
le copier **en entier** et le coller dans le terminal du serveur (question `config_token>`).

Puis : `Configure this as a Shared Drive?` → `n` ; `Keep this "gdrive" remote?` → `y` ; menu → `q`.

### 2.3 Ranger le mot de passe

Le script crée un dossier `homeos-sauvegardes` sur le Drive, puis **affiche une seule fois** le mot de passe
de la sauvegarde distante. Le copier immédiatement dans vos deux gestionnaires de mots de passe, puis
appuyer sur Entrée. Il initialise ensuite le dépôt distant et lance une première sauvegarde (quelques minutes).

### 2.4 Vérifier

- Dans Telegram : `/statut` → « Copie hors site » affiche la date du jour.
- Sur le serveur : `sudo systemctl start homeos-restaurer-test`, puis
  `journalctl -u homeos-restaurer-test -n 5` doit finir par « OK : … fichiers, base intègre ». `/statut` affiche
  « Restauration testée : ✅ ».
- Dans Google Drive : le dossier `homeos-sauvegardes` contient des sous-dossiers `data`, `index`, `keys`…
  **Ne jamais les déplacer ni les supprimer à la main** : cela casserait la sauvegarde.

Le healthcheck prévient dans le groupe Telegram si la copie hors site a plus de 48 h ou si le test de
restauration mensuel échoue.

## 3. Restaurer

Toujours depuis le compte `admin`. Les commandes `restic` ont besoin du mot de passe ; on le charge depuis
`/etc/homeos/restic.env` sans l'afficher.

### 3.1 Récupérer un fichier ou une version précédente (serveur intact)

```bash
sudo -i                                    # session root le temps de la manipulation
set -a; . /etc/homeos/restic.env; set +a   # RESTIC_PASSWORD (dépôt local)
export RESTIC_REPOSITORY=/var/backups/homeos
restic snapshots                           # liste des sauvegardes (date, identifiant)
restic restore <identifiant> --target /var/tmp/restauration --include /srv/home-os/data/foyer
ls /var/tmp/restauration/srv/home-os/data/foyer   # comparer, recopier ce qui manque, puis :
rm -rf /var/tmp/restauration ; exit
```

### 3.2 Tout remettre en place sur le même serveur

```bash
sudo systemctl stop homeos-bot
sudo -i
set -a; . /etc/homeos/restic.env; set +a
export RESTIC_REPOSITORY=/var/backups/homeos
restic snapshots
restic restore <identifiant> --target /        # remet /srv/home-os/data et /var/lib/homeos
exit
sudo bash /srv/home-os/install/install.sh       # remet les droits en place
sudo systemctl start homeos-bot
```

### 3.3 Le serveur a disparu : nouveau VPS depuis Google Drive

1. Nouveau VPS Ubuntu 24.04, compte `admin` avec ta clé SSH (comme la première fois).
2. Copier le kit et installer : `sudo bash install/install.sh` (voir README). L'installation crée des
   secrets neufs : on les remplace à l'étape suivante.
3. Remettre les secrets depuis le gestionnaire de mots de passe :
   `sudo nano /etc/homeos/restic.env` (`RESTIC_PASSWORD` d'origine, et les lignes
   `RESTIC_REPOSITORY_DISTANT=rclone:gdrive:homeos-sauvegardes`, `RESTIC_PASSWORD_DISTANT=…`,
   `RCLONE_CONFIG=/var/lib/homeos/rclone/rclone.conf`), `sudo nano /etc/homeos/coffre.env` (`COFFRE_CLE`
   d'origine), `telegram.env`, `anthropic.env`, `ical.env`. Le dépôt local créé à l'étape 2 l'a été avec le
   mot de passe neuf : l'effacer (`sudo rm -rf /var/backups/homeos`), install.sh le recréera à l'étape 5.
4. Reconnecter Google Drive : `sudo bash /srv/home-os/scripts/configurer-hors-site.sh` (mêmes réponses qu'au
   §2.2 ; le mot de passe distant déjà présent n'est pas regénéré, le dépôt existant est reconnu).
5. Restaurer depuis le Drive :

   ```bash
   sudo systemctl stop homeos-bot
   sudo -i
   set -a; . /etc/homeos/restic.env; set +a
   export RESTIC_REPOSITORY="$RESTIC_REPOSITORY_DISTANT" RESTIC_PASSWORD="$RESTIC_PASSWORD_DISTANT"
   restic snapshots
   restic restore latest --target /
   exit
   sudo bash /srv/home-os/install/install.sh
   ```
6. Reconnecter les comptes Claude (`sudo -u <personne> -H claude auth login`), puis `/statut` dans Telegram.

## 4. Questions fréquentes

- **Combien de place sur le Drive ?** Quelques centaines de Mo au plus (la sauvegarde ne stocke que ce qui change).
- **Google peut-il lire nos données ?** Non : tout est chiffré par restic avec `RESTIC_PASSWORD_DISTANT` avant l'envoi.
- **rclone voit-il tout notre Drive ?** Non, avec la portée `drive.file` il ne voit que ce qu'il a créé.
- **Révoquer l'accès** : <https://myaccount.google.com/permissions> → rclone → supprimer l'accès (la copie
  hors site s'arrête ; le healthcheck le signalera sous 48 h).
- **Le jeton Google expire ?** rclone le renouvelle seul (il est conservé dans
  `/var/lib/homeos/rclone/rclone.conf`, lisible par `homeos` seulement, jamais sauvegardé).
