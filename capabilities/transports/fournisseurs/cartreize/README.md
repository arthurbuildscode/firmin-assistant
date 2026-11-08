# Fournisseur « cartreize » — cars de la Métropole Aix-Marseille-Provence

Ex-capacité « bus » (lot 5c), devenue un fournisseur standard de la capacité transports (`docs/transports.md`).
Grille théorique GTFS et temps réel SIRI, en données ouvertes, sans identifiant. Portée **locale** : le réseau n'est
proposé qu'à un foyer qui l'a choisi (paramètre `fournisseurs` de la capacité) ou dont un trajet l'utilise.

La logique vient d'un projet d'horaires de bus en données ouvertes (app Node sans dépendance), intégrée sans réécriture : le service local
`homeos-transports-cartreize` la fait tourner, et `source.py` l'interroge en HTTP (hôte local seulement).

```
fournisseurs/cartreize/
  fournisseur.yaml  manifeste du fournisseur (modes bus et car, hôtes, licence)
  source.py         class Source(interface.Source) : resoudre_trajet, prochains_departs, rafraichir, verifier
  service/          app.mjs, server.mjs, prepare.py, config.json, public/ (copie du projet d'origine)
```

Unités systemd : `capabilities/transports/systemd/homeos-transports-cartreize{,-recharger}.{service,path}`, installées
par `capabilities/transports/install.sh` **seulement si le foyer utilise ce réseau**.

## Trajet

Un trajet Cartreize = ligne, sens, arrêt de montée. La personne donne l'origine (« Mairie ») et la destination
(« vers la gare » ou un arrêt plus loin sur la ligne) ; `resoudre_trajet` trouve la ligne (ou la cherche parmi toutes
si elle n'est pas donnée), le sens (libellé « Vers … » ou arrêt de destination situé après l'origine) et l'arrêt, et
refuse les cas ambigus avec la liste des candidats. Les identifiants sont gardés dans `details` (`sens`, `arret`).

## Routes de l'API utilisées

Toutes servies par `gerer()` dans `service/app.mjs`, sur `http://127.0.0.1:8081` :

| Route | Utilisée par | Contenu |
| --- | --- | --- |
| `GET /api/lignes` | résolution d'une ligne | `[{id, ligne, nom, couleur, texte, tempsReel}]` |
| `GET /api/ligne/<ligne>` | résolution sens/arrêt | `{…, sens: [{id, libelle, arrets: [{id, nom, commune}]}]}` ; 404 si inconnue |
| `GET /api/passages/<ligne>/<sens>/<arrêt>` | `prochains_departs` | `{ligne, sens, arret, heure, tempsReel: {ok, disponible, ageSecondes, erreur…}, passes, prochains}` ; chaque passage : `prevu`, `estime`, `retard`, `dans`, `statut` (`a_venir`, `a_l_arret`, `supprime`, `prevu`, `passe`, `passe_theorique`), `suivi`, `course` |
| `GET /api/diagnostic` | `verifier` (smoke) | `{instanceDepuisSecondes, appelsDerniereMinute, fluxEnPause, dernieresErreurs}` (n'appelle pas SIRI) |
| `GET /sante` | `install.sh` | `{ok: true}` |

Adresse du service : `HOMEOS_TRANSPORTS_CARTREIZE_URL`, sinon `HOMEOS_BUS_URL` (ancienne capacité), sinon
`transports.cartreize.url` puis `bus.url` dans config.yaml, sinon `http://127.0.0.1:8081`.

## Quota SIRI

Le flux temps réel de La Métropole Mobilité (`RequestorRef=open-data`) accepte environ 10 à 20 requêtes par minute.
`app.mjs` s'en charge : chaque quai est gardé 60 s en cache, au plus 8 quais par minute partent vers SIRI (au-delà :
horaires théoriques), un flux qui échoue 3 fois de suite est mis en pause 60 s, délai de 3 s (2 essais). Un appel
`prochains_departs` coûte un quai ; les alertes (toutes les 10 min) ne regardent que les trajets qui partent dans l'heure.

## Grille : jamais versionnée

`prepare.py` produit `reseau.json` (≈ 0,2 Mo) et `horaires.json` (19 à 25 Mo). Ils vivent dans
`data/capacites/transports/cartreize/gtfs/` (2770 homeos:foyer, exclus du git de `data/`, inclus dans la sauvegarde)
et `service/data` est un lien symbolique vers ce dossier, créé par `install.sh`. Le cron `rafraichir_grilles` (chaque
jour 04:00) ne relance `prepare.py` que si la grille a plus de 7 jours, dans un dossier temporaire ; la grille n'est
remplacée (atomiquement) que si le résultat est lisible et non vide, puis `gtfs/.maj` est réécrit :
`homeos-transports-cartreize-recharger.path` le surveille et redémarre le service (`app.mjs` ne lit la grille qu'au
démarrage). Migration depuis la capacité bus : `data/capacites/bus/gtfs/` est déplacé, pas re-téléchargé.

Dépannage sans réseau : copier `reseau.json` et `horaires.json` d'un clone du projet d'origine dans
`data/capacites/transports/cartreize/gtfs/`, puis `sudo systemctl restart homeos-transports-cartreize`.

## Écarts avec le projet d'origine

Pour resynchroniser : recopier `app.mjs`, `server.mjs`, `prepare.py`, `config.json`, `public/` puis réappliquer :

1. `server.mjs` : adresse d'écoute réglable par `HOST` (défaut d'origine `0.0.0.0` conservé).
2. `app.mjs` : redirection des anciens liens du prototype (vers un arrêt particulier) retirée — aucune donnée
   personnelle dans le kit.
3. `prepare.py` : ligne `# ruff: noqa` (script repris tel quel, exclu du lint du kit).

La clé `apiKey` de `config.json` est celle de l'URL publique du jeu de données GTFS publiée sur transport.data.gouv.fr
(open data), pas un identifiant du foyer.

## Tests

`pytest capabilities/transports/tests/test_cartreize.py` : sans réseau, un faux service HTTP local renvoie des réponses
capturées sur le vrai `node server.mjs` (noms d'arrêts, de communes et de lignes remplacés par des noms
fictifs) ; mêmes valeurs qu'avec l'ancienne capacité bus (résumés, retards, suppressions, passages récents, alertes).
`HOMEOS_TESTS_RESEAU=1 pytest -m reseau capabilities/transports` lance le vrai service sur un port libre (grille :
`BUS_GTFS_DIR`, sinon `service/data`, sinon `/srv/home-os/data/capacites/transports/cartreize/gtfs`) ; arrêt testé :
`BUS_TEST_LIGNE`, `BUS_TEST_ORIGINE`, `BUS_TEST_DESTINATION` (défaut : premier arrêt de la première ligne).
