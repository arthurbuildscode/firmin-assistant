# Spec lot 3 — Images et documents

Objectif : envoyer une photo ou un PDF au bot (facture, ordonnance, étiquette, courrier, document d'école) → l'assistant le lit,
en extrait l'essentiel, le range, et met à jour les fiches concernées (voiture, chien, échéances, contacts…). Photos « souvenirs »
rangées pour le journal du mois.

Contrat général : `CONTRACT.md`. Le bot lance `claude -p` ; Claude Code sait lire une image ou un PDF avec l'outil `Read`
(chemin absolu dans `--add-dir`).

## Bot (`bot/handlers.py`)
- Nouveaux handlers : `PHOTO`, `Document` (pdf, jpg, png, heic → convertir heic en jpg via `pillow-heif` si dispo, sinon message
  d'erreur clair). Taille max 20 Mo (limite Telegram bot).
- Téléchargement dans `data/fichiers/entrants/<YYYY-MM-DD>_<HHMMSS>_<prenom>_<nom-original-nettoyé>.<ext>` (umask 002).
- Le prompt envoyé à Claude : ligne de contexte habituelle + `[fichier reçu: <chemin absolu>] <légende éventuelle>` + si pas de légende :
  « Lis ce fichier, dis en une ligne ce que c'est, extrais ce qui compte, propose où le ranger. »
- Réponse streamée comme un message normal. Si la réponse contient `CONFIRMATION_REQUISE`, mécanisme existant.
- Journal `actions_log` : kind `fichier`, summary `reçu <nom> (<taille>)`.
- Photos envoyées en « album » (media group) : traiter chaque photo, mais un seul prompt listant tous les chemins.

## Outils MCP (dans `tools/server.py`)
- `fichiers_ranger(chemin_source: str, categorie: str, nouveau_nom: str, description: str, fiche: str | None = None)` :
  catégories autorisées `voiture`, `chien` (devenue `animaux`), `enfants`, `maison`, `sante`, `administratif`, `factures`, `photos`, `autre` ;
  déplace vers `data/fichiers/<categorie>/<YYYY>/<nouveau_nom>` (nom nettoyé, extension conservée) ; écrit une ligne dans
  `data/fichiers/<categorie>/index.md` (`- <date> · <nouveau_nom> · <description>`) ; si `fiche` (chemin relatif autorisé par
  `memoire`, ex. `foyer/voiture.md` ou `animaux/<nom>.md`) : ajoute une ligne datée avec la description et le lien relatif du fichier.
  `memoire.py` : étendre les racines autorisées à `foyer`, `personnes`, `alimentation`, `animaux`, `voitures`.
- `fichiers_lister(categorie: str | None, limite: int = 20)` : derniers fichiers rangés.
- `fichiers_entrants()` : fichiers encore dans `entrants/` (non rangés) avec âge.
- `photos_journal_ajouter(chemin_source, legende, date: str | None)` : réduit l'image (max 1600 px, JPEG q85, Pillow) vers
  `data/photos/<YYYY>/<MM>/<YYYY-MM-DD>_<slug>.jpg`, ajoute une ligne dans `data/foyer/journal/<YYYY-MM>.md`
  (`- <date> 📷 <legende> (photos/...)`).
- Purge : `tools/runner.py purge` (appelé par `scripts/backup.sh` avant la sauvegarde) : supprime `entrants/` > 30 jours,
  `.kd/captures` > 90 jours, vocaux temporaires > 7 jours.

## Contenu (`content/`)
- `CLAUDE.md` : section « Fichiers reçus » : lire avec `Read`, ne jamais stocker de données médicales détaillées dans les fiches
  (pour une ordonnance : nom du prescripteur, date, « renouvellement avant le … » → échéance ; PAS les médicaments ni les doses, sauf
  pour le chien), extraction type par catégorie : facture garage → date, garage, kilométrage, montant, intervention → `voitures/<nom>.md`
  + `fichiers_ranger(categorie=voiture)` ; facture/garantie équipement → `foyer/maison.md` + échéance fin de garantie ; document école
  → `personnes/enfants.md` + échéances ; courrier administratif → échéance si date limite ; photo souvenir → `photos_journal_ajouter`.
  Toujours dire en 2 lignes ce qui a été rangé et où. Demander la catégorie si ambigu.
- Module onboarding `30-vehicules-animaux.md` : noms et immatriculations des véhicules (pas de numéro de carte grise), nom du chien,
  vétérinaire, dates de rappel → `voitures/<nom>.md`, `animaux/<nom>.md`.

## Tests
`tests/test_fichiers.py` : rangement (déplacement, index, ligne de fiche), noms nettoyés, catégories refusées, réduction photo
(Pillow, image générée), purge avec dates simulées (`HOMEOS_NOW`). `bot/requirements.txt` : `Pillow>=10`, `pillow-heif` optionnel.
