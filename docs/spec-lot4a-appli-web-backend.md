# Spec lot 4a — Appli web : backend conversationnel

Objectif : remplacer Telegram comme point d'entrée (décision du porteur du projet) par une appli web auto-hébergée, accessible via Tailscale, qui offre la **même conversation** que le bot (même moteur `claude -p` par personne, mêmes outils via le serveur d'outils du lot 7, mêmes confirmations) avec des **cartes génératives**, la voix, et une saisie sécurisée des identifiants. Ce lot = le backend + le contrat des cartes ; le lot 4b = le front (à partir de la maquette v1.x, shadcn, palettes).

## 1. Stack (open source, sur le VPS)

- Python 3.12, **FastAPI** + uvicorn (déjà présent pour le serveur d'outils), **SSE** pour le streaming (`sse-starlette`), SQLite existant + nouvelle table `conversations`/`messages`, fichiers statiques du front servis par FastAPI.
- Service systemd `homeos-web-app.service` (User=homeos, profil strict du lot 5d, `SupplementaryGroups=homeos`), écoute sur l'adresse Tailscale du serveur uniquement (`tailscale ip -4`, port 8443 via `tailscale serve --https=8443` ou 443 avec chemin `/app` — choisir et documenter ; Vaultwarden occupe déjà `https://<magicdns>/`). Aucun port public.
- Réutilise `bot/claude_runner.py` (extraire la logique commune dans `coeur/conversation.py` : construction de la commande, prompt système par personne, parsing stream-json, confirmations) — Telegram et le web deviennent deux **canaux** du même cœur (préfigure le lot 6 §3). Pas de duplication : le bot est refactoré pour appeler le cœur.

## 2. Authentification et sessions

- **Passkeys (WebAuthn)** avec `py_webauthn` : enrôlement initial par un lien à usage unique généré sur le serveur (`python -m web.invitation <personne>` → URL avec jeton 10 min, affichée dans le terminal ou envoyée par Telegram à la personne), puis connexion par passkey (empreinte/Face) ; plusieurs passkeys par personne (téléphone, PC). Repli : code à usage unique envoyé par Telegram.
- Session = cookie HttpOnly/Secure/SameSite=Strict, 30 jours, renouvelé ; table `web_sessions`. Révocation par personne (liste des appareils dans Réglages).
- **Step-up** (décision du porteur du projet) : un endpoint `POST /auth/step-up` qui exige une nouvelle assertion passkey avec `userVerification: required` et renvoie un jeton 5 min lié à la session ; les actions sensibles (`coffre`, documents sensibles, administration, paiements à venir) l'exigent. Documenté dans CONTRACT §12.
- Identité → `linux_user` ; chaque requête de conversation lance `claude -p` sous cette personne (wrapper) avec son jeton d'outils, exactement comme le bot. Personnes en `cle_api` : `--bare` + `mcp-<personne>.json` comme dans le bot.

## 3. API

- `GET /api/conversations` ; `POST /api/conversations` (nouvelle) ; `GET /api/conversations/{id}/messages`.
- `POST /api/conversations/{id}/messages` : corps `{texte, pieces_jointes: [ids], contexte: {ecran, objet}}` → réponse SSE : événements `etape` (ligne de réflexion : `{"etape": "Je lis l'agenda…", "outil": "agenda_lire", "debut": ts}` dérivée des `tool_use` du flux), `texte` (deltas), `carte` (voir §4), `confirmation` (`{id, resume, auto}`), `fin` (`{duree_ms, cout_usd, etapes: [...]}`).
- `POST /api/confirmations/{id}` `{decision: valider|annuler}` → même chemin que le bouton Telegram (`bot/confirmations.executer_action` via le cœur), diffusion du résultat en SSE à la conversation (`GET /api/conversations/{id}/flux` : flux d'événements long-vivant par conversation pour les résultats asynchrones, routines, messages spontanés).
- `POST /api/fichiers` (upload photo/document, limites comme le bot, rangé dans `data/fichiers/entrants`) → le message suivant reçoit `[fichier reçu: …]`.
- `POST /api/voix/transcrire` (audio → faster-whisper déjà présent) ; `POST /api/voix/synthese` (texte → audio) avec **Piper** (open source, voix française `fr_FR-siwis-medium` ou `upmc`) installé dans `/opt/piper`, cache des phrases ; si Piper indisponible, le front utilise la synthèse du navigateur.
- `GET /api/brief/jour?date=` (carte du jour, feuilletable J+1, J+2) construit à partir des outils (agenda, échéances, courses, météo via Open-Meteo, open source et sans clé, commune du foyer).
- `GET /api/etat` (pour `/admin`-like), `GET /api/routines`, `PATCH /api/routines/{capacite}/{nom}` (délègue à `tools/administration`).
- `POST /api/coffre/{capacite}` : **carte identifiants** — corps chiffré ? Non : TLS Tailscale suffit ; le serveur écrit directement via `tools/coffre.ecrire` sous step-up, ne journalise jamais les valeurs, renvoie `{ok, champs}`. Le modèle n'est jamais dans la boucle.
- Toutes les routes : CSRF (en-tête `X-Requested-With` + SameSite), limites de taille, journal `actions_log` avec `who`.

## 4. Contrat des cartes génératives

Une carte = JSON `{type, id, titre?, donnees, actions: [{libelle, action, args, confirmation?}]}` ; types v1 : `bonjour`, `brief`, `a_noter`, `agenda_jour`, `evenement`, `courses`, `repas_semaine`, `kitchen_daily_proposition` (plats avec image réelle du site, quantités, filtres préférences), `kitchen_daily_extras`, `echeance`, `comparatif`, `fichier_classe`, `document_sensible`, `bus`, `routine_resultat`, `identifiants` (formulaire rendu par le front, soumis à `/api/coffre`), `reflexion` (étapes). Comment une carte naît : (a) un outil renvoie dans son résultat une ligne `CARTE:<json>` que le cœur extrait (comme `CONFIRMATION_REQUISE`/`CAPTURE`) ; (b) le cœur génère lui-même `bonjour`/`brief`/`a_noter` au premier message du jour. Les outils existants (`courses_lister`, `agenda_lire`, `kitchen_daily_menu`…) ajoutent cette ligne quand `HOMEOS_CANAL=web` (variable passée au serveur d'outils via l'en-tête `X-Homeos-Canal`, transmise dans le ContextVar). Documenter dans CONTRACT §14 et dans le skill ux-capacites (une capacité fournit ses cartes).

## 5. Télégram ↔ web

Même conversation : les messages Telegram et web d'une personne partagent la session Claude (`sessions.json`) et la table `messages` ; une réponse donnée sur un canal est visible sur l'autre (le bot écrit aussi dans `messages`). Cartes sur Telegram = texte + boutons (rendu dégradé par type).

## 6. Livrables et vérification

- `web/` (app FastAPI), `coeur/` (conversation partagée), migration du bot vers le cœur sans régression (tests existants verts), `install/systemd/homeos-web-app.service`, `install.sh` (Piper, service, `tailscale serve` pour l'app), `scripts/verifier.sh` (sonde `/api/sante`), README « Appli web ».
- Tests : auth passkey (avec `webauthn` en mode test), step-up, SSE (étapes, texte, carte, confirmation), extraction `CARTE:`, upload, coffre via API (jamais de valeur dans les logs), brief du jour, partage de session Telegram/web.
- Un front minimal de démonstration (une page HTML sans framework, 1 fichier) qui permet de se connecter, d'envoyer un message et de voir le flux SSE et les cartes en JSON brut — juste pour valider le backend ; le vrai front est le lot 4b.
