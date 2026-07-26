# Permissions Meta

## Besoin métier

L'application doit :

1. lire les commentaires des publications Instagram et Facebook ;
2. détecter un mot-clé explicite, par exemple `Proxy` ;
3. liker le commentaire ;
4. répondre publiquement : `C'est envoyé, check tes DM` ;
5. envoyer une ressource par message privé.

## Permissions à demander

```text
pages_show_list
pages_read_engagement
pages_read_user_content
pages_manage_engagement
pages_messaging
pages_manage_metadata
instagram_basic
instagram_manage_comments
```

## Justification par permission

| Permission | Utilisation |
|---|---|
| `pages_show_list` | Lister les Pages Facebook accessibles par l'utilisateur et sélectionner la Page liée au compte Instagram. |
| `pages_read_engagement` | Lire les publications, commentaires et signaux d'engagement nécessaires au flow. |
| `pages_read_user_content` | Lire le contenu généré par les utilisateurs, notamment les commentaires Facebook. |
| `pages_manage_engagement` | Liker et répondre aux commentaires Facebook. |
| `pages_messaging` | Envoyer une private reply / DM à une personne qui a commenté. |
| `pages_manage_metadata` | Configurer et recevoir les webhooks liés aux Pages/commentaires/messages. |
| `instagram_basic` | Accéder au compte Instagram professionnel lié à la Page Facebook. |
| `instagram_manage_comments` | Lire, liker et répondre aux commentaires Instagram, et déclencher les private replies Instagram. |

## Permissions à ne pas demander pour le MVP

```text
ads_read
ads_management
business_management
instagram_content_publish
instagram_manage_insights
pages_manage_posts
```

Ces permissions ne sont nécessaires que si l'application gère aussi la publicité, la publication de contenu ou les statistiques avancées.

## Texte de justification Meta Review

> Notre application permet aux créateurs de contenu de répondre automatiquement aux utilisateurs qui commentent leurs publications Facebook ou Instagram avec un mot-clé explicite, par exemple “Proxy”. Quand un utilisateur commente avec ce mot-clé, l'application lit le commentaire, ajoute une réaction/like, répond publiquement au commentaire pour informer l'utilisateur que la ressource a été envoyée, puis envoie un message privé contenant le lien demandé. Les messages privés ne sont envoyés qu'aux utilisateurs ayant explicitement demandé la ressource via un commentaire contenant le mot-clé configuré.
