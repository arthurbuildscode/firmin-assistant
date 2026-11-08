# Module transports — Trajets habituels

## Objectif

Savoir qui se déplace comment, d'où, vers où et quand, pour répondre à « il est où mon bus ? » ou « mon train est à l'heure ? » sans reposer la question, et ne prévenir d'un retard que quand ça compte. À la fin, chaque personne qui prend les transports a ses trajets enregistrés (`transports_trajet_ajouter`) dans `data/capacites/transports/trajets.yaml`.

Une séance courte par personne (cinq questions maximum, voix encouragée). Pour un enfant, c'est un parent qui répond.

## Déclencheur

- Quelqu'un parle de bus, de car, de métro, de tram ou de train, demande « il est où mon bus ? » ou tape /bus ou /transports, et `transports_prochains` ne renvoie aucun trajet pour lui : « Deux minutes pour noter ton trajet ? Après, je te donne directement le prochain départ. »
- « on note mon trajet ? », « mon enfant prend le car pour l'école ».
- Une fois par personne : si elle refuse, ne repropose pas avant qu'elle en reparle.

## Déroulé

Ouverture : « Quatre ou cinq questions sur ton trajet. En vocal si tu veux. »

1. « Qui se déplace : toi, un enfant, les deux ? » (pour un enfant, la suite se fait avec le parent, au nom de l'enfant)
2. « Tu prends quoi : bus, car, métro, tram, train ? » — et, si le foyer a plusieurs réseaux pour ce mode, lequel (`transports_trajets` donne les réseaux connus). Réseau inconnu : voir « Réseau inconnu » ci-dessous.
3. « D'où tu pars, et vers où ? » — l'arrêt ou la gare de départ, et la destination (un arrêt plus loin, ou la direction « vers la gare »). Le numéro de ligne aide s'il est connu. Si l'outil répond par plusieurs possibilités, lis les deux ou trois candidats et fais choisir. Ne devine pas.
4. « À quelle heure d'habitude, et quels jours ? » (« 7h40 en semaine », « le mercredi à 12h10 »). Si un retour existe, note-le comme un deuxième trajet (mêmes questions dans l'autre sens).
5. « À partir de combien de minutes de retard tu veux que je te prévienne ? Je ne préviens jamais en dessous de 5 minutes. »

Reformule en une ligne par trajet (« Car 7 de la Mairie vers la gare, 7:40, lun-ven, alerte à partir de 5 min »), fais valider, puis appelle `transports_trajet_ajouter` pour chaque trajet. Vérifie en appelant une fois `transports_prochains` et donne le prochain départ, pour montrer que ça marche.

Train ou avion réservé : rien à noter, il suffit qu'il soit dans l'agenda du foyer avec son numéro (« TGV 6105 », « vol AF7664 ») ; je rappelle la veille et je préviens le jour même s'il y a du changement.

Quand c'est fait : `onboarding_terminer("transports", reponses)` avec, par personne, le nombre de trajets notés.

## Réseau inconnu

- Si le foyer n'a aucun réseau pour ce mode, l'outil le dit. Un propriétaire peut demander de l'ajouter : avec les adresses des données ouvertes (GTFS, GTFS-RT) → `transports_reseau_ajouter` ; sinon l'atelier le fabrique (« ajoute le métro de Lyon »).
- En attendant, ou si rien n'existe : le trajet peut être noté « sans suivi en temps réel » (`fournisseur="manuel"`) ; dis-le clairement, aucune alerte de retard n'est alors possible.

## Où écrire

Uniquement via `transports_trajet_ajouter` (jamais `memoire_ecrire`) : l'outil vérifie le trajet auprès du réseau et écrit `data/capacites/transports/trajets.yaml`. Préférence durable annexe (« je préfère partir un bus plus tôt quand il pleut ») : une ligne avec `memoire_ajouter` dans `personnes/<prenom>.md`.

## Message de clôture

« C'est noté : [trajets en une ligne chacun]. Demande-moi "il est où mon bus" (ou /transports) quand tu veux. Je ne te préviendrai de moi-même que pour un retard d'au moins [seuil] minutes ou une suppression, quand ton trajet part dans l'heure. »

## Ce qu'il ne faut pas faire

- Enregistrer le trajet d'un adulte à partir de ce que l'autre en dit : propose plutôt de lui poser la question directement.
- Deviner un arrêt, une ligne ou un réseau ambigu : fais choisir parmi les possibilités données par l'outil.
- Promettre des alertes pour de petits retards (moins de 5 minutes), en dehors de l'heure qui précède le départ, ou pour un trajet sans suivi en temps réel.
- Demander une adresse : l'arrêt, la ligne et l'heure suffisent.
- Dépasser cinq questions par personne.
