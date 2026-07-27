# Configuration YouTube OAuth — Guide d'installation

Ce guide vous accompagne pas à pas dans la création d'un projet Google Cloud,
l'activation de l'API YouTube Data v3, et l'obtention d'un *refresh token* OAuth
pour interagir avec YouTube de façon automatisée.

---

## 1. Créer un projet Google Cloud

1. Rendez-vous sur la [console Google Cloud](https://console.cloud.google.com).
2. Cliquez sur le sélecteur de projet en haut à gauche, puis sur **Nouveau projet**.
3. Donnez un nom explicite à votre projet (ex. `hermes-social-youtube`).
4. Cliquez sur **Créer**.
5. Une fois le projet créé, assurez-vous qu'il est sélectionné dans le sélecteur.

> 💡 **Conseil** : notez l'ID du projet — vous en aurez besoin plus tard.

---

## 2. Activer YouTube Data API v3

1. Dans le menu de navigation (hamburger ☰), allez dans **APIs & Services** → **Bibliothèque**.
2. Recherchez **YouTube Data API v3**.
3. Cliquez sur le résultat, puis sur le bouton **Activer**.

Après activation, vous serez redirigé vers le tableau de bord des APIs.

---

## 3. Configurer l'écran de consentement OAuth (externe)

L'écran de consentement OAuth est obligatoire pour toute application utilisant OAuth 2.0 avec des données utilisateur.

1. Dans le menu de gauche : **APIs & Services** → **Écran de consentement OAuth**.
2. Sélectionnez **Externe** comme type d'utilisateur (même pour un usage personnel, cette option est nécessaire pour générer un *refresh token*).
3. Cliquez sur **Créer**.

Remplissez les champs obligatoires :

| Champ | Valeur |
|---|---|
| **Nom de l'application** | `Hermes Social — YouTube` |
| **E-mail d'assistance utilisateur** | votre@email.com |
| **Coordonnées du développeur** | votre@email.com |
| **Domaine autorisé** | (laisser vide si non applicable) |

4. Cliquez sur **Enregistrer et continuer**.

### Scopes (portées) — Étape suivante

Passez cette étape pour l'instant, nous ajouterons le scope manuellement dans la section suivante.

### Utilisateurs test

Ajoutez votre adresse Gmail comme utilisateur test. Sans cela, votre propre compte ne pourra pas utiliser l'application en mode externe.

Cliquez sur **Enregistrer et continuer**, puis sur **Retour au tableau de bord**.

---

## 4. Ajouter le scope `youtube.force-ssl`

Même si l'écran de consentement est créé, il faut explicitement déclarer le scope OAuth.

1. Depuis l'**Écran de consentement OAuth**, cliquez sur **Modifier**.
2. Allez à l'étape **Scopes** (ou cliquez sur l'onglet correspondant).
3. Cliquez sur **Ajouter des scopes**.
4. Dans le panneau qui s'ouvre, filtrez par `youtube` et cochez :

   ```
   https://www.googleapis.com/auth/youtube.force-ssl
   ```

   Ce scope permet toutes les opérations en lecture et écriture sur YouTube
   (uploads, modifications de playlists, abonnements, etc.).

5. Cliquez sur **Ajouter**, puis **Enregistrer et continuer** jusqu'à valider.

> ⚠️ **Important** : le scope `youtube.force-ssl` ne signifie pas uniquement
> « forcer SSL » — c'est le scope principal pour l'API YouTube Data v3 qui
> donne accès en lecture/écriture à l'ensemble des ressources YouTube.

---

## 5. Créer un OAuth Client ID (type Application de bureau)

1. Dans le menu de gauche : **APIs & Services** → **Identifiants**.
2. Cliquez sur **Créer des identifiants** → **ID client OAuth**.
3. Dans le champ **Type d'application**, sélectionnez **Application de bureau**.
4. Donnez un nom, par exemple : `Hermes Social Desktop`.
5. Cliquez sur **Créer**.

Une fenêtre modale s'affiche avec votre **Client ID** et **Client Secret**.

---

## 6. Télécharger `client_secret.json`

1. Dans la même fenêtre modale, cliquez sur **Télécharger JSON**.
2. Un fichier nommé `client_secret_<ID>.json` sera téléchargé.
3. Placez ce fichier dans le répertoire de votre projet :

   ```
   /opt/repos/hermes-social/client_secret.json
   ```

   > 🔒 **Ne commitez jamais ce fichier !** Ajoutez-le à votre `.gitignore`.

### Structure du fichier `client_secret.json`

```json
{
  "installed": {
    "client_id": "XXXXX.apps.googleusercontent.com",
    "project_id": "votre-projet-id",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_secret": "GOCSPX-XXXXX",
    "redirect_uris": ["http://localhost"]
  }
}
```

