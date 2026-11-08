# Lot 2 — Kitchen Daily : notes pour le chantier contenu et l'intégration

Écrit par le chantier outils. À intégrer par l'orchestrateur (fichiers hors périmètre outils).

## `content/CLAUDE.md` — section Kitchen Daily (lignes proposées)

- Outils : `kitchen_daily_livraisons()` (livraisons à venir, `id`, `modifiable_jusqua`, `modifiable`),
  `kitchen_daily_menu(livraison_id)` (plats, badges, `formule_plats`, `date_limite`),
  `kitchen_daily_preferences()` (goûts + historique), `kitchen_daily_proposer(livraison_id, selection, justification)`,
  `kitchen_daily_historique_ajouter(livraison_date, plats, notes)`.
- Ordre : livraisons → menu de la prochaine livraison `modifiable: true` → préférences → proposition en texte →
  quand la personne est d'accord, `kitchen_daily_proposer` et restituer `CONFIRMATION_REQUISE:…` tel quel.
  Jamais d'application sans ce bouton.
- `selection` : `{titre exact du menu: quantité}`, somme = `formule_plats` (18). Recopier les titres du menu ;
  l'outil tolère accents/majuscules mais refuse un titre absent ou ambigu (corriger et rappeler l'outil).
- Les outils lisent le site en direct : compter quelques secondes, ne pas les appeler en boucle.
- `selectionne` / `quantite` peuvent valoir `null` (`detection_selection: "indeterminee"`) : ne pas affirmer
  quels plats sont « déjà choisis » dans ce cas ; la validation remplace de toute façon toute la sélection.
- Erreur « Session Kitchen Daily expirée… » : la transmettre telle quelle (action manuelle sur le serveur, pas
  de demande d'identifiants dans Telegram — ne jamais demander l'e-mail ni le mot de passe du site).
- Après validation, le bot poste le résultat et une capture d'écran dans le groupe ; la sélection validée est
  déjà ajoutée à `alimentation/historique.md`. Après la livraison, demander un avis et le noter avec
  `kitchen_daily_historique_ajouter` (notes seulement, ou `memoire_ajouter` sur les fiches de goûts).
- Ne jamais toucher à l'interrupteur « Je veux ma livraison », à la formule ni aux extras (aucun outil ne le fait).

## `content/prompts/rappel-kitchen-daily.md` — clés injectées par `runner rappel kitchen-daily`

- `livraisons` : liste (même format que `kitchen_daily_livraisons`).
- `livraison_cible` : prochaine livraison modifiable (ou `null`).
- `menu` : menu de `livraison_cible` (`formule_plats`, `plats[].titre/badges/selectionne`, `detection_selection`).
- `date_limite` : `modifiable_jusqua` de la cible (ISO, ex. `2026-11-06T23:59:00+01:00`), repli `kitchen_daily.date_limite` de config.yaml.
- `preferences` : `data/alimentation/*.md` + 80 dernières lignes d'`historique.md`.
- `kitchen_daily_erreur` : présent **à la place** de `livraisons`/`menu` si le site n'a pas pu être lu (session
  expirée, Chromium absent, site modifié) — le rappel doit alors le dire en une ligne (« relancer le login sur le serveur »).
- `alimentation` : alias temporaire de `preferences` (ancien prompt) ; à retirer de `tools/runner.py` quand le prompt est à jour.
- Le timer passe à « Wed 18:00 » (config.example.yaml, défaut d'install.sh) : le titre du prompt « (mardi soir) » est à ajuster.

## Intégration (hors périmètre outils)

- `tests/test_tools.py::test_server_outils_exposes` : ajouter
  `attendus |= {"kitchen_daily_livraisons", "kitchen_daily_menu", "kitchen_daily_preferences", "kitchen_daily_proposer", "kitchen_daily_historique_ajouter"}  # lot 2`.
- `bot/handlers.py` (facultatif) : `confirmations.traiter_callback(..., envoyer_photo=...)` accepte un envoi de photo
  dans la conversation d'origine ; sans ce paramètre, les captures partent dans le groupe (`groupe_telegram_id`).
- `install/systemd/homeos-rappel-kitchen-daily.timer` : la valeur par défaut `Tue` reste dans le fichier, mais le
  drop-in généré par install.sh impose désormais `Wed 18:00` par défaut. `CONTRACT.md` §4 indique encore `Tue 18:00`.
