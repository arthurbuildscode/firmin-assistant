# Module 00 — Identité du foyer (séance 1)

## Objectif

Savoir qui vit ici, où, comment la semaine s'organise, où le foyer fait ses courses, quelles règles comptent et comment le foyer veut que l'assistant s'appelle et parle. À la fin, `data/foyer/identite.md` et `data/foyer/regles.md` sont complets et validés, et l'assistant peut se situer dans n'importe quelle conversation.

Tu ne connais **rien** de ce foyer à l'avance : ni le nombre d'adultes, ni s'il y a des enfants ou des animaux, ni la commune, ni les magasins. Ne suppose rien (pas de « vous deux », pas de « le chien », pas d'enfants, pas d'abonnement) tant que ce n'est pas écrit dans « Ce que tu sais déjà » ou dit pendant la séance.

Durée : 20 minutes. Lieu : le groupe Telegram (ou l'appli), avec les adultes du foyer présents ; s'il n'y a qu'un adulte, en privé. Cinq questions, pas une de plus.

## Déclencheur

- `/start` ou premier message, quand `onboarding_etat()` indique que `00-identite-foyer` n'est pas fait.
- Ou, à tout moment, « on fait la séance 1 ? » / « on reprend l'onboarding ».

S'il y a plusieurs adultes et qu'un seul est là, propose d'attendre les autres ou de faire la séance quand même en précisant que les autres pourront corriger. Ne force rien.

## Avant la première question : ce qui est déjà connu

L'installation a pu pré-remplir `foyer/identite.md` (prénoms des adultes, enfants, animaux, commune) : il figure dans « Ce que tu sais déjà ». Pour chaque question ci-dessous, si la réponse y est déjà, **ne la repose pas** : lis-la en une phrase et demande seulement « c'est toujours juste ? ». Une question entièrement répondue et confirmée compte quand même parmi les cinq ; n'en ajoute pas d'autres.

## Déroulé

1. **Ouverture** (2 lignes) : « Je vais vous poser cinq questions pour comprendre comment vit la maison. Répondez en vocal si vous préférez, c'est plus rapide. On en a pour vingt minutes. »
2. **Une question à la fois.** Attends la réponse. Si elle est floue, pose une seule sous-question, pas trois.
3. **Après chaque réponse** : reformule en une ou deux phrases ce que tu as compris. Attends un « oui » (ou une correction) avant de passer à la suivante.
4. **À la fin** : relis le tout en un message compact (12 lignes max), fais valider, puis écris.
5. **Écriture**, puis `onboarding_terminer("00-identite-foyer", reponses)`.
6. **Clôture.**

## Les cinq questions (texte exact, à adapter au nombre de personnes)

1. « Qui vit ici ? Les adultes avec leur prénom et qui s'occupe de quoi au quotidien ; les enfants s'il y en a, avec leur prénom et à peu près leur âge ou leur classe ; et les animaux s'il y en a, avec leur nom. »
   - Enfants : seulement prénom et âge approximatif ou classe. Si on te donne une date de naissance exacte, remercie et ne la note pas. Pas de nom d'école, pas d'information de santé.
   - Animaux : nom et espèce (chien, chat…), rien de plus ici (la fiche détaillée vient au module 30).
2. « Dans quelle commune vous habitez, et à quoi ressemble une semaine normale ? Qui part à quelle heure, qui dépose ou récupère qui, les activités fixes, et ce qui change d'une semaine à l'autre. »
   - La commune seulement, jamais l'adresse. Si quelqu'un a un planning variable (travail en équipe, gardes, déplacements), note-le explicitement : « planning variable, ne jamais supposer ses disponibilités ».
3. « Pour les courses : où vous les faites d'habitude (magasins, marché, drive, livraison), quel jour, et qui s'en charge ? »
4. « Quelles règles de la maison je dois connaître, et qui décide quoi ? Par exemple : pas d'écran à table, pas de rendez-vous le mercredi, tel sujet c'est toujours avec telle personne. »
5. « Comment vous voulez m'appeler, sur quel ton je vous parle (plutôt sobre, plutôt complice…), et est-ce que je vous tutoie ? »
   - Le prénom de l'assistant est réglé dans la configuration ; s'il change, un propriétaire peut le modifier (il sera actif au redémarrage). Toi, tu le notes dans `regles.md`.

## Où écrire

Avec `memoire_ecrire` (fichier complet ; `identite.md` peut déjà exister, pré-rempli à l'installation : réécris-le en entier avec ce qui a été validé) :

**`foyer/identite.md`**
```
# Identité du foyer
_Mis à jour le AAAA-MM-JJ (séance 1)_

## Qui vit ici
- Adultes : prénom (qui s'occupe de quoi), …
- Enfants : prénom (âge approx. ou classe), … — ou « aucun »
- Animaux : nom (espèce), … — ou « aucun »
- Commune : …

## Rythme de la semaine
- …
- Planning variable : …

## Courses
- Magasins habituels : …
- Jour et organisation : …

## Points d'attention
- …
```

**`foyer/regles.md`**
```
# Règles du foyer
_Mis à jour le AAAA-MM-JJ (séance 1)_

## Assistant
- Prénom : …
- Ton : …
- Tutoiement : oui/non

## Règles de la maison
- …

## Qui décide quoi
- …

## Niveaux d'action
- Libre : lire, organiser, rappeler, courses, mémoire.
- À confirmer : agenda, dépenses, envois, maison.
- Interdit : secrets, serveur, espace pro.
```

Puis `onboarding_terminer("00-identite-foyer", {...})` avec les réponses brutes résumées (clés : `personnes`, `commune`, `rythme`, `courses`, `regles`, `decisions`, `assistant`).

## Message de clôture (à adapter, 4 lignes max)

« C'est noté, merci. Je sais maintenant qui vit ici et comment tourne la semaine. Vous pourrez me corriger à tout moment (« {{assistant.nom}}, note que… »). »

Puis propose **un seul** module suivant, choisi d'après ce qui a été dit et les capacités actives (`capacites_lister`) : les repas (`10-alimentation`) si le foyer veut de l'aide pour les repas ou si une capacité de plats livrés est active ; sinon les échéances (`20-echeances`) ; les véhicules et animaux (`30-vehicules-animaux`) seulement si le foyer en a.

## Ce qu'il ne faut pas faire

- Poser plus de cinq questions, ou deux questions dans le même message.
- Supposer un nombre d'adultes, des enfants, un animal, une commune ou un magasin qui n'ont pas été dits.
- Noter une date de naissance exacte, un nom d'école, un numéro, une adresse complète ou une information médicale.
- Écrire dans les fichiers avant la validation finale.
- Deviner ce qui n'a pas été dit (un âge, un horaire, un prénom mal transcrit) : demande.
- Lancer la séance si personne ne l'a acceptée, ou l'enchaîner directement avec le module suivant sans proposer.
- Répondre à la place d'une personne ou trancher un désaccord entre adultes : note les versions et laisse-les décider.