---

## 7. Obtenir le refresh token

Le *refresh token* est nécessaire pour que votre application puisse renouveler
automatiquement l'accès à YouTube sans intervention manuelle. Voici comment
l'obtenir en une fois.

### A. Générer l'URL d'autorisation

Utilisez le script suivant ou construisez l'URL manuellement :

```python
# scripts/generate_auth_url.py
import json
from urllib.parse import urlencode

with open("client_secret.json") as f:
    config = json.load(f)["installed"]

params = {
    "client_id": config["client_id"],
    "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
    "response_type": "code",
    "scope": "https://www.googleapis.com/auth/youtube.force-ssl",
    "access_type": "offline",
    "prompt": "consent",
}

url = "https://accounts.google.com/o/oauth2/auth?" + urlencode(params)
print("Ouvrez cette URL dans votre navigateur :")
print(url)
```

Exécutez-le :

```bash
cd /opt/repos/hermes-social
python scripts/generate_auth_url.py
```

### B. Ouvrir l'URL et cliquer

1. Copiez-collez l'URL générée dans un navigateur (connecté avec le compte
   YouTube cible).
2. Connectez-vous si ce n'est pas déjà fait.
3. Sélectionnez le compte Gmail associé à votre chaîne YouTube.
4. Cliquez sur **Continuer**.
5. Cliquez sur **Continuer** pour autoriser les permissions.
6. **Autorisez** l'accès.

### C. Copier le code d'autorisation

Google affiche un code à coller (ex. `4/0AeaYS...`). Copiez-le.

### D. Échanger le code contre un refresh token

```python
# scripts/exchange_code.py
import json
import requests

with open("client_secret.json") as f:
    config = json.load(f)["installed"]

code = input("Collez le code d'autorisation : ")

resp = requests.post(
    config["token_uri"],
    data={
        "code": code,
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        "grant_type": "authorization_code",
    },
)

token = resp.json()
print("\n✅ Refresh token obtenu :")
print(token["refresh_token"])
print("\nAjoutez-le à votre fichier .env (voir section suivante).")
```

Exécutez :

```bash
python scripts/exchange_code.py
```

> 🔄 Le *refresh token* ne expire jamais tant que l'application reste active
> dans votre compte Google. Si vous révoquez l'accès ou changez de mot de passe,
> il faudra répéter cette procédure.

---

## 8. Variables d'environnement (`.env`)

Créez ou éditez votre fichier `.env` à la racine du projet :

```bash
# /opt/repos/hermes-social/.env
YOUTUBE_API_KEY=votre_api_key
YOUTUBE_REFRESH_TOKEN=votre_refresh_token
YOUTUBE_CLIENT_ID=votre_client_id.apps.googleusercontent.com
YOUTUBE_CLIENT_SECRET=GOCSPX-votre_client_secret
```

| Variable | Source |
|---|---|
| `YOUTUBE_API_KEY` | **APIs & Services** → **Identifiants** → **Créer des identifiants** → **Clé API** |
| `YOUTUBE_REFRESH_TOKEN` | Obtenu à l'étape 7 |
| `YOUTUBE_CLIENT_ID` | Fichier `client_secret.json` ou identifiants OAuth |
| `YOUTUBE_CLIENT_SECRET` | Fichier `client_secret.json` ou identifiants OAuth |

> 🔒 **Ne commitez pas le `.env` !** Ajoutez-le à votre `.gitignore`.

### Obtenir une clé API (YOUTUBE_API_KEY)

La clé API est utilisée pour les requêtes en lecture seule (recherche, statistiques
publiques). Pour les opérations en écriture, le flux OAuth avec *refresh token*
est utilisé.

1. **APIs & Services** → **Identifiants** → **Créer des identifiants** → **Clé API**.
2. Restreignez la clé à **YouTube Data API v3** uniquement.
3. Copiez la clé générée dans la variable `YOUTUBE_API_KEY`.

---

## 9. Pas de DM possible sur YouTube — réponse publique uniquement

Il est important de comprendre une limitation structurelle de YouTube :

> **YouTube ne dispose pas de système de messages privés (DM) via son API publique.**

Contrairement à d'autres plateformes :

| Plateforme | Messages privés (DM) | Commentaires publics |
|---|---|---|
| **YouTube** | ❌ Non disponible | ✅ Oui |
| **Twitter / X** | ✅ Oui | ✅ Oui |
| **Instagram** | ✅ Oui (selon API) | ✅ Oui |

**Conséquence pour votre application :**

- Toute interaction automatisée avec les utilisateurs se fait exclusivement via
  les **commentaires** sur les vidéos.
