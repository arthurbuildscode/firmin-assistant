# Capacité « transports »

Bus, car, métro, tram, train, avion : prochains départs et retards sur les trajets habituels du foyer, état d'un train
ou d'un vol par son numéro, alertes « ça vaut le coup », veille des réservations notées dans l'agenda. Remplace la
capacité « bus » (lot 5c) ; vue d'ensemble, sources et choix : `docs/transports.md`.

```
capabilities/transports/
  capability.yaml   manifeste (remplace: [bus], crons, seuils retard et reservation, paramètres du foyer)
  tools.py          outils transports_* + executer (transports_reseau_ajouter)
  interface.py      interface commune des fournisseurs (Source, NonPrisEnCharge, depart, etat…) — seule
                    dépendance permise à un fournisseur de l'atelier
  fournisseurs.py   découverte (standard, atelier, instances), manifeste fournisseur.yaml, Client réseau, chargement
  gtfs.py           moteur GTFS / GTFS-RT (décodeur protobuf maison, grille en flux, SourceGtfs)
  modele.py         trajets (data/capacites/transports/trajets.yaml) et migration depuis bus
  reservations.py   numéros de train et de vol repérés dans l'agenda
  crons.py          rafraichir_grilles, alertes_retard, veille_reservations (+ CLI migrer / utilise)
  smoke.py          vérification des réseaux utilisés par le foyer
  CLAUDE.md         consignes données à l'assistant ; onboarding.md : découverte « tu prends quoi… »
  install.sh        migration bus, service local Cartreize si le foyer l'utilise
  systemd/          homeos-transports-cartreize{,-recharger}.{service,path}
  fournisseurs/     cartreize/, sncf/, gtfs_rt/ (modèle), vols/, manuel/
  tests/            pytest sans réseau (+ test « reseau » optionnel du vrai service Cartreize)
```

Données : `data/capacites/transports/` — `trajets.yaml`, `parametres.yaml` (`fournisseurs`, `seuil_retard_min`,
`heure_veille`), `reseaux.yaml` (instances de modèles ajoutées par le foyer), `alertes.json`, `veille.json`, un dossier
par fournisseur (`cartreize/gtfs/`, `sncf/grille.zip` + `cache/`). Grilles jamais versionnées.

Réseau : hôtes des fournisseurs standard dans `reseau:` du manifeste (liste blanche du proxy) ; ceux d'un réseau ajouté
par le foyer ou fabriqué par l'atelier sont approuvés par un propriétaire (`etat.json`, `transports.reseau_approuve`),
et `Client` refuse tout autre hôte.
