# Meta Comment DM Automation

Automatisation Meta pour le scénario :

> Un utilisateur commente un mot-clé comme `Proxy` sous une publication Instagram ou Facebook. L'app détecte le commentaire, like le commentaire, répond publiquement, puis envoie la ressource demandée en message privé.

## Objectif

Construire proprement une intégration Meta Graph API avec :

- lecture des commentaires Instagram/Facebook ;
- détection de mots-clés ;
- like de commentaires ;
- réponse publique ;
- private reply / DM avec lien de ressource ;
- webhook Meta pour automatiser le flow ;
- anti-doublon pour éviter les envois multiples.

## Statut

Validation manuelle Graph API Explorer réussie :

- compte Instagram connecté à l'app Meta ;
- token d'accès généré ;
- récupération Page Facebook ;
- récupération Instagram Business Account ;
- lecture médias ;
- lecture commentaires ;
- like commentaire ;
- réponse publique ;
- private reply / DM.

## Permissions Meta prévues

Voir [`docs/meta-permissions.md`](docs/meta-permissions.md).

## Routes Graph API testées

Voir [`docs/api-test-routes.md`](docs/api-test-routes.md).

## Sécurité

Ne jamais committer :

- Access Tokens ;
- App Secret ;
- Page Access Token ;
- Verify Token réel ;
- fichiers `.env`.

Utiliser `.env.example` comme modèle.

## Stack technique

Le backend démarre en FastAPI pour garder une base simple à comprendre et tester.

Structure actuelle :

```text
app/main.py              # endpoints FastAPI: health + webhook Meta + pages légales Meta
app/config.py            # configuration depuis les variables d'environnement
app/security.py          # validation X-Hub-Signature-256
app/keyword.py           # détection du mot-clé
app/graph_client.py      # client Meta Graph API
app/webhook_parser.py    # extraction des commentaires depuis les payloads Meta
app/processor.py                # orchestration: règle campagne -> reply -> DM
app/store.py                    # anti-doublon SQLite + statut livraison public/DM
app/campaign_rules.py           # règles campagnes stockées dans SQLite
app/comment_review_store.py     # file SQLite de revue manuelle commentaires/réponses/DMs
app/comment_review_classifier.py # classification des commentaires à traiter manuellement
app/comment_review_scanner.py   # logique partagée pour alimenter la file de revue
app/review_comments.py          # CLI: next/link/reply/skip/count depuis Hermes
app/scan_comment_reviews.py     # CLI: scan historique vers file de revue
app/webhook_review_enqueue.py   # enqueue temps réel commentaires/réponses/DMs entrants
app/webhook_manual_review.py    # réconciliation réponses faites directement sur Meta
app/kanban_sync.py              # synchronisation Kanban Hermes -> SQLite
app/backfill_comments.py        # rattrapage des commentaires non traités
app/platform_utils.py           # utilitaires IDs Facebook, labels, owner detection
app/sqlite_store.py             # base commune pour stores SQLite
app/token_renewer.py            # renouvellement tokens Meta
tests/                          # tests pytest
```

## Installation locale

Avec `uv` :

```bash
uv sync --dev
```

Copier le fichier d'environnement :

```bash
cp .env.example .env
```

Puis remplir `.env` avec tes valeurs Meta sans jamais les committer.

Variables owner optionnelles :

```text
META_OWNER_IDS=123,456
META_OWNER_USERNAMES=dupflodev
```

`META_PAGE_ID` et `META_IG_USER_ID` sont automatiquement considérés comme des identités owner. Ces variables servent à ignorer tes propres réponses et à réconcilier les commentaires déjà traités directement sur Facebook/Instagram.

## Lancer l'API

```bash
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Health check :

```bash
curl http://localhost:8000/health
```

Endpoint webhook Meta :

```text
GET  /webhook/meta   # vérification webhook Meta
POST /webhook/meta   # réception des événements Meta
```

Le `POST /webhook/meta` parse les événements commentaire, ignore les commentaires sans mot-clé, applique un anti-doublon SQLite, puis appelle Meta Graph API pour liker, répondre publiquement et envoyer la private reply.

## Campagnes via Kanban Hermes

La configuration métier multi-campagnes ne doit pas vivre dans `.env`. Le `.env` reste réservé aux secrets, IDs et paramètres d'infrastructure.

Le backoffice métier est le tableau Kanban Hermes :

```text
meta-campaigns
```

Chaque carte active peut décrire une campagne avec les champs suivants dans le corps de la carte :

```text
Statut métier: Actif
Plateforme: any
Media/Post ID: 105680984487785_1315755877208970, 18101011072913138
Mots-clés: proxy, proxi
Réponse publique: C'est envoyé check tes DMs !
Message DM: Bonjour, ... lien complet ...
URL ressource: https://dupflodev.vercel.app/1jour1skill?ep=ep9
```

Synchroniser le Kanban vers SQLite :

```bash
uv run python -m app.kanban_sync \
  --kanban-db /opt/data/kanban/boards/meta-campaigns/kanban.db \
  --db data/processed_comments.sqlite3
