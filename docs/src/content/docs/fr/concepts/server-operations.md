---
title: Exploitation du serveur
description: Gérer stockage, réseau, clés, limites et observabilité d'un serveur Latet.
---

Ces exigences s'appliquent à chaque instance d'un serveur Latet configuré. Conservez
la même adresse HTTPS publique, la même base PostgreSQL, le même Valkey et la même clé
principale sur toutes les instances. [Latet sur un serveur](../server/) explique les
données qui atteignent le serveur et celles qui restent lisibles dans ses magasins.

## Configuration et clés

`server.yaml` contient les réglages d'instance. `--config` choisit le fichier pour les
commandes serveur ; `LATET_SERVER_CONFIG` le choisit par l'environnement. Les variables
`LATET_SERVER_*` correspondantes remplacent les valeurs du fichier. Les réglages d'instance
sont distincts des réglages partagés et des réglages et règles propres à chaque identité,
conservés dans PostgreSQL.
Le [tableau généré des réglages du serveur](../../../reference/configuration/#server-settings), en
anglais, présente chaque réglage d'instance, sa valeur par défaut et son rôle.

Fournissez la clé principale par `master_key_file` ou l'entrée d'environnement prise en
charge pour cette clé. Préférez un fichier secret protégé ; ne la mettez pas dans un
dépôt ni sur une ligne de commande. Toutes les instances ont besoin de la même clé.
Sauvegardez-la séparément avec un accès restreint : remplacer le fichier par une nouvelle
clé aléatoire ne renouvelle pas le chiffrement des données et rend les données existantes illisibles.
Une entrée du coffre-fort qui ne s'ouvre pas (scellée avec une autre clé, ou modifiée dans la base de
données) est laissée de côté : le journal donne son identifiant, jamais sa valeur, et le masquage
continue avec les autres entrées.

Utilisez `database_password_file`, `valkey_password_file`, `metrics_token_file` et
`reviewer_key_file` pour les secrets correspondants. La clé du relecteur est lue une
fois au démarrage et envoyée uniquement au relecteur partagé choisi par les administrateurs.
Elle doit être l'identifiant secret de ce relecteur, jamais une clé principale ni un
jeton d'administration. Le serveur ne cherche pas la clé du relecteur dans l'environnement,
le trousseau ou une clé locale enregistrée d'une personne.

## Services système

Installez le paquet serveur dans un emplacement de confiance à l'échelle du système,
hors des dossiers personnels. Préparez `server.yaml` avec les URL, chemins de fichiers
secrets et réglages TLS décrits plus haut ; le service doit pouvoir lire ces fichiers
protégés. Créez d'abord la clé principale et initialisez dans votre terminal administrateur
privé : le premier jeton d'administration est affiché une fois. Ne conservez pas cette
sortie dans des journaux partagés.

Sous Linux, quelques choses viennent d'abord, en tant que root. Installez Latet avec l'extra
`server` pour toute la machine : les dossiers d'uv pour les outils, les commandes et Python,
tous hors des dossiers personnels. Créez l'utilisateur et le groupe `latet` sous lesquels
tourne le service, puis le dossier qui contient les réglages et les secrets, avec la clé
principale et le mot de passe de la base de données :

```sh
UV_TOOL_DIR=/opt/latet/tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/opt/latet/python \
  uv tool install --python ">=3.12,<3.14" "latet[server] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"
useradd --system --user-group --no-create-home --shell /usr/sbin/nologin latet
install -d -m 0750 -o root -g latet /etc/latet
(umask 077 && openssl rand -base64 32 > /etc/latet/master.key)
(umask 077 && openssl rand -hex 32 > /etc/latet/db.password)   # or the password your database administrator gives
```

Mettez le mot de passe de Valkey dans `/etc/latet/valkey.password`, et le certificat TLS avec sa
clé dans `/etc/latet/server.crt` et `/etc/latet/server.key`. Écrivez ensuite
`/etc/latet/server.yaml` ; en voici un minimal pour un service sur l'hôte :

