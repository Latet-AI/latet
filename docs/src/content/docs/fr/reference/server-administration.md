---
title: Administration du serveur
description: Administrez un serveur depuis votre machine et gardez ses identifiants confidentiels.
---

Exécutez `latet server` dans votre propre terminal. Les commandes qui modifient l'administration
refusent un agent ; `config get` et `rules export`, dans le terminal ou dans un fichier, le refusent
aussi, car ils peuvent afficher des valeurs en clair (les termes d'une clé d'API, les consignes du
relecteur). L'état, les listes d'identités et le journal
d'administration ne contiennent pas d'identifiants secrets.

Indiquez le serveur et saisissez le jeton à l'invite masquée :

```sh
latet server --server https://latet.acme.lan status
```

Ou utilisez `--admin-token-file FILE` : un fichier UTF-8 privé contenant le jeton seul, ou
un enregistrement `{"url": "https://latet.acme.lan", "token": "<administration token>"}`.
Un enregistrement n'est accepté que pour son serveur. Sous POSIX, utilisez `chmod 600 FILE`
(ou `0400` pour le compte qui exécute un conteneur), dans un dossier que les autres utilisateurs
ne peuvent pas modifier. Sous Windows, le fichier et son dossier doivent autoriser uniquement
votre compte, SYSTEM et Administrators ; suivez la correction `icacls` affichée par la commande.
`--admin-token-file -` lit une ligne sur l'entrée standard.

Sur une machine où des agents s'exécutent, gardez les jetons d'administration hors des profils
du shell et des variables d'environnement du compte : les processus du même utilisateur peuvent
lire l'environnement d'un autre processus. `LATET_SERVER_ADMIN_TOKEN` est destiné au magasin
de secrets d'une CI ou d'un conteneur, avec `LATET_SERVER_URL` ; il n'est jamais envoyé à un autre serveur.

`keys add`, `keys add-token`, `keys rotate` et `admin-token issue` acceptent `--out FILE` : le nouveau fichier
privé reçoit le secret et la commande n'affiche que son identifiant et le fichier. Ne mettez
jamais de code, clé ou jeton d'administration dans un ticket, un canal partagé ou une conversation
avec un agent. Le code à usage unique d'une personne permet à quiconque l'utilise de connecter
une machine sous cette identité et d'ouvrir son tableau de bord ; son émission est consignée.

Utilisez `admin-token issue --label "Daniel's laptop" --days 30` pour un jeton avec échéance.
Un jeton `--never` exige un libellé et permet tout ce qu'un administrateur peut faire jusqu'à
révocation : personnes et leurs codes, clés API, paramètres partagés et jetons d'administration.
Un jeton en émet d'autres qui se terminent au plus tard avec lui ; seul un jeton sans expiration
peut donc en émettre un autre sans expiration ; sur la machine du serveur,
`latet server init --new-admin-token --never` en crée un. Gardez ce jeton à part,
uniquement pour en émettre d'autres. Une CI qui le détient donne ces pouvoirs pour toujours à
quiconque peut modifier ses tâches ; donnez-lui un jeton avec `--days` et renouvelez-le avant son échéance.

Les suppressions demandent confirmation dans votre terminal même avec `--yes` ; les scripts
ont besoin de `--yes`. `config reset` remplace uniquement, après confirmation, des paramètres
partagés que le serveur ne peut pas ouvrir. `config get` et `rules export` peuvent afficher des
valeurs en clair : gardez leur sortie privée. `log verify` affiche la tête de la chaîne ; conservez-en
une copie ailleurs et comparez-la la fois suivante : supprimer les dernières entrées laisse
la chaîne restante intacte.

## Qui administre, et la perte d'un jeton

**L'administrateur est quiconque détient un jeton d'administration.** Il n'existe pas de compte
administrateur : `latet server init` affiche le premier jeton à celui qui initialise le serveur,
et chaque jeton peut en émettre d'autres. Un jeton n'est pas une personne : le journal
d'administration nomme le jeton qui a fait chaque chose (`admin:a_3fk7…`), et un administrateur n'a
pas besoin d'un espace à lui sur le serveur, même s'il peut être ajouté comme personne, comme
n'importe qui. Dans la Community Edition, chaque jeton peut tout faire ; les rôles, comme celui d'un
auditeur en lecture seule, sont prévus pour l'Enterprise Edition.

Un jeton émis sans `--days` ni `--never` prend fin après `admin_token_days` (90 jours, sauf
indication contraire dans les paramètres du serveur), ou plus tôt quand le jeton qui l'a émis prend fin
avant lui ; un jeton émis avec `--days` prend fin après ce nombre de jours. Un tel jeton, oublié,
cesse de fonctionner de lui-même. Un jeton `--never` fonctionne jusqu'à sa révocation : listez les
jetons de temps en temps avec `admin-token list`, et révoquez ceux que personne n'utilise.

