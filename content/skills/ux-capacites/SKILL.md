---
name: ux-capacites
description: Principes d'expérience utilisateur à appliquer avant de concevoir une capacité, une carte générative ou un écran de Firmin Assistant. À lire par tout agent (ou par l'assistant lui-même) qui crée ou modifie une interface ou un parcours.
---

# Expérience utilisateur des capacités

Ces principes viennent des retours des utilisateurs et s'appliquent à toute nouvelle capacité, carte ou écran. Chaque nouveau retour utilisateur doit être traduit ici en règle générique avant d'être corrigé localement.

**Prérequis génériques** : avant toute interface, charger les skills Anthropic `frontend-design` (direction visuelle, typographie, éviter le rendu « template ») et, s'il y a des graphiques, `dataviz` ; pour une page publiée, `artifact-design`. Le présent skill ajoute ce qui est propre à Firmin Assistant ; il ne remplace pas ces bases. Design system : shadcn/ui, palettes au choix de chaque personne.

## 1. La conversation est le produit

- L'accueil est le fil de conversation. Tout le reste (agenda, courses, repas, échéances) se consulte **aussi** depuis le fil, sous forme de cartes génératives insérées par l'assistant. Les écrans dédiés existent pour approfondir, pas pour remplacer.
- Le champ de saisie est **toujours accessible**, quel que soit l'endroit où l'on se trouve dans l'écran ou l'appli. Rien ne le recouvre jamais (toasts, cartes dépliées, claviers) ; sur mobile il est fixé en bas.
- Les suggestions de messages sont **sous** le champ, discrètes et facultatives : elles aident, elles n'imposent pas.
- Un **écran dédié montre ses données** (liste, planning, échéances, documents), en plein écran, jamais un extrait de
  conversation : pas de bulle, de méta ni de ligne de réflexion hors du fil. La réponse à une demande faite depuis un
  écran tient sur une ligne au-dessus du champ, avec « Voir dans la conversation ».
