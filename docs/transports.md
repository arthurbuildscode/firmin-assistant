# Transports — bus, car, métro, tram, train, avion

Capacité `transports` (remplace la capacité `bus` du lot 5c, décision du porteur du projet). Elle
répond à « il est où mon bus ? », « mon train est à l'heure ? », « le TGV 6105 de demain ? », prévient seulement quand
ça vaut le coup (retard d'au moins le seuil ou suppression, dans l'heure du départ), et rappelle la veille un train ou
un vol noté dans l'agenda. Code : `capabilities/transports/` (README du dossier) ; contrat : CONTRACT.md §9.

## Modèle

- **Trajet** : origine, destination, mode, fournisseur, heure et jours habituels, seuil d'alerte, personne
  (`data/capacites/transports/trajets.yaml`). Découverte en conversation : « tu prends quoi, d'où, vers où, quels
  jours ? » → `transports_trajet_ajouter`, qui fait vérifier le trajet par le réseau (arrêts, ligne, sens) et liste les
  possibilités quand c'est ambigu.
- **Fournisseur** : un réseau ou un opérateur, module déclaratif `fournisseurs/<nom>/fournisseur.yaml` (modes, hôtes,
  type de source `api_ouverte` | `page_web` | `manuel`, portée nationale ou locale, licence) + `source.py`, classe
  `Source` de l'interface commune (`interface.py`) :

  | Méthode | Rôle |
  | --- | --- |
  | `resoudre_trajet(trajet)` | identifiants propres au réseau (arrêts, ligne, sens) ; erreur avec candidats si ambigu |
  | `prochains_departs(trajet, horizon)` | `{departs, passes, temps_reel, avertissement}`, départs normalisés (`interface.depart`) |
  | `perturbations(trajet)` | travaux, grèves, suppressions annoncées |
  | `etat_train(numero, date)` / `etat_vol(numero, date)` | état par numéro (`interface.etat`) |
  | `rafraichir(forcer)` | grille théorique (cron `rafraichir_grilles`) |
  | `verifier()` | smoke test du fournisseur |

  Ce qu'un réseau ne sait pas donner lève `NonPrisEnCharge` (comportement de base) : l'assistant le dit, n'invente rien.
- **Accès réseau** : uniquement par le `Client` du fournisseur — https (http seulement en local), hôtes du manifeste ;
  pour un réseau ajouté par le foyer ou fabriqué par l'atelier, hôtes **approuvés par un propriétaire**
  (`etat.json`, `transports.reseau_approuve`, aussi ajoutés à la liste blanche du proxy sortant) ; robots.txt respecté
  pour une source `page_web` ; navigation par `tools.navigation` (arrêt sur vérification humaine, aucun contournement
  anti-robot) ; agent utilisateur honnête, délais et tailles plafonnés, cache court des flux temps réel.

## Fournisseurs livrés

| Fournisseur | Modes | Source | Clé | Portée |
| --- | --- | --- | --- | --- |
| `cartreize` | bus, car | GTFS + SIRI (Métropole Aix-Marseille-Provence) via le service local Node `homeos-transports-cartreize` | aucune | locale |
| `sncf` | train | GTFS national + GTFS-RT (trip updates, alertes) via transport.data.gouv.fr | aucune | nationale |
| `gtfs_rt` (modèle) | bus, métro, tram, train… | toute autorité qui publie GTFS (+ GTFS-RT) : instance créée par le foyer | aucune | locale |
| `vols` | avion | **manuel** : aucune source ouverte, gratuite et fiable (voir plus bas) | — | nationale |
| `manuel` | tous | trajet sans suivi en temps réel (heure habituelle seulement) | — | nationale |

**Aucun fournisseur livré ne demande de clé.** L'interface prévoit une clé d'API typée `cle_api` (déclarée dans le
manifeste de la capacité sous `transports/<fournisseur>`, lue dans le coffre par `client.cle_api()`, facultative ou
non) pour un futur fournisseur qui en aurait besoin.

### SNCF

- Grille GTFS nationale TGV INOUI, OUIGO, Intercités, TER (≈ 5,5 Mo zippés, 151 jours, mise à jour quotidienne) :
  `https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip`.