```yaml
url: https://latet.acme.lan
database_url: postgresql://latet@db.acme.lan:5432/latet?sslmode=verify-full&sslrootcert=system
database_password_file: /etc/latet/db.password
valkey_url: rediss://valkey.acme.lan:6379/0
valkey_password_file: /etc/latet/valkey.password
master_key_file: /etc/latet/master.key
certificate: /etc/latet/server.crt
key: /etc/latet/server.key
```

Donnez `server.yaml` et chaque fichier secret qu'il nomme, `key` compris, à root et au groupe
`latet`, lisibles par eux seuls (`0640`, ou `0440`) :

```sh
cd /etc/latet
chown root:latet server.yaml master.key db.password valkey.password server.key
chmod 0640 server.yaml master.key db.password valkey.password server.key
```

Ajoutez `ner` et `ocr` à l'extra pour la détection des noms et la reconnaissance de texte.
L'installation du service dit ce qui manque parmi ces choses : une installation sous un
dossier personnel, aucun utilisateur ou groupe `latet`, ou un fichier avec un autre
propriétaire ou un autre mode.

La configuration par défaut est `/etc/latet/server.yaml`, l'unité `latet-server` et le
dossier de données `/var/lib/latet` :

```sh
latet server init --config /etc/latet/server.yaml
latet server service install --config /etc/latet/server.yaml --print
latet server service install --config /etc/latet/server.yaml
systemctl start latet-server
```

Sous Windows, la configuration par défaut est `C:/ProgramData/Latet/server.yaml`.
Le service s'exécute sous `NT SERVICE\LatetServer`, avec des ACL de dossiers protégées.
Son journal est `C:/ProgramData/Latet/logs/server.log`, avec rotation sur cinq fichiers
de 10 MiB :

```powershell
latet server init --config C:/ProgramData/Latet/server.yaml
latet server service install --config C:/ProgramData/Latet/server.yaml --print
latet server service install --config C:/ProgramData/Latet/server.yaml
Start-Service LatetServer
```

`--print` affiche le plan sans installer. L'installation nécessite root sous Linux ou
les droits administrateur sous Windows et active le service au démarrage ; démarrez-le
explicitement quand vous êtes prêt. Les appels d'agents sont refusés même avec `--print`
ou `--yes`. Dans votre propre terminal, l'installation demande confirmation avant de
modifier quoi que ce soit, avec `--yes` ou sans ; `--yes` permet à un script à vous, sans
terminal, d'installer sans la question. L'installation dans un conteneur est refusée. Ces
commandes décrivent la procédure, pas un service déjà installé sur votre machine.

## Le profil serveur de Compose

Utilisez `compose.yaml` et `deploy/Caddyfile` du dépôt. Préparez un dossier hôte privé
`secrets/` : sous POSIX, il appartient à l'administrateur avec le mode `0700` ; les
fichiers montés individuellement ont le mode `0444` pour que chaque utilisateur de
conteneur puisse les lire. Sous Windows, limitez les ACL des dossiers et fichiers aux
administrateurs et au compte du service Docker qui en a besoin. Les secrets montés par
Compose ne remappent ni propriétaires ni modes ; mettre tous les fichiers en `0600`
peut les rendre illisibles dans les conteneurs.

Créez `master.key` (32 octets aléatoires en base64), `db-admin.password`, `db.password`,
`valkey.password` et `metrics.token` dans ce dossier avant l'initialisation. Utilisez des
valeurs aléatoires robustes ; `db.password` et `valkey.password` doivent être hexadécimaux
pour les commandes de démarrage fournies (`openssl rand -hex 32`). Ne remplacez jamais une
clé principale existante pour faciliter l'installation. Le fichier facultatif `reviewer.key`
nécessite de décommenter ensemble son réglage, son montage et sa définition de secret. Ces
secrets viennent uniquement de fichiers, sans repli sur l'environnement. Fournissez les
fichiers TLS dans `certs/` et protégez la clé privée.