**Un jeton perdu, alors qu'il en reste un autre :** depuis n'importe quelle machine, listez-les,
révoquez celui qui est perdu et émettez-en un nouveau :

```sh
latet server --server https://latet.acme.lan admin-token list
latet server --server https://latet.acme.lan admin-token revoke a_3fk7qz2mwd
latet server --server https://latet.acme.lan admin-token issue --label "Marco" --out marco.token
```

Sans `--days`, le nouveau jeton dure ce que dure le défaut du serveur, coupé à la fin du jeton qui
l'émet. Un jeton avec `--days` ne peut pas se terminer après le jeton qui l'émet : le serveur le
refuse, et `admin-token list` montre quand chaque jeton prend fin.

**Plus aucun jeton**, parce que tous sont perdus ou arrivés à échéance : là où l'on peut atteindre les
paramètres du serveur, sa clé principale et sa base de données, `latet server init --new-admin-token`
en émet un nouveau (avec `--never`, un jeton de secours à garder à part). Dans le déploiement Compose,
relancez le `docker run` de l'initialisation donné dans
[Exploitation du serveur](../../concepts/server-operations/), terminé par
`server init --new-admin-token`, avec la journalisation de Docker désactivée comme avant. Révoquez
ensuite les jetons perdus, s'ils ne sont pas déjà arrivés à échéance. Les personnes, les clés API, les
paramètres, les coffres-forts et l'historique restent inchangés dans les deux cas.

**Un jeton qui a fuité** : révoquez-le tout de suite depuis un autre jeton, puis lisez ce qu'il a fait :

```sh
latet server --server https://latet.acme.lan log --actor admin:a_3fk7qz2mwd --since 30d
```

Perdre la **clé principale** est pire que perdre un jeton : elle scelle la clé de chaque espace,
et sans elle aucun coffre-fort ne pourra plus être lu. Gardez-en une copie à part des sauvegardes
de la base de données, comme l'explique [Exploitation du serveur](../../concepts/server-operations/).

## Oublier des valeurs d'une clé d'API

Quand une personne demande que ses données quittent le coffre-fort d'une clé d'API, `keys forget` en
retire ses valeurs, avec ce que les conversations et l'historique de la clé en gardent, sur chaque
instance dès que les appels déjà en cours y sont finis :

```sh
latet server --server https://latet.acme.lan keys forget "Shop" --file erasure.txt
```

Le fichier contient une valeur par ligne, en UTF-8 ; sans `--file`, tapez les valeurs dans votre
terminal, une ligne vide les terminant. Une valeur n'est jamais prise sur la ligne de commande, que le
shell garde dans son historique, et la commande refuse un agent, comme `config get`. Elle demande avant
de les envoyer, dans votre terminal même avec `--yes` ; un script a besoin de `--yes`. Chaque valeur est
cherchée exactement telle qu'elle est écrite : citez chaque forme qui doit disparaître, et aussi le
domaine d'entreprise d'une adresse, que le coffre-fort garde comme une valeur à part. Chaque appel, de
1 000 valeurs au plus, est une entrée `key.forgotten` avec le nombre de valeurs demandées, oubliées et
absentes du coffre-fort, jamais une valeur. Cela fonctionne aussi sur une clé désactivée. Une
application fait de même avec un jeton à qui vous avez donné `--forget` (`keys add-token "Shop" erasure
--forget`), qu'aucune autre permission n'inclut : voir [oublier des valeurs](../../applications/forget/).

## Lire le journal d'administration

`latet server log --limit 100000 --json` lit les pages successives, du plus récent au plus ancien,
puis par id décroissant lorsque les horodatages sont identiques. `--limit` accepte au maximum
1 000 000 d'entrées. Chaque requête en demande au plus 10 000 ; une page plus courte peut encore
avoir une suite, car les pages ont aussi une limite en octets. La sortie JSON ne contient que des
entrées, une par ligne ; les avis sont écrits sur stderr.

`--since` inclut sa borne et `--until` l'exclut. `--actor` sélectionne une identité ou un jeton
d'administration ; `--action` sélectionne une action ou un préfixe littéral terminé par `*`.
Les actions nomment ce qui a changé (`person.added`, `code.issued`, `token.added`, `token.revoked`,
`key.rotated`, `key.updated`, `settings.changed`, `shared.changed`, `admin-token.issued`,
`admin-token.revoked`), et les lectures sont enregistrées sous le nom de l'appel (`status.read`,
`log.read`) : `--action 'admin-token.*'` sélectionne tous les changements des jetons d'administration.