- Temps réel GTFS-RT, toutes les 2 minutes, trains qui partent dans l'heure :
  `https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates` ; alertes :
  `https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts`.
- Licence ODbL ; **vérifié le 8 nov. 2026 : accessibles sans authentification** (protobuf de 0,5 Mo, 708 courses
  suivies ; alertes de 0,7 Mo).
- Particularités traitées dans `fournisseurs/sncf/source.py` : les identifiants de course du temps réel ne sont pas
  ceux de la grille (date de circulation contre date de fin de validité) → appariement par **numéro de train**
  (`trip_headsign` de la grille, « OCESN6105F… » dans le temps réel) et jour ; la voie n'est pas publiée dans ces flux
  (l'assistant ne la donne pas) ; avant l'heure qui précède le départ, l'état est théorique, avec les suppressions déjà
  annoncées dans les alertes.
- L'API SNCF (Navitia, `api.sncf.com`) répond 401 sans jeton ; le jeton est gratuit sur inscription
  (numerique.sncf.com) mais **inutile ici** : les flux ouverts ci-dessus suffisent.
- Mesuré sur la vraie grille : résolution d'un trajet entre deux gares 0,7 s (stop_times.txt de
  83 Mo parcouru en flux avec un préfiltre), prochains départs 1,4 s, état d'un train par numéro 1,5 s ; résultats mis
  en cache par grille.

### Réseaux urbains (modèle `gtfs_rt`)

Le catalogue national transport.data.gouv.fr recense les autorités qui publient une grille GTFS et souvent un flux
GTFS-RT. Le foyer (un propriétaire) ajoute un réseau en conversation avec `transports_reseau_ajouter(titre, url_grille,
url_temps_reel, url_alertes, modes)` : bouton Valider, qui approuve les hôtes ; l'instance est rangée dans
`data/capacites/transports/reseaux.yaml`. Le moteur `gtfs.py` décode le protobuf GTFS-RT sans dépendance (et sa forme
JSON), lit la grille en flux, apparie les courses par `trip_id` (spécification), applique retards (`delay`, `time`),
suppressions de course (`CANCELED`) et d'arrêt (`SKIPPED`), et filtre les alertes par ligne, arrêt ou course.

### Vols : pourquoi « manuel »

Étudié le 8 nov. 2026 :

- **OpenSky Network** : accès anonyme limité à 400 crédits par jour, uniquement les positions récentes ; `/states/all`
  ne filtre pas par numéro de vol (seulement par appareil ou zone) ; aucun retard ni porte ; les historiques de vols
  (`/flights/*`) demandent un compte OAuth2 et ne couvrent que la veille. Inadapté à « mon vol est-il à l'heure ? ».
- **Open data des aéroports** : aucun flux ouvert et gratuit trouvé pour les retards et portes (Paris Aéroport,
  Marseille Provence) ; les API de vols (FlightAware AeroAPI, Aviationstack…) sont commerciales ou à clé avec quotas
  très bas.

Le fournisseur `vols` ne fait donc aucun appel réseau : il reconnaît le numéro (AF7664, U2 4321…) et la compagnie, et
dit où vérifier le retard et la porte. La veille des réservations rappelle le vol la veille, puis, le jour même, de
vérifier la porte sur l'appli de la compagnie. Un futur fournisseur avec une vraie source (clé `cle_api`) remplacerait
ce comportement sans changer l'interface.

## Outils, routines, cartes

- Outils : `transports_prochains` (trajets habituels ou trajet ponctuel), `transports_trajets` (trajets notés et
  réseaux connus), `transports_trajet_ajouter`, `transports_trajet_supprimer`, `transports_etat` (train ou vol par
  numéro), `transports_reseau_ajouter` (action validée par un propriétaire, `confirmation_toujours`).
