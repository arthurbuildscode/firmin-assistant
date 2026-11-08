# Spec lot 4 — Maquette navigable de l'application web (mobile + desktop)

Objectif : une maquette **full front**, en un seul fichier HTML autonome (pas de serveur, pas de build), avec données d'exemple,
qui montre l'application privée du foyer de bout en bout. Elle sert à valider les écrans à deux (Alex, Camille) avant
de coder l'app réelle, via un dispositif d'interview intégré. Elle est publiée comme artefact ; elle doit être parfaite à 390 px
(iPhone) et à 1280 px+ (desktop), et correcte entre les deux.

## Style
- Base shadcn/ui : neutres zinc, rayon 0.5rem, bordures fines, police Geist (Google Fonts) + Geist Mono pour heures/prix.
  Reprendre exactement les tokens et le sélecteur de palette de le fichier de choix du design system
  (lire ce fichier : `:root` tokens, blocs `data-palette`, dark mode via `prefers-color-scheme` + `data-theme`, classes `.btn`, `.badge`, `.msg`, `.pcard`).
- Palette par personne : le sélecteur (8 couleurs) vit dans Réglages ; la couleur courante est aussi celle de l'avatar de la personne connectée.
  Un bouton en haut permet de « voir comme » Alex ou Camille (switch de profil, qui change la palette et le prénom).
- Thème clair et sombre, tous deux soignés ; `color-scheme` suit.
- Aucune image externe : avatars = initiales, photos = tuiles SVG/CSS colorées avec légende (dégradés sobres), icônes = SVG inline simples
  (lucide-like, dessinées à la main : maison, calendrier, panier, horloge, chef, livre, cerveau, réglages, micro, trombone).
- Mobile : barre de navigation en bas (5 entrées : Maison, Semaine, Courses, Repas, Plus) + écran « Plus » listant le reste ; en-tête compact.
  Desktop : barre latérale gauche fixe (240 px) avec toutes les sections, contenu centré max 960 px ; la Conversation peut être
  ouverte en panneau latéral droit (360 px) sur desktop depuis n'importe quel écran (bouton « Parler à Firmin »).
- Interactions réelles en JS (pas de backend) : navigation par hash (`#maison`, `#semaine`, `#courses`, `#repas`, `#echeances`,
  `#journal`, `#memoire`, `#conversation`, `#reglages`, `#fichiers`), état en mémoire (+ localStorage pour palette/profil, try/catch).

## Écrans (données d'exemple cohérentes avec le foyer : Alex, Camille, deux enfants (prénoms fictifs), un chien « Rex »,
maison dans « la commune », livraisons Kitchen Daily chaque semaine, novembre 2026)
1. **Maison** (accueil) : salutation + date, le brief du jour (3-4 lignes), 4 tuiles : aujourd'hui (2 événements), courses (n articles),
   prochaine échéance, Kitchen Daily (date limite, « proposition prête »), puis « À valider » (1 carte : sélection Kitchen Daily
   12 plats, boutons Valider/Modifier), puis « Dernières actions » (journal : 5 lignes).
2. **Semaine** : vue semaine (lun→dim) en colonnes sur desktop, liste par jour sur mobile ; événements colorés par personne (Alex, Camille,
   Foyer, enfants) ; bouton « + » ouvre une feuille (sheet) « Ajouter en langage naturel » avec un champ texte et un bouton micro ;
   en haut, le brief hebdo repliable.
3. **Courses** : liste groupée par rayon, cases à cocher qui fonctionnent, champ d'ajout rapide, « Qui a ajouté » en petit, bouton
   « Vider les cochés », mode « en magasin » (gros texte, une colonne).
4. **Repas** (Kitchen Daily) : bandeau livraison du jeudi 12/11 avec date limite ; la proposition de 12 plats de l'assistant en cartes
   (titre, badges Végétarien / Protéines + / Contrôle calories, quantité, « pourquoi » en 3 mots), compteur 12/12, boutons
   « Valider la commande » / « Demander une autre proposition » ; en dessous, « Préférences du foyer » (adultes, enfants) éditables,
   et « Historique » (3 dernières semaines avec notes ★).
5. **Échéances & anniversaires** : deux onglets ; échéances triées par date avec badge J-x et catégorie (admin, maison, voiture, animaux) ;
   anniversaires avec délai cadeau (ex. « Claire · Canada · commander avant le 24/11 »), idées de cadeaux repliables.
6. **Journal** : par mois ; entrées datées (texte) et photos (tuiles) avec légendes ; en haut, « Résumé du mois » généré ; bouton « Ajouter une photo ».
7. **Fichiers** : derniers documents rangés par catégorie (voiture, animaux, administratif, factures) avec description, et « entrants » à traiter.
8. **Mémoire du foyer** : fiches (Identité, Règles, Contacts, Infos pratiques, Alex, Camille, Enfants, Rex) sous forme de texte éditable
   (textarea stylé) avec « Modifié le » et bouton « Enregistrer » (local), plus un encart « Ce que l'assistant a appris cette semaine ».
9. **Conversation** : chat avec Firmin (bulles, note vocale, carte de proposition avec boutons), champ de saisie + micro ; même
   contenu que sur Telegram ; l'envoi ajoute le message et une réponse d'exemple pré-écrite (3 scénarios : courses, agenda, repas).
10. **Réglages** : profil (Alex/Camille), palette, thème, heure des briefs, personnes et rôles (propriétaire/membre/invité), statut
    (Claude connecté, dernière sauvegarde, Kitchen Daily connecté), bouton « Exporter les données ».

## Dispositif d'interview (sur chaque écran)
- Un bouton flottant discret « Avis » (en bas à droite, au-dessus de la barre mobile) ouvre un petit panneau : « Sur cet écran : ça t'aiderait ? »
  note 1-5 (pastilles), champ libre, bouton micro (visuel), « Envoyer » ; les réponses sont stockées en mémoire + localStorage par écran et par profil,
  et visibles dans Réglages → « Retours » (tableau écran × personne × note × commentaire, bouton « Copier en JSON »).
- Un indicateur de progression « 4/10 écrans évalués » dans l'en-tête de Réglages et en petit dans l'écran Plus.

## Qualité
- Un seul fichier HTML, < 400 Ko, sans bibliothèque (vanilla JS), accessible (focus visible, aria-labels sur les icônes, contrastes),
  `prefers-reduced-motion` respecté, pas de défilement horizontal, gutters `max(16px, env(safe-area-inset-*))`, `<title>` = « Firmin — Maquette ».
- Le fichier est écrit dans `docs/maquette/` (et c'est le coordinateur qui le publie).
- Vérification : rendu avec Playwright (Chromium pré-installé : `require('playwright')` en Node) à 390×844 et 1280×800, clair et sombre,
  captures dans `docs/maquette/captures/` ; corriger ce qui déborde ou se chevauche avant de rendre la main.
