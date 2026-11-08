# Module 20 — Premières échéances et anniversaires

## Objectif

Alimenter le système avec ce qui revient chaque année ou chaque trimestre et qu'on oublie toujours : administratif, entretien de la maison, véhicules, animaux, et les anniversaires des proches. À la fin, `echeances_lister()` et `anniversaires_lister()` renvoient de quoi nourrir le brief hebdo.

Durée : 15 minutes, en groupe ou en privé (peu importe, ce sont des données partagées). Cinq questions, voix encouragée : « dicte-moi tout en vrac, je trie ».

## Déclencheur

- `onboarding_etat()` indique `00-identite-foyer` fait et `20-echeances` non fait, et quelqu'un parle d'un rappel, d'une date, d'un anniversaire ou d'un entretien.
- Fin du module 10 si le foyer a envie d'enchaîner.
- « on rentre les échéances ? »

## Déroulé

1. Ouverture : « On va rentrer ce qui revient régulièrement, pour que je puisse te prévenir à l'avance. Pas besoin d'être exhaustif aujourd'hui : on complète au fil de l'eau. »
2. Une question à la fois. Pour chaque élément cité, demande ce qui manque (date ou mois, fréquence) **avant** d'écrire. Si la personne ne sait pas la date exacte, note le mois et le dis : « je mets le 1er mars, tu me corrigeras ».
3. Après chaque question, récapitule les éléments compris en puces et fais valider, puis appelle `echeances_ajouter` ou `anniversaires_ajouter` pour chacun. Dis en une ligne ce qui a été créé.
4. À la fin, `echeances_lister(horizon_jours=365)` et `anniversaires_lister(horizon_jours=365)` pour relire l'ensemble, puis `onboarding_terminer("20-echeances", reponses)`.

## Les cinq questions (texte exact)

1. « Côté administratif : qu'est-ce qui tombe chaque année ? Impôts, assurances, abonnements à renouveler, contrat d'énergie… Donne-moi le nom et à peu près le moment. »
2. « Côté maison : entretien de la chaudière ou de la pompe à chaleur, ramonage, jardin, alarme… Quoi, et tous les combien ? »
3. « Côté voiture(s) : contrôle technique, révision, assurance, pneus. Pour chacune, la date ou le mois. »
4. « Côté animaux, s'il y en a : vaccins, vermifuge, antiparasitaire, toilettage. Quelle fréquence, et c'est quand la prochaine fois ? »
5. « Les anniversaires des proches que vous ne voulez pas rater : prénom, jour et mois. Et pour chacun, la personne habite près d'ici ou il faut envoyer le cadeau par la poste ? »

Pour les anniversaires :
- Le **lieu de résidence** sert à régler `delai_cadeau_jours` : proche (on peut acheter sur place) = 14 jours ; ailleurs en France = 21 jours (valeur par défaut) ; à l'étranger = 30 jours. Dis la règle en une phrase et laisse ajuster.
- Ne demande ni l'année de naissance ni l'âge pour les enfants extérieurs au foyer. Si on te la donne pour un adulte, tu peux la noter dans `notes` (utile pour les anniversaires ronds) ; pour un enfant, non.

Pour les échéances :
- `rappels_jours` par défaut `[30, 7, 1]`. Pour un vermifuge ou un antiparasitaire, `[7, 1]` suffit ; pour un contrôle technique ou une assurance, garde `[30, 7, 1]`. Propose, n'impose pas.
- `recurrence` : `annuelle`, `trimestrielle`, `mensuelle`, ou une durée explicite si l'outil l'accepte ; sinon mets la fréquence dans `notes`.

## Où écrire

- Échéances → `echeances_ajouter(titre, date, rappels_jours, recurrence, notes)`. Titre court et explicite : « Contrôle technique voiture », « Vermifuge chien », « Entretien chaudière ».
- Anniversaires → `anniversaires_ajouter(prenom, date, lieu, notes, delai_cadeau_jours)`. `lieu` = ville ou « proche » / « loin », jamais une adresse complète.
- Si un prestataire est cité (garage, vétérinaire, chauffagiste), ajoute une ligne dans `foyer/contacts.md` avec `memoire_ajouter` : nom et rôle, numéro seulement si on te le donne spontanément.
- Si une règle apparaît (« la voiture, c'est toujours Alex qui s'en occupe »), ajoute-la dans `foyer/regles.md` avec `memoire_ajouter`.
- `onboarding_terminer("20-echeances", {...})` avec le nombre d'échéances et d'anniversaires créés et la liste de ce qui reste à préciser.

## Message de clôture

« C'est rentré : [N] échéances et [M] anniversaires. Je te préviendrai à l'avance dans le brief du dimanche et le matin du jour J. Ce qui reste flou : [liste courte, ou « rien »]. Pour en ajouter plus tard, dis-moi juste « rappelle-moi … le … ». »

## Ce qu'il ne faut pas faire

- Inventer une date : si on ne la connaît pas, noter le mois et le signaler, ou laisser de côté.
- Enregistrer une adresse complète, un numéro de contrat ou un numéro de police d'assurance (la plaque d'immatriculation, elle, va dans la fiche du véhicule : module 30).
- Proposer de prendre le rendez-vous (garage, vétérinaire, médecin) à la place des gens : tu rappelles, tu ne réserves pas, et tu ne choisis pas de créneau.
- Créer un événement d'agenda à partir d'une échéance sans passer par la confirmation : l'échéance est un rappel, l'événement d'agenda est une autre chose.
- Poser une sixième question : si la liste est longue, propose une deuxième séance.
