# Routes Graph API de test

Ces routes servent à valider manuellement le flow dans Graph API Explorer.

## 1. Vérifier le token utilisateur

```http
GET /me?fields=id,name
```

## 2. Récupérer les Pages

```http
GET /me/accounts
```

À conserver :

- `PAGE_ID` ;
- `PAGE_ACCESS_TOKEN`.

## 3. Récupérer le compte Instagram Business lié

```http
GET /{PAGE_ID}?fields=instagram_business_account
```

À conserver :

- `IG_USER_ID`.

## 4. Lister les médias Instagram

```http
GET /{IG_USER_ID}/media?fields=id,caption,comments_count,permalink,timestamp
```

À conserver :

- `MEDIA_ID`.

## 5. Lire les commentaires Instagram

```http
GET /{MEDIA_ID}/comments?fields=id,text,username,timestamp,like_count
```

À conserver :

- `COMMENT_ID`.

## 6. Répondre publiquement à un commentaire Instagram

```http
POST /{COMMENT_ID}/replies
```

Paramètre :

```text
message=C'est envoyé, check tes DM
```

## 7. Liker un commentaire Instagram

```http
POST /{COMMENT_ID}/likes
```

## 8. Envoyer une private reply Instagram

```http
POST /{PAGE_ID}/messages
```

Body :

```json
{
  "recipient": {
    "comment_id": "COMMENT_ID"
  },
  "message": {
    "text": "Voici la ressource demandée : https://example.com/resource"
  }
}
```

Utiliser le Page Access Token.

## Facebook Page

Lire les posts/commentaires :

```http
GET /{PAGE_ID}/posts?fields=id,message,comments.limit(10){id,message,from,created_time}
```

Répondre à un commentaire Facebook :

```http
POST /{COMMENT_ID}/comments
```

Paramètre :

```text
message=C'est envoyé, check tes DM
```

Liker un commentaire Facebook :

```http
POST /{COMMENT_ID}/likes
```

Private reply Facebook :

```http
POST /{COMMENT_ID}/private_replies
```

Paramètre :

```text
message=Voici la ressource demandée : https://example.com/resource
```
