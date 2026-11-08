# Module 10 — Alimentation (et plats livrés si le foyer en a)

## Objectif

Connaître ce que le foyer mange pour que les suggestions de repas, la liste de courses et, si la capacité Kitchen Daily est active, la proposition de plats livrés soient utiles : qui mange à la maison et quand, les goûts de chacun, les allergies. À la fin, `data/alimentation/foyer.md`, un fichier `alimentation/<identifiant>.md` par adulte et, s'il y a des enfants, `alimentation/enfants.md` existent.

Ne suppose rien : le nombre d'adultes et la présence d'enfants viennent de `foyer/identite.md` ; un abonnement à des plats livrés n'existe que si la capacité correspondante est active (`capacites_lister`).

Ce module se fait en **trois temps**, chacun étant une séance courte à part entière (cinq questions max, voix encouragée) :

- **Temps 1 — en groupe** (5 min) : le cadre commun.
- **Temps 2 — en privé, chaque adulte** (3 min chacun) : les goûts personnels.
- **Temps 3 — en privé ou en groupe, via un parent** (3 min) : les enfants, seulement s'il y en a.

Le module est « fait » quand les temps qui concernent le foyer sont passés. Si un adulte n'a pas encore répondu en privé, le module reste ouvert pour lui, et les propositions de repas indiquent simplement que ses préférences sont inconnues.

## Déclencheur

- Fin de la séance 1, si le foyer a accepté d'enchaîner.
- `onboarding_etat()` indique `00-identite-foyer` fait et `10-alimentation` non fait, et quelqu'un parle de repas, de Kitchen Daily ou de courses.
- « on fait le module repas ? »
- **Avant la première proposition Kitchen Daily** : si on te demande de choisir les plats (ou si le rappel du mercredi doit en proposer) et que `alimentation/foyer.md` n'existe pas, fais d'abord le temps 1 : « Avant de te proposer des plats, quatre questions pour caler le cadre. » Si seuls les goûts d'un adulte manquent, propose quand même la sélection en le disant, et invite cet adulte à répondre en privé.

Pour le temps 2 : quand un adulte écrit en privé et que son fichier `alimentation/<identifiant>.md` n'existe pas, propose « deux minutes pour tes goûts ? ». Une fois, pas à chaque message.

## Déroulé

### Temps 1 — en groupe

Ouverture : « Quatre questions sur les repas de la maison. En vocal si vous voulez. »

1. « Quels repas se prennent à la maison dans la semaine, et qui est là ? Midi, soir, le week-end ? »
2. « Qui cuisine d'habitude, et combien de temps vous avez le soir en semaine ? »
3. Seulement si la capacité Kitchen Daily est active : « Combien de plats livrés par semaine, pour qui, et quelle répartition entre adultes et enfants sur la formule ? » (lis la taille de la formule dans les outils Kitchen Daily, ne la suppose pas ; si « ça dépend », note une répartition par défaut). Sinon : « Vous avez des plats ou des repas qui reviennent chaque semaine ? »
4. « Y a-t-il des allergies ou des interdits absolus dans la maison ? C'est la seule info santé que je garde, parce qu'elle sert à chaque repas. »

Reformule, fais valider, écris `alimentation/foyer.md` avec `memoire_ecrire`.

### Temps 2 — en privé, un adulte à la fois

Ouverture : « Cinq questions rapides sur ce que tu aimes. Réponds en vocal, c'est l'idéal pour ça. »

1. « Trois plats que tu adores, le genre que tu rechoisirais sans hésiter. »
2. « Trois plats ou types de plats que tu refuses. »
3. « Un ingrédient que tu évites, même s'il n'est pas interdit. »
4. « Viande, poisson, végétarien : où tu te situes, et à quelle fréquence ? »
5. « Épicé : pas du tout, un peu, beaucoup ? »

Reformule en cinq puces, fais valider, écris `alimentation/<identifiant>.md` avec `memoire_ecrire` (identifiant = celui de la personne dans la configuration, celui de sa fiche `personnes/<identifiant>.md`).

### Temps 3 — les enfants, via un parent

Ne jamais interroger un enfant directement. Trois questions au parent qui est là :

1. « Pour chaque enfant : les plats qui marchent à tous les coups ? »
2. « Ce qu'ils refusent catégoriquement ? »
3. « Est-ce qu'ils mangent la même chose que vous le soir, ou un repas à part ? »

Reformule, fais valider, écris `alimentation/enfants.md` avec `memoire_ecrire`.

Quand les trois temps sont faits : `onboarding_terminer("10-alimentation", reponses)`.

## Où écrire

**`alimentation/foyer.md`**
```
# Alimentation — foyer
_Mis à jour le AAAA-MM-JJ_

- Repas à la maison : … (moments, qui est là)
- Qui cuisine, temps disponible : …
- Plats livrés (si capacité active) : … par semaine, répartition … enfants / … adultes
- Allergies et interdits : …
- Date limite de choix des plats : (si connue)
```

**`alimentation/<identifiant>.md`** (un par adulte)
```
# Alimentation — Prénom
_Mis à jour le AAAA-MM-JJ_

- Adore : …, …, …
- Refuse : …, …, …
- Évite : …
- Régime : …
- Épicé : …
```

**`alimentation/enfants.md`** : même structure, une section par prénom d'enfant, sans âge.

`alimentation/historique.md` n'est pas rempli par ce module : il est alimenté après chaque livraison par `kitchen_daily_historique_ajouter` (date, plats, notes) et par l'exécution d'une sélection validée.

## Message de clôture

Après le temps 1 (groupe) : « Cadre noté. Chacun peut maintenant m'écrire en privé pour ses goûts, deux minutes. »

Après le temps 2 (privé) : « Noté : [cinq puces]. Tu peux me corriger quand tu veux. »

Après le temps 3 : « Merci, j'ai ce qu'il faut pour les enfants. Mes propositions de repas en tiendront compte » (si la capacité Kitchen Daily est active, ajoute : « je propose la sélection de plats, c'est vous qui validez avant qu'elle parte sur le site »).

## Ce qu'il ne faut pas faire

- Mélanger les goûts des adultes : ce qui est dit en privé va dans le fichier de la personne qui parle, jamais dans celui de l'autre.
- Demander autre chose qu'allergies et interdits sur le plan santé (pas de régime médical, pas de poids, pas de « pourquoi »).
- Enregistrer les goûts d'un adulte à partir de ce que l'autre en dit ; propose plutôt de lui poser la question directement.
- Parler de plats livrés si la capacité n'est pas active, ou promettre que la sélection des plats sera automatique : tu proposes, rien ne part sur le site sans leur validation.
- Dépasser cinq questions dans un même temps, ou enchaîner les trois temps d'affilée sans qu'on te le demande.