```

Le webhook lit ensuite uniquement SQLite pour rester rapide et robuste.

`Media/Post ID` peut contenir plusieurs IDs séparés par virgule, par exemple l'ID du Reel Facebook et l'ID du Reel Instagram de la même campagne. Quand ce champ est rempli, le mot-clé ne déclenche la campagne que sur ces médias précis.

## Actions Meta Graph API utilisées

Toutes les requêtes Graph API utilisent `META_PAGE_ACCESS_TOKEN`, sauf le renouvellement de token qui part de `META_USER_ACCESS_TOKEN`.

### Webhook temps réel

```text
GET /webhook/meta
```

Valide l'abonnement Meta avec `hub.mode`, `hub.verify_token` et `hub.challenge`.

```text
POST /webhook/meta
```

Reçoit les événements Meta signés avec `X-Hub-Signature-256`, parse les commentaires Instagram/Facebook, applique les règles campagne et l'anti-doublon.

### Instagram

Actions utilisées pour le flow commentaire -> réponse publique -> DM :

```text
POST /{ig-comment-id}/replies
params: message=<réponse publique>
```

Répond publiquement à un commentaire Instagram.

```text
POST /{page-id}/messages
json: {"recipient": {"comment_id": "<ig-comment-id>"}, "message": {"text": "<message DM>"}}
```

Envoie la private reply Instagram liée au commentaire.

Important : le like automatique Instagram est volontairement désactivé. L'endpoint `POST /{comment_id}/likes` a renvoyé des erreurs `Unsupported post request` sur des commentaires Instagram ; une erreur de like ne doit jamais bloquer l'envoi du DM.

### Facebook

Actions utilisées pour le flow commentaire -> réponse publique -> DM :

```text
POST /{fb-comment-id}/likes
```

Like le commentaire Facebook en best effort. Si Meta refuse le like, le flow continue.

```text
POST /{fb-comment-id}/comments
params: message=<réponse publique>
```

Répond publiquement au commentaire Facebook.

```text
POST /{page-id}/messages
json: {"recipient": {"comment_id": "<fb-comment-id-complet>"}, "message": {"text": "<message DM>"}}
```

Envoie la réponse privée Facebook liée au commentaire. En test réel, cette variante a fonctionné sur un commentaire de Reel Facebook alors que `POST /{fb-comment-id}/private_replies` renvoyait `Unsupported post request` malgré `can_reply_privately=true`.

Note sur les IDs Facebook : les webhooks et certains endpoints retournent parfois un ID composé `post_id_comment_id`. Pour les actions publiques (`likes`, `comments`), le code utilise la partie courte après `_`. Pour le DM via `/{page-id}/messages`, le code utilise l'ID complet reçu par webhook/backfill. L'anti-doublon SQLite conserve aussi cet ID complet.

### Lecture/rattrapage de commentaires

Le script de rattrapage lit les médias/posts récents puis leurs commentaires :

```text
GET /{ig-user-id}/media?fields=id,caption,timestamp,permalink
GET /{ig-media-id}/comments?fields=id,text,username,timestamp,replies{id,text,username,timestamp}
```

```text
GET /{page-id}/posts?fields=id,message,created_time,permalink_url
GET /{fb-post-id}/comments?fields=id,message,from,created_time
```

La pagination suit `paging.next` avec de petites pages (`limit` interne plafonné à 25), car Meta peut ne pas renvoyer `paging.next` correctement lorsqu'on demande des pages trop grosses sur certains Reels.

Le backfill ignore aussi les commentaires Instagram qui ont déjà une réponse publique du compte `dupflodev`, afin d'éviter de retraiter des commentaires déjà gérés manuellement ou via Cowork.

## Rattrapage des commentaires non traités

Le script de rattrapage fonctionne en dry-run par défaut : il liste les candidats sans envoyer de message.

Instagram :

```bash
uv run python -m app.backfill_comments \
  --platform instagram \
  --media-limit 10 \
  --comments-limit 500
```

Facebook :

```bash
uv run python -m app.backfill_comments \
  --platform facebook \
  --media-limit 10 \
  --comments-limit 500
```

Sur les Reels/posts avec beaucoup de commentaires, garder une limite élevée est important : le script suit `paging.next`, mais `--comments-limit` reste le nombre maximal de commentaires inspectés par post.

Traiter uniquement un commentaire précis :

```bash
uv run python -m app.backfill_comments \
  --platform instagram \
  --comment-id 18118927912879503 \
  --apply