- Un texte prérempli (« Ajoute à l'agenda : ») ne suit pas la personne sur un autre écran s'il n'a pas été complété.
- Sous chaque réponse, une barre d'outils discrète : copier, écouter, pouce haut / bas (commentaire facultatif),
  réessayer ; icônes seules, au survol sur ordinateur, légères mais visibles sur mobile.
- Entrées vocales : le micro (dicter un message) est visible mais discret ; l'enregistrement long (réunion, rendez-vous à analyser) est une icône seule sur mobile, avec son libellé en info-bulle. Les deux doivent être compréhensibles sans explication, sinon c'est l'icône qui est mauvaise.

## 2. Zéro bruit

- Pas d'information de contexte technique dans l'interface : jamais de « même fil que Telegram », de nom de canal, d'identifiant, de nom d'outil, de fichier ou de format.
- Pas de redondance : si une carte porte la date, pas de pastille de date au-dessus ; si une bulle annonce « 12 articles », la carte ne répète pas « liste de 12 articles ».
- L'en-tête ne contient que : le prénom de l'assistant (celui choisi par le foyer, jamais un nom de produit ni de famille), et le minimum de navigation.
- Une échéance n'apparaît dans le brief ou sur l'accueil que si elle appelle une **action maintenant** (acheter, réserver, préparer), avec la raison en clair (« Vermifuge Filou J-6 · à acheter, il n'en reste plus ») ; sinon elle attend son propre rappel. Jamais de « J-6 » sans raison.
- Un message spontané par jour (le brief) agrège tout. Une capacité ne parle hors brief que sur un seuil déclaré dans son manifeste (ex. retard de bus ≥ 5 min dans l'heure du trajet). Le silence est la valeur par défaut.
  Mise en œuvre (CONTRACT §15) : les crons passent tous par `coeur.spontane` (`ctx.publier` pour une personne, texte renvoyé par le cron pour le foyer) ; la capacité déclare ses `seuils:` dans le manifeste ; la notification est une phrase fixe, sans montant, sans titre d'événement ni contenu de carte (« Une sélection de plats t'attend, à valider. »).

## 3. Progressif : résumé d'abord, détail au geste

- Sur l'accueil, une capacité se présente en **carte compacte** : une phrase d'état, une échéance, un bouton d'action (« Voir la proposition », « Valider »). Jamais la liste complète.
- Le détail s'ouvre au geste et doit alors être **riche et agréable** : vignettes (images réelles quand la source en fournit, sinon illustrations générées), ingrédients ou attributs principaux, badges (végé, enfants ok, favori), quantités. Sur desktop en grille, sur mobile en liste.
- **Rien ne bouge sous le doigt** : cocher, valider ou marquer un élément ne réordonne jamais une liste ; l'élément change d'état sur place (grisé, barré), même si cochés et non cochés alternent. Le rangement (retirer les cochés) est une action explicite et séparée.
- Toute proposition de l'assistant se termine par un choix clair : Valider / Modifier / Annuler. Modifier se fait en conversation (« remplace le dahl ») et la proposition est réaffichée en entier.

## 4. Chacun son espace, la famille en commun

- Trois niveaux de données : Famille (vu par tous les adultes), personnel (vu par son auteur ; l'autre adulte voit l'agenda sauf les événements marqués privés), sensible (chiffré, déverrouillé par empreinte/passkey, jamais envoyé par messagerie).
- Pas de catégorie « enfants » séparée : les événements des enfants vont dans Famille. Les enfants ne sont pas des utilisateurs.
- Les briefs sont personnalisés par personne et ne mentionnent jamais les événements privés de l'autre.
- Le nom de l'assistant, les prénoms, le lieu, les magasins : toujours des variables de configuration, jamais des constantes.

## 5. Le monde réel, pas que l'abonnement

- Une capacité liée à un service (repas livrés, transports, coiffeur) est une **source parmi d'autres** d'une activité plus large : le planning des repas inclut les repas maison, les invités, le restaurant ; les rituels récurrents (« pâtes le mardi ») sont des données de première classe.
- **Simplicité d'abord** : une colonne, pas de sélecteur ni d'option tant qu'elle n'a pas prouvé sa valeur à l'usage (exemple : pas de choix de magasin dans les courses au départ ; l'ordre des rayons pourra s'apprendre plus tard, silencieusement).
- La valeur vient des **données réelles du foyer** (tickets de caisse reçus par mail, historique) et des **routines de veille** (promotions, catalogues, offres), pas des réglages manuels demandés à l'utilisateur.
- **La valeur se mesure** : une veille (prix, promotions, offres) ne montre jamais une information brute (« −10 % sur X ») ; elle répond à une question d'argent ou de temps : « combien me coûtent mes courses selon le jour ? », « quel jour est le moins cher ? », « cet article habituel est en vraie promo, je l'ajoute ? ». Chaque économie réalisée alimente un **compteur d'économies** du foyer (« économisé grâce à l'assistant »), affiché en euros ; une suggestion ne sort que si elle dépasse le seuil « ça vaut le coup » déclaré dans le manifeste.
- Une capacité qui dépend d'un opérateur (réseau de transport, enseigne, site) est **générique et paramétrable par foyer** : chaque opérateur est un fournisseur interchangeable derrière une interface commune, fourni en standard quand une source ouverte existe, sinon fabriqué par l'atelier ou noté « manuel » — jamais un réseau, une ville ou un magasin codé en dur (exemple : transports, `docs/transports.md`).
- Depuis une échéance ou une information, proposer l'**action suivante** utile (comparer des offres, chercher un créneau, préparer un mail) — à condition qu'elle passe le filtre « ça vaut le coup » : l'assistant ne propose un créneau que s'il connaît l'agenda de la personne concernée.

## 6. Lire, écouter, dicter

- Tout brief et toute carte textuelle longue a un bouton de lecture vocale : **icône haut-parleur seule**, sans libellé (le mot « Écouter » reste en info-bulle et en aria-label), identique partout ; si la voix n'est pas disponible, le dire en une ligne.
- La dictée remplace la saisie, jamais l'inverse : une capacité doit être utilisable à la voix seule (questions courtes, réponses par oui/non ou par choix).
- Lieu : la météo et les événements locaux utilisent la commune du foyer, pas la grande ville voisine (outil
  `meteo_lire` et carte `meteo` : commune `meteo.commune` de config.yaml par défaut, une autre seulement sur demande
  explicite).

## 7. Découverte et feedback en continu

- Avant qu'une capacité propose quoi que ce soit, elle pose ses questions de découverte (onboarding du module), de préférence à la voix, une fois, et mémorise.
- Une permission (notifications, installation de l'appli, micro) n'est jamais à aller chercher dans Réglages : l'assistant la **propose en contexte**, dans le fil, au moment où elle devient utile (après le premier échange réussi, à l'activation d'une routine), en une phrase et deux boutons (Oui / Plus tard), une seule fois ; « Plus tard » n'est reproposé que si une fonction activée en a besoin.
- Chaque écran ou carte nouvelle peut recueillir un avis en une question (« ça t'aiderait ? » + note + commentaire), une seule fois par personne et par fonction.
- Une fonction inutilisée pendant six semaines est proposée au retrait.

## 8. Tout se règle en parlant

- Tout réglage d'une capacité (horaire, mode de validation, activation, paramètre du foyer) doit pouvoir se faire en conversation par un propriétaire du foyer, sans terminal ni fichier : la capacité déclare ses crons et ses `parametres` dans son manifeste, l'administration (`admin_*`) fait le reste.
- Un réglage se reformule en une ligne dans les mots du foyer (« rappel Kitchen Daily le mercredi à midi »), jamais avec un nom de clé ; il se valide par bouton, se journalise et s'annule (« annule le dernier changement »).
- **Toujours une solution** (retour d'usage : « je n'ai pas accès à la météo » alors que la météo existait
  dans le brief). L'assistant ne répond jamais par un « je n'ai pas accès à X » sec. Il vérifie d'abord ses outils et
  `capacites_lister`, puis propose la voie concrète, dans cet ordre : (1) une capacité existante à activer ou à régler
  (administration, propriétaire) ; (2) une capacité qu'il peut se fabriquer avec l'atelier (« Je peux m'ajouter ça : je
  lance la fabrication ? », puis bouton Valider d'`atelier_creer`) ; (3) une alternative immédiate (l'information
  obtenue autrement, un lien, l'étape manuelle exacte) ; (4) à défaut, la demande notée pour l'atelier humain avec une
  phrase honnête : « Ça demande un développement : je le note pour l'atelier. » Jamais deux fois la même excuse (on
  passe à l'option suivante) ; jamais « c'est fait » pour ce qui ne l'est pas.
- Conséquence pour une capacité ou un outil : une donnée déjà calculée pour une carte (météo du brief, horaires…) doit
  aussi être **joignable en conversation** par un outil ; un message d'erreur d'outil dit quoi faire (réessayer, lien,
  réglage manquant), jamais seulement « indisponible ».
- Une capacité qui manque peut se demander en parlant (« ajoute Planity ») : l'atelier pose ses questions une à une, reformule en nommant le site contacté, travaille en arrière-plan, prévient à la fin (« prête à l'essai : il me faut tes identifiants » ou « ça n'a pas marché, je garde ce que j'ai appris ») ; la première action d'une capacité fabriquée est validée par un propriétaire.
- Ce qui ne peut pas passer par la conversation (connexion à un site, identifiants) a une procédure claire, donnée à la demande, et bientôt une carte dans l'appli.
- Les identifiants se **gèrent** en parlant (lister, préparer, renommer, attribuer, supprimer : outils `identifiants_*`) mais leurs **valeurs** ne passent jamais par le fil : l'assistant ne connaît que des noms, des types et « ••• 1234 ». Une donnée sensible envoyée par erreur est masquée avant le modèle, et l'interface montre ce qui lui a été transmis (« Transmis à … : [carte bancaire ••1234] », avec un cadenas).

## 9. Une capacité fournit ses cartes (contrat technique : CONTRACT §14)

- Une carte est un objet `{type, id, titre?, donnees, actions: [{libelle, action, args, confirmation?}]}` d'un **type connu** (`coeur/cartes.py` : `agenda_jour`, `courses`, `kitchen_daily_proposition`, `transports_prochains`, `comparatif`, `identifiants`…). Un type inconnu est écarté : on étend d'abord le contrat, puis le front (lot 4b).
- Dans `tools.py`, la capacité déclare `@ctx.carte_pour("<outil>")` (constructeur `(resultat, arguments) -> carte | [cartes] | None`) ou appelle `ctx.ajouter_carte(type, donnees, actions=…)` depuis un outil. Les cartes ne sont produites que pour l'appli (canal web) ; Telegram reçoit le texte habituel. Les valeurs du coffre sont masquées automatiquement.
- Le résultat de l'outil reste un texte ou un objet utile au modèle ; la carte est un plus pour l'œil, jamais la seule source d'information. Le modèle ne la recopie pas : il résume en une phrase.
- Une proposition qui demande validation porte les boutons Valider / Modifier / Annuler (`tools.cartes.boutons_confirmation(id)`) liés à l'action en attente ; aucun bouton n'exécute quoi que ce soit sans passer par l'API (confirmation, step-up pour le sensible).
- Images : seulement des URL réelles fournies par la source (site du service) ou des fichiers servis par l'appli ; jamais un chemin du serveur.
- Identifiants manquants : rien à faire, le cœur transforme « Identifiants … à renseigner » en carte de saisie sécurisée.

## 10. Checklist avant de livrer une carte ou un écran

- [ ] Compact sur l'accueil, riche au geste
- [ ] Champ de saisie accessible, rien ne le recouvre
- [ ] Aucun mot technique, aucune redondance, prénom de l'assistant variable
- [ ] Événements privés de l'autre absents, données sensibles verrouillées
- [ ] Valider / Modifier / Annuler explicites ; aucune action avec effet sans validation
- [ ] Bouton Écouter sur le texte long ; utilisable à la voix
- [ ] Questions de découverte posées avant la première proposition
- [ ] Réglages de la capacité modifiables en conversation (manifeste : crons, parametres), sans terminal
- [ ] Ce que montre une carte est aussi joignable par un outil en conversation ; une erreur d'outil propose une
  solution (réessayer, lien, réglage, atelier), jamais un « pas accès » sec
- [ ] Capacité fabriquée par l'atelier : toutes ses actions avec effet en `confirmation_toujours`, sites approuvés par un propriétaire, messages de fin écrits par le système (jamais « c'est fait » par le modèle)
- [ ] Une capacité déclare ses secrets avec leur type (`secrets: [{cle, type, champs}]`) ; saisie hors conversation ou par carte sécurisée (`identifiants`), jamais une valeur dans le fil ; carte bancaire : virtuelle à plafond, action de paiement en `confirmation_toujours`
- [ ] Vérifié sur mobile 390 px et desktop, sans défilement horizontal
- [ ] Carte d'un type du contrat, données suffisantes sans le texte du modèle, testée en canal web et dégradée en texte sur Telegram
