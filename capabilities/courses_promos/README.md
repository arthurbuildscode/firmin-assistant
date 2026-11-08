# Capacité « courses & promos » — notes de conception

Spec : `docs/spec-capacite-courses-promos.md`. Ce premier lot couvre l'apprentissage par les tickets de caisse,
sans aucune lecture de site marchand.

## Ce qui est livré

- `tickets.py` : lecture d'un ticket photographié (consigne pour le modèle + validation stricte du JSON renvoyé)
  et d'un ticket reçu par mail (texte, HTML, `.eml`, PDF via `pypdf`), avec un gabarit générique déterministe ;
  filtre des données personnelles et bancaires avant tout appel au modèle et avant tout stockage.
- `stockage.py` : base SQLite `data/capacites/courses_promos/promos.sqlite` (`tickets`, `achats`,
  `articles_canoniques`, `alias`, `prix_observes`), prix de référence (médiane hors promo sur 90 jours).
- `economies.py` : compteur d'économies selon la définition de la spec §7 (choix suscités et suivis seulement,
  référence observée et figée, pertes comptées, estimations confirmées ou annulées par les tickets).
- Outils `promos_ticket_consigne`, `promos_ticket_enregistrer`, `promos_ticket_importer`, `promos_associer`,
  `promos_historique`, `promos_prix_habituels`, `promos_economies` (carte `economies`).
- Routine `veille_promos` déclarée mais **inerte** tant que le paramètre `mode` vaut `a_choisir`.

## Décisions

- **Aucun contournement anti-robot** (spec §2.2) : pas de proxy résidentiel, pas de solveur de captcha, pas
  d'usurpation d'empreinte. Un site qui répond par un défi ou un blocage est indisponible, point.
- **`robots.txt` et CGU respectés** pour tout parcours automatisé futur ; `reseau: []` dans ce lot.
- **Voies légitimes d'abord** : tickets dématérialisés (mail, espace client, appli de fidélité), export de
  l'espace client, droit d'accès et de portabilité (RGPD art. 15 et 20), prix communautaires Open Prices,
  photo du ticket ou de l'étiquette.
- Les noms d'outils sont préfixés `promos_` : les outils `courses_*` sont ceux de la liste de courses du socle.
- Rien ne dépend d'une enseigne en particulier : l'enseigne et le magasin sont des paramètres du foyer.
