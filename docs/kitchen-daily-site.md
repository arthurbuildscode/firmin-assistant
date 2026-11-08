# Kitchen Daily — observations du site (lecture seule, compte connecté)

Fixtures de test anonymisées (produits inventés, structure DOM identique) : `tests/fixtures/kd_tunnel.html`
(tunnel complet simulé), `kd_detail.html` (interrupteur, forme supposée), `kd_upcoming.html`, `kd_select.html`.

Site Next.js, sans API publique. Le front appelle une API interne Sylius (`/api-proxy/api/v2/shop/...`)
avec un JWT en localStorage et probablement un en-tête de canal ; un appel rejoué sans ces en-têtes renvoie
« User … has not been found on the channel KITCHEN_DAILY ». **On manipule donc le DOM avec Playwright**, en
conservant l'état de session (cookies + localStorage) via `storage_state`.

## Connexion
- Bouton « Compte » dans l'en-tête → modale « Pour continuer, connectez-vous ! » : champs `E-mail *`, `Mot de passe *`,
  bouton « Connexion », lien « Mot de passe oublié ? », ou bouton Google (à ne pas utiliser).
- Une fois connecté, l'en-tête a un lien « Mon Espace » → `/account/shippings/upcoming`.
- Bannière cookies possible (Didomi) : bouton « Continuer sans accepter » (en haut à droite de la modale) ou « Accepter & Fermer ».
- Un bandeau « Quel est votre niveau de motivation actuellement ? » (1–5, « Je valide ») peut apparaître en haut : à ignorer (ne pas cliquer sur ce « Je valide »-là ; celui de la sélection est en bas de page, dans la barre fixe avec le compteur).

## Livraisons à venir — `/account/shippings/upcoming`
Texte structuré par livraison :
```
Jeudi 12/11/2026
9h - 12h
Livraison confirmée - 110000000
116,00 € - Sera prélevée le 08/11/2026
À modifier avant le 06/11/2026 à 23h59
Voir le détail            → href="/account/shippings/details/<id>"   (ex. AbCd1234Ef)
```
Une livraison déjà verrouillée affiche « Modifiable jusqu'au 30/10/2026 à 23h59 » et « Payée le … ».
Rythme observé : une livraison par semaine, modifiable jusqu'à une date limite quelques jours avant, puis prélèvement.

## Détail d'une livraison — `/account/shippings/details/<id>`
- Bandeau : « Livraison du 12/11/2026 - 110000000 — Modifiable jusqu'au 06/11/2026 à 23h59 »
- Interrupteur « Je veux ma livraison » Non/Oui (ne jamais toucher sans demande explicite ; lot 2b :
  `kitchen_daily_livraison_definir`, toujours après « Valider »)
- « Ma formule 18 plats », boutons « Modifier ma formule », « Modifier mes plats »
- Texte : « Pas le temps de choisir vos repas ? Nos commis sélectionneront un menu en tenant compte de vos préférences. »
- Lien « Choisir mes repas » → `/account/shippings/edit/select-meal/quotidien/<id>`
- « Mes extras » → « Ajouter des extras »

## Sélection des plats — `/account/shippings/edit/select-meal/quotidien/<id>`
- Étapes en haut : PLATS · DESSERTS · ENTRÉES, SOUPES & APÉROS · JUS ET SHOTS DETOX (plats au lot 2, extras au lot 2b :
  voir « Tunnel et extras » plus bas).
- Filtres régime (boutons radio) : « Je mange de tout » (actif), « Contrôle calories », « Protéines + », « Végétarien » ; bouton « Filtrer ».
- 30 cartes `.product-item` (classe module `ItemProduct-module-scss-module__*__productItem`). Dans chaque carte :
  - une image, le titre en majuscules (h3/h4 ou `[class*=title]`), ex. « Parmentier de pulled pork aux patates douces »
  - des badges optionnels : « protéines + », « Végétarien », « Contrôle calories » (éléments `[class*=badge|tag|label]`)
  - un compteur : bouton `-`, `input.input-text.qty` (type text), bouton `+`
  - la quantité affichée est `1` sur toutes les cartes même quand le total est 18/18 : la valeur de l'input n'indique PAS
    la sélection ; il faut déterminer l'état sélectionné autrement (classe sur la carte, couleur du `+`, ou lire le panier
    après validation). À vérifier à l'implémentation ; en cas de doute, utiliser « Remise à zéro » puis sélectionner.
- Barre fixe en bas : « Remise à zéro » (bouton), compteur « 18 / 18 PLATS », bouton « Je valide »
  (classes `Button-module-scss-module__*__root`). C'est CE « Je valide » qui enregistre la sélection.
- Deux boutons « Je valide » existent dans le DOM (bandeau motivation + barre du bas) : sélectionner celui de la barre du bas
  (le dernier, ou celui proche du texte « / 18 PLATS »).

## Tunnel et extras (HTML des étapes capturé, session connectée, lecture seule)

Une seule URL (`…/select-meal/quotidien/<id>`) pour les quatre étapes : React remplace la liste des produits, les
onglets et la barre du bas sans changer d'adresse.