Les instances se connectent à PostgreSQL avec le rôle `latet`, qui n'est pas un
superutilisateur. Sur un nouveau volume de données, `deploy/postgres-init.sh` le crée avec le
mot de passe de `db.password`, comme propriétaire de la base `latet`. Le superutilisateur de
PostgreSQL, `postgres`, a `db-admin.password`, qu'aucune instance ne reçoit : une instance
compromise ne peut pas lancer de programme dans le conteneur de la base (`COPY … PROGRAM`
demande un superutilisateur). Le script ne tourne que tant que le volume est neuf et ne
modifie aucune base existante. Un déploiement dont le volume a été créé avant garde `latet`
comme superutilisateur de PostgreSQL, un attribut que PostgreSQL ne laisse jamais perdre à ce
rôle (`ALTER ROLE latet NOSUPERUSER` est refusé) : pour le faire passer à la nouvelle
configuration, exportez la base avec `pg_dump --no-owner --no-privileges`, démarrez PostgreSQL
sur un nouveau volume et restaurez l'export en tant que `latet`.

Écrivez l'adresse du serveur dans un fichier nommé `.env` à côté de `compose.yaml`, que Compose
lit pour chaque commande, afin qu'une mise à niveau lancée plus tard depuis un autre terminal la
conserve (il ne contient aucun secret) :

```sh
printf 'LATET_SERVER_URL=https://latet.acme.lan\nLATET_SERVER_HOST=latet.acme.lan\n' > .env
```

Cet exemple pour shell POSIX choisit le projet `latet`, avec le réseau interne
`latet_latet-back`. L'initialisation utilise `--log-driver none` de Docker car elle
affiche le jeton d'administration ; gardez le terminal privé et transférez ce jeton dans
votre magasin de secrets. Vérifiez la réussite avant de démarrer les instances :

```sh
# --wait returns only once PostgreSQL and Valkey are healthy (at most 120 s): go on only if it succeeds
docker compose -p latet --profile server up -d --wait --wait-timeout 120 postgres valkey
docker run --rm --log-driver none --network latet_latet-back \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  --mount "type=bind,source=$PWD/secrets/master.key,target=/run/secrets/latet-master-key,readonly" \
  --mount "type=bind,source=$PWD/secrets/db.password,target=/run/secrets/latet-db-password,readonly" \
  --mount "type=bind,source=$PWD/secrets/valkey.password,target=/run/secrets/latet-valkey-password,readonly" \
  --mount "type=bind,source=$PWD/secrets/metrics.token,target=/run/secrets/latet-metrics-token,readonly" \
  --env LATET_SERVER_URL=https://latet.acme.lan \
  --env LATET_SERVER_DATABASE_URL=postgresql://latet@postgres:5432/latet \
  --env LATET_SERVER_DATABASE_PASSWORD_FILE=/run/secrets/latet-db-password \
  --env LATET_SERVER_DATABASE_PRIVATE_NETWORK=1 \
  --env LATET_SERVER_VALKEY_URL=redis://valkey:6379/0 \
  --env LATET_SERVER_VALKEY_PASSWORD_FILE=/run/secrets/latet-valkey-password \
  --env LATET_SERVER_VALKEY_PRIVATE_NETWORK=1 \
  --env LATET_MASTER_KEY_FILE=/run/secrets/latet-master-key \
  --env LATET_SERVER_METRICS_TOKEN_FILE=/run/secrets/latet-metrics-token \
  ghcr.io/latet-ai/latet:latest server init
```

Après une initialisation réussie, démarrez les instances et Caddy :

```sh
docker compose -p latet --profile server up -d latet-server-1 latet-server-2 caddy
```

Dans le profil serveur, seul Caddy publie un port : `443`. Le service local séparé
`latet` reste dans le fichier Compose ; la liste explicite ci-dessus ne le démarre pas.
Le réseau frontal est `172.28.0.0/24`, avec allocation automatique dans
`172.28.0.128/25` ; l'adresse fixe de Caddy, `172.28.0.2`, est hors de cette plage
et constitue le seul proxy de confiance. Le réseau arrière est interne.