- La réponse à un commentaire est publique (visible par tous).
- Pour un suivi privé, redirigez l'utilisateur vers une autre plateforme
  (email, formulaire, etc.).

### Exemple : répondre à un commentaire

```python
# scripts/reply_to_comment.py
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import os

def get_authenticated_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.force-ssl"],
    )
    return build("youtube", "v3", credentials=creds)

def reply_to_comment(comment_id, text):
    youtube = get_authenticated_service()
    request = youtube.comments().insert(
        part="snippet",
        body={
            "snippet": {
                "parentId": comment_id,
                "textOriginal": text,
            }
        },
    )
    return request.execute()
```

---

## 10. Structure du projet

Une fois la configuration terminée, votre projet devrait ressembler à ceci :

```
/opt/repos/hermes-social/
├── .env                      # Variables d'environnement (ne pas commiter)
├── .gitignore                # .env, client_secret.json, *.pyc, __pycache__/
├── client_secret.json        # Téléchargé depuis Google Cloud (ne pas commiter)
├── app/
│   ├── __init__.py
│   ├── oauth.py              # Logique OAuth (authentification, refresh)
│   └── ...
├── scripts/
│   ├── generate_auth_url.py  # Génère l'URL d'autorisation OAuth
│   ├── exchange_code.py      # Échange le code contre un refresh token
│   ├── youtube_cron.py       # Script cron pour interagir avec YouTube
│   └── reply_to_comment.py   # Exemple : répondre à un commentaire
└── docs/
    └── guide/
        └── setup-youtube.md  # Ce guide
```

### `app/oauth.py` — Logique d'authentification

Ce module centralise la gestion du token OAuth :

```python
# app/oauth.py
"""
Module OAuth pour YouTube.
Gère l'authentification et le rafraîchissement automatique du token.
"""

import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]


def get_credentials() -> Credentials:
    """Retourne des credentials OAuth rafraîchis automatiquement."""
    return Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )


def get_youtube_client():
    """Retourne un client YouTube API authentifié."""
    creds = get_credentials()
    return build("youtube", "v3", credentials=creds)
```

### `scripts/youtube_cron.py` — Script d'interaction cron

Ce script est destiné à être exécuté périodiquement (via cron, systemd timer,
ou un scheduler) pour effectuer des actions sur YouTube :

```python
# scripts/youtube_cron.py
"""
Script exécuté par cron pour interagir avec YouTube.
Exemple : répondre aux nouveaux commentaires.
"""

import os
import sys
from dotenv import load_dotenv

# Ajouter la racine du projet au PATH
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from app.oauth import get_youtube_client  # noqa: E402


def fetch_latest_comments(video_id: str, max_results: int = 10):
    """Récupère les derniers commentaires d'une vidéo."""
    youtube = get_youtube_client()
    request = youtube.commentThreads().list(
        part="snippet",
        videoId=video_id,
        maxResults=max_results,
        order="time",
    )
    return request.execute()


def main():
    """Point d'entrée du cron."""
    video_id = os.getenv("YOUTUBE_VIDEO_ID")
    if not video_id:
        print("❌ YOUTUBE_VIDEO_ID non défini dans .env")
        sys.exit(1)

    comments = fetch_latest_comments(video_id)
    print(f"✅ {len(comments.get('items', []))} commentaires récupérés.")

    # Traitement personnalisé ici...


if __name__ == "__main__":
    main()
```

---

## Vérification finale

Avant de commencer à développer, assurez-vous que tout est opérationnel :

```bash
# 1. Vérifier que le refresh token fonctionne
python -c "from app.oauth import get_youtube_client; client = get_youtube_client(); print('✅ Authentification OK —', client)"

# 2. Tester un appel API simple
python -c "
from app.oauth import get_youtube_client
client = get_youtube_client()
channel = client.channels().list(part='snippet', mine=True).execute()
print('✅ Chaîne :', channel['items'][0]['snippet']['title'])
"
```

---

## Dépannage

| Problème | Cause probable | Solution |
|---|---|---|
| `invalid_grant` | Refresh token expiré ou révoqué | Répéter l'étape 7 |
| `access_denied` | Scope non autorisé | Vérifier l'écran de consentement (étape 3) |
| `quotaExceeded` | Quota API dépassé | Attendre la réinitialisation ou demander plus de quota |
| `NOT_FOUND` | Ressource inexistante ou privée | Vérifier les IDs et la visibilité |
| `token expired` | Token d'accès expiré | Le refresh automatique devrait le gérer |

---

## Ressources

- [Documentation YouTube Data API v3](https://developers.google.com/youtube/v3)
- [Console Google Cloud](https://console.google.com)
- [Guide OAuth 2.0 pour applications de bureau](https://developers.google.com/identity/protocols/oauth2/native-app)
