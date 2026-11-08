# Documents sensibles : chiffrement dans le navigateur

Passeports, cartes d'identité, ordonnances, avis d'imposition, relevés : ces documents sont **chiffrés dans le
navigateur** avant d'être envoyés. Le serveur range des blobs opaques et quelques métadonnées ; il ne possède jamais
la clé, le modèle n'y a aucun accès, et ils ne passent jamais par Telegram. Côté serveur : `web/sensibles.py`
(contrat : CONTRACT §12 et §15). Ce document décrit le schéma et son implémentation dans le front (lot 4b,
`web/front/`) : `src/lib/sensibles/crypto.ts` (fonctions pures WebCrypto), `src/lib/sensibles/appareil.ts`
(IndexedDB), `src/store/sensibles.ts` (parcours, verrouillage), `src/ecrans/Sensibles.tsx` (écran Fichiers).

Implémentation de référence en Python (mêmes constantes) : `tests/sensibles_reference.py`, aller-retour complet avec
le serveur dans `tests/test_web_sensibles.py::test_schema_de_reference_aller_retour`. **Interopérabilité vérifiée**
dans les deux sens : vecteurs communs `tests/fixtures/sensibles-vecteurs.json` (régénérés par la référence, contrôlés
par pytest, relus par Vitest : mêmes octets pour un document, une enveloppe PRF, une enveloppe d'appareil, une phrase)
et `tests/front/test_front_sensibles.py` (`pytest -m navigateur`, Chromium avec passkey virtuelle avec et sans PRF :
un document déposé par le front se déchiffre en Python avec la clé tirée de la phrase affichée, un document chiffré
en Python s'ouvre dans le front).

## 1. Ce que voit chacun

| Qui | Voit |
| --- | --- |
| La personne, appareil déverrouillé (passkey ou phrase) | Ses documents en clair, dans l'onglet seulement |
| L'autre adulte | Rien (ni liste, ni blob : 404) — pas de partage en v1 |
| Le serveur, ses sauvegardes restic, un attaquant qui les vole | Blobs chiffrés + libellé, catégorie, date, taille, identifiant de clé |
| Le modèle (Claude) | Rien : aucun outil, lecture refusée (`tools.securite`), dossier hors des index de fichiers |

Le libellé (`nom`) reste en clair sur le serveur : le front propose un libellé neutre (« Passeport », « Ordonnance »)
et chiffre le nom d'origine du fichier avec le contenu.

## 2. Clés

**Clé maîtresse des documents (CMD)** : 32 octets aléatoires (`crypto.getRandomValues`), une par personne, créée au
premier dépôt. Identifiant de clé `kid` : 16 octets aléatoires (indépendants de la clé), écrit en hexadécimal
(32 caractères) dans chaque blob et dans le trousseau. La CMD n'existe en clair qu'en mémoire, le temps d'un
déverrouillage (importée en `CryptoKey` AES-GCM **non extractible** ; oubliée après 5 min d'inactivité, quand
l'onglet passe en arrière-plan, ou à la déconnexion).

Elle est protégée de deux façons, essayées dans cet ordre :

### a) Passkey avec l'extension WebAuthn PRF (préféré)

Disponible sur les passkeys récentes (iCloud Keychain, gestionnaire Google, Windows Hello, clés FIDO2 avec
`hmac-secret`). Une passkey synchronisée donne la même sortie PRF sur tous les appareils de la personne : aucun
secret à recopier.

1. **Annoncer PRF à l'enrôlement** : avant `navigator.credentials.create`, ajouter aux options reçues de
   `/auth/inscription/options` : `extensions: { prf: {} }`. `getClientExtensionResults().prf?.enabled` dit si la
   passkey sait faire.
2. **Obtenir la sortie PRF** pendant un step-up (`/auth/step-up/options`) : ajouter aux options
   `extensions: { prf: { evalByCredential: { [credential_id]: { first: sel } } } }` (`sel` : 32 octets de l'enveloppe
   de cette passkey ; à la création d'une enveloppe, un `sel` neuf et `eval: { first: sel }`).
   `getClientExtensionResults().prf.results.first` : 32 octets.
3. **Ne jamais envoyer la sortie PRF** : supprimer `clientExtensionResults.prf.results` de la réponse avant de la
   poster à `/auth/step-up`. Le serveur **refuse (400)** toute assertion qui la contient.
4. **Clé d'enveloppe (KEK)** : `HKDF-SHA-256(ikm = sortie PRF, salt = sel, info = "homeos-sensibles-v1/kek")` →
   clé AES-GCM 256 bits non extractible (`crypto.subtle.importKey("raw", prf, "HKDF", false, ["deriveKey"])` puis
   `deriveKey`).
5. **Enveloppe** : `AES-GCM(KEK, iv = 12 octets aléatoires, additionalData = UTF-8("homeos-sensibles-v1|" + kid +
   "|" + credential_id))` appliqué aux 32 octets de la CMD. Objet envoyé au trousseau :
   `{type: "prf", credential_id, sel, iv, cle_chiffree, cree?}` (valeurs en base64url sans remplissage ;
   `credential_id` = `rawId` de la passkey).

Une enveloppe par passkey de la personne (10 au plus). Après l'ajout d'une passkey, déverrouiller avec une autre
méthode puis créer l'enveloppe de la nouvelle.

**Dans le front** : `GET /api/sensibles/trousseau` exige déjà un step-up ; pour demander la bonne sortie PRF dès la
première empreinte, le front garde dans `localStorage` (`homeos-sensibles:sels:<linux_user>`) les couples
`{credential_id, sel}` du trousseau (publics, déjà sur le serveur ; jamais de clé). Chaque step-up des documents
porte `eval: {first: sel neuf}` et `evalByCredential` pour les passkeys connues (filtré sur `allowCredentials`,
sinon le navigateur refuse). Sel inconnu de l'appareil (première ouverture ici) : une seconde empreinte avec le sel
de l'enveloppe. Passkey sans enveloppe mais avec PRF : sa sortie (sel neuf) reste en mémoire le temps de retrouver
la clé par la clé d'appareil ou la phrase, puis son enveloppe est ajoutée au trousseau (`PUT`). L'enrôlement
annonce `extensions: {prf: {}}` (`web/front/src/lib/api.ts`) ; `credentialJson` retire toujours
`clientExtensionResults.prf.results` (sinon un `{}` sérialisé ferait refuser l'assertion).

### b) Clé d'appareil dans IndexedDB (repli, sans PRF)

- KEK d'appareil : `crypto.subtle.generateKey({name: "AES-GCM", length: 256}, false, ["encrypt", "decrypt"])`
  (non extractible), rangée dans IndexedDB (base `homeos-sensibles`, magasin `cles`, clé `kek-appareil:<linux_user>`).
- Enveloppe d'appareil `{type: "appareil", kid, iv, cle_chiffree}` (même chiffrement, `additionalData =
  "homeos-sensibles-v1|" + kid + "|appareil"`), rangée **dans IndexedDB seulement** : le serveur refuse de la stocker.
- Ouvrir sur un autre appareil demande la phrase de récupération.
- Dans le front, l'enveloppe est rangée sous `enveloppe-appareil:<linux_user>` (même magasin). Repli utilisé quand la
  passkey ne rend pas de sortie PRF ; après une ouverture par la phrase sur un appareil sans PRF, une clé d'appareil
  est créée pour les fois suivantes. IndexedDB indisponible (navigation privée) : rien n'est gardé, la phrase sert à
  chaque ouverture.

### c) Phrase de récupération (toujours, affichée une seule fois)

À la création de la CMD, quelle que soit la méthode : `CMD (32 octets) || SHA-256(CMD)[0..2]` (34 octets), en
base32 Crockford (`0123456789ABCDEFGHJKMNPQRSTVWXYZ`, bits complétés par des zéros) : 55 caractères, affichés en
11 groupes de 5 séparés par des tirets. Écran dédié : « Note cette phrase sur papier. Sans elle et sans ta passkey,
tes documents sont perdus. » ; confirmation en retapant le dernier groupe ; jamais copiée dans le presse-papiers par
défaut, jamais envoyée (ni au serveur, ni à l'assistant, ni à Telegram).
À la saisie : majuscules, `O`→`0`, `I`/`L`→`1`, tirets ignorés, somme de contrôle vérifiée, puis essai de
déchiffrement d'un blob du `kid` attendu avant d'accepter.

**Perte** de la passkey et de la phrase : les documents sont irrécupérables, par construction. L'interface le dit
au moment de la création, en une phrase.

**Dans le front** : la clé n'est enregistrée (trousseau ou clé d'appareil) qu'après « Je l'ai notée » (dernier
groupe retapé) ; « Plus tard » ou la fermeture abandonne tout (une nouvelle phrase sera proposée). Premier usage =
ni trousseau, ni clé d'appareil, ni document. L'écran de la phrase se verrouille comme les documents (5 min,
arrière-plan). Essai de la phrase : le plus petit document du `kid` attendu (celui du trousseau, sinon le plus
fréquent des documents) ; refus : « Cette phrase n'ouvre pas tes documents ».

## 3. Format d'un document (enveloppe `HSB1`, version 1)

```
octets  0-3    "HSB1"                       (vérifié par le serveur)
octet   4      0x01  version                (vérifié par le serveur)
octets  5-20   kid (16 octets)              (lu par le serveur : métadonnée « cle »)
octets 21-32   iv (12 octets aléatoires, jamais réutilisé avec la même CMD)
octets 33-     AES-256-GCM(CMD, iv, données associées = octets 0-32, étiquette 128 bits)
```

Texte clair chiffré : `longueur L du JSON (4 octets, gros-boutiste) || JSON UTF-8 {nom, type, taille, cree?}
(nom d'origine du fichier, type MIME, taille en octets) || contenu du fichier`.

Le serveur refuse (415) tout ce qui ne commence pas par `HSB1` + version 1 ou fait moins de 49 octets : un fichier en
clair envoyé par erreur n'est jamais stocké. 20 Mo au plus par document, 300 documents par personne.

## 4. Points d'accès (tous sous step-up, `X-Homeos-Step-Up`)

- `GET /api/sensibles` → `{documents: [{id, nom, categorie, date, taille, cle}], trousseau: bool, categories}`.
- `POST /api/sensibles` multipart : `blob` (le blob `HSB1`), `nom` (libellé, 80 caractères), `categorie`
  (`identite`, `sante`, `animal`, `administratif`, `banque`, `logement`, `voiture`, `enfants`, `autre`) → `{document}` ;
  415 blob en clair ou tronqué, 409 plafond atteint, 413 trop gros.
- `GET /api/sensibles/{id}` → le blob, octet pour octet (`application/octet-stream`, `Cache-Control: no-store`,
  en-tête `X-Homeos-Cle: <kid>`) ; 404 s'il n'appartient pas à la personne.
- `DELETE /api/sensibles/{id}` → `{ok}`.
- `GET /api/sensibles/trousseau` → `{trousseau: {version: 1, cle, enveloppes: [...], maj} | null}` ;
  `PUT /api/sensibles/trousseau` `{version: 1, cle, enveloppes: [{type: "prf", credential_id, sel, iv,
  cle_chiffree, cree?}]}` : seules les enveloppes PRF sont acceptées, tout autre champ est refusé (400).
- Événement `maj {domaine: "sensibles", action}` sur le flux de la personne après un dépôt ou une suppression.

## 5. Parcours dans l'appli

1. Écran Fichiers → section « Documents sensibles » (cadenas). Ouvrir = step-up passkey (avec PRF si connu).
2. Premier usage : création de la CMD, enveloppe PRF (ou d'appareil), `PUT /api/sensibles/trousseau` (PRF),
   phrase de récupération affichée une fois.
3. Ajouter (bouton de l'écran, ou « + » → Document sensible, qui ouvre l'écran Fichiers avec le fichier en attente
   du déverrouillage) : photo ou PDF, catégorie parmi celles de la maquette (pièce d'identité `identite`, carnet de
   santé `sante`, carnet de l'animal `animal`, autre `autre` ; les autres catégories du serveur s'affichent dans
   « Autres »), libellé neutre proposé selon la catégorie (modifiable, « le nom reste lisible ») ; chiffrement
   `crypto.subtle` (asynchrone, hors du fil principal du navigateur : pas de Web Worker, 20 Mo au plus) ;
   `POST /api/sensibles`.
4. Ouvrir : `GET /api/sensibles/{id}`, vérifier `kid`, déchiffrer, afficher depuis une URL `blob:` révoquée à la
   fermeture et au verrouillage ; aucune copie sur disque, pas de téléchargement. Types affichés : images et PDF
   (cadre `blob:`, d'où `frame-src blob:` dans la CSP de l'appli) ; tout autre type devient
   `application/octet-stream` (jamais rendu comme une page).
5. Supprimer : depuis l'aperçu, après confirmation (« effacé pour de bon »).
6. Carte `document_sensible` dans le fil (action `deverrouiller`, `args.id`) : écran Fichiers, empreinte, document
   ouvert.
7. États de l'écran : verrouillé (vignettes floutées si la liste est connue de l'onglet), première fois (phrase),
   « Cet appareil n'a pas la clé : utilise ta phrase de secours », ouvert (« Protégé par ton empreinte » ou « sur cet
   appareil »), vide, connexion de secours (aucune empreinte demandée : « tes documents sensibles restent fermés »).
   Aucun mot technique à l'écran. Verrouillage automatique : 5 min sans activité (pointeur, clavier, toucher,
   molette), passage en arrière-plan (`visibilitychange`, `pagehide`), déconnexion ; la liste des libellés reste
   affichée, verrouillée, jusqu'au rechargement.

## 6. Règles pour le front

- Le **service worker ne met jamais en cache** `/api/sensibles*` ni les URL `blob:` des documents déchiffrés.
- Aucune journalisation (console, télémétrie) d'une clé, d'une sortie PRF, d'une phrase ou d'un contenu.
- La CMD n'est jamais écrite en clair dans IndexedDB, `localStorage` ou `sessionStorage`.
- Rien de ce parcours ne passe par la conversation ni par le modèle ; Telegram n'y a pas accès.
- CSP stricte de l'appli (pas de script en ligne) : une faille XSS donnerait accès aux documents déverrouillés, d'où
  le verrouillage automatique.

## 7. Serveur, sauvegardes, restauration

- `data/fichiers/sensibles/<linux_user>/` : 700 `homeos` (l'appli), blobs et `index.json` en 600, `trousseau.json`.
  Créé par l'appli au premier dépôt et par `install.sh` (exclu des droits de groupe `foyer`). `scripts/verifier-secrets.sh`
  vérifie qu'aucune personne ne peut les lire (le déchiffrement n'a lieu que dans le navigateur).
- Hors du dépôt git de `data/` (`tools.memoire.GITIGNORE`), hors de la purge, hors des index de `tools.fichiers`.
- **Sauvegardés** par restic avec le reste de `data/` (blobs chiffrés, utilisables après restauration avec la passkey
  ou la phrase). La restauration ne demande aucune clé côté serveur.