- **Onglets** (`ul` dans `TabsMeals…__tabsMeals`) : un `li.TabsMeals…__dot` par étape, avec `<strong id="tab_meal">`
  (Plats), `tab_dessert` (Desserts), `tab_Entrees-soupes` (Entrées, Soupes & Apéros), `tab_drink` (Jus et shots detox),
  plus « Repas minceur » (`tab_0`, avant) et « Extras » (`tab_0`, après, sur l'étape plats). L'étape courante porte la
  classe `TabsMeals…__current` (les étapes passées `…__active`). C'est le premier signal de `detecter_etape`.
- **Titre** : « Je choisis mes <strong>desserts</strong> » (`FlexibilityMeals…__title`) — second signal.
- **Desserts** : bandeau « 🎁 Et profitez de 1 dessert OFFERT marqué du picto 🎁 à chaque livraison ! ». Les desserts
  éligibles ont la classe `ItemProduct…__isOfferedExtras` sur le `<li>` parent de la carte. Observé : un dessert
  éligible à 1 → barre « 1 Desserts - 0 € » (le premier dessert éligible est gratuit ; les suivants payants, à
  confirmer sur le site). Texte d'erreur de la barre : « Veuillez choisir 1 produit » (sens exact inconnu).
- **Cartes d'extras** : `li.ItemProduct…__product.ItemProduct…__hasPrice[data-testid="item-<n>"]` >
  `div.product-item.ItemProduct…__productItem` : vignette (+ `span.ItemProduct…__promotion` « promo » sur les packs),
  `h3` titre, prix `div.ItemProduct…__price` « 2,<sup>99</sup>€ / unité », compteur `button.ItemProduct…__moins` (`<span>-</span>`),
  `input.input-text.qty[readonly][data-testid="KD…"]` (**valeur réelle** : 0, 1…), `button.ItemProduct…__plus`
  (`<span>+</span>`) ; parfois un bloc promo `div.ItemProduct…__promo` « <u>4,20€</u> Économisez <b>0,90€</b> » (prix barré
  d'un lot) et « Stock limité : plus que quelques plats restants ! » (`…__stockAvailablily`). Packs observés :
  « Pack 3 Desserts gourmands », « Pack 3 Apéros », « Pack 3 Jus détox ».
- **Barre du bas** (`FixedBar…__fixedBar ProgressBar…__barre`) :
  - PLATS : « Remise à zéro » (`ProgressBar…__reset`), « <span>18</span>/ 18 Plats », « Je valide » ;
  - extras : bouton retour vers l'étape précédente (`ProgressBar…__prevTab`, ex. « Plats »), résumé
    « N <Étape> - X € » (`ProgressBar…__nbr` : « 1 Desserts - 0 € », « 0 Entrées, Soupes & Apéros - 0 € »,
    « 0 Jus et shots detox - 0 € ») — montant de l'étape, lu avant/après réglage pour vérifier qu'un dessert offert
    ne coûte rien —, puis « Je valide » ;
  - JUS (dernière étape) : le même bouton affiche « Je valide ma commande ».
  - Le bouton d'action est toujours `button[data-testid="confirmOrder"]` (préféré par `_bouton_etape`) ; repli :
    le « Je valide » hors bandeau « motivation ».
- **Enregistrement** : « Je valide » ne fait que changer d'étape ; seul « Je valide ma commande » enregistre plats et
  extras ensemble (POST `…/orders/<id>/bulk`). Les extras déjà pris sont pré-remplis à l'entrée du tunnel
  (un dessert à 1 observé), donc traverser les étapes sans y toucher les conserve. Le code relit quand même les
  extras avant d'enregistrer des plats et les réapplique à chaque étape s'ils ont bougé (cas où « Remise à zéro »
  viderait aussi les extras : non observé, non exclu), puis les relit après enregistrement (`verification_extras`).
- **Plats** (même capture) : les champs quantité affichent eux aussi la valeur réelle (0, 2…) : la détection
  « quantite » s'applique.

## Interrupteur « Je veux ma livraison »

- Décrit sur la page de détail `/account/shippings/details/<id>` (« Non/Oui ») ; **absent** de la page des livraisons
  à venir (vérifié sur une capture). Son HTML n'a pas encore été capturé : le code le repère par son
  libellé (plus petit élément contenant « Je veux ma livraison », puis jusqu'à 6 ancêtres) et accepte un
  `[role=switch]`, une case à cocher (valeur réelle `checked`), `aria-checked` / `aria-pressed`, ou une paire de
  boutons « Oui » / « Non » dont l'actif porte une classe `…active|selected|checked|current|on` ou un attribut ARIA.
- Clic : l'interrupteur, sinon son `<label>`, sinon son parent, sinon un clic synthétique ; puis confirmation d'une
  modale éventuelle (`[role=dialog]`, `[class*=modal|popin|popup]`, bouton « Oui / Confirmer / Valider / OK /
  Continuer ») et d'un `confirm()` natif ; relecture après rechargement de la page.
- `livraisons()` ouvre le détail de chaque livraison modifiable pour lire l'état (`livraison_voulue`). Un statut
  explicite (« annulée », « suspendue »…) sur la page des livraisons vaut `false` sans ouvrir le détail.
- À vérifier au premier essai réel : forme exacte de l'interrupteur, présence d'une modale (motif d'annulation à
  choisir ?), effet sur la liste « À venir » (la livraison sautée reste-t-elle listée, avec quel statut ?).

## Plats observés (pour fixtures de test)
Parmentier de pulled pork aux patates douces · Pavé de saumon, riz basmati sauté et sauce citron confit et gingembre (protéines +) ·
Tagliatelles carbonara · Dahl de lentilles corail au lait de coco (Végétarien) · Filet de poulet au cidre et son écrasé de pommes de
terre aux carottes (protéines +, Contrôle calories) · Risotto forestier aux graines de pavot et émincé de poulet (protéines +) ·
Tajine de poulet aux pois chiches, boulgour et épices · Poulet aux épices, légumes et coco · … (30 au total)

## Compte
Le compte Kitchen Daily est celui d'une personne du foyer (formule de N plats, `kitchen_daily.formule_plats`). Identifiants : jamais dans le dépôt ni dans Telegram ;
saisis une fois dans le terminal du VPS pour créer l'état de session.
