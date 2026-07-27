# Configuration TikTok + Camofox

Guide d'installation et de déploiement de l'automatisation TikTok
via Camofox (navigateur headless) pour le backoffice Hermes Social.

---

## 1. Présentation : Camofox

**Camofox** est un navigateur headless (basé sur Firefox) spécialement conçu
pour l'automatisation de tâches web dans un environnement conteneurisé. Il
expose une API REST sur le port `9377` permettant de créer des onglets,
exécuter du JavaScript (`.evaluate()`), cliquer sur des sélecteurs
(`.click()`), et interagir avec le DOM des pages chargées.

Contrairement à un navigateur headless classique, Camofox embarque un
**serveur X11/VNC** qui rend le bureau virtuel accessible visuellement. Cela
permet :

- de **voir** ce que le navigateur voit en temps réel ;
- de **déboguer** les interactions automatisées ;
- d'effectuer des **connexions manuelles initiales** via noVNC (voir §5).

Dans l'architecture Hermes Social, Camofox joue le rôle de couche
d'interaction avec TikTok : lecture des commentaires, navigation dans les
messages privés (DMs), et envoi de réponses par collage de texte dans le
presse-papiers X11.

---

## 2. Image Docker `camofox-browser:135.0.1-x86_64`

L'image utilisée est construite à partir du dépôt
[jo-inc/camofox-browser](https://github.com/jo-inc/camofox-browser) avec la
commande `make camofox`. Le tag `135.0.1-x86_64` correspond à une version
spécifique du moteur de rendu Firefox 135.0.1, compilée pour architecture
x86_64.

Déclaration dans `deploy/docker-compose.yml` :

```yaml
services:
  camofox:
    image: camofox-browser:135.0.1-x86_64
    restart: unless-stopped
    ports:
      - "127.0.0.1:6080:6080"   # noVNC — local uniquement, tunnel SSH
      - "127.0.0.1:5901:5900"   # VNC brut — local uniquement
    environment:
      CAMOFOX_PORT: "9377"
      ENABLE_VNC: "1"
      VNC_BIND: 0.0.0.0
      VNC_RESOLUTION: 1920x1080
      MAX_OLD_SPACE_SIZE: "2048"
    volumes:
      - ./camofox-data:/root/.camofox        # profil utilisateur persistant
      - ./camofox.config.json:/app/camofox.config.json:ro
```

Points importants :

- **`ENABLE_VNC=1`** active le serveur X11/VNC embarqué.
- **`VNC_RESOLUTION=1920x1080`** définit la résolution du bureau virtuel.
- Les ports VNC sont liés à `127.0.0.1` uniquement — aucune exposition
  publique.
- Le volume `./camofox-data:/root/.camofox` persiste le profil navigateur
  (voir §3).

Si vous déployez sur une architecture ARM64 (Apple Silicon, etc.),
remplacez le tag par `camofox-browser:135.0.1-aarch64` (vérifiez la
disponibilité auprès de l'éditeur de l'image).

---

## 3. Persistance IndexedDB pour session TikTok persistée

TikTok ne requiert pas de jeton API pour l'automatisation par navigateur
(voir §6). L'authentification est gérée par la **session navigateur**
stockée dans le profil Firefox persistant.

Le mécanisme est le suivant :

1. **Connexion manuelle initiale** via noVNC (voir §5) — l'utilisateur se
   connecte à son compte TikTok une fois.
2. Le profil Firefox est sauvegardé dans le volume Docker
   `./camofox-data:/root/.camofox`.
3. TikTok stocke ses tokens de session dans **IndexedDB** et **Storage**
   (localStorage / sessionStorage) du navigateur.
4. Tant que le profil n'est pas supprimé et que la session TikTok n'est pas
   déconnectée, le navigateur conserve l'authentification entre les
   redémarrages du conteneur.

**Conséquences importantes :**

- Ne **jamais** supprimer le volume `camofox-data` sans avoir vérifié que la
  session TikTok peut être re-créée.
- Ne **jamais** cliquer sur « Déconnexion » (Log out) dans l'interface
  TikTok — cela invaliderait définitivement la session et nécessiterait une
  nouvelle connexion manuelle (y compris 2FA le cas échéant).
- Une session valide est détectée par le code via la présence du mot
  « Messages » dans le texte du DOM et l'absence de mentions « Log in » /
  « Sign up ». Voir `_COMMENT_EXTRACTION_JS` dans `camofox_reader.py` :

```javascript
logged_in: text.includes('Messages') &&
  !/\b(Log\s*in|Sign\s*up)\b/i.test(text)
```

---

## 4. Installer `xclip` + `xdotool` dans le conteneur

L'envoi de messages privés (DMs) sur TikTok utilise une technique de
**collage de texte dans le presse-papiers X11**, car le champ de saisie des
DMs TikTok est difficile à cibler par sélecteur DOM classique.

Deux outils sont nécessaires dans le conteneur Camofox :

| Outil    | Rôle                                           |
|----------|------------------------------------------------|
| `xclip`  | Écrire le texte du DM dans le presse-papiers X11 |
| `xdotool`| Simuler le collage (`Ctrl+V`) dans le champ cible |

### Procédure d'installation

Connectez-vous au conteneur en cours d'exécution :

```bash
docker exec -it hermes-social-camofox-1 bash
```

Installez les paquets :

```bash
apt-get update && apt-get install -y xclip xdotool
```

Vérifiez que les outils répondent :

```bash
xclip -version
xdotool --version
```

### Utilisation dans le code

Le script de DM automatisé envoie les commandes suivantes via le protocole
Camofox (expression JavaScript exécutée dans le navigateur) :

1. Clique dans le champ de saisie du DM.
2. Copie le texte de réponse dans le presse-papiers X11.
3. Colle avec `Ctrl+V` via xdotool.
4. Vérifie la visibilité du texte collé.
5. Clique sur le bouton d'envoi.

> **Note :** Ces installations sont perdues au redémarrage du conteneur si
> l'image n'est pas modifiée. Pour une solution permanente, créez une image
> dérivée avec un `Dockerfile` contenant ces instructions, ou montez un
> script d'initialisation qui installe les paquets au démarrage.

---

## 5. Connexion manuelle initiale via noVNC

Avant que l'automatisation puisse fonctionner, une **connexion manuelle**
à TikTok est requise pour initialiser la session persistante.

### Étape 1 : Tunnel SSH

Depuis votre machine locale, créez un tunnel SSH vers le VPS qui héberge le
conteneur Camofox :

```bash
ssh -L 6080:127.0.0.1:6080 root@<ADRESSE_DU_VPS>
```

Cette commande redirige le port `6080` local vers le port `6080` du VPS
(le service noVNC du conteneur).

### Étape 2 : Accéder à noVNC

Ouvrez un navigateur web sur votre machine locale et rendez-vous sur :

```
http://localhost:6080/vnc.html
```

Vous verrez le bureau virtuel X11 du conteneur Camofox. Si le navigateur
Firefox n'est pas encore ouvert, lancez-le depuis le terminal du conteneur :

```bash
docker exec -it hermes-social-camofox-1 firefox &
```

Ou, si Firefox est déjà lancé mais masqué, l'automatisation le détecte via
le DOM retourné par Camofox.

### Étape 3 : Connexion TikTok

Dans le navigateur Firefox visible via noVNC :

1. Accédez à `https://www.tiktok.com`.
2. Connectez-vous avec le compte TikTok dédié à l'automatisation.
3. Si la 2FA (authentification à deux facteurs) est activée, validez-la.
4. Une fois connecté, **ne fermez pas la session** et ne cliquez pas sur
   « Déconnexion ».

### Étape 4 : Vérification

Le code d'extraction des commentaires vérifie automatiquement l'état de la
connexion via l'expression JavaScript mentionnée en §3. Pour une vérification
rapide depuis l'hôte :

```bash
docker exec hermes-social-camofox-1 sh -c \
  'echo "✓ Camofox running"; curl -s http://localhost:9377/tabs/health 2>/dev/null || echo "✗ Camofox API unreachable"'
```

---

## 6. Aucun token API nécessaire — session navigateur

Contrairement à Meta (Facebook/Instagram) qui utilise des tokens OAuth et
des webhooks API, **TikTok ne fournit pas d'API publique standard** pour
la lecture des commentaires ou l'envoi de messages privés sur les vidéos
grand public.

L'architecture contourne cette limitation en utilisant une **session
navigateur persistante** :

- **Pas de client ID, pas de client secret, pas de token d'accès.**
- L'authentification est gérée par la session HTTP du navigateur
  (cookies, IndexedDB, localStorage).
- Toutes les interactions passent par l'API REST de Camofox, qui exécute
  du JavaScript dans le contexte de la page TikTok chargée.

### Implications

| Aspect               | Approche classique (API) | Approche Camofox (navigateur) |
|----------------------|--------------------------|-------------------------------|
| Authentification     | Token OAuth              | Session navigateur persistée  |
| Renouvellement       | Refresh token            | Connexion manuelle (rare)     |
| Blocage              | Révocation de token      | CAPTCHA / déconnexion         |
| Dépendance           | Documentation API        | DOM TikTok (peut changer)     |

**Risque connu :** Le DOM de TikTok peut changer à tout moment, ce qui peut
casser les sélecteurs CSS et les expressions JavaScript utilisées pour
l'extraction des commentaires ou l'envoi de DMs. Les tests automatisés
(`test_camofox_comments.py`) aident à détecter ces changements.

---

## 7. Structure du code

Deux fichiers principaux composent le cœur de l'automatisation TikTok :

### `app/camofox_reader.py`

**Chemin :** `social/tools/tiktok-backoffice/app/camofox_reader.py`

Rôle : Interface avec l'API REST de Camofox pour lire les commentaires
d'une vidéo TikTok.

Classes et fonctions principales :

| Symbole                          | Rôle                                                |
|----------------------------------|-----------------------------------------------------|
| `CamofoxClient`                  | Client HTTP vers l'API REST de Camofox              |
| `CamofoxClient.create_tab()`     | Ouvre un nouvel onglet vers une URL                 |
| `CamofoxClient.evaluate()`       | Exécute du JavaScript dans l'onglet                 |
| `CamofoxClient.click()`          | Clique sur un élément via sélecteur CSS             |
| `fetch_comments_from_camofox()`  | Orchestre la lecture des commentaires d'une vidéo   |
| `extract_comments_from_dom_result()` | Parse le DOM pour extraire les commentaires     |
| `_COMMENT_EXTRACTION_JS`         | Script JS pour l'extraction de commentaires         |
| `_ACTIVATE_COMMENTS_TAB_JS`      | Script JS pour ouvrir l'onglet commentaires         |
| `_target_video_coherence()`      | Vérifie que la vidéo cible est bien affichée        |
| `DEFAULT_CAMOFOX_BASE_URL`       | `http://camofox:9377`                               |
| `DEFAULT_CAMOFOX_USER_ID`        | `hermes_80317d7dba`                                 |

Flux d'exécution :

```
fetch_comments_from_camofox(video_url)
  +-- CamofoxClient.create_tab(session_key, url)
  +-- Attente (wait_seconds)
  +-- Boucle: activation onglet Commentaires (click ou JS fallback)
  +-- CamofoxClient.evaluate(_COMMENT_EXTRACTION_JS)
  +-- extract_comments_from_dom_result()
  +-- _target_video_coherence() -- validation croisée
  +-- Retour: {ok, comments, logged_in, captcha, diagnostics}
```

### `scripts/tiktok_cron_auto_dm.py`

**Chemin :** `scripts/tiktok_cron_auto_dm.py`

Rôle : Worker cron qui exécute le pipeline complet de lecture et d'envoi
de DMs, sans intervention LLM (mode `no_agent=true`).

Pipeline attendu :

1. Lecture des cibles vidéo depuis la base SQLite
   (`poll_targets()`).
2. Pour chaque vidéo : appel à `fetch_comments_from_camofox()`.
3. Ingestion des commentaires dans la base (`ingest_comments()`).
4. Pour chaque commentaire matché : vérification anti-dedup (voir §8).
5. Si nouveau : navigation vers le profil de l'auteur, ouverture du DM,
   collage de la réponse via xclip/xdotool, envoi.
6. Enregistrement de l'événement dans `tiktok_browser_events`.

Planification cron (toutes les 15 minutes) via le fichier `cron/jobs.json`
de l'agent Hermes :

```json
{
  "tiktok-auto-dm": {
    "schedule": "*/15 * * * *",
    "command": "uv run python scripts/tiktok_cron_auto_dm.py --apply",
    "no_agent": true,
    "timeout": 600
  }
}
```

### Autres fichiers connexes

| Fichier                                 | Rôle                                      |
|-----------------------------------------|-------------------------------------------|
| `app/store.py`                          | Modèles SQLite et opérations base         |
| `app/models.py`                         | Dataclasses (Comment, Draft, Campaign...) |
| `app/cli.py`                            | CLI `tiktok-backoffice`                   |
| `app/discovery.py`                      | Découverte de vidéos d'un profil TikTok   |
| `app/keyword.py`                        | Détection de mots-clés dans les commentaires |
| `app/kanban_import.py`                  | Synchronisation campagnes depuis Kanban   |
| `tests/test_camofox_comments.py`        | Tests d'extraction Camofox               |

---

## 8. Anti-dedup : vérification historique DM scoped avec handle cible

L'anti-dédoublonnage empêche l'envoi de plusieurs DMs à un même utilisateur
pour le même commentaire ou pour des commentaires similaires sur la même
vidéo.

### Base SQLite

La table `tiktok_comments` utilise un identifiant unique (`comment_id`)
construit par empreinte SHA-256 :

```python
def _comment_fingerprint(*, video_url, author, text, created_time=None):
    material = "\␟".join([
        video_url, author or "", text,
        "" if created_time is None else str(created_time)
    ])
    return "fp:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
```

La contrainte `ON CONFLICT(comment_id) DO NOTHING` garantit qu'un même
commentaire n'est inséré qu'une seule fois.

### Scope par handle cible

L'anti-dedup est **scopé par handle cible** : pour chaque commentaire
matché par mot-clé, le système vérifie dans l'historique DM si un message
a déjà été envoyé à l'auteur de ce commentaire **pour la même campagne**.
Cette vérification s'appuie sur la jointure entre les tables suivantes :

```sql
SELECT ri.comment_id
FROM tiktok_review_items ri
JOIN tiktok_comments c ON c.comment_id = ri.comment_id
WHERE c.author = ?            -- handle cible (ex: @nomutilisateur)
  AND ri.campaign_slug = ?    -- campagne en cours
  AND ri.status IN ('posted', 'drafted_in_browser')
```

Logique de décision :

| Condition                                          | Action            |
|----------------------------------------------------|-------------------|
| Auteur déjà contacté pour cette campagne           | Ignorer (skip)    |
| Auteur jamais contacté pour cette campagne         | Créer un review item |
| Commentaire déjà enregistré (`comment_id` existant)| Ignorer (skip)    |
| Commentaire nouveau + auteur nouveau pour campagne | Ajouter + traiter |

Cette approche évite les spams involontaires tout en permettant de
contacter un même utilisateur pour différentes campagnes (ex: « proxy »
puis « bot ») si ses commentaires le justifient.

---

## 9. Attention : ne pas logout TikTok, ne pas supprimer profil Camofox

### Règle n°1 : Ne jamais cliquer sur « Déconnexion »

Si la session TikTok est déconnectée depuis l'interface du navigateur :

- Les cookies et tokens IndexedDB sont invalidés.
- Une nouvelle connexion manuelle via noVNC est obligatoire.
- Si la 2FA est activée, celle-ci devra être validée à nouveau.

**Scénarios à risque :**

- Navigation manuelle dans le navigateur Camofox via noVNC : ne pas cliquer
  sur l'avatar -> « Log out » ou « Déconnexion ».
- Automation qui échoue : le code ne déconnecte jamais la session, mais une
  erreur JavaScript accidentelle pourrait cliquer sur le mauvais élément.
  Les expressions JS utilisent des sélecteurs ciblés pour éviter cela.

### Règle n°2 : Ne jamais supprimer le profil Camofox

Le volume Docker `./camofox-data` contient le profil utilisateur complet de
Firefox, incluant :

- `profiles/<id>/storage-state.json` -- état du stockage navigateur
  (IndexedDB, localStorage, cookies).
- `profiles/<id>/meta.json` -- métadonnées du profil.

Supprimer ce volume équivaut à **perdre la session TikTok** et toutes les
autres données de navigation persistées.

### Procédure de vérification de la session

Pour vérifier que la session TikTok est toujours valide, sans risque :

```bash
# Verifier que le conteneur tourne
docker ps | grep camofox

# Verifier que le profil existe
docker exec hermes-social-camofox-1 ls -la /root/.camofox/profiles/

# Tester l'API Camofox
curl -s http://camofox:9377/tabs/health | head -1
```

En cas de session invalide (détectée par `logged_in: false` dans le résultat
de `fetch_comments_from_camofox()`), l'automatisation **ne tente pas** de
reconnexion et émet une alerte demandant une intervention manuelle via
noVNC.

### Règle n°3 : Redémarrage du conteneur

Le redémarrage du conteneur Camofox est sans risque pour la session :

```bash
docker restart hermes-social-camofox-1
```

La session TikTok est conservée dans le volume persistant et restaurée au
démarrage. Attendez quelques secondes que Camofox soit prêt avant
d'utiliser l'API :

```bash
sleep 3
curl -s http://camofox:9377/tabs/health
```

---

## Références

- [Camofox Browser](https://github.com/jo-inc/camofox-browser) -- depot officiel
- [`deploy/docker-compose.yml`](/deploy/docker-compose.yml) -- service Camofox
- [`social/tools/tiktok-backoffice/app/camofox_reader.py`](/social/tools/tiktok-backoffice/app/camofox_reader.py)
- [`social/tools/tiktok-backoffice/app/store.py`](/social/tools/tiktok-backoffice/app/store.py)
- [Feuille de route TikTok + Meta](/docs/roadmap/tiktok-meta-backoffice-roadmap.md)
