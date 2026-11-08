# Capacité « Salon démo » (fabriquée par l'atelier)

Demande : « ajoute le salon démo pour prendre les rendez-vous coiffeur » — site, compte personnel, action attendue :
voir les créneaux libres et réserver.

Exploration (pages publiques) :
- robots.txt : seul `/admin` est interdit ; les CGU autorisent la gestion de ses propres rendez-vous par un assistant.
- `/connexion` : champs « Adresse e-mail » et « Mot de passe », bouton « Se connecter » ; redirection vers `/creneaux`.
- `/creneaux` : liste vide au chargement, remplie par `/api/creneaux` (JSON `{creneaux: [{id, libelle, prestation}]}`) :
  le connecteur écoute cette réponse et retombe sur le DOM (`li.creneau`) sinon.
- `/reserver?id=…` : récapitulatif `.recap`, bouton « Confirmer la réservation » → « Rendez-vous confirmé », référence
  `RDV-0001`.

Décisions : pas de routine (la demande ne parle pas de surveiller) ; réservation en `confirmation_toujours` ; aucun
paiement sur ce site.
