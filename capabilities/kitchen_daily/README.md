# Capacité Kitchen Daily

Plats cuisinés livrés : lecture des livraisons et du menu sur le site, proposition de la sélection de la
semaine, enregistrement sur le site **après « Valider »** dans Telegram (action `kitchen_daily_selection`) ;
extras (desserts, entrées, jus) et semaines sautées (lot 2b, actions `kitchen_daily_extras_definir` et
`kitchen_daily_livraison_definir`, toujours validées par bouton ; le dessert offert seul est pris sans confirmation).

- Bibliothèque et CLI : `tools/kitchen_daily.py` (`python -m tools.kitchen_daily
  login|statut|livraisons|menu|appliquer|extras|extras-definir|livraison`). Exploration du site : `docs/kitchen-daily-site.md`.
- Outils : `kitchen_daily_livraisons`, `kitchen_daily_menu`, `kitchen_daily_preferences`,
  `kitchen_daily_proposer` (action avec effet, renvoie `CONFIRMATION_REQUISE`), `kitchen_daily_historique_ajouter`,
  `kitchen_daily_extras`, `kitchen_daily_extras_definir` et `kitchen_daily_livraison_definir` (actions avec effet,
  listées dans `confirmation_toujours` : jamais exécutées sans bouton, même en validation automatique).
- Cron `rappel` (mercredi 18:00) → timer `homeos-cap-kitchen_daily-rappel` ; l'horaire se règle dans
  `config.yaml` (`capacites.kitchen_daily.crons.rappel`, ou l'ancienne clé `crons.rappel_kitchen_daily`).
- Smoke test : validité de la session du site (`statut`), vérifiée au plus une fois par heure par le healthcheck.
- Préférences : `data/alimentation/*.md`, remplies par le module commun `content/onboarding/10-alimentation.md`.
- Pas de secret dans le coffre : la session est un état (`/var/lib/homeos/kd/state.json`, `data/.kd/` en développement), les identifiants ne sont jamais conservés.
