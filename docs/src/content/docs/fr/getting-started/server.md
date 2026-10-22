---
title: Configurer et administrer un serveur Latet
description: Déployer un serveur, connecter une machine et gérer personnes, clés d'API et réglages partagés.
---

Exécutez les commandes d'administration dans votre propre terminal sur une machine avec Latet.
Lisez [les espaces et la confiance](../../concepts/server/) avant d'accorder un accès.
Un administrateur prépare le déploiement ; chaque personne connecte sa propre machine.
Un agent d'IA peut expliquer les étapes et vérifier la machine, mais les commandes qui modifient
l'administration, `config get`, `rules export`, `latet server init`, `latet server service install`
et `latet connect` refusent de s'exécuter dans un agent : indiquez-lui [`llms-server.md`](https://github.com/Latet-AI/latet/blob/main/llms-server.md).
Ne donnez jamais un jeton d'administration à un agent : avec un jeton, les commandes qui ne font que
lire (l'état, les listes de personnes et de clés, le journal) s'exécutent pour lui.

## Préparer le déploiement

Choisissez le [service système ou Compose](../../concepts/server-operations/).
Le service nécessite une installation de confiance à l'échelle du système et une configuration
protégée ; le profil serveur de Compose fournit PostgreSQL, Valkey, deux instances et Caddy.
Définissez l'adresse HTTPS publique et préparez d'abord la clé principale et les fichiers
secrets. L'initialisation affiche un jeton d'administration : gardez-le dans un terminal privé
et un magasin de secrets. Pour un conteneur, suivez l'initialisation avec les journaux Docker
désactivés du guide d'exploitation ; une initialisation journalisée peut conserver ce jeton.

Utilisez un certificat auquel les machines font confiance. Pour un premier essai, créez un
certificat serveur auto-signé et vérifiez son empreinte :

```sh
latet server certificate --make --url https://latet.acme.lan --out certs
latet server certificate certs/server.crt
```

Gardez `server.key` privé. Compose monte `certs/server.crt` et `certs/server.key` dans Caddy ;
sans conteneur, configurez TLS sur l'instance ou son reverse proxy de confiance.
Transmettez l'empreinte vérifiée par un canal de confiance séparé. Une empreinte affichée
depuis une connexion réseau non vérifiée n'authentifie pas à elle seule le serveur.

## Vérifier le serveur

```sh
# a self-signed certificate: its fingerprint, verified as above, for every `latet server` command in this shell
export LATET_SERVER_FINGERPRINT="<fingerprint>"
latet server --server https://latet.acme.lan status
```

Saisissez le jeton d'administration à l'invite masquée. La réponse indique la base de
données, Valkey, les identités et l'instance qui a répondu. Elle ne vérifie pas chaque
agent et ne prouve pas que tout le trafic d'une machine passe par Latet.

Pour administrer régulièrement le serveur, `--admin-token-file FILE` lit un fichier privé
contenant le jeton ou un enregistrement lié à ce serveur. Gardez-le hors des dépôts et
des espaces de travail des agents. Suivez les contrôles de permissions et d'origine dans
[Administration du serveur](../../reference/server-administration/). N'exportez pas de
jeton d'administration dans l'environnement shell d'un compte où des agents s'exécutent.

Dans la réponse, `database` est toujours vrai, puisqu'une instance qui ne joint pas PostgreSQL ne donne
aucun état, et `valkey` l'est en général pour la même raison : lisez leur santé dans les métriques
(`latet_database_up`, `latet_valkey_up`). `audit` donne le dernier verdict de la chaîne (`ok`, `broken` ou
`unknown`), et `vault` liste les identités à 90 % ou plus de la limite de leur coffre-fort.

## Donner son propre espace à une personne

```sh
latet server --server https://latet.acme.lan people add "Emily Carter" --email emily.carter@acme.com
latet server --server https://latet.acme.lan people list
```

La première commande affiche un code à usage unique. Transmettez-le en privé à cette
personne : quiconque l'utilise peut se connecter en son nom et ouvrir son tableau de bord.
L'émission du code est consignée dans le journal d'administration. Le code ne fonctionne qu'une fois,
dans les 24 heures. `people code "Emily Carter"` en émet un autre quand il a expiré ou s'est perdu, et
le précédent cesse de fonctionner ; ne mettez jamais un code dans un ticket, un canal partagé ou une
conversation avec un agent.

Les machines d'une personne partagent un espace : son coffre-fort, son historique et ses
réglages. Pour consulter les jetons des machines, utilisez :

```sh
latet server --server https://latet.acme.lan people tokens "Emily Carter"
```

Révoquez le jeton d'une machine perdue avec son identifiant dans la liste et
`--revoke TOKEN_ID`. `people disable "Emily Carter"` arrête en quelques secondes tous les jetons de
cette personne et conserve son espace (`people enable` le lui rend) ; `people remove "Emily Carter"`
efface son espace actif sur le serveur, coffre-fort, historique et réglages compris, une fois que vous
avez confirmé. `keys disable`, `enable` et `remove` font de même pour une clé d'API. Aucune de ces
opérations n'efface les copies dans les sauvegardes, les navigateurs ou chez les
fournisseurs de modèles. Lisez la confirmation avant une suppression.

Lors d'une suppression, les données de la personne dans PostgreSQL partent avec elle, ses données dans Valkey
sont effacées une fois par une instance (de nouveau à la vérification suivante si cela ne peut finir), ce qui y
reste expire de lui-même, en huit jours au plus, et ses sessions du tableau de bord et AWS prennent fin aussitôt.

## Connecter la machine d'une personne

Quand le serveur est prêt et que l'administrateur a émis le code de la personne, exécutez :

```sh
latet connect https://latet.acme.lan
```

Saisissez le code à usage unique à l'invite. Avec un certificat auto-signé, ajoutez
`--fingerprint FINGERPRINT` avec l'empreinte vérifiée auprès de l'administrateur.
Ne supprimez pas la liaison au certificat pour contourner une erreur. L'enregistrement de
connexion, y compris le fichier choisi par `LATET_CONNECTION`, contient métadonnées et
empreinte ; le jeton personnel et la clé de la machine restent ensemble, à part, dans le magasin
d'identifiants ou un fichier privé.

Le processus auxiliaire connecté de Claude Code est pris en charge dans un **terminal Windows**.
Appliquez l'intégration et redémarrez Claude Code, ou lancez une seule session :

```sh
latet agents apply claude
latet claude
```

`latet agents apply claude` et `latet global-install` écrivent le jeton personnel dans le
`settings.json` de l'utilisateur Claude, sous `LATET_BRIDGE_TOKEN` ; la commande indique où.
C'est l'exception d'installation à la règle qui exclut le jeton des fichiers d'agents.
La liaison privée du processus auxiliaire contient des métadonnées ; il reçoit le jeton
existant par stdin. Un logiciel capable d'observer cette entrée peut le lire. Ce contrat
ne prend pas en charge le processus auxiliaire connecté dans Claude Desktop, VS Code,
JetBrains, Linux ni macOS : sous Linux et macOS, ces commandes le disent d'emblée et n'écrivent
pas le jeton, et le plugin retient chaque prompt.

Ce jeton ouvre le bridge du plugin et le proxy de l'espace de partout où le serveur est joignable ;
il n'ouvre jamais le tableau de bord, ne change jamais l'identité de la personne et ne réduit jamais sa
protection : la seule modification qu'il peut apporter à ses réglages est l'ajout d'une destination pour
une adresse, qui ne réduit rien. Le tableau de bord et l'identité demandent aussi la clé de la machine,
que `latet connect` garde avec le jeton dans le magasin d'identifiants (ou le fichier privé) et que
seuls `latet dashboard` et `latet identity send` lisent, tous deux refusant de s'exécuter dans un
agent ; aucun agent ne la reçoit.

Sur la machine connectée, utilisez :

```sh
latet status
latet doctor
latet dashboard
latet identity send
latet disconnect
```

`status` et `doctor` fournissent des diagnostics bornés, pas une preuve pour chaque parcours
d'agent. `dashboard` ouvre le tableau de bord de la personne sur le serveur ; sur une machine connectée,
un agent ne peut pas l'exécuter. Il n'affiche
le lien privé de connexion qu'avec `--print-url` ; gardez-le privé. Pendant la connexion,
`config`, `rules`, `llm`, `project`, `identity` et `reload` refusent de lire les réglages comme
de les modifier ; seuls `config schema`, `rules test` et `identity send` s'exécutent. Utilisez
le tableau de bord du serveur ou les commandes d'administration.
`latet identity send` renvoie explicitement les informations d'identité de cette machine.
`latet start` refuse aussi : pendant la connexion, rien n'utilise le daemon de cette machine.

`latet disconnect` révoque le jeton de cette machine et retire les liaisons des agents à la
connexion, y compris le jeton Claude enregistré. La révocation termine aussi les sessions
du tableau de bord de la personne sur ses autres machines. Si elle ne peut être confirmée,
lisez le résultat et faites vérifier les identifiants des jetons par un administrateur avant
de réessayer. Sur une machine connectée, le mode d'impression (`claude -p`) n'est pas pris en charge : le
plugin retient ses requêtes et le dit. Utilisez le terminal interactif. Le
[guide Claude Code](../../agents/claude-code/) distingue la prise en charge connectée des interfaces locales.

Retirer les copies de cette machine ne révoque pas le jeton. Avec `LATET_KEYRING=off`, Latet
n'enregistre aucun identifiant dans le trousseau du système d'exploitation, mais une copie plus ancienne
peut s'y trouver encore : `latet connect` refuse tant qu'elle y est, et `latet disconnect` la lit, la
révoque auprès de son serveur et la retire ; un trousseau verrouillé peut demander à être déverrouillé. Si
une connexion ou une déconnexion est interrompue, un fichier temporaire de jeton resté sur place est
supprimé sans être lu : la machine ne peut donc pas révoquer le jeton qu'il contenait. Dans ce cas, ou
quand `latet disconnect` indique qu'une copie subsiste, un administrateur le révoque sur le serveur : il
liste les jetons de la personne comme ci-dessus, trouve le bon par son identifiant (l'étiquette avec le
nom d'hôte n'est qu'un indice : deux machines peuvent porter le même nom) et le révoque avec
`--revoke TOKEN_ID`. Si le bon jeton ne peut pas être distingué, `--revoke-all` révoque tous les jetons de
la personne, et chacune de ses machines doit ensuite se reconnecter.

## Donner une clé d'API à un programme

Une clé d'API possède son propre espace, sans tableau de bord personnel ni inscription
de machines. Son administrateur gère ses réglages et ses règles. Créez le secret dans un
nouveau fichier privé :

```sh
latet server --server https://latet.acme.lan keys add "Billing CI" --out billing.key
latet server --server https://latet.acme.lan keys list
```

Utilisez un dossier protégé hors du dépôt pour ce fichier. La commande affiche l'identité
et le chemin du fichier sans afficher le secret. Conservez la clé dans le magasin de
secrets du programme. `keys rotate "Billing CI" --out replacement.key` remplace
son identifiant secret ; mettez à jour ensemble la clé du programme et ses adresses de
routage. Une clé de routage dans une adresse est également sensible.

## Définir les réglages partagés et les règles des clés d'API

Les réglages partagés contiennent ce dont le serveur a besoin pour fonctionner, les mêmes pour
chaque espace : les destinations vers lesquelles les requêtes peuvent aller (`upstreams`), le
relecteur local partagé (`llm_review`), la reconnaissance de texte (`ocr`), l'historique et sa
durée de conservation (`retention_days`).

```sh
latet server --server https://latet.acme.lan config get
latet server --server https://latet.acme.lan config add upstreams "{name: llm-acme-com, url: https://llm.acme.com}"
```

Sans `--key`, `config get`, `set`, `add` et `remove` n'agissent que sur ces réglages. Une règle de
protection, comme un terme ou le profil, est refusée, avec un message qui explique comment la
partager à la main. Avec `--key`, les mêmes commandes lisent et modifient les réglages et les règles propres
à une clé d'API, et `latet server rules --key` gère ses reconnaisseurs et y importe un fichier de
règles :

```sh
latet server --server https://latet.acme.lan config --key "Billing CI" set profile max
latet server --server https://latet.acme.lan rules --key "Billing CI" import team-rules.yaml
```

`config get` et `rules export` peuvent afficher des valeurs en clair (les termes d'une clé d'API,
les consignes du relecteur) : gardez leur sortie privée. Les règles propres d'une personne se gèrent
dans son tableau de bord, où elle les définit elle-même.

Pour donner les mêmes règles à une équipe, partagez-les à la main : exportez-les depuis un endroit
(`latet rules export FILE` sur une machine qui n'est connectée à aucun serveur, ou
**Exporter les règles** sur la page **Règles** d'un tableau de bord) et importez le fichier dans
chacun des autres (`latet rules import FILE`, ou **Importer des règles** dans le tableau de bord) ;
celles d'une clé d'API avec `latet server rules --key KEY import FILE`. Sur les machines qui ne sont
connectées à aucun serveur, le `latet.yaml` d'un projet les porte dans son dépôt. Les règles
définies pour tout le monde, que personne ne peut assouplir, sont prévues pour l'édition
Enterprise.

Un changement des réglages partagés qui réduit la protection d'une personne, comme une destination
déplacée vers une autre adresse ou le relecteur désactivé, attend son approbation.
Enregistrer ses modifications personnelles n'approuve pas un changement partagé en
attente. Un enregistrement peut conserver ces modifications sans parvenir à actualiser
la proposition en attente ; lisez le résultat et rechargez avant de faire un autre
changement. Une clé d'API n'a personne pour approuver un tel changement : il atteint tout de suite
l'espace de la clé, même un changement qui réduit la protection ; vérifiez donc ce qu'un changement
des réglages partagés fait à vos programmes avant de le faire.
[Réglages partagés et règles propres](../../concepts/server/) explique cette distinction.

Utilisez des jetons d'administration avec une date d'expiration pour le travail courant.
La [référence d'administration](../../reference/server-administration/) décrit leur
émission et leur révocation, les fichiers de sortie privés, la récupération et la lecture du journal.