Caddy utilise les fichiers TLS fournis, une limite de requête de **32 MiB**, la transmission
immédiate des flux et les mises à niveau WebSocket. Gardez les journaux d'accès désactivés
et conservez l'exclusion globale `log default { exclude http.log.error }` : sinon les
erreurs du proxy peuvent journaliser les URI et les en-têtes d'authentification personnalisés.
Caddy n'inclut pas de limiteur de débit ; les limites par client relèvent d'un proxy
externe ou d'un WAF.

Les conteneurs serveur utilisent des journaux JSON-file bornés et non bloquants.
Les instances tournent sous l'utilisateur non privilégié de l'image, uid et gid `10001`,
jamais en root. Elles ont une racine en lecture seule, des capacités supprimées et un `/tmp`
temporaire ; cet exemple n'impose pas de limite mémoire aux instances de production.
Dimensionnez les espaces, modèles et traitements OCR simultanés sans reprendre les
limites des tests de fumée. Valkey a une mémoire bornée avec `noeviction`.
L'image inclut les avis de tiers.

## PostgreSQL et Valkey

Pour PostgreSQL hors de la machine de l'instance, utilisez TLS vérifié dans l'URL de
connexion, par exemple `?sslmode=verify-full&sslrootcert=system`, ou indiquez le fichier
du certificat de confiance. `verify-full` sans `sslrootcert` ne suffit pas ici. Les URL
sans hôte et les connexions par nom de service sont refusées ; les variables `PG*` ne
complètent pas les réglages manquants. Encodez les caractères réservés des mots de passe
dans les URL avec l'encodage pour cent, ou utilisez le fichier de mot de passe. Le délai
de connexion n'inclut pas la résolution DNS.

Utilisez Valkey 7.2 ou ultérieur, ou Redis 7, comme un seul serveur logique ; les réplicas
sont possibles, Cluster n'est pas pris en charge. Les scripts travaillent sur plusieurs
préfixes de clés. Réglez `maxmemory-policy` sur `noeviction`, prévoyez de la mémoire pour
les espaces attendus et surveillez la capacité. Perdre l'état de coordination n'équivaut
pas à évincer un cache applicatif sans conséquence.

Exigez l'authentification et TLS vérifié (`rediss://`) lorsque Valkey est hors de la machine
de l'instance. Les exceptions `database_private_network`, `valkey_private_network` et
`valkey_unauthenticated` sont des décisions explicites pour des services que seules les
instances prévues peuvent atteindre ; ne les utilisez pas pour masquer une erreur de
certificat ou d'authentification sur un réseau partagé ordinaire.

Avant d'utiliser cette procédure, vérifiez que l'utilisateur Valkey `latet` n'existe pas.
Elle crée un nouvel utilisateur ; s'il existe déjà, arrêtez-vous et faites examiner son
ACL, ses mots de passe et ses sélecteurs par l'opérateur. Appliquer la commande à un
utilisateur existant complète sa configuration ; cette procédure ne la réinitialise pas
et ne renouvelle pas ses mots de passe.

Attribuez à l'application un utilisateur Valkey dédié avec cette ACL. Elle utilise
l'espace de noms par défaut `latet:` ; remplacez le texte d'exemple `password` au moyen d'une
connexion d'administration protégée. C'est une commande Valkey, pas une commande shell :
gardez le vrai secret hors des arguments du shell, de l'historique et des dépôts.

```text
ACL SETUSER latet on >password ~latet:* resetchannels &latet:identity:* &latet:approvals:* &latet:feed:* &latet:shared &latet:identities &latet:signins &latet:beat +@all -@dangerous -monitor -config -keys -debug
```

Conservez littéralement les motifs d'abonnement : les abonnements par motif exigent ces
autorisations exactes. Les scripts Lua et `SCAN` restent disponibles pour la coordination
et la purge ciblée ; `MONITOR`, `CONFIG`, `KEYS` et `DEBUG` sont refusés. Un test automatisé
isolé vérifie cette recette en remplaçant uniquement utilisateur, mot de passe et espace
de noms ; il n'examine ni ne configure votre service déployé.

