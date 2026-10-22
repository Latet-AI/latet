---
title: Latet sur un serveur
description: Espaces, stockage, réglages partagés et frontière de confiance d'un serveur Latet.
---

Un serveur Latet reçoit les requêtes protégées des agents connectés. En mode passerelle,
il les masque avant de les transmettre au fournisseur du modèle et restaure le chemin de
retour protégé ; les requêtes reconnues comme Claude Code utilisent le plugin et la
vérification seule décrite ci-dessous. Des données originales peuvent atteindre le serveur
Latet. Un serveur change les acteurs auxquels vous devez faire confiance ; il ne rend
pas la détection infaillible et ne protège pas le trafic qui n'atteint jamais Latet.

## Clients connectés

La machine doit utiliser la route du serveur et garder privées les liaisons de connexion
et d'identifiants. Le masquage serveur couvre les clients passerelle. Claude Code nécessite
aussi son plugin : dans le terminal Windows connecté pris en charge, la liaison privée du
processus auxiliaire et les réglages de confiance choisissent le bridge serveur.
Le jeton personnel passe par stdin ; la liaison de métadonnées ne stocke pas de jeton.
Une liaison absente ou non sûre arrête cette voie.

Pour les requêtes reconnues comme Claude, le plugin masque les descriptions d'outils,
et la vérification finale du serveur lit aussi les descriptions, schémas de paramètres et
exemples des outils : une valeur restée en clair arrête la requête.
Les marqueurs n'attestent pas que le plugin a été exécuté. Lisez le [périmètre de protection](../protection-scope/)
pour cette exception précise et les vérifications séparées des schémas de sortie.
[Connecter une machine](../../getting-started/server/) explique l'application, les diagnostics,
le renvoi de l'identité et la déconnexion.

Si Latet est désactivé dans un dossier, une requête qu'une instance reçoit pour ce dossier avant que le plugin
l'ait signalé au serveur est masquée comme si Latet était actif : dans le doute, il protège.

### Le jeton et la clé d'une machine

Quand une machine se connecte, le serveur lui donne deux secrets, chacun affiché une seule fois. Le
**jeton** va aux agents : Claude Code le garde dans ses réglages et son environnement
(`LATET_BRIDGE_TOKEN`), où chaque commande qu'il exécute peut le lire. De partout où le serveur est
joignable, le jeton ouvre le bridge du plugin et le proxy de l'espace (qui le détient peut faire masquer
des textes dans l'espace et faire restaurer les valeurs factices qu'il possède), dit à qui il appartient et
se révoque lui-même. La **clé de la machine** reste dans le magasin d'identifiants de la machine, ou dans le
fichier privé du jeton, et aucun agent ne la reçoit : le serveur la demande aussi avant de donner un lien
de connexion au tableau de bord ou d'ajouter quoi que ce soit à l'identité de la personne. Ainsi, le
jeton seul n'ouvre jamais le tableau de bord, où chaque original peut être affiché, et ne réduit jamais
la protection de la personne : la seule modification qu'il peut apporter à ses réglages est l'ajout
d'une destination pour une adresse, qui ne réduit rien. La clé empêche un jeton qui a fuité (depuis
l'environnement, les réglages, un journal ou un seul `curl`) d'ouvrir le tableau de bord ; elle
n'arrête pas une commande écrite pour lire les deux dans le magasin d'identifiants. Sur une machine
connectée, `latet dashboard` et `latet identity send` refusent de s'exécuter dans un agent, et
`latet disconnect` oublie les deux.

### Ce qui reste sur la machine

- **Le `latet.yaml` d'un dépôt ne s'applique pas.** Le serveur ne lit aucun fichier de la machine :
  ce qu'une équipe partage dans son fichier de projet (un profil plus strict, ses termes et
  reconnaisseurs) n'atteint pas les requêtes d'une personne connectée. Dans un tel projet,
  `latet doctor` et `latet claude` le disent. Réglez-le dans votre tableau de bord : importez le
  `latet.yaml` du projet dans **Règles** avec **Importer des règles**.
- **La vérification des fichiers CLAUDE.md**, que Claude Code donne à son relecteur du mode auto,
  s'exécute sur la machine, dans `latet doctor`.
- **Claude Code via un serveur nécessite un terminal Windows** dans cette version. Sous Linux et
  macOS, son plugin retient chaque prompt, et Latet le dit d'emblée là où il l'appliquerait. Le mode
  d'impression (`claude -p`) est retenu sur toute machine connectée.

### Certificats et connexions propres des agents

Chaque agent fait confiance au certificat du serveur par une variable qui lui est propre et que Latet
définit : `NODE_EXTRA_CA_CERTS` pour Claude Code et OpenCode, `CODEX_CA_CERTIFICATE` pour Codex. Elle s'ajoute à
ce que l'agent approuve déjà, et la copie que Latet écrit garde les certificats que la personne y avait
indiqués, l'autorité de son entreprise par exemple. Un certificat auto-signé n'atteint les agents que sous la
forme que lui donne `latet server certificate --make`, limitée au nom ou à l'adresse du serveur ; un certificat
de l'autorité de l'entreprise fonctionne tel quel.

