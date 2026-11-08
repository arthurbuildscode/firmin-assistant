# Module courses et promos — d'où viennent tes prix

## Objectif

Savoir où le foyer fait ses courses et comment {{assistant.nom}} connaîtra ses prix, pour estimer plus tard le coût de
la liste et signaler les vraies promos. À la fin, les paramètres `enseigne`, `magasin`, `mode` et `carte_fidelite`
sont renseignés (administration en conversation), et la personne sait comment envoyer ses tickets.

Quatre questions au plus, à la voix de préférence, une seule fois.

## Déclencheur

- Première question sur le prix des courses (« c'est cher, mes courses ? », « c'est en promo ? »), ou premier
  ticket de caisse envoyé.
- Si la personne refuse, ne repropose pas avant qu'elle reparle du prix des courses.

## Déroulé

1. « Dans quelle enseigne fais-tu la plupart de tes courses ? » (une seule au départ)
2. « Quel magasin précisément ? » (ville ou nom ; ne devine jamais)
3. « As-tu la carte de fidélité de cette enseigne ? » (sans demander son numéro)
4. « Le plus fiable, c'est que je lise tes tickets : tu peux m'envoyer une photo, ou le ticket reçu par mail en
   PDF. Ça te va comme ça ? » → `mode` = `tickets`. Si l'enseigne envoie les tickets par mail ou les range dans son
   appli, explique comment les retrouver (espace client, appli de fidélité) et les déposer ici.

Ne propose pas la lecture d'un site d'enseigne : elle n'existe pas encore. Si la personne la demande, propose la
voie immédiate (photo du prospectus ou de l'étiquette, ticket déposé), puis : « Pour le reste, je le note pour
l'atelier. »

Reformule en une ligne (« Courses chez <enseigne>, magasin de <ville>, carte de fidélité : oui, prix appris par
tes tickets »), fais valider, puis `onboarding_terminer("courses_promos", reponses)`.