Exécutez les migrations en tant que propriétaire du schéma. La première crée le schéma
et nécessite `CREATE` sur la base ; les suivantes n'ont pas besoin de ce droit de création.
Le verrou de migration attend au plus 600 secondes. Un rôle applicatif sans `DELETE`
sur `audit` et `audit_anchor` ne peut pas purger le journal d'administration : accordez
ces droits ou conservez le rôle du propriétaire pour les instances. Gardez une sauvegarde
récupérable avant une mise à niveau. Un pooler de connexions doit préserver les
`options` de démarrage PostgreSQL requises.

### Dimensionner Valkey

Les budgets en octets par identité sont de 32 MiB pour les textes tels qu'ils sont
sortis, 64 MiB pour la file d'approbation et 16 MiB pour les événements du flux stockés
par référence. Leur somme, **112 MiB, n'est pas une limite de mémoire totale**. D'autres
familles sont limitées en nombre d'entrées : la taille de leurs enregistrements compte
aussi. Les conversations admettent 1 000 000 de champs et textes approuvés répartis
sur au plus 10 000 conversations. Les marques de blocs vérifiés et enregistrés admettent
200 000 entrées chacune ; les textes inverses et associations de réponses 50 000 chacun,
les sessions AWS 200 et les sessions du tableau de bord 10 par personne.
Les textes inverses sont des sorties du modèle conservées pour les reconnaître lorsqu'elles
reviennent dans une requête ultérieure.

Ce sont des exemples de planification, pas des mesures. On suppose 256 octets alloués
par entrée de conversation et 192 par marque de bloc. Les autres tailles désignent le
texte clair encodé : les estimations ajoutent les 30 octets de la structure de chiffrement,
l'arrondi d'allocation et 256 octets estimés pour la clé et l'index. Les trois budgets
en octets comptent déjà ces coûts.

| Famille | Exemple d'occupation modérée | Nombres saturés, mêmes tailles d'enregistrement |
| --- | --- | --- |
| Textes / file / événements | 8 / 8 / 1 MiB | 32 / 64 / 16 MiB |
| Entrées de conversation | 20 000 de 256 octets | 1 000 000 de 256 octets |
| Marques vérifiées / enregistrées | 10 000 chacune de 192 octets | 200 000 chacune de 192 octets |
| Textes inverses | 5 000 de 2 KiB | 50 000 de 2 KiB |
| Associations de réponses | 5 000 de 64 octets | 50 000 de 64 octets |
| Sessions AWS | 10 de 2 KiB | 200 de 2 KiB |
| Sessions du tableau de bord | 2 de 128 octets | 10 de 128 octets |
| **Sous-total modélisé par identité** | **40,76 MiB** | **581,75 MiB** |

Une réserve illustrative de 50 % donne 61,13 et 872,62 MiB par identité, soit environ
5,97 et 85,22 GiB pour 100 identités. Prévoyez séparément la mémoire de base de Valkey,
les connexions, la réplication/persistance et les structures restantes ou la fragmentation
qui dépassent cette réserve. Comptez les identités dont les données sont encore conservées,
pas seulement celles qui envoient des requêtes maintenant. Les caches applicatifs et les
modèles OCR nécessitent leur propre RAM.

L'exemple saturé **n'est pas le pire cas absolu** : les petits enregistrements restent
une hypothèse. La file des écritures en attente de 4 MiB ne limite pas à 4 MiB la famille
persistante des textes inverses. Une succession d'enregistrements proches de 4 MiB peut
approcher 195 GiB pour 50 000 enregistrements inverses seuls, avant les autres familles.
C'est une estimation large tirée du code source, pas une charge mesurée ni un quota total.
Une longue clé de conversation ou un long chemin de dossier peut aussi dépasser la taille
supposée. Gardez `noeviction`, surveillez tailles et occupation réelles, et dimensionnez
pour la charge admise ; les nombres d'entrées seuls ne garantissent pas un petit plafond
de mémoire par identité.

## HTTPS, proxys et capacité

