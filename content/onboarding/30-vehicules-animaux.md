# Module 30 — Véhicules et animaux

## Objectif

Créer une fiche par véhicule et par animal, pour que les factures de garage, les papiers du vétérinaire et les rappels (contrôle technique, vaccins, vermifuge) aient un endroit où atterrir. À la fin, `voitures/<nom>.md` existe pour chaque véhicule, `animaux/<nom>.md` pour chaque animal, et les prochaines dates sont dans les échéances.

Durée : 10 minutes, en groupe ou en privé (ce sont des données partagées). Cinq questions, voix encouragée : « dicte-moi tout, je trie ».

## Déclencheur

- Un fichier reçu concerne un véhicule ou un animal et la fiche correspondante n'existe pas encore (« J'ai rangé la facture. Tu veux qu'on crée la fiche de la voiture ? Cinq questions. »).
- `onboarding_etat()` indique `00-identite-foyer` fait et `30-vehicules-animaux` non fait, et quelqu'un parle de la voiture, du garage, d'un animal ou du vétérinaire.
- Fin du module 20 si le foyer a envie d'enchaîner.
- « on fait les fiches voiture et animaux ? »

## Déroulé

1. Ouverture : « On fait une fiche par véhicule et une par animal. Comme ça, quand tu m'envoies une facture ou un papier du véto, je sais où le noter. »
2. Une question à la fois. Pour chaque véhicule ou animal, demande ce qui manque **avant** d'écrire. Si une date n'est pas connue, note le mois et dis-le : « je mets début mars, tu me corrigeras ».
3. Après chaque question, récapitule en puces et fais valider.
4. Écris chaque fiche avec `memoire_ecrire` (gabarits ci-dessous), puis crée les échéances avec `echeances_ajouter`. Dis en une ligne ce qui a été créé.
5. Termine par `onboarding_terminer("30-vehicules-animaux", reponses)` et le message de clôture.

## Les cinq questions (texte exact)

1. « Vous avez combien de véhicules ? Pour chacun : comment vous l'appelez entre vous, la marque et le modèle, et l'immatriculation. »
2. « Pour chaque véhicule : le kilométrage à peu près, le garage habituel, et la date du prochain contrôle technique ou de la prochaine révision si vous la connaissez. »
3. « Les animaux : pour chacun, son nom, son espèce et sa race, et son âge à peu près. »
4. « Votre vétérinaire : le nom du cabinet et la ville. »
5. « Les rappels de chaque animal : vaccins, vermifuge, antiparasitaire. C'est quand la prochaine fois, et tous les combien ? »

Si le foyer n'a pas de véhicule ou pas d'animal (voir « Ce que tu sais déjà »), saute les questions correspondantes. Plusieurs animaux : la question 3 les couvre tous, sans compter une question de plus.

## Où écrire

Le nom du fichier est le nom usuel, en minuscules, sans accent ni espace : `voitures/twingo.md`, `voitures/voiture.md`, `animaux/<nom-de-l-animal>.md`.

**`voitures/<nom>.md`**
```
# Voiture — Nom usuel
_Mis à jour le AAAA-MM-JJ_

- Modèle : marque, modèle, année si connue
- Immatriculation : AB-123-CD
- Kilométrage : … km (au AAAA-MM-JJ)
- Garage habituel : nom, ville
- Prochain contrôle technique : AAAA-MM
- Prochaine révision : AAAA-MM ou … km

## Historique
_Une ligne par intervention ou facture rangée._
```

**`animaux/<nom>.md`**
```
# Animal — Nom (espèce)
_Mis à jour le AAAA-MM-JJ_

- Race : …
- Né le : AAAA-MM-JJ (ou âge approximatif)
- Vétérinaire : cabinet, ville
- Vaccins : prochain rappel AAAA-MM-JJ, tous les … mois
- Vermifuge : prochain AAAA-MM-JJ, tous les … mois
- Antiparasitaire : prochain AAAA-MM-JJ, tous les … mois

## Historique
_Une ligne par visite ou papier du vétérinaire rangé._
```

Puis :
- Échéances → `echeances_ajouter` pour chaque date : « Contrôle technique Twingo » (`[30, 7, 1]`, `annuelle` ou tous les deux ans dans `notes`), « Révision Twingo », « Vaccin <nom> » (`[30, 7, 1]`, `annuelle`), « Vermifuge <nom> » (`[7, 1]`, `trimestrielle` si c'est le rythme).
- Garage et vétérinaire → une ligne dans `foyer/contacts.md` avec `memoire_ajouter` (nom, rôle, ville ; numéro seulement si on te le donne spontanément).
- `onboarding_terminer("30-vehicules-animaux", {...})` avec la liste des fiches créées, le nombre d'échéances et ce qui reste à préciser.

## Message de clôture

« C'est fait : [fiches créées] et [N] rappels. Quand tu m'envoies une facture de garage ou un papier du véto en photo, je le range et je complète la fiche. Ce qui reste flou : [liste courte, ou « rien »]. »

## Ce qu'il ne faut pas faire

- Demander ou noter le numéro de carte grise (certificat d'immatriculation), le numéro de série du véhicule (VIN), un numéro de contrat d'assurance ou le numéro de puce du chien.
- Inventer une date : si on ne la connaît pas, noter le mois et le signaler, ou laisser de côté.
- Prendre rendez-vous au garage ou chez le vétérinaire à la place des gens, ou choisir un créneau : tu rappelles, tu ne réserves pas.
- Poser une sixième question : si la liste est longue (plusieurs animaux, flotte de véhicules), propose une deuxième séance.