- Routines (crons du manifeste) : `rafraichir_grilles` (04:00, chaque source décide : Cartreize chaque semaine, SNCF
  chaque jour, GTFS selon `frequence_jours`) ; `alertes_retard` (toutes les 10 min de 6 h à 21 h, seuil déclaré
  `retard`, une fois par jour et par départ, à la personne concernée seulement) ; `veille_reservations` (toutes les
  15 min de 6 h à 22 h) : agenda du foyer (jamais les agendas privés), événement avec un numéro de train ou de vol →
  rappel la veille à partir de `heure_veille` (18 h), alerte le jour même dans les 3 h avant le départ si le train est
  supprimé ou en retard d'au moins le seuil (seuil déclaré `reservation`, palier de 10 min), rappel de vérifier la
  porte d'un vol. Destinataire : la personne dont le prénom est dans le titre, sinon les adultes.
- Cartes (CONTRACT §14) : `transports_prochains` (`trajets[]` avec alerte, départs, avertissement, perturbations) et
  `transports_etat` (statut, départ, arrivée, arrêts, perturbations), rendues par `web/front/src/components/cartes/
  transports.tsx` ; texte dégradé sur Telegram. Le type `bus` reste reconnu pour l'historique.
- Paramètres du foyer (`data/capacites/transports/parametres.yaml`, réglables en conversation) : `fournisseurs`
  (réseaux locaux choisis), `seuil_retard_min` (5 minimum), `heure_veille`.

## Réseau inconnu : l'atelier fabrique un fournisseur

« Ajoute le métro de Lyon » : sans adresses de données ouvertes, `atelier_creer(…, fabrication="fournisseur_transports",
modes, zone)` fabrique un **fournisseur** (pas une capacité entière) à partir du gabarit
`content/atelier/gabarits/fournisseur_transports/` : lecture seule, aucun identifiant, imports limités à ceux d'une
capacité plus `capabilities.transports.interface` (dont `interface.SourceGtfs` si le réseau publie du GTFS),
contrôles, tests, smoke test, relecture, installation dans `<etat>/fournisseurs-atelier/<nom>/`, hôte approuvé à la
validation de la demande. Détails : `docs/atelier.md`.

## Migration depuis la capacité bus

Automatique et idempotente : le manifeste déclare `remplace: [bus]` — l'état (`etat.json`, statut et smoke) et les
réglages (`capacites.bus` de config.yaml, `bus.url`) de l'ancienne capacité valent tant que la nouvelle n'a pas les
siens, et `install.sh` (`python -m tools.capacites migrer`) les transfère ; au premier accès, `data/capacites/bus/
trajets.yaml` devient des trajets `cartreize` (seuils et jours conservés), la grille GTFS est déplacée (pas
re-téléchargée), Cartreize devient un réseau choisi par le foyer ; l'ancien dossier `data/capacites/bus/` est conservé.
`capabilities/transports/install.sh` retire les unités `homeos-bus*` et n'installe `homeos-transports-cartreize` que
si le foyer utilise ce réseau ; les timers `homeos-cap-bus-*` disparaissent avec l'ancien manifeste
(`homeos-appliquer-config`). « /bus » reste une commande, « coupe le bus » désigne toujours la capacité.

## Sources

- [Réseau SNCF TGV, Intercités et TER — transport.data.gouv.fr](https://transport.data.gouv.fr/datasets/horaires-sncf?locale=en)
  (GTFS, GTFS-RT trip updates et service alerts, SIRI Lite ; licence ODbL)
- [Spécification GTFS Realtime](https://gtfs.org/realtime/reference/) et [GTFS Schedule](https://gtfs.org/schedule/reference/)
- [OpenSky Network REST API](https://openskynetwork.github.io/opensky-api/rest.html) (limites anonymes, OAuth2,
  absence de filtre par indicatif, historiques J-1)
- [Exemple d'appel de l'API SNCF (Navitia) avec jeton](https://docs.rs/crate/terapi/latest/source/examples/collections/sncf.toml)
  et [obtention du jeton SNCF](https://community.homey.app/t/api-sncf/126178)
- [FlightAware AeroAPI](https://fr.flightaware.com/commercial/aeroapi) et
  [Aviationstack](https://www.blogdumoderateur.com/aviationstack-api-gratuite-flight-tracking-temps-reel/) (API de vols
  commerciales ou à clé)
- Cartreize : grille GTFS et SIRI de La Métropole Mobilité (voir `capabilities/transports/fournisseurs/cartreize/README.md`)