Terminez TLS à l'instance ou au répartiteur de charge. Réglez `forwarded_allow_ips` sur
les proxys réellement fiables, jamais `*` ; les en-têtes transférés depuis d'autres
adresses ne sont pas considérés comme fiables. Synchronisez les horloges avec un écart
de 60 secondes au plus. Restreignez séparément l'accès direct aux fournisseurs si le
serveur est le point de contrôle obligatoire de l'entreprise.

La limite par défaut du corps d'une requête est de 32 MiB. `requests_in_flight` vaut 16
par identité sur une instance, avec des quotas séparés pour les destinations, le tableau
de bord et le bridge. Les requêtes longues et les flux ont leurs propres limites. Un
refus de capacité appelle une vérification de la charge et des limites, pas la désactivation
de la protection. L'arrêt laisse 30 secondes à l'instance pour terminer ; les longs flux
peuvent être coupés, les clients doivent donc gérer la déconnexion.

L'OCR utilise des lecteurs limités, chacun gardant ses modèles en mémoire. `ocr_readers`
vaut 2 par défaut et ne peut pas dépasser les processeurs disponibles pour l'instance.
`ocr_read_seconds` vaut 30 ; `ocr_wait_seconds` vaut 35 et ne peut pas être inférieur
au budget de lecture. Ajouter des lecteurs consomme de la mémoire et du CPU. Une image
non lue est retenue, ou sa requête vérifiée est refusée, selon le chemin protégé ;
augmenter les délais n'améliore pas la précision de reconnaissance.

### Connexions, limites et confiance

- **Les identifiants refusés** comptent pour l'adresse d'où ils viennent : une adresse IPv4 ou, en IPv6, son /64
  et, ensemble, son /48. Un identifiant reconnu valide dans l'heure écoulée, et un lien de connexion émis par le
  serveur, échappent à cette limite : les tentatives échouées d'un voisin derrière le même NAT ne retiennent que les
  identifiants non reconnus valides récemment, tant que les tentatives durent ; un proxy ou un pare-feu en amont qui
  limite chaque client l'évite. Tant que les threads qui vérifient les identifiants sont occupés, les
  inscriptions de machines et les identifiants nouveaux pour toutes les instances reçoivent 503 avec
  `Retry-After`, et un lien de connexion au tableau de bord est renvoyé vers `?login=busy`.
- **Le jeton et la clé d'une machine :** le jeton que gardent les agents d'une machine ouvre le bridge du plugin,
  le proxy de l'espace et les deux appels de la machine au démarrage d'un agent (une destination ajoutée pour
  une adresse, qui ne réduit rien, et la valeur factice du nom d'utilisateur), dit à qui il appartient et se
  révoque lui-même ; un lien de connexion au tableau de
  bord et une modification de l'identité de la personne demandent aussi la clé de la machine (en-tête
  `x-latet-computer-key`), qui reste dans le magasin d'identifiants de cette machine. Une clé absente ou fausse
  est refusée (403 `computer_key_required`) et compte pour son adresse comme un identifiant refusé.
- **Une identité** a au plus `requests_in_flight` requêtes en cours sur une instance (16 par défaut) ; au-delà,
  429 avec `Retry-After`. Un corps est lu jusqu'à `max_request_mb` (32 Mio par défaut) ; un corps JSON proche de
  cette limite tient le verrou de l'interpréteur une à trois secondes partout où il est analysé : gardez la limite
  aussi basse que vos agents le permettent et faites tourner plusieurs instances.
- **PostgreSQL :** chaque instance garde jusqu'à 10 connexions ; le `max_connections` de PostgreSQL doit couvrir
  les 10 de chaque instance et les commandes des administrateurs.
- **Valkey :** chaque instance garde jusqu'à `valkey_connections` connexions (100 par défaut), et au-delà un appel
  attend cinq secondes au plus. Le `maxclients` de Valkey doit couvrir les connexions de toutes les instances. La
  connexion à Valkey, son TLS et sa première réponse partagent une seule limite de cinq secondes.
- **Les certificats des fournisseurs :** une instance fait confiance au magasin de certificats du système
  d'exploitation ; sous Linux, aux chemins par défaut d'OpenSSL, que `SSL_CERT_FILE` et `SSL_CERT_DIR` changent
  pour tout le processus. Ne les changez que pour ajouter l'autorité de votre entreprise.
