# Guide de configuration Meta — Messenger & Instagram

> **Version :** 1.0  
> **Cible :** Intégration Messenger + Instagram Graph API  
> **Prérequis :** Compte Facebook personnel, Page Facebook, compte Instagram Business

---

## Table des matières

1. [Créer une application Facebook Developer](#1-créer-une-application-facebook-developer)
2. [Ajouter Messenger + Instagram Graph API](#2-ajouter-messenger--instagram-graph-api)
3. [Configurer le webhook (URL + token)](#3-configurer-le-webhook-url--token)
4. [Générer un Page Access Token](#4-générer-un-page-access-token)
5. [Connecter un compte Instagram Business](#5-connecter-un-compte-instagram-business)
6. [Remplir le fichier `.env`](#6-remplir-le-fichier-env)
7. [Tester avec `curl`](#7-tester-avec-curl)
8. [Dépannage](#8-dépannage)

---

## 1. Créer une application Facebook Developer

1. Rendez-vous sur le [portail développeur Facebook](https://developers.facebook.com).
2. Connectez-vous avec votre compte Facebook personnel.
3. Cliquez sur **Mes applications** → **Créer une application**.
4. Choisissez le type d'application **Entreprise** (recommandé pour les chatbots, pages et Instagram).
5. Remplissez les informations :
   - **Nom de l'application** : `Hermes Social` (ou un nom au choix)
   - **Email de contact** : votre email
6. Cliquez sur **Créer une application**.
7. Complétez éventuellement le Captcha de sécurité.

> ✅ Une fois créée, notez **l'identifiant de l'application** (`App ID`) — vous en aurez besoin pour le fichier `.env`.

---

## 2. Ajouter Messenger + Instagram Graph API

### Ajouter Messenger

1. Dans le tableau de bord de votre application, cliquez sur **Ajouter un produit**.
2. Trouvez **Messenger** et cliquez sur **Configurer**.
3. Messenger apparaît désormais dans la barre latérale gauche.

### Ajouter Instagram Graph API

1. Toujours dans **Ajouter un produit**, cherchez **Instagram Graph API**.
2. Cliquez sur **Configurer**.
3. Le produit Instagram s'ajoute dans la barre latérale.

> ⚠️ Ces deux produits sont nécessaires pour lire, envoyer et gérer les messages Messenger ainsi que les commentaires Instagram.

---

## 3. Configurer le webhook (URL + token)

### Obtenir l'URL de votre webhook

Avant toute configuration, votre service doit être déployé et accessible publiquement (via un domaine HTTPS). Exemple :

```
https://votre-domaine.com/api/meta/webhook
```

> Pendant le développement, vous pouvez utiliser un tunnel comme **ngrok** : `ngrok http 8000` — l'URL générée (ex. `https://xxxx.ngrok.io`) servira d'URL de webhook.

### Configurer dans Facebook

1. Dans le tableau de bord, allez dans **Messenger** → **Paramètres**.
2. Section **Webhooks** → cliquez sur **Configurer les webhooks**.
3. Renseignez :
   - **URL de rappel** : `https://votre-domaine.com/api/meta/webhook`
   - **Token de vérification** : une chaîne secrète de votre choix (ex. `hermes_verify_token_2025`)
4. Dans la liste des champs d'abonnement, cochez au minimum :
   - `messages`
   - `messaging_handovers`
   - `messaging_postbacks`
5. Cliquez sur **Vérifier et enregistrer**.

> Facebook enverra une requête GET à votre URL avec les paramètres `hub.mode`, `hub.verify_token` et `hub.challenge`. Votre serveur doit répondre avec le contenu de `hub.challenge` si le `hub.verify_token` correspond.

> ✅ Conservez précieusement votre **Verify Token** — il sera ajouté au fichier `.env`.

### Abonner votre Page au webhook

1. Dans la même section **Webhooks**, sélectionnez votre Page Facebook dans le menu déroulant.
2. Cliquez sur **S'abonner**.
3. La page est maintenant connectée au webhook.

---

## 4. Générer un Page Access Token

1. Dans **Messenger** → **Paramètres** → **Génération de jeton**.
2. Sélectionnez votre **Page Facebook**.
3. Cliquez sur **Générer le jeton**.
4. Un **Page Access Token** (token d'accès à la page) est créé.

> ⚠️ **Sécurité** :
> - Ce token expire généralement au bout de **60 à 90 jours**.
> - Ne le partagez jamais publiquement.
> - Stockez-le immédiatement dans votre fichier `.env`.

> Si vous avez besoin d'un token longue durée (60 jours), échangez votre token court via l'endpoint :
> ```
> GET /oauth/access_token?grant_type=fb_exchange_token&client_id={APP_ID}&client_secret={APP_SECRET}&fb_exchange_token={SHORT_TOKEN}
> ```

### Récupérer le Page ID

1. Allez sur votre **Page Facebook**.
2. L'identifiant figure dans l'URL : `facebook.com/people/**_VOTRE_PAGE_ID_**/`
3. Vous pouvez aussi l'obtenir avec `curl` :
   ```bash
   curl -X GET "https://graph.facebook.com/v22.0/me?access_token=VOTRE_TOKEN"
   ```
   La réponse inclut le champ `id`.

---

## 5. Connecter un compte Instagram Business

> ⚠️ **Prérequis impératif :** Votre compte Instagram doit être un **compte professionnel (Business)** — un compte personnel ou créateur ne fonctionne pas.

### Convertir votre compte Instagram en compte Business

1. Ouvrez l'application Instagram → **Paramètres** → **Type de compte**.
2. Sélectionnez **Passer au compte professionnel**.
3. Connectez-le à votre **Page Facebook** (nécessaire pour l'API).

### Associer le compte Instagram à l'application Facebook

1. Dans le tableau de bord développeur, allez dans **Instagram Graph API** → **Configuration**.
2. Cliquez sur **Se connecter à Instagram**.
3. Authentifiez-vous avec votre compte Instagram Business.
4. Sélectionnez la **Page Facebook** liée à votre compte Instagram.

### Récupérer l'Instagram User ID

```bash
curl -X GET "https://graph.facebook.com/v22.0/{PAGE_ID}?fields=instagram_business_account&access_token={PAGE_ACCESS_TOKEN}"
```

La réponse contient :

```json
{
  "instagram_business_account": {
    "id": "17841XXXXXXXXXX"
  }
}
```

Notez cet `id` — c'est votre **Instagram User ID**.

---

## 6. Remplir le fichier `.env`

Voici les variables à configurer dans votre fichier `.env` (ou directement dans l'environnement).

```bash
# === META (Facebook / Instagram / Messenger) ===

# Identifiant de votre application Facebook Developer
# Obtenu à l'étape 1 (App ID)
META_APP_ID="123456789012345"

# Clé secrète de votre application Facebook Developer
# Obtenue dans Tableau de bord → Paramètres → Authentification de base
META_APP_SECRET="a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6"

# Token de vérification du webhook (chaîne libre définie à l'étape 3)
# Doit correspondre exactement à celui configuré dans Facebook
META_VERIFY_TOKEN="hermes_verify_token_2025"

# Token d'accès longue durée à la Page Facebook (étape 4)
# Permet d'appeler l'API Graph au nom de la page
META_PAGE_ACCESS_TOKEN="EAAx...abCd"

# Identifiant de la Page Facebook (étape 4)
META_PAGE_ID="109876543210987"

# Identifiant du compte Instagram Business (étape 5)
# Format : nombre à 15 chiffres environ, commençant par 178
META_IG_USER_ID="17841234567890123"

# Mot(s)-clé pour identifier une ressource dans un message (ex. lien, image)
# Utilisé par le bot pour détecter quand un utilisateur partage un média
RESOURCE_KEYWORD="[photo],[video],[link]"

# URL de base de la ressource (template pour construire le lien final)
# Exemple : "https://votre-site.com/uploads/"
RESOURCE_URL="https://votre-domaine.com/uploads/"

# Mots-clés d'intérêt uniquement (séparés par des virgules)
# Le bot ne répondra QUE si le message contient un de ces mots
# Laissez vide pour répondre à tous les messages
INTEREST_ONLY_KEYWORDS="commande,prix,livraison,info"
```

### Explication détaillée de chaque variable

| Variable | Rôle | Source |
|---|---|---|
| `META_APP_ID` | Identifie votre application auprès de l'API Graph Facebook. Obligatoire pour toute requête OAuth. | Tableau de bord développeur → Paramètres → Identifiants |
| `META_APP_SECRET` | Clé secrète de l'application. Utilisée avec l'App ID pour générer des tokens et pour la validation des webhooks (signature SHA256 du payload). | Tableau de bord développeur → Paramètres → Authentification de base |
| `META_VERIFY_TOKEN` | Chaîne secrète libre que vous choisissez. Facebook l'envoie lors de la configuration du webhook ; votre serveur doit la vérifier pour confirmer l'abonnement. | Définie par vous (doit correspondre à celle saisie dans Facebook) |
| `META_PAGE_ACCESS_TOKEN` | Token d'accès permettant d'agir au nom de votre Page Facebook (lire les messages, envoyer des réponses, gérer les conversations). | Généré dans Messenger → Paramètres → Génération de jeton |
| `META_PAGE_ID` | Identifiant numérique de votre Page Facebook. Utilisé dans les endpoints Graph API (conversations, abonnés, etc.). | URL de la page ou réponse de l'API `/me` |
| `META_IG_USER_ID` | Identifiant du compte Instagram Business connecté à votre Page. Nécessaire pour lire/répondre aux commentaires Instagram via l'API. | Endpoint `/{PAGE_ID}?fields=instagram_business_account` |
| `RESOURCE_KEYWORD` | Mot-clé que le bot recherche dans les messages entrants pour détecter un partage de média (image, vidéo, lien). | Définie par vous (selon la logique métier) |
| `RESOURCE_URL` | URL de base servant à construire le lien complet vers une ressource hébergée sur votre serveur. | Définie par vous (URL de votre serveur de fichiers) |
| `INTEREST_ONLY_KEYWORDS` | Liste de mots-clés séparés par des virgules. Si renseignée, le bot ignore tout message ne contenant pas au moins un de ces mots. Permet de filtrer le bruit et de ne traiter que les demandes pertinentes. | Définie par vous (selon votre domaine d'activité) |

---

## 7. Tester avec `curl`

### 7.1 Tester le webhook (vérification)

```bash
curl -X GET "https://votre-domaine.com/api/meta/webhook?hub.mode=subscribe&hub.verify_token=hermes_verify_token_2025&hub.challenge=123456789"
```

**Réponse attendue (status 200) :**
```
123456789
```

### 7.2 Récupérer les informations de la Page

```bash
curl -X GET "https://graph.facebook.com/v22.0/me?access_token=VOTRE_TOKEN"
```

**Réponse :**
```json
{
  "name": "Nom de votre Page",
  "id": "109876543210987"
}
```

### 7.3 Récupérer la liste des conversations Messenger

```bash
curl -X GET "https://graph.facebook.com/v22.0/{PAGE_ID}/conversations?access_token={PAGE_ACCESS_TOKEN}"
```

**Réponse :**
```json
{
  "data": [
    { "id": "t_1234567890", "updated_time": "2025-07-27T10:00:00+0000" }
  ]
}
```

### 7.4 Envoyer un message Messenger

```bash
curl -X POST "https://graph.facebook.com/v22.0/{PAGE_ID}/messages?access_token={PAGE_ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "recipient": {"id": "UTILISATEUR_PSID"},
    "message": {"text": "Bonjour ! Comment puis-je vous aider ?"}
  }'
```

**Réponse attendue :**
```json
{
  "recipient_id": "UTILISATEUR_PSID",
  "message_id": "m_abc123"
}
```

> 💡 L'`UTILISATEUR_PSID` est le `sender.id` reçu dans le payload du webhook lors d'un message entrant.

### 7.5 Récupérer les commentaires Instagram

```bash
curl -X GET "https://graph.facebook.com/v22.0/{IG_USER_ID}/media?fields=comments&access_token={PAGE_ACCESS_TOKEN}"
```

### 7.6 Répondre à un commentaire Instagram

```bash
curl -X POST "https://graph.facebook.com/v22.0/{COMMENT_ID}/replies?access_token={PAGE_ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"message": "Merci pour votre message !"}'
```

---

## 8. Dépannage

### 8.1 Erreur 403 — Permission denied

| Cause possible | Solution |
|---|---|
| Le token n'a pas les permissions requises | Vérifiez que votre application a bien les produits **Messenger** et **Instagram Graph API** ajoutés. |
| La permission `pages_messaging` ou `instagram_basic` manque | Allez dans **Outils de l'application** → **Permissions et fonctionnalités** et demandez les permissions manquantes. |
| L'application est en mode *Développement* (non publiée) | Seuls les admins/développeurs/testeurs peuvent intéragir. Passez en mode *Live* (publication) pour les utilisateurs finaux. |
| Le token est invalide ou a expiré | Régénérez un nouveau Page Access Token. |

### 8.2 Token expiré

**Symptômes :** Requêtes API renvoyant `Error: #190 - Access token has expired`.

**Solutions :**
- Régénérez manuellement un nouveau token via le tableau de bord.
- Mettez en place un **renouvellement automatique** :
  - Facebook fournit les tokens longue durée (60 jours) via l'endpoint d'échange.
  - Stockez la date d'expiration et programmez un renouvellement périodique (ex. tous les 45 jours via un cron).
- Envisagez un mécanisme de **refresh token** si votre scénario le permet.

**Commande de renouvellement :**
```bash
curl -X GET "https://graph.facebook.com/v22.0/oauth/access_token?grant_type=fb_exchange_token&client_id={APP_ID}&client_secret={APP_SECRET}&fb_exchange_token={EXPIRED_TOKEN}"
```

### 8.3 Permissions insuffisantes (Instagram)

| Erreur | Cause | Solution |
|---|---|---|
| `(#200) Access to this data is forbidden` | Le token n'a pas la permission `instagram_basic` ou le compte Instagram n'est pas Business. | Vérifiez le type de compte et ajoutez la permission dans l'application. |
| `(#100) The parameter ig_user_id is required` | L'Instagram User ID est manquant ou incorrect. | Ré-exécutez la requête de l'étape 5 pour récupérer le bon ID. |

### 8.4 Le webhook ne reçoit pas d'événements

1. **Vérifiez l'URL** — Facebook doit pouvoir joindre votre serveur (HTTPS obligatoire, pas de localhost).
2. **Vérifiez les abonnements** — Dans Messenger → Webhooks, confirmez que votre Page est bien abonnée.
3. **Vérifiez les logs** — Consultez les logs de votre serveur pour voir si Facebook envoie bien les requêtes POST.
4. **Testez avec le tableau de bord** — Dans **Messenger** → **Outils** → **Tests de webhook**, vous pouvez envoyer des événements factices.

### 8.5 Erreur `(#10) Application does not have permission for this action`

**Cause :** L'application Facebook tente d'accéder à une ressource qui n'est pas dans le champ d'application des tokens accordés.

**Solution :**
- Allez dans **Outils de l'application** → **Permissions et fonctionnalités**.
- Vérifiez que les permissions suivantes sont accordées :
  - `pages_messaging`
  - `pages_manage_metadata`
  - `pages_show_list`
  - `instagram_basic`
  - `instagram_manage_comments`
- Pour Instagram : `instagram_basic` et `instagram_manage_comments` sont obligatoires.
- Si une permission est en statut *Avancé*, suivez le processus de révision (App Review) de Facebook.

---

## Ressources utiles

- [Documentation Messenger Platform](https://developers.facebook.com/docs/messenger-platform)
- [Documentation Instagram Graph API](https://developers.facebook.com/docs/instagram-api)
- [Outil Graph API Explorer](https://developers.facebook.com/tools/explorer/)
- [Webhooks / Real-time updates](https://developers.facebook.com/docs/graph-api/webhooks)

---

*Document généré le 27 juillet 2025 — Adapté au projet Hermes Social.*