Au démarrage, les agents appellent aussi leurs propres éditeurs, hors de Latet : Claude Code `api.anthropic.com`,
avec sa clé d'API, sauf si son trafic non essentiel est désactivé dans ses réglages ; Codex `chatgpt.com` et
`api.github.com` pour ses plugins ; OpenCode le registre npm et GitHub. Aucun de ces appels ne porte l'adresse de
l'espace, sa clé de routage ni le jeton. Là où le serveur doit être le seul chemin vers les modèles, bloquez les
hôtes des fournisseurs de modèles, dont `api.anthropic.com` et `chatgpt.com`, et désactivez le trafic non essentiel
de Claude Code ; la liste des plugins de Codex échoue alors. Par exemple :

```text
allow  latet.acme.lan:443                        # the Latet server
allow  api.github.com:443, github.com:443        # Codex's plugins and OpenCode's start-up, if wanted
allow  registry.npmjs.org:443                    # OpenCode's start-up, if wanted
deny   api.anthropic.com:443, chatgpt.com:443    # the models only through the server
deny   every other host of the AI providers
```

## Espaces et instances

Chaque personne et chaque clé d'API possède un espace distinct, avec son coffre-fort, son
historique et ses réglages. Les machines d'une personne partagent cet espace. Une clé
d'API représente un programme, n'a pas de tableau de bord personnel et reçoit ses réglages de l'administrateur.

Une instance est un processus serveur Latet en cours d'exécution. Plusieurs instances
peuvent servir la même adresse HTTPS publique derrière un répartiteur de charge, avec
la même base PostgreSQL, le même Valkey et la même clé principale. PostgreSQL conserve
l'état durable ; Valkey coordonne l'état temporaire. Les requêtes ne nécessitent pas
d'affinité de session. Ajouter des instances ne crée pas de copies distinctes de l'espace
d'une personne. [Exploitation du serveur](../server-operations/) décrit les exigences des services.

## Ce que le serveur conserve

Les valeurs et valeurs factices du coffre-fort, les textes et images enregistrés et les réglages
d'un espace sont chiffrés avec sa clé d'identité. Cette clé est chiffrée avec la clé
principale du serveur ; les réglages partagés sont chiffrés avec la clé principale.
Gardez cette dernière séparée des sauvegardes de la base. Sans la clé correspondante,
les données chiffrées ne peuvent pas être récupérées.

Certaines métadonnées restent lisibles dans PostgreSQL : noms et adresses e-mail des
identités, identifiants externes, libellés et dates des jetons, noms des règles du coffre-fort,
compteurs et dates, ainsi qu'acteurs, actions, cibles, identifiants, nombres et adresses
clientes du journal d'administration. Les identifiants de projet et de conversation
de l'historique utilisent des hachages à clé. Valkey utilise des noms dérivés avec une
clé, des données sensibles chiffrées et des messages de coordination signés.

Le chiffrement au repos ne cache pas les originaux au serveur en fonctionnement ni à
son opérateur. Les vues autorisées du tableau de bord reçoivent les originaux dans le
navigateur. Un administrateur peut émettre le code d'inscription d'une personne et
l'utiliser pour accéder à son tableau de bord ; cette opération est consignée. Il peut
aussi configurer un service de relecture qui reçoit du texte original. Faites donc
confiance aux administrateurs du serveur, à l'opérateur du déploiement et à ce relecteur.
Chiffrement et signatures n'empêchent pas une personne pouvant écrire dans la base de
restaurer un état antérieur valide.

Effacer l'historique enregistré supprime les échanges de cet espace, pas son coffre-fort.
Cela n'efface ni sauvegardes, ni transcriptions d'agents, ni enregistrements du fournisseur,
ni copies déjà reçues par un navigateur.

## Réglages partagés et règles propres

Les réglages partagés sont ce dont le serveur a besoin pour fonctionner, les mêmes pour chaque
espace : les destinations vers lesquelles les requêtes peuvent aller, le relecteur local partagé,
la reconnaissance de texte, l'historique et sa conservation. Les administrateurs les définissent.
Les règles de protection (termes, reconnaisseurs, profil, politiques et le reste) sont propres à
chaque identité : les personnes définissent les leurs dans leur tableau de bord, et un
administrateur définit celles d'une clé d'API avec `latet server config --key` et
`latet server rules --key`.

Une équipe partage ses règles à la main : une personne les exporte (**Exporter les règles** sur la
page **Règles** du tableau de bord, ou `latet rules export` sur une machine qui n'est connectée à
aucun serveur) et chacun importe le fichier dans son propre tableau de bord.
[Exportation et importation](../export-import/) décrit le fichier. Les règles définies pour tout le
monde, que personne ne peut assouplir, sont prévues pour l'édition Enterprise.