- **`SSLKEYLOGFILE`** ne doit pas être définie pour une instance : les connexions TLS qui la lisent, dont celle
  vers Valkey, écrivent leurs clés de session dans ce fichier, et qui le lit lit ce trafic. Une instance démarrée
  avec elle le signale dans son journal.

## Mettre à jour et ajouter des instances

Les instances partagent les mêmes tables, et une instance ne démarre que sur des tables de sa version. La
base de données est à vous de la garder : Latet n'en copie rien. Avant de récupérer une nouvelle image et de
lancer `latet server migrate`, faites-en une copie avec `pg_dump`, qui reste cohérente même pendant que les
instances écrivent :

```sh
pg_dump --format=custom --file=latet-before-upgrade.dump "postgresql://latet@db.acme.lan/latet"
```

Pour revenir en arrière, restaurez cette copie (`pg_restore --clean --if-exists`) et démarrez l'image
précédente : une instance ne démarre pas sur les tables d'une version plus récente. Les valeurs de la copie
restent chiffrées, comme dans la base : seule la clé maîtresse du serveur les ouvre. Avant une mise à jour, demandez à la
nouvelle version si elle modifie les tables :

```sh
latet server migrate --check
```

- **Les tables restent les mêmes :** remplacez les instances une par une derrière le répartiteur de
  charge. Une instance arrêtée termine ses requêtes en 30 secondes au plus.
- **Les tables changent :** arrêtez toutes les instances, lancez `latet server migrate` une fois avec la
  nouvelle version, puis démarrez les instances avec elle. Une instance ne démarre pas sur les tables
  d'une autre version.

Dans l'exemple Compose, quand les tables changent, les mêmes étapes tournent dans des conteneurs de la
nouvelle image :

```sh
docker compose -p latet --profile server pull
docker compose -p latet --profile server run --rm latet-server-1 server migrate --check
docker compose -p latet --profile server stop latet-server-1 latet-server-2
docker compose -p latet --profile server run --rm latet-server-1 server migrate
docker compose -p latet --profile server up -d latet-server-1 latet-server-2
```

Pour ajouter une instance, démarrez-en une autre avec les mêmes réglages, la même clé principale et les
mêmes PostgreSQL et Valkey, puis ajoutez son adresse au répartiteur de charge. Les instances gardent leur
état dans PostgreSQL et Valkey : n'importe laquelle peut répondre à n'importe quelle requête.

## Modèles de reconnaissance sur un serveur

La détection des noms et la reconnaissance de texte lisent leurs modèles dans le dossier de données du
serveur, pas sur l'ordinateur de l'administrateur. Installez-les là, avec le compte sous lequel
tournent les instances : `latet ner install` et `latet ocr install` avec `LATET_DATA_DIR` pointant vers
ce dossier. Dans l'exemple Compose, ce dossier est le volume `latet-server-data`, monté sur `/data`
dans chaque instance : installez-les depuis un conteneur d'une instance, puis redémarrez les instances.

```sh
docker compose -p latet --profile server run --rm latet-server-1 ner install --yes
docker compose -p latet --profile server run --rm latet-server-1 ocr install --yes
docker compose -p latet --profile server restart latet-server-1 latet-server-2
```

Avec le service Linux, les instances tournent sous l'utilisateur `latet`, et leur dossier est
`/var/lib/latet`, que systemd crée au premier démarrage du service. Une fois celui-ci démarré,
installez les modèles sous cet utilisateur, puis redémarrez le service :

```sh
sudo -u latet env LATET_DATA_DIR=/var/lib/latet latet ner install --yes
sudo -u latet env LATET_DATA_DIR=/var/lib/latet latet ocr install --yes
systemctl restart latet-server
```

Avec le service Windows, la procédure d'installation des modèles n'est pas encore documentée.

## Programmes avec une clé d'API

