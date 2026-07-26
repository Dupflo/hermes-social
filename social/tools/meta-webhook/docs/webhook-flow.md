# Flow webhook Meta

## Vérification webhook

Meta appelle l'endpoint en `GET` avec :

```text
hub.mode=subscribe
hub.verify_token=...
hub.challenge=...
```

Le serveur doit :

1. comparer `hub.verify_token` avec `META_VERIFY_TOKEN` ;
2. répondre avec la valeur brute de `hub.challenge` si le token correspond ;
3. répondre `403` sinon.

## Réception des événements

Meta envoie ensuite les événements en `POST`.

Flow attendu :

1. recevoir un événement commentaire ;
2. extraire le `COMMENT_ID` et le texte du commentaire ;
3. normaliser le texte en lowercase ;
4. vérifier si le mot-clé configuré est présent ;
5. vérifier que le commentaire n'a pas déjà été traité ;
6. liker le commentaire ;
7. répondre publiquement ;
8. envoyer la ressource en private reply / DM ;
9. enregistrer le traitement pour anti-doublon.

## Anti-doublon

Meta peut renvoyer plusieurs fois le même événement webhook.

Stocker au minimum :

```text
comment_id
platform
keyword
processed_at
like_sent
public_reply_sent
dm_sent
```

Ne jamais retraiter un `comment_id` déjà marqué comme traité.

## Sécurité

À prévoir :

- validation du verify token ;
- validation de la signature Meta `X-Hub-Signature-256` ;
- tokens en variables d'environnement ;
- logs sans secrets ;
- rate limiting ;
- retry contrôlé des appels Graph API.