Un changement partagé qui réduit la protection, comme une destination déplacée ou le relecteur
désactivé, attend l'approbation de la personne. En attendant, ses réglages plus stricts
précédemment approuvés restent en vigueur. Enregistrer des modifications personnelles n'approuve
pas le changement partagé en attente, même si l'enregistrement contient le même élément. Un
enregistrement personnel ne peut pas supprimer silencieusement une destination définie pour tout le
monde.

Une clé d'API n'a personne pour approuver un tel changement : il atteint tout de suite l'espace de la
clé, même un changement qui réduit la protection ; vérifiez donc ce qu'un changement des réglages
partagés fait à vos programmes avant de le faire.

Distinguez ces résultats d'enregistrement :

| Résultat | Signification |
| --- | --- |
| `409 settings_stale` | Les réglages ont changé depuis leur lecture par la page ; rien de cet enregistrement n'a été conservé. Rechargez avant de les modifier à nouveau. |
| `409 settings_change_waits` | Le changement en attente ne peut pas être combiné aux réglages approuvés pour le moment ; rien de cet enregistrement n'a été conservé. |
| `409 settings_saved_change_pending`, `saved: true` | Vos modifications ont été conservées, mais l'actualisation de la proposition en attente a échoué. Rechargez et examinez le changement en attente ; ne répétez pas l'enregistrement sans vérifier. |
| `409 settings_terms_too_long` | Un terme dépasse la longueur acceptée ; rien de cet enregistrement n'a été conservé. |

D'autres limites ou erreurs de stockage peuvent aussi refuser un enregistrement. Une
erreur HTTP seule ne prouve pas que rien n'a été écrit ; conservez le motif renvoyé et
toute indication d'enregistrement partiel.

### Les destinations propres d'une personne

Une destination qu'une personne définit dans ses propres réglages ne mène jamais dans le réseau du serveur. Une
adresse privée, unique-local, de NAT carrier-grade, de bouclage, link-local, multicast ou réservée est refusée,
écrite comme adresse ou comme nom qui s'y résout ; de même pour les formes IPv6 qui portent une adresse IPv4 ou
mènent à une passerelle (6to4, Teredo, préfixes NAT64). Sur un réseau avec NAT64, le serveur en découvre les
préfixes comme le décrit la RFC 7050, et tant qu'il ne les connaît pas, il ne joint aucune adresse IPv6 qu'un NAT64
pourrait traduire : un nom qui a aussi une adresse IPv4 va là. Un nom est résolu de nouveau quand le serveur se
connecte, et la requête va à l'adresse qui a été vérifiée. Aucun proxy n'est utilisé pour ces destinations. Seul
un administrateur, dans les réglages partagés, définit une destination à l'intérieur du réseau.

Un serveur uniquement IPv6 derrière un DNS64 sur le préfixe bien connu n'atteint donc aucune destination
propre joignable seulement en IPv4.

## Les limites d'un espace

- **Règles :** les réglages propres d'une personne contiennent au plus 500 reconnaisseurs, 500 règles de champs
  et 500 motifs de valeurs à ne jamais masquer, comptés sur les réglages et tous leurs projets. Un
  enregistrement au-delà est refusé.
- **Projets :** au plus 200 par personne.
- **Valeurs :** une valeur de plus de 16 384 caractères est caviardée plutôt que remplacée par une valeur
  factice, et n'est jamais conservée ni restaurée ; un terme compte au plus 8 192 caractères.
- **Mémoire :** une instance donne à ses espaces 2 Gio fixes, et un espace en utilise au plus la moitié. Un
  espace arrivé à sa part n'apprend plus de nouvelle valeur : une requête qui en apporte une est arrêtée avec un
  message qui le dit, jusqu'à ce que des valeurs soient oubliées dans le tableau de bord, que les règles ou les
  projets soient moins nombreux, ou que le serveur en ait davantage. Aucun espace d'une autre personne n'est
  fermé pour faire de la place.
- **Les statistiques** comptent les 100 000 derniers échanges, et une personne garde ses dix sessions du tableau
  de bord les plus récentes.

## La limite de la protection

Latet ne peut examiner que les requêtes prises en charge qui passent par lui. Si la
politique de l'entreprise exige que tout le trafic des modèles passe par le serveur,
le réseau doit empêcher les machines d'atteindre directement les fournisseurs. Configurez
et entretenez cette restriction à la frontière du réseau ; Latet ne l'installe ni ne
l'impose. Le trafic réseau arbitraire des outils est hors de cette garantie.

[Ce qui est protégé et ce qui ne l'est pas](../protection-scope/) décrit les limites de
détection, d'images et de protocoles. Un contenu non pris en charge peut être refusé ou
retenu sur un chemin serveur protégé ; un indicateur historique ou un endpoint disponible
du fournisseur n'établit pas sa prise en charge. Un serveur ne peut pas lire le dépôt
d'une machine connectée simplement parce qu'un projet apparaît dans son tableau de bord.
Les informations de projet qu'il possède sont celles qui lui ont été fournies.

Pour les commandes d'administration, commencez par [Administrer un serveur Latet](../../getting-started/server/).