Un programme atteint ses routes avec sa clé d'API et garde sa propre clé de fournisseur. Écrivez la
clé d'API dans un fichier privé, puis affichez les variables qui font passer le programme par le
serveur :

```sh
latet env --key-file program.key --server https://latet.acme.lan
```

Les variables affichées portent la clé de routage : gardez-les aussi secrètes que la clé, jamais dans un dépôt
ni dans un profil partagé.

Le programme doit faire confiance au certificat du serveur : celui d'une autorité à laquelle il fait
déjà confiance, ou le certificat ajouté à ses propres réglages de confiance.

## Journaux, métriques et récupération

Les journaux applicatifs vont sur la sortie standard en `text` ou `json`. Protégez et
faites tourner les fichiers de journaux du déploiement. Le journal d'administration
est distinct et consigne les opérations, dont l'émission de codes et les changements
d'identifiants. Lisez-le avec `latet server log` et vérifiez sa chaîne avec
`latet server log verify`. Gardez la tête renvoyée hors de la base : supprimer la fin
de la chaîne peut laisser la partie restante valide.

Le verdict de chaîne de la commande status est mis en cache et peut être inconnu ;
ce n'est pas une nouvelle vérification complète. Les métriques Prometheus utilisent
un point d'écoute séparé, `127.0.0.1:7283` par défaut, et nécessitent `metrics_token_file`.
Distinguez un verdict inconnu d'une
chaîne rompue. La [référence d'administration](../../reference/server-administration/)
définit la pagination du journal et le sens précis de `latet_ocr_unread_total{reason}`.

Dans cette configuration Compose, collectez `127.0.0.1:7283` depuis l'intérieur de chaque
instance avec son jeton de métriques. Il n'y a ni port publié, ni route Caddy, ni réseau de
surveillance fourni. Un autre déploiement doit prévoir explicitement un accès protégé et
authentifié ; le port HTTPS de l'application ne fournit pas ces métriques. L'écouteur des
métriques ne répond qu'à `GET /_latet/metrics`, à tout autre chemin par `404` : Prometheus demande
`/metrics`, sauf si son `metrics_path` dit autre chose.

Une instance par heure parcourt la chaîne du journal d'administration et en garde le verdict ; l'état et les
métriques montrent ce verdict pendant deux heures, et le disent inconnu quand il n'y en a pas (un nouveau
serveur, ou un Valkey redémarré à vide). Latet n'a aucun outil qui répare une chaîne rompue : comparez-la avec la
tête que vous avez gardée hors de la base, trouvez l'entrée qu'elle désigne et restaurez la base depuis une
sauvegarde si le journal a été modifié.

Un Prometheus à côté d'une instance l'interroge ainsi, toutes les 30 secondes ; tout intervalle jusqu'à
deux minutes convient. Les alertes attendent plus d'une interrogation, car la première après un
démarrage n'a pas encore les mesures des services :

```yaml
scrape_configs:
  - job_name: latet
    scrape_interval: 30s
    metrics_path: /_latet/metrics
    authorization:
      credentials_file: /etc/prometheus/latet-metrics.token
    static_configs:
      - targets: ["127.0.0.1:7283"]
```

```yaml
groups:
  - name: latet
    rules:
      - alert: LatetDatabaseDown
        expr: latet_database_up == 0
        for: 2m
      - alert: LatetValkeyDown
        expr: latet_valkey_up == 0
        for: 2m
      - alert: LatetAuditChainBroken
        expr: latet_audit_chain_known == 1 and latet_audit_chain_ok == 0
        for: 5m
      - alert: LatetLogsDropped
        expr: increase(latet_log_dropped_total[10m]) > 0
```

Sauvegardez PostgreSQL, la clé principale correspondante et la configuration avec les
contrôles d'accès appropriés. Testez la restauration dans un environnement isolé avant
de compter sur la sauvegarde. Restaurer une ancienne base peut aussi restaurer d'anciennes
autorisations : réconciliez identifiants et révocations avant de rouvrir le service.
La conservation de l'historique et son effacement dans le tableau de bord ne suppriment
ni sauvegardes ni coffre-fort. Surveillez ces magasins séparément.
