# Journal des versions

Ce qui a changé, version par version, en langage courant. Les versions suivent les « lots » de construction du
produit ; les dates sont celles de la mise au point avec les premiers utilisateurs. Le détail technique est dans
`CONTRACT.md` et dans l'historique du dépôt.

## Prochaine version — Un produit sans trace du premier foyer

- **« Animaux » remplace « Chien »** dans le rangement des fichiers : les documents déjà rangés, leur index et les
  liens des fiches sont déplacés automatiquement (à l'installation et à chaque application de la configuration).
- **Exemples neutres partout** : prénoms, lieux, lignes de bus, numéros de livraison et habitudes des exemples, des
  tests et de la documentation sont fictifs ; le contrôle automatique du paquet refuse leur retour.
- **Fichier `NOTICE`** (licence Apache 2.0).

## Prochaine version — Les transports

- **Le bus devient les transports** : bus, car, métro, tram, train et avion. Chacun note ses trajets habituels en
  parlant (« tu prends quoi, d'où, vers où, quels jours ? ») ; « il est où mon bus ? », « mon train est à l'heure ? »
  ou /transports donnent les prochains départs, les retards et les suppressions en direct quand le réseau les publie.
- **Les trains SNCF partout en France**, sans inscription : horaires, retards, suppressions et perturbations, et
  l'état d'un train par son numéro (« le TGV 6105 de demain ? »). La voie n'est pas publiée : l'assistant ne l'invente
  pas.
- **Les voyages de l'agenda** : un train ou un vol noté avec son numéro est rappelé la veille ; le jour même,
  l'assistant prévient si le train est supprimé ou en retard. Pour un vol, faute de source fiable et gratuite, il
  rappelle de vérifier la porte sur l'appli de la compagnie.
- **N'importe quel réseau** : un propriétaire ajoute le réseau de sa ville en conversation (« ajoute le métro de
  Lyon ») — avec ses données ouvertes s'il les a, sinon l'atelier le fabrique, testé et relu, sans identifiant. Rien
  n'est supposé à l'installation : seul le train est proposé d'office.
- **Rien à refaire pour les foyers déjà installés** : les trajets, réglages et alertes du bus sont repris automatiquement ;
  « /bus » marche toujours.

## Prochaine version — Météo en conversation et « toujours une solution »

- **La météo se demande en parlant** : « quel temps aujourd'hui ? », « je peux étendre le linge ? », « on sort
  samedi ? » : l'assistant lit la météo de la commune du foyer (jamais celle de la grande ville voisine), avec le
  matin, l'après-midi et le soir, la pluie et le vent ; dans l'appli, une carte météo s'affiche. C'est la même
  source que la carte du jour.
- **Plus jamais de « je n'ai pas accès » sec** : quand une demande dépasse ce qu'il sait faire, l'assistant vérifie
  d'abord ce qu'il a déjà, puis propose une vraie voie : une capacité à activer, une capacité qu'il peut se fabriquer
  avec l'atelier (« Je peux m'ajouter ça : je lance la fabrication ? »), une solution immédiate (un lien, l'étape à
  faire), ou à défaut il note la demande pour l'atelier. Il ne répète jamais la même excuse et ne dit jamais
  « c'est fait » pour ce qui ne l'est pas.

## Prochaine version — Courses et promos (premier lot, en essai)

- **Tes tickets de caisse t'apprennent tes prix** : envoie une photo de ton ticket, ou dépose le ticket reçu par mail
  (PDF, page web) ; l'assistant en tire chaque article, sa quantité et le prix vraiment payé (remise déduite). Un
  ticket déjà lu n'est jamais compté deux fois. S'il n'est pas sûr d'une ligne, il le dit au lieu d'inventer.
- **Dans tes mots** : « CAFE MOUL.ARAB 250G, c'est ton café moulu ? » — au plus cinq questions par ticket, une à
  la fois ; ensuite tous les tickets suivent.
- **« Combien je paie le café d'habitude ? »** : dernier prix, prix habituel (hors promo, sur trois mois), plus bas
  observé, et la date du ticket d'où vient chaque prix. « Qu'est-ce que j'ai acheté la semaine dernière ? » marche
  aussi.
- **Compteur d'économies honnête** : il ne compte que ce que l'assistant t'a fait faire et qu'un ticket confirme,
  par rapport à ton prix habituel (jamais le prix barré du magasin) ; une mauvaise affaire est comptée aussi. Rien
  ne s'affiche tant qu'aucun achat n'est suivi.
- **Aucune donnée bancaire ni personnelle** : numéros de carte, de carte de fidélité, nom, téléphone et courriel
  sont retirés du ticket avant toute lecture par l'assistant et avant tout enregistrement.