```

`--apply` envoie réellement les réponses publiques et DMs. Sans `--apply`, aucun message n'est envoyé.

## File de revue manuelle des commentaires

Les commentaires intéressants mais non automatisables peuvent être stockés dans SQLite puis traités un par un depuis Telegram/Hermes.

Scanner les commentaires Facebook + Instagram sans poster de réponse :

```bash
uv run python -m app.scan_comment_reviews --platform all --media-limit 200 --comments-limit 1000
```

Afficher le prochain commentaire à traiter :

```bash
uv run python -m app.review_comments next
```

Obtenir le lien/contexte du commentaire courant :

```bash
uv run python -m app.review_comments link --platform facebook --comment-id 'POST_COMMENT'
```

Poster une réponse publique validée par Florian :

```bash
uv run python -m app.review_comments reply --platform facebook --comment-id 'POST_COMMENT' --text 'Merci, voici le lien ...'
```

Répondre à un message privé entrant validé par Florian :

```bash
uv run python -m app.review_comments reply --platform facebook_dm --comment-id 'MESSAGE_ID' --text 'Message à envoyer en DM'
```

Pour les DMs, `username` contient le PSID Meta du sender. Le code envoie via `POST /me/messages` avec le token de Page, puis marque l'item `replied`.

Ignorer un commentaire :

```bash
uv run python -m app.review_comments skip --platform facebook --comment-id 'POST_COMMENT'
```

Statuts SQLite possibles dans `comment_review_items` :

- `pending` : à traiter ;
- `in_review` : actuellement montré à Florian ;
- `replied` : réponse postée via Hermes ;
- `manually_replied` : réponse faite directement sur Facebook/Instagram puis détectée par webhook ou scan ;
- `skipped` : ignoré ;
- `error` : tentative de réponse échouée.

Sécurité : le scan ne poste jamais de réponse. Une réponse n'est postée que via `review_comments reply`, après validation explicite du texte par Florian.

Mots-clés d'intérêt seulement : `INTEREST_ONLY_KEYWORDS` permet de compter/ignorer des signaux comme `migration` sans créer de campagne de livraison ni DM automatique. Ces mots-clés servent à mesurer la demande pour une future vidéo.

Le webhook peut aussi alimenter cette file en temps réel :

- commentaires racine intéressants mais non liés à une campagne active ;
- réponses utilisateur dans un fil déjà connu ;
- messages privés entrants fournis par `entry.messaging`.

Les réponses faites directement sur Facebook/Instagram par le compte owner sont réconciliées en `manually_replied`, ce qui évite que Hermes repropose un commentaire déjà traité ailleurs. Pour les réponses dans un fil, le scan historique regarde aussi si une réponse owner arrive après la réponse utilisateur ; si oui, l'item est ignoré ou marqué `manually_replied`. Le webhook applique la même règle quand une réponse owner arrive manuellement dans un fil déjà suivi.

## Architecture multi-plateformes future

Le repo est encore centré Meta, mais certaines briques sont déjà isolées pour préparer TikTok/YouTube :

- `comment_review_store.py` : file de revue humaine indépendante de Meta ;
- `comment_review_classifier.py` : scoring métier des commentaires intéressants ;
- `comment_review_scanner.py` : logique générique `comment -> queue` ;
- `platform_utils.py` : helpers de plateforme à garder hors du cœur métier ;
- `sqlite_store.py` : base commune des stores SQLite.

Recommandation actuelle : garder ce repo comme preuve de concept Meta jusqu'à stabilisation du backoffice, puis extraire une couche commune si TikTok/YouTube reprennent vraiment le même modèle :

```text
social_automation_core/  # queue, campagnes, classification, stores, interfaces
platforms/meta/          # Graph API, webhook parser, actions Meta
platforms/tiktok/        # TikTok API/parser/actions
platforms/youtube/       # YouTube Data API/parser/actions
```

Éviter pour l'instant trois repos séparés : ça dupliquerait la file de revue, les campagnes, les statuts, les tests et la logique Hermes. Séparer en plusieurs repos seulement si les déploiements, clients ou permissions deviennent vraiment indépendants.

## Tests

```bash
uv run pytest -q
```

## Renouvellement du token Meta

Le fichier `.env` contient deux tokens utilisés par l'application :

- `META_USER_ACCESS_TOKEN` : token utilisateur long-lived utilisé pour demander un nouveau token et récupérer les Pages ;
- `META_PAGE_ACCESS_TOKEN` : token de Page réellement utilisé par le webhook pour liker/répondre/envoyer la private reply.

Un renouvellement automatisé est disponible avec :

```bash
uv run python -m app.token_renewer --env-file .env --deploy-trigger .deploy-trigger
```

Ce script :

1. échange `META_USER_ACCESS_TOKEN` auprès de Meta ;
2. récupère le nouveau token de la Page configurée par `META_PAGE_ID` via `/me/accounts` ;
3. met à jour `META_USER_ACCESS_TOKEN` et `META_PAGE_ACCESS_TOKEN` dans `.env` sans les afficher ;
4. crée une sauvegarde locale `.env.bak` ;
5. touche `.deploy-trigger` pour relancer le déploiement/restart.

Un wrapper prêt pour la planification est disponible :

```bash
scripts/renew_meta_tokens.sh
```

Important : Meta ne garantit pas qu'un token long-lived puisse être renouvelé indéfiniment sans réauthentification utilisateur. Si Meta refuse l'échange du token, il faut regénérer un token utilisateur manuellement via le flow Meta/Login, puis relancer le script.