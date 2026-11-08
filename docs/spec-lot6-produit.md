# Spec lot 6 — « Le kit est un produit, le premier foyer est une instance »

décision du porteur du projet (7 nov.) : tout doit être facilement duplicable et vendable en paiement unique à quelqu'un qui l'installe sur son VPS avec sa propre licence Claude Code ou une autre IA. **Aucune donnée personnelle en dur dans le code**, tout par configuration, sécurisé par défaut. À exécuter après la fusion du lot 5 (capacités, coffre, bus).

## 1. Séparation produit / instance

- `home-os` (ce dépôt) = produit. Interdit : prénoms, identifiants Telegram, lieux, noms d'animaux, exemples personnels, captures contenant des données du foyer. Un test `tests/test_generique.py` grep l'arborescence (hors `docs/exemples-anonymes/`) sur une liste de motifs interdits fournie par un fichier **hors dépôt** (`HOMEOS_MOTIFS_INTERDITS`, défaut : liste vide + motifs génériques `telegram_id` numériques en dur) et échoue si trouvé. Le test est aussi exécuté par un hook pre-commit.
- Dépôt privé de configuration du premier foyer (à créer par le porteur du projet, hors de ce dépôt) : `config.yaml`, specs et notes contenant des exemples réels (`docs/spec-lot2…` actuels y migrent, remplacés dans le produit par des versions anonymisées), maquette avec données de démo génériques dans le produit et jeu de données du premier foyer dans la config privée.
- `docs/` du produit : CONTRACT, README, docs techniques, specs anonymisées (prénoms → « Alice/Bruno », enfants « Nina/Sam », animal « le chien », ville « la commune »).

## 2. Tout dérive de `config.yaml`

Schéma (extension du §4 de CONTRACT.md) :

```yaml
assistant: { nom: "...", tutoiement: true, langue: fr, fuseau: Europe/Paris }
foyer:
  nom: "..."              # affiché dans l'appli
  lieu: "..."             # météo, événements locaux
  adultes: [alice, bruno] # slugs = utilisateurs Linux = clés de personnes[]
  enfants: [{ prenom: "...", naissance: 2022-05 }]
  animaux: [{ nom: "...", espece: chien }]
personnes:
  alice: { prenom: Alice, telegram_id: 0, auth: abonnement|cle_api, agendas: [...] }
canaux: { telegram: { actif: true, groupe_id: 0 }, web: { actif: false } }
ia: { fournisseur: claude_code|anthropic_api|gemini_cli|codex_cli, modele: ... }
coffre: { backend: fichier|vaultwarden }
```

- `install.sh` crée les utilisateurs Linux, sudoers, dossiers mémoire et agendas **à partir de `foyer.adultes`** (plus de `alex`/`camille` en dur ; idempotent si on ajoute une personne).
- `tools/memoire.py` : fichiers par défaut générés depuis `personnes` ; `scripts/healthcheck.sh` : plus de repli en dur.
- Prompts (`content/CLAUDE.md`, `content/prompts/*.md`, onboarding, capacités) : variables `{{assistant.nom}}`, `{{foyer.adultes_prenoms}}` (« Alice et Bruno »), `{{foyer.lieu}}`, `{{foyer.enfants}}`, `{{foyer.animaux}}`, `{{personne.prenom}}` ; rendu par `tools/common.rendre_prompt()` ; test qui rend chaque prompt avec une config fictive et vérifie qu'il ne reste aucun `{{`.
- Les noms d'agendas (Famille, par personne) viennent de la config, pas du code.

## 3. Canal découplé

- `bot/` devient `canaux/telegram/` ; interface `canaux/base.py` : `Canal.recevoir(message: MessageEntrant) -> None`, `Canal.envoyer(personne, reponse: Reponse)` où `Reponse` porte texte, pièces jointes, **cartes** (structure JSON de generative UI : type, données) et **demandes de confirmation** ; Telegram rend les cartes en texte + boutons, l'appli web (lot 7) les rend en composants.
- Le cœur conversationnel (`coeur/conversation.py`) : identification de la personne, session, appel du fournisseur d'IA, confirmations, journal — indépendant du canal. Tests du cœur sans Telegram.

## 4. Fournisseur d'IA découplé

- `ia/base.py` : `Fournisseur.repondre(personne, prompt_systeme, messages, outils_mcp, session_id) -> flux d'événements` (texte partiel, appel d'outil, fin, usage).
- `ia/claude_code.py` (actuel `claude_runner.py`, abonnement ou clé API), `ia/anthropic_api.py` (SDK officiel, outils MCP via le serveur homeos en stdio → adaptation en outils d'API), `ia/gemini_cli.py` et `ia/codex_cli.py` au minimum documentés avec un squelette et un test de contrat (peuvent rester `NotImplementedError` tant que non demandés, mais l'interface est fixée).
- Choix par `ia.fournisseur` ; par personne possible (`personnes.x.ia`).

## 5. Installateur

- `install/configurer.py` interactif (questions : prénoms et slugs, enfants, animaux, lieu, nom de l'assistant, canal, fournisseur d'IA, fuseau) → génère `config.yaml` et `secrets.env` (clés vides à remplir, mots de passe générés), relançable, jamais de valeur par défaut personnelle.
- `install.sh` appelle `configurer.py` si `config.yaml` est absent. README réécrit pour un acheteur : prérequis, 15 minutes, aucune mention du premier foyer.
- `make verifier` (ou `scripts/verifier.sh`) : tests, ruff, test générique, lint des manifestes de capacités.

## 6. Licence et livraison

- `LICENSE` : licence propriétaire à paiement unique, usage sur un foyer, modifications autorisées pour usage propre, pas de redistribution (texte de base en français, mention « à faire relire par un juriste »). `CHANGELOG.md`, tags `v0.x`, `docs/mise-a-jour.md`.
- Dépendances : vérifier que toutes les licences des bibliothèques sont compatibles (MIT/BSD/Apache/MPL) ; lister dans `docs/licences-tierces.md`.

## 7. Migration de la première instance

- Script `scripts/migrer-v6.sh` : renomme les fichiers mémoire si besoin, régénère les prompts, ne touche pas aux données. Déploiement sur le VPS sans perte (sauvegarde restic avant).
- Vérification : suite verte, `test_generique` vert, `/statut` et un échange Telegram fonctionnels, brief du jour identique en contenu.