Les jetons d'une clé d'API sont nommés par leur identifiant, jamais par leur secret ni par leur nom :
`token.added` donne le nouveau jeton et ses permissions, `key.rotated` le jeton qui reste (`token`) et
celui qu'il a remplacé (`replaced`), et `key.updated` un changement comme la limite du coffre-fort de
la clé (`vault_limit`). `apps.unmasked` est une restauration par
l'[API pour les applications](../apps-api/) : le jeton, le nombre de textes et le nombre de valeurs
restaurés, jamais un texte ni une valeur. `--action apps.unmasked` liste qui a restauré, et quand.
`key.forgotten` et `apps.forgotten` sont des valeurs retirées du coffre-fort d'une clé d'API, par
`keys forget` et par l'API pour les applications : le nombre de valeurs demandées, oubliées et absentes,
et pour l'API le jeton, jamais une valeur.
Pour reprendre, passez le curseur du journal affiché à `--cursor`, avec exactement les filtres
d'origine. Utilisez des dates absolues ou des secondes epoch exactes pour reprendre plus tard :
une durée relative comme `7d` change entre les exécutions. Ne remplacez jamais `--until` par un
horodatage arrondi de la sortie texte. Un curseur indique une position ; ce n'est ni un identifiant
d'accès ni une preuve que le journal est inchangé.

Chaque exécution est limitée à 1 000 requêtes de pages, 128 MiB de corps reçus, y compris les
réponses des nouvelles tentatives, 128 MiB de sortie UTF-8 produite et 300 secondes de lecture
et de traitement. Le JSON échappé peut être plus volumineux que le corps reçu. Les pages sont
validées et préparées avant leur affichage ; une page qui dépasse le budget restant n'est pas
affichée en partie. Ces contrôles de temps ne peuvent pas interrompre un tube de sortie bloqué du système d'exploitation.

Une limite atteinte, une page invalide ou un échec du serveur termine l'exécution avec un code
non nul, indique le nombre d'entrées affichées et signale que la lecture est incomplète. Un curseur
de reprise validé, lorsqu'il est disponible, pointe après la dernière page affichée. Si la première
page dépasse le budget de sortie, demandez moins de lignes. Un refus tel que `log_entry_too_large`
ne saute pas l'entrée concernée. Atteindre le nombre de lignes demandé est une fin normale ; l'avis
précise s'il reste une suite.

La première page fixe une limite d'insertion : les entrées ajoutées ensuite, même antidatées,
ne rejoignent pas cette lecture. La conservation ou la suppression peuvent cependant retirer des
entrées non encore lues. L'épuisement signifie seulement qu'aucune autre ligne correspondante
n'était visible lors de ces lectures ; ce n'est ni un export immuable ni une vérification de la
chaîne. Utilisez `log verify` séparément, avec la limite sur la suppression des dernières entrées décrite plus haut.

## Métriques des images non lues

`latet_ocr_unread_total{reason}` compte les occurrences d'images dont le résultat
OCR final est non lu sur chaque instance du serveur, qu'une requête vérifiée soit
refusée ou qu'une image réécrite soit retenue. Sa seule étiquette est `reason`,
avec cinq valeurs normales :

| Motif | Signification |
| --- | --- |
| `busy` | L'identité, le lecteur ou le pool n'a pas admis l'image dans la limite d'attente ou de file. |
| `too_long` | La lecture a dépassé son budget de temps. |
| `too_many_tiles` | L'image nécessitait plus de tuiles que l'OCR n'en autorise. |
| `too_wide` | Une ligne de texte a dépassé la largeur limite du reconnaisseur. |
| `unavailable` | L'OCR était en chargement, n'a pas pu se charger ou a rencontré une autre erreur d'exécution prise en charge. |

Une occurrence ultérieure de la même image originale compte à nouveau si elle
reçoit un échec OCR en cache. Le compteur mesure les occurrences d'images non lues,
pas les appels aux modèles. Les nouvelles tentatives internes, tuiles, lots et
réparations de lecteurs sans image soumise n'ajoutent rien. Sont exclus l'OCR réussi,
la rétention manuelle, les erreurs de décodage ou de politique de pixels, l'OCR
désactivé ou absent, la répétition exacte d'une image peinte ou d'un espace réservé
de Latet et l'OCR local d'un ordinateur. Les vérifications finales du même travail
ne comptent pas à nouveau. Aucune étiquette ne contient d'image, de chemin ou d'identité.
