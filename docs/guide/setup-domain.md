# Guide de configuration domaine et DNS

> **Contexte :** Ce guide explique comment acheter un nom de domaine, le faire pointer vers un
> serveur (VPS), et sécuriser la communication avec HTTPS. L'exemple utilisé est
> `meta.dupuisweb.com` — un sous-domaine pointant vers un serveur hébergeant l'API Meta
> Graph de Hermes Social.

---

## 1. Acheter un nom de domaine

### 1.1 Chez OVH (recommandé)

1. Rendez-vous sur [ovh.com/fr](https://www.ovh.com/fr/domaines/) et connectez-vous à votre
   compte client.
2. Utilisez la barre de recherche pour vérifier la disponibilité du domaine souhaité
   (ex. `dupuisweb.com`).
3. Sélectionnez la période d'enregistrement (1 an, 2 ans, etc.) et ajoutez au panier.
4. Choisissez les options :
   - **Zone DNS** : gardez l'hébergement DNS chez OVH (par défaut).
   - **DNSSEC** : activer pour la sécurité (recommandé).
5. Validez la commande et effectuez le paiement.

### 1.2 Alternatives

| Registrar     | Particularité                                      |
|---------------|----------------------------------------------------|
| Gandi         | DNS inclus, éthique, prix légèrement plus élevés   |
| Namecheap     | DNS gratuit, WhoIsGuard offert la 1ʳᵉ année        |
| Cloudflare    | Prix coûtant, DNS performant, protection anti-DDoS |
| GoDaddy       | Très accessible, nombreux TLD, ventes fréquentes   |

### 1.3 Créer un sous-domaine (ex. `meta`)

Une fois le domaine principal (`dupuisweb.com`) enregistré, créez un sous-domaine
`meta.dupuisweb.com` pour isoler l'API Hermes Social du reste de l'infrastructure.

---

## 2. Configurer les enregistrements DNS

### 2.1 Enregistrement A — pointer vers le VPS

L'enregistrement **A** associe un nom de domaine à une adresse IPv4. Pour que
`meta.dupuisweb.com` pointe vers votre VPS :

| Type | Nom              | Valeur (IPv4)     | TTL      |
|------|------------------|-------------------|----------|
| A    | `meta`           | `203.0.113.42`    | 300 s    |
| A    | `meta.dupuisweb.com.` | `203.0.113.42` | 300 s    |

> **Remarque :** Le champ « Nom » ne contient que le sous-domaine (`meta`) car l'interface
> OVH complète automatiquement avec le domaine principal. Si vous configurez en zone DNS
> directe, utilisez le nom complet avec un point final (`meta.dupuisweb.com.`).

### 2.2 Étapes sur l'interface OVH

1. Connectez-vous à l'espace client OVH.
2. Allez dans **Web Cloud** → **Noms de domaine** → cliquez sur `dupuisweb.com`.
3. Allez dans l'onglet **Zone DNS**.
4. Cliquez sur **Ajouter une entrée**.
5. Choisissez **A** (si IPv4) ou **AAAA** (si IPv6).
6. Renseignez `meta` comme sous-domaine et l'IP de votre VPS comme cible.
7. Validez.

### 2.3 Propagation DNS

Les modifications DNS peuvent prendre de quelques minutes à 48 heures pour se propager
mondialement. Pour vérifier la propagation :

```bash
dig meta.dupuisweb.com A +short
# Résultat attendu : 203.0.113.42 (l'IP de votre VPS)

nslookup meta.dupuisweb.com
```

---

## 3. Reverse proxy pour HTTPS (Caddy / Traefik)

### 3.1 Pourquoi un reverse proxy ?

Un reverse proxy se place devant votre application et :

- Termine le chiffrement TLS/SSL (HTTPS).
- Achemine les requêtes vers le bon service interne (FastAPI, Node.js, etc.).
- Protège le backend de l'exposition directe sur Internet.

### 3.2 Avec Caddy (solution recommandée — facile, SSL automatique)

Caddy est le choix le plus simple car il gère automatiquement le certificat Let's
Encrypt et le renouvellement.

**Caddyfile :**

```caddyfile
meta.dupuisweb.com {
    reverse_proxy localhost:8000
}
```

**Installation rapide (Ubuntu/Debian) :**

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy

# Démarrer le service
sudo systemctl enable --now caddy
```

**Vérification :**

```bash
sudo systemctl status caddy
```

### 3.3 Avec Traefik (alternative avancée — orientée conteneurs)

Traefik excelle dans un environnement Docker/Docker Compose grâce à la détection
automatique des services via des labels.

**Exemple Docker Compose :**

```yaml
version: "3.8"

services:
  traefik:
    image: traefik:v3.0
    container_name: traefik
    command:
      - "--providers.docker=true"
      - "--entrypoints.websecure.address=:443"
      - "--certificatesresolvers.letsencrypt.acme.tlschallenge=true"
      - "--certificatesresolvers.letsencrypt.acme.email=admin@dupuisweb.com"
      - "--certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json"
    ports:
      - "443:443"
      - "80:80"
    volumes:
      - "/var/run/docker.sock:/var/run/docker.sock"
      - "letsencrypt_data:/letsencrypt"

  api:
    image: hermes-social-api
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.api.rule=Host(`meta.dupuisweb.com`)"
      - "traefik.http.routers.api.tls=true"
      - "traefik.http.routers.api.tls.certresolver=letsencrypt"

volumes:
  letsencrypt_data:
```

---

## 4. SSL / Let's Encrypt — automatique avec Caddy

### 4.1 Fonctionnement

Caddy intègre nativement le protocole **ACME** (Automatic Certificate Management
Environment) de Let's Encrypt. Dès qu'un `Caddyfile` contient un nom de domaine
valide pointant vers votre serveur, Caddy :

1. Contacte Let's Encrypt pour demander un certificat.
2. Prouve la propriété du domaine (HTTP-01 challenge).
3. Obtient le certificat (validité 90 jours).
4. Le renouvelle automatiquement avant expiration.

**Aucune intervention manuelle n'est nécessaire.**

### 4.2 Vérifier le certificat

```bash
# Via OpenSSL
echo | openssl s_client -connect meta.dupuisweb.com:443 -servername meta.dupuisweb.com 2>/dev/null | openssl x509 -noout -dates

# Via curl
curl -vI https://meta.dupuisweb.com 2>&1 | grep -i "SSL certificate"
```

### 4.3 Forcer le renouvellement (si besoin)

```bash
sudo caddy renew --force
```

### 4.4 Avec Traefik : vérification ACME

```bash
docker logs traefik 2>&1 | grep -i "acme\|certificate\|letsencrypt"
```

---

## 5. Vérifier que le port 443 est ouvert

### 5.1 Depuis la machine locale

```bash
sudo ss -tlnp | grep :443
# Résultat attendu : LISTEN 0  ...  *:443  ...  utilisateurs:(("caddy",pid=...))
```

### 5.2 Depuis l'extérieur (autre machine ou outil web)

```bash
nc -zv meta.dupuisweb.com 443
# Résultat attendu : Connection to meta.dupuisweb.com port 443 [tcp/https] succeeded!
```

### 5.3 Via votre navigateur

Accédez à `https://meta.dupuisweb.com`. Si tout est configuré correctement :

- Le cadenas vert (🔒) doit apparaître dans la barre d'adresse.
- Aucun avertissement de sécurité ne doit s'afficher.

### 5.4 Dépannage si le port 443 est fermé

| Cause probable                    | Solution                                                |
|-----------------------------------|---------------------------------------------------------|
| Pare-feu du VPS (ufw / iptables)  | `sudo ufw allow 443/tcp` / `sudo ufw reload`            |
| Pare-feu chez l'hébergeur (cloud) | Ajouter une règle entrante TCP/443 dans la console VPS  |
| Caddy / Traefik ne tourne pas     | `sudo systemctl restart caddy` ou `docker restart traefik` |
| Domaine mal résolu                | Re-vérifier l'enregistrement A avec `dig`               |

### 5.5 Test complet automatisé

```bash
#!/usr/bin/env bash
# test-ssl.sh
DOMAIN="meta.dupuisweb.com"

echo "🔍 Résolution DNS :"
dig +short "$DOMAIN" A

echo ""
echo "🔍 Port 443 joignable :"
nc -zv "$DOMAIN" 443 2>&1

echo ""
echo "🔍 Certificat SSL :"
echo | openssl s_client -connect "$DOMAIN":443 -servername "$DOMAIN" 2>/dev/null \
  | openssl x509 -noout -subject -dates -issuer
```

---

## Résumé des étapes

```
1. Acheter le domaine (OVH, Gandi, Cloudflare, etc.)
2. Créer un enregistrement A : meta → IP_DU_VPS
3. Attendre la propagation DNS (dig pour vérifier)
4. Installer Caddy (ou Traefik) avec reverse_proxy
5. Laisser Caddy obtenir le certificat Let's Encrypt automatiquement
6. Vérifier : https://meta.dupuisweb.com → 🔒 OK
```

---

*Guide maintenu par l'équipe Hermes Social — Dernière mise à jour : juillet 2026.*
