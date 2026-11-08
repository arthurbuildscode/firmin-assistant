# Spec lot 2 — Kitchen Daily

Objectif : depuis Telegram, « choisis les plats de la semaine » → l'assistant lit le menu, propose 18 plats selon les préférences,
et sur « Valider » enregistre la sélection sur kitchen-daily.fr. Rappel automatique avant la date limite.

Référence site : `docs/kitchen-daily-site.md`. Contrat général : `CONTRACT.md` (outils MCP §6, confirmations §5).

## Composants

### `tools/kitchen_daily.py` (module Playwright, Python, sync API, Chromium headless)
- État de session : `data/.kd/state.json` (storage_state Playwright : cookies + localStorage), chmod 600, groupe foyer.
- `login()` : CLI `python -m tools.kitchen_daily login` — demande e-mail et mot de passe avec `getpass` (jamais d'argument CLI,
  jamais de fichier), ouvre le site, ferme la bannière cookies (« Continuer sans accepter »), clique « Compte », remplit la modale,
  « Connexion », attend le lien « Mon Espace », sauvegarde `state.json`. Les identifiants ne sont écrits nulle part.
- `session_valide() -> bool` : charge le state, ouvre `/account/shippings/upcoming`, vérifie la présence de « Mes livraisons ».
- `livraisons() -> list[dict]` : parse la page upcoming → `[{id, date (ISO), creneau, numero, montant, statut, modifiable_jusqua (ISO),
  prelevement_le, modifiable: bool}]`.
- `menu(livraison_id) -> dict` : ouvre `/account/shippings/edit/select-meal/quotidien/<id>`, retourne
  `{livraison_id, formule_plats: 18, plats: [{index, titre, badges: [...], selectionne: bool|None, quantite: int|None}], etapes: [...]}`.
  Déterminer l'état « sélectionné » (voir doc site : à investiguer avec une capture ; stratégie de repli : « Remise à zéro » n'est utilisée
  QUE lors de l'application d'une sélection, jamais en lecture).
- `appliquer_selection(livraison_id, selection: dict[str, int], dry_run=False) -> dict` : sur la page de sélection, « Remise à zéro »,
  puis pour chaque plat (correspondance par titre normalisé, tolérante aux accents/majuscules) cliquer `+` quantité fois ; vérifier que le
  compteur affiche `N / 18 PLATS` avec N = somme ; capture d'écran `data/.kd/captures/<date>-avant.png` ; si `dry_run` s'arrêter là,
  sinon cliquer le « Je valide » de la barre du bas, attendre la confirmation (retour page détail ou toast), capture `-apres.png`,
  relire `menu()` et renvoyer `{ok, compteur, selection_appliquee, captures}`.
- Robustesse : timeouts 30 s, `page.set_default_timeout`, messages d'erreur en français et explicites (session expirée → « Session Kitchen
  Daily expirée : lance `python -m tools.kitchen_daily login` sur le serveur »), jamais de secret dans les logs.
- Tests : `tests/test_kitchen_daily.py` avec des pages HTML fixtures statiques (`tests/fixtures/kd_upcoming.html`, `kd_select.html`)
  servies via `page.set_content` ou `file://` ; tester le parsing de `livraisons()` et `menu()` et la normalisation des titres. Playwright
  et Chromium sont installés dans le venv de dev (`playwright install chromium`) ; les tests sont marqués `skip` si Chromium absent.

### Outils MCP (dans `tools/server.py`, noms exacts)
- `kitchen_daily_livraisons()` → liste des livraisons à venir avec date limite de modification.
- `kitchen_daily_menu(livraison_id: str)` → plats disponibles (titre, badges) + formule + date limite.
- `kitchen_daily_preferences()` → concatène `data/alimentation/*.md` + historique (`data/alimentation/historique.md`) : c'est le modèle
  qui choisit, pas l'outil.
- `kitchen_daily_proposer(livraison_id: str, selection: dict[str,int], justification: str)` → vérifie que la somme = formule et que
  chaque titre existe dans le menu, enregistre une action `pending` (kind `kitchen_daily_selection`, payload {livraison_id, selection,
  justification}) et renvoie `CONFIRMATION_REQUISE:<id>:<résumé court : « 12 plats pour la livraison du 12/11 »>`.
- `kitchen_daily_historique_ajouter(livraison_date, plats: list[str], notes: str|None)` → ajoute à `data/alimentation/historique.md`
  (date, plats, notes) pour éviter les répétitions et mémoriser les avis.
- `runner.py execute` : kind `kitchen_daily_selection` → `appliquer_selection(...)` puis `historique_ajouter`, résultat avec le chemin des
  captures (le bot les enverra dans le groupe si le fichier existe — ajouter cette capacité au bot : si `result` contient
  `CAPTURE:<chemin>`, envoyer la photo).

### Rappel et proposition automatiques (`tools/runner.py rappel kitchen-daily`)
- Enrichir les données injectées : `livraisons` (réel, via `livraisons()`) et `menu` de la prochaine livraison modifiable, `date_limite`
  calculée (modifiable_jusqua), `preferences`.
- Le prompt `content/prompts/rappel-kitchen-daily.md` est à mettre à jour (chantier contenu) : le rappel inclut une proposition de 18 plats
  **sans l'appliquer** ; l'utilisateur répond au bot pour valider (le bot, en conversation, appellera `kitchen_daily_proposer`).
- Timer : passer `rappel_kitchen_daily` à « Wed 18:00 » par défaut (date limite vendredi 23h59), configurable.

### Installation
- `install/install.sh` : `.venv/bin/pip install playwright` puis `.venv/bin/playwright install --with-deps chromium` (en root ; les
  navigateurs dans `/opt/playwright` via `PLAYWRIGHT_BROWSERS_PATH=/opt/playwright`, lisible par tous — exporter cette variable dans les
  unités systemd et dans `tools/mcp.json` env).
- `tools/requirements.txt` : `playwright>=1.47`.
- README : section « Kitchen Daily » (login une fois, test `python -m tools.kitchen_daily livraisons`).

### Contenu (chantier contenu)
- `content/CLAUDE.md` : section Kitchen Daily : quand utiliser chaque outil, règle « proposer d'abord, jamais appliquer sans
  CONFIRMATION_REQUISE », équilibre (variété, pas de répétition sur 3 semaines via historique, préférences adultes/enfants, badges),
  format de la proposition (liste numérotée, 1 ligne par plat, pourquoi en 3 mots), demander la note après livraison.
- `content/onboarding/10-alimentation.md` : ajouter le déclencheur « avant la première proposition Kitchen Daily ».
- `content/prompts/rappel-kitchen-daily.md` : nouvelle version avec clés `livraisons`, `menu`, `preferences`, `date_limite`.