- Aucun site de magasin n'est lu : la veille des promotions attend que le foyer choisisse d'où viennent ses prix.

## Prochaine version — L'atelier (lot 7b)

- **L'assistant fabrique ses propres capacités** : « ajoute Planity pour prendre les rendez-vous coiffeur ». Il pose
  trois questions (quel site, quel compte, quoi faire), reformule en disant à quel site il parlera, et après le
  bouton Valider il écrit la capacité en arrière-plan. Elle est testée, relue par un second agent de sécurité, puis
  installée en essai ; un message prévient à la fin (« prête à l'essai : il me faut tes identifiants ») ou dit
  simplement que ça n'a pas marché.
- **Première action validée à la main** : la première réservation d'une capacité fabriquée attend le bouton d'un
  propriétaire du foyer ; si elle réussit, la capacité devient active. Toute action avec effet reste ensuite
  derrière le bouton Valider.
- **Sûr par construction** : le code fabriqué ne voit ni les données ni les secrets du foyer, ne parle qu'aux sites
  approuvés, respecte robots.txt et les conditions des sites, ne contourne jamais une vérification anti-robot, et se
  retire en une phrase (« enlève Planity »).

## Prochaine version — L'appli (lot 4b, en cours)

- **Une vraie appli** sur le téléphone et l'ordinateur, à la place de la page de démonstration : la conversation en
  direct (avec la ligne « Je lis l'agenda… » qu'on peut déplier), les cartes (journée, courses, repas, échéances,
  documents, comparatifs…), des boutons Valider / Modifier / Annuler, la dictée et un mode voix « comme au
  téléphone », les écrans Semaine, Courses, Repas, Échéances, Journal, Fichiers, Mémoire, Routines et Réglages.
- **Chacun ses couleurs** : 8 palettes par personne, thème clair ou sombre.
- **Sécurité visible** : ce qui est transmis à l'assistant après masquage d'une donnée sensible s'affiche sous le
  message (cadenas) ; identifiants et documents sensibles demandent l'empreinte ou le visage.
- **Installable** sur Android et iPhone (écran d'accueil), lisible hors ligne, avec des **notifications** quand
  l'appli est fermée.
- **Connexion mieux protégée** (suite de la revue de sécurité) : une session ne marche que depuis le compte Tailscale
  qui l'a ouverte (un cookie copié sur un autre appareil est refusé), et chacun peut fixer son compte Tailscale dans
  la configuration ; la connexion par code Telegram devient une **connexion de secours** de 24 h, pour converser,
  lire et ajouter une passkey (pas de coffre ni d'administration) ; chaque enregistrement dans le coffre redemande
  l'empreinte.
- **Les routines parlent dans l'appli** : brief du matin, brief de la semaine, proposition ou sélection
  Kitchen Daily et résultats des routines arrivent dans le fil de chacun (avec leur carte), en plus de Telegram.
  Une seule notification par jour et par personne, sauf ce qui ne peut pas attendre (date limite des plats, bus
  en retard sur le trajet habituel) ; la notification ne dit jamais un montant ni le contenu d'un rendez-vous.
- **Alerte bus** : si le bus du trajet habituel est en retard d'au moins 5 minutes (ou supprimé) dans l'heure du
  départ, la personne concernée — et elle seule — est prévenue.
- **Écrans directs** : courses (cocher sans que la liste bouge, retirer les cochés), échéances, journal (avec
  photos), fichiers et mémoire se consultent et se modifient sans passer par l'assistant, avec les mêmes droits
  (famille, personnel) ; un changement fait par l'un s'affiche aussitôt chez l'autre.
- **Documents sensibles chiffrés sur le téléphone** : passeports, ordonnances ou avis d'imposition sont chiffrés
  dans l'appli avant l'envoi, déverrouillés par l'empreinte ou le visage ; le serveur ne garde que du chiffré et
  l'assistant n'y a jamais accès. Une phrase de récupération, affichée une seule fois, permet de les rouvrir.
  Dans l'écran Fichiers (ou depuis le « + » → Document sensible) : déverrouiller par l'empreinte, ajouter une photo
  ou un PDF (pièce d'identité, carnet de santé, carnet de l'animal, autre), l'ouvrir dans l'appli sans qu'il soit
  jamais écrit en clair ailleurs, le supprimer après confirmation. Tout se reverrouille après 5 minutes sans
  activité ou quand l'appli passe en arrière-plan. Sur un appareil qui n'a pas la clé, la phrase de secours suffit ;
  en connexion de secours, les documents restent fermés.
- **Plus fidèle à la maquette** (retour du 8 nov.) : chaque écran montre ses données en plein écran (courses,
  échéances avec la raison d'agir, journal, documents, mémoire) depuis les écrans directs, plus jamais un morceau de
  conversation par-dessus ;
  la réponse à une demande faite depuis un écran tient sur une ligne au-dessus du champ. Les cartes arrivent enfin
  dans l'appli (la liste de courses n'est plus rendue en texte). Cocher un article se fait sur place.
- **Sous chaque réponse** : copier, écouter, pouce haut ou bas (avec un mot facultatif), réessayer.
- **Les permissions se proposent au bon moment** : « Tu veux que je te prévienne ici ? » après le premier échange,
  puis « Installer l'application » après quelques messages, une seule fois.

## 0.6.0 — Le kit devient un produit (lot 6, novembre 2026)

- **Installable par n'importe quel foyer** : un seul script (`install/installer-un-foyer.sh`) pose les questions
  en français (prénom de l'assistant, adultes, enfants, animaux, commune, Telegram, compte Claude de chacun,
  Tailscale, coffre, sauvegardes) puis installe et vérifie tout, en une trentaine de minutes. Il termine par la
  liste de ce qui reste à faire avec un téléphone (connexion des comptes Claude, Tailscale, copie hors site).
- **Aucune donnée des premiers utilisateurs dans le produit** : prénoms, lieux, trajets, identifiants et exemples réels ont
  été retirés du code, des consignes de l'assistant, des tests et de la documentation. Un contrôle automatique
  l'empêche de revenir.
- **Tout vient de la configuration** : le prénom de l'assistant et ceux du foyer sont lus dans la configuration,
  jamais écrits en dur. « Firmin » n'est que le prénom proposé par défaut.
- **Première séance sans a priori** : l'assistant ne suppose plus deux adultes, un chien ou un abonnement de plats
  livrés ; il demande, et ne repose pas ce que l'installation a déjà appris.
- **Paquet versionné** (`scripts/empaqueter.sh`) avec ce journal, le numéro de version et un guide
  d'installation pas à pas (`docs/installation.md`).

## 0.5.x — Capacités, coffre, bus et sécurité (lots 5, 5d, 7 et 7a)

- **Capacités** : chaque fonction (plats livrés, bus, administration…) est un module que le foyer active ou
  désactive ; l'assistant sait dire ce qu'il sait faire.
- **Coffre** : les identifiants des services (cantine, prise de rendez-vous…) sont chiffrés sur le serveur et
  ne passent jamais par la conversation. Vaultwarden en option pour un vrai gestionnaire de mots de passe.
- **Prochain bus** : horaires et retards en temps réel, trajets habituels de chacun, alerte seulement quand un
  retard change vraiment quelque chose.
- **Sécurité renforcée** : secrets rangés hors de portée de l'assistant, serveur durci (pare-feu, mises à jour
  automatiques, connexion SSH par clé seulement), sauvegarde chiffrée quotidienne testée chaque mois, copie hors
  du serveur possible.
- **Outils toujours prêts** : un serveur d'outils unique, avec une clé par personne ; un navigateur isolé pour
  les sites des fournisseurs.
- **Régler en parlant** : les propriétaires du foyer changent les horaires, activent une capacité ou mettent en
  pause en le demandant à l'assistant, avec validation par bouton et retour arrière.

## 0.4.x — L'appli (lots 4, 4a et 4b)

- Maquette de l'appli du foyer (semaine, courses, repas, échéances, fichiers, mémoire, réglages), validée écran
  par écran avec les premiers utilisateurs.
- **Appli web conversationnelle** : la même conversation que dans Telegram, avec des cartes (agenda, courses,
  propositions) et la voix ; connexion par empreinte ou visage (passkey), accessible seulement depuis les
  appareils du foyer (Tailscale).
- Choix automatique du modèle d'IA selon la demande (rapide pour « ajoute du lait », plus réfléchi pour choisir).

## 0.3.0 — Photos et papiers (lot 3)

- Envoyer une photo ou un PDF à l'assistant : il le lit, le range (voiture, maison, enfants, santé,
  administratif…), note ce qui compte et propose les rappels (contrôle technique, fin de garantie).
- Photos souvenirs ajoutées au journal du mois. Jamais de détail médical noté.

## 0.2.x — Plats livrés (lots 2 et 2b)

- Rappel hebdomadaire et proposition de plats selon les goûts de chacun, enregistrée sur le site du fournisseur
  après validation ; desserts et extras, semaines sautées.
- Découverte des goûts du foyer en quelques questions.

## 0.1.0 — Les fondations (lot 1)

- Assistant de famille sur Telegram, chaque adulte avec son propre compte Claude.
- Agenda, liste de courses, échéances, anniversaires, mémoire du foyer, journal.
- Brief du matin et brief de la semaine dans le groupe du foyer.
- Installation sur un serveur Ubuntu, sauvegardes, première séance de découverte du foyer.
