---
title: L'API pour les applications
description: Analyser, masquer et restaurer du texte depuis vos propres programmes, avec une clé d'API d'un serveur Latet.
sidebar:
  label: Vue d'ensemble
---

Latet protège ce qu'un agent envoie à un modèle. Mais les programmes envoient aussi du texte sensible
ailleurs : leurs journaux à Datadog, Splunk ou Elastic, leurs documents à l'index d'une application
RAG. L'API pour les applications permet à un programme de demander à un serveur Latet, avec le jeton
d'une clé d'API :

- d'**analyser** un texte (`scan`) : ce qu'il contient de sensible, et où ;
- de le **masquer** (`mask`) : le même texte, avec des valeurs factices réalistes à la place de ses
  valeurs ;
- de le **restaurer** (`unmask`) : les vraies valeurs à nouveau à la place des valeurs factices ;
- d'**oublier** des valeurs (`forget`) : les retirer du coffre-fort de la clé, quand une personne
  demande que ses données disparaissent ([oublier des valeurs](forget/)).

C'est une fonction du serveur Latet : les clés d'API et leurs jetons n'existent que là, et une machine
sans serveur n'en a rien. Pour l'essayer sur votre propre machine, lancez-y un serveur Community avec
le profil serveur de `compose.yaml` ([exploitation du serveur](../concepts/server-operations/)).

Ce qu'elle apporte de plus qu'une règle de caviardage :

- **La même valeur factice à chaque fois.** Une valeur reçoit la même valeur factice à chaque appel,
  sur chaque instance du serveur, tant que le coffre-fort de la clé la conserve. Dans un service de
  journaux, les graphiques et les recherches fonctionnent toujours, et l'on peut suivre un client sur
  des jours de journaux sans que personne voie qui il est. Dans un index RAG, une personne reste la
  même personne d'un document à l'autre.
- **Un seul coffre-fort pour tout le flux.** Les documents et les questions masqués avec la même clé
  partagent leurs valeurs factices : un modèle raisonne sur des valeurs factices cohérentes, et
  `unmask` remet les vraies valeurs dans sa réponse.
- **Réversible, sous contrôle.** La restauration est une permission à part, elle n'atteint que les
  valeurs factices de la clé, et chaque restauration par l'API pour les applications est inscrite
  dans le journal d'administration du serveur.

Deux pages suivent les deux usages pour lesquels elle a été conçue : les [journaux](logs/) et le
[RAG](rag/). La [référence HTTP](../reference/apps-api/) donne le contrat pour les programmes dans
n'importe quel langage ; les programmes Python peuvent utiliser le paquet `latet-client`.

## Un premier appel

Un programme envoie une liste de textes et reçoit une liste en retour, dans le même ordre :

```sh
printf 'Authorization: Bearer %s' "$(cat shop.key)" | curl -sS -H @- \
  -H "Content-Type: application/json" \
  -d '{"texts": ["Remboursez la commande de mario.rossi@acme.it sur IT60X0542811101000000123456, merci."]}' \
  https://latet.acme.lan/_latet/apps/v1/mask
```

```json
{"results": [{"text": "Remboursez la commande de tommaso.gallo@margie-6e5a.example sur IT17F3945471087320356678487, merci.",
  "replacements": [{"type": "EMAIL", "class": "pii", "start": 26, "end": 59},
                   {"type": "IBAN", "class": "pii", "start": 64, "end": 91}],
  "counts": {"EMAIL": 1, "IBAN": 1}}]}
```

`shop.key` contient le premier jeton de la clé, que `keys add` écrit ([plus bas](#clés-dapi-et-jetons)).
`printf`, que le shell exécute lui-même, passe l'en-tête à curl sur son entrée standard (`-H @-`) :
ainsi le jeton n'apparaît pas dans la ligne de commande de curl, où d'autres utilisateurs de la
machine pourraient le lire. Les valeurs factices sont celles de la clé : une autre clé, ou un autre
serveur, en donne d'autres. Avec un jeton qui peut restaurer, le même texte envoyé à
`/_latet/apps/v1/unmask` revient tel qu'il était, avec `"restored": 2`.

## Clés d'API et jetons

Une clé d'API est un espace sur le serveur : ses règles, son coffre-fort de valeurs factices, son
historique. Elle peut avoir plusieurs **jetons**, les secrets qui agissent dans cet espace, et chaque
jeton a ses propres **permissions** :

| Permission | Ce que le jeton peut faire |
| --- | --- |
| `scan` | Dire ce que contient un texte et où ; il ne change rien. |
| `mask` | Remplacer les valeurs d'un texte par les valeurs factices de la clé. Un jeton qui peut masquer peut aussi analyser. |
| `unmask` | Remettre les vraies valeurs à la place des valeurs factices de la clé. |
| `forget` | Retirer des valeurs du coffre-fort de la clé ([oublier des valeurs](forget/)). Aucune autre permission ne l'inclut. |

Donnez à chaque programme seulement ce dont il a besoin. Une application qui expédie ses journaux a
besoin de `mask` ; l'outil de support qui lit une ligne copiée depuis le service de journaux a besoin
de `unmask` ; une application RAG qui masque ses documents et restaure la réponse du modèle a besoin
des deux.

```sh
latet server --server https://latet.acme.lan keys add "Shop" --out shop.key
latet server --server https://latet.acme.lan keys add-token "Shop" shipper --mask --out shipper.token
latet server --server https://latet.acme.lan keys add-token "Shop" support --unmask --out support.token
latet server --server https://latet.acme.lan keys tokens "Shop"
```

Le premier jeton d'une clé, celui que crée `keys add`, a `scan`, `mask` et `unmask`, tout comme le
jeton de chaque clé créée avant que les jetons aient des permissions : ces clés fonctionnent comme
avant. Aucun d'eux ne peut oublier : `forget` se donne à un jeton à part
(`keys add-token "Shop" erasure --forget`).
Le premier jeton de la clé a aussi une adresse pour les modèles, la
[passerelle](../getting-started/server/#donner-une-clé-dapi-à-un-programme), et sert par elle les
agents de la clé : `keys tokens` le signale par `serves agents (has an address)`, pour qu'il ne soit
pas révoqué à la place d'un jeton ajouté avec les mêmes permissions. Un jeton créé avec `keys add-token` n'a pas d'adresse, quelles que soient ses
permissions, `--mask --unmask` compris : la passerelle et le bridge du plugin le refusent (403), et il
appelle uniquement l'API pour les applications.

Chaque jeton s'arrête séparément : `keys tokens "Shop" --revoke t_4kxq2m7zap` en révoque un par
l'identifiant que `keys tokens` affiche, en quelques secondes sur chaque instance. `keys rotate "Shop"
--token t_4kxq2m7zap --out shipper.token` remplace ce jeton par un nouveau, avec un nouveau secret et
un nouvel identifiant, le même nom et les mêmes permissions ; une clé qui n'a qu'un jeton n'a pas
besoin de `--token`. Chaque changement figure dans le journal d'administration (`token.added`,
`token.revoked`, `key.rotated`), avec les identifiants des jetons, jamais un secret.

L'API pour les applications n'accepte que les jetons des clés d'API : le jeton de la machine d'une
personne est refusé (403), tout comme une requête faite par une page web. Les personnes utilisent
leur tableau de bord.

## Prose et données

`scan` et `mask` prennent un `kind` pour les textes d'un appel :

- `prose` (par défaut) : du texte écrit par des personnes, comme des messages, des tickets et des
  documents. Il est masqué comme la passerelle masque vos propres messages.
- `data` : des journaux, des enregistrements, la sortie d'un programme. Elles sont masquées comme la
  passerelle masque la sortie d'un outil.

La différence tient à ce qui est probable : dans le profil `balanced`, les noms ne sont cherchés que
dans la prose (avec le [modèle local de noms](../concepts/server-operations/#modèles-de-reconnaissance-sur-un-serveur)
installé sur le serveur), et une suite de chiffres ne compte comme numéro de téléphone que dans la
prose, alors que dans un journal c'est bien plus souvent un identifiant. Avec `phone_regions: [IT]`
dans les règles de la clé, le numéro dans `Appelez-moi demain au 3471234567.` reçoit une valeur factice
en prose et reste tel quel en données. Un document JSON envoyé comme texte est masqué comme du texte,
sa mise en forme conservée, de sorte que les positions de la réponse sont des positions dans ce
document.

## Ce que lit un masquage

`scan` et `mask` utilisent les réglages de la clé tels qu'ils sont à l'arrivée de l'appel : les règles
propres de la clé par-dessus les réglages partagés du serveur, comme le fait la passerelle pour les
requêtes de la clé. Il n'existe pas de règles pour la seule API. On les règle avec
`latet server config --key` et `latet server rules --key`
([réglages partagés et règles des clés d'API](../getting-started/server/#définir-les-réglages-partagés-et-les-règles-des-clés-dapi)).

- **Le relecteur local**, quand les règles de la clé en désignent un, lit chaque texte d'un appel
  `mask` avant la réponse, quoi que dise `llm_review.inline` pour la passerelle : une application n'a
  pas de moment ultérieur. Il dispose de `llm_review.budget_ms` s'il est réglé, sinon de 30 secondes.
  Un texte dont la relecture échoue suit `llm_review.on_failure` : avec `stop` (par défaut) la réponse
  est `blocked`, avec `send` c'est le texte tel que les règles l'ont masqué. Avec un relecteur à
  l'arrêt, chaque texte est donc bloqué jusqu'à ce qu'il réponde à nouveau.
- **`scan` n'exécute pas le relecteur local.** Le relecteur apprend au coffre-fort ce qu'il trouve, et
  l'analyse ne change rien. Sur une clé avec un relecteur, `scan` peut signaler moins que ce que `mask`
  remplace.
- **Le Relecteur en direct ne retient jamais le texte d'une application.** Une application ne peut
  pas attendre une personne : un texte est masqué par les règles et le relecteur local seuls, même
  quand les règles de la clé activent le Relecteur en direct.
- **Le garde-fou anti-extraction** s'applique aux textes `data` comme à la sortie d'un outil : un
  texte qui contient plus de valeurs personnelles que `dump_guard` n'en permet reçoit `blocked`.

Un texte bloqué revient sous la forme `{"blocked": "<raison>"}` sans texte, et la raison ne nomme
aucune valeur. Quand quelque chose a fonctionné en mode réduit pendant l'appel (un détecteur qui n'a
pas pu lire un texte, une relecture qui n'a pas abouti), la réponse le dit dans `"degraded"`, avec le
nom de ce qui était concerné, comme `ner` ou `llm-review`.

## Masqué deux fois, restauré deux fois

`mask` tient pour réelle chaque valeur qu'il trouve, y compris les propres valeurs factices de la
clé : personne ne peut dire, à partir d'une chaîne, si c'est une valeur factice ou une vraie valeur
qui lui ressemble. Un texte masqué deux fois contient des valeurs factices de valeurs factices, et une
restauration le ramène d'un pas en arrière :

| Étape | Texte |
| --- | --- |
| Original | `Écrivez à mario.rossi@acme.it` |
| Masqué | `Écrivez à tommaso.gallo@margie-6e5a.example` |
| Masqué à nouveau | `Écrivez à simone.desantis@humongous-0e54.example` |
| Restauré une fois | `Écrivez à tommaso.gallo@margie-6e5a.example` |

Masquez un texte une seule fois, à partir de son original. La passerelle suit la même règle : un texte
masqué par l'API puis envoyé à un modèle par la passerelle de Latet y est masqué à nouveau, et la
réponse du modèle revient avec les valeurs factices de l'API. C'est pourquoi une application RAG
envoie le texte masqué directement au modèle et restaure la réponse par l'API ([RAG](rag/)).

## Qui peut apprendre quoi

**La restauration** donne les vraies valeurs des valeurs factices de la clé, quel que soit l'appel ou la
conversation de la clé qui les a créées, même celles de l'an dernier. Elle n'atteint ni les valeurs
factices d'une autre clé ni celles d'une personne : le texte qui n'est pas une valeur factice de la clé
reste tel quel. Le nom d'une personne ne revient qu'entier : une de ses parties seule (un prénom) peut
être un mot ordinaire, ou le nom d'une autre personne, et reste tel quel. Chaque restauration est
inscrite dans le journal d'administration avant l'envoi de sa réponse (`apps.unmasked`, avec le jeton,
le nombre de textes et le nombre de valeurs ; jamais un texte ni une valeur), et un journal qui ne peut
pas l'inscrire refuse l'appel (503). Le premier jeton de la clé, qui peut analyser, masquer et
restaurer, sert aussi des agents par la passerelle : leurs réponses sont restaurées comme celles de tout
agent, sans entrée du journal d'administration pour chaque restauration. Un jeton créé avec
`keys add-token` ne le fait jamais : chacune de ses restaurations est une entrée `apps.unmasked`.
Donnez `unmask` aux outils des
personnes qui peuvent voir les vraies données, et lisez le journal avec
`latet server log --action apps.unmasked`.

**Le masquage aussi révèle quelque chose.** Avec un jeton qui peut masquer et un accès aux journaux
masqués, quelqu'un peut confirmer une supposition : masquer `mario.rossi@acme.it` et chercher sa
valeur factice dans les journaux. Si elle y est, Mario Rossi est un client. Donnez donc `mask` aux
seuls programmes qui en ont besoin, gardez le jeton là où ils gardent leurs secrets, et traitez les
journaux masqués comme des données pseudonymes, non anonymes.

**L'analyse aussi révèle quelque chose.** Elle ne crée aucune valeur factice et ne change rien, mais
elle signale les valeurs que le coffre-fort de la clé connaît déjà, où qu'elles apparaissent. Un jeton
qui peut seulement analyser peut donc confirmer qu'une valeur qu'aucun détecteur ne trouverait seul
(un nom dans un texte `data`, une valeur qu'une règle de la clé a trouvée avant d'être retirée) est
l'une de celles de la clé. Donnez `scan` avec le même soin que `mask`.

**L'oubli aussi révèle quelque chose.** Sa réponse dit combien des valeurs le coffre-fort de la clé
contenait : un jeton qui peut oublier peut donc confirmer qu'une valeur est à la clé, comme un jeton
qui peut analyser. Donnez `forget` seulement au processus qui traite les demandes d'effacement ;
chaque oubli figure dans le journal d'administration (`apps.forgotten`, avec le jeton et les nombres,
jamais une valeur).

## Le coffre-fort d'une clé

Le coffre-fort d'une clé conserve chaque valeur qu'il a masquée, avec sa valeur factice, tant que la clé
existe : une restauration fonctionne sur une ligne de journal de l'an dernier, et un client garde la
même valeur factice pendant des années. Les valeurs n'expirent pas : une valeur quitte le coffre-fort
quand elle est oubliée ([oublier des valeurs](forget/)), et toutes partent avec la clé, puisque
`latet server keys remove` efface son espace, et son coffre-fort avec. Une valeur trouvée une fois est masquée
ensuite partout où elle apparaît, même là où aucun détecteur ne la trouverait à nouveau : l'adresse d'un
client qu'une règle de la clé a trouvée une fois, puis qui a été retirée, reste masquée.

Un espace de journaux peut contenir des millions de valeurs : une instance ne garde donc pas le
coffre-fort d'une clé en mémoire. Elle garde les valeurs que la clé a utilisées en dernier
(`key_working_values`, 5 000 par défaut, environ 10 Mio par clé) et trouve toute autre valeur dans
PostgreSQL quand les textes d'un appel en ont besoin, avec une seule recherche pour tous, grâce à un
index des mots des valeurs et des valeurs factices. Cet index contient des empreintes calculées avec
une clé secrète, jamais un mot en clair.

**Secrets collés à d'autres lettres.** Dans PostgreSQL, un secret écrit comme une seule suite de
lettres et de chiffres, sans espace ni ponctuation, est trouvé par son mot, et par ses six premiers et
six derniers caractères quand d'autres lettres ou chiffres sont collés à l'un de ses côtés. Collé des
deux côtés (`xhunter2y`), ou collé de quelque façon que ce soit quand le mot à son bord a six
caractères ou moins, aucun mot du texte ne le trouve. Une instance garde donc en mémoire pour de bon
les secrets courts d'une clé, ceux d'une seule suite de 16 caractères ou moins ou avec un mot aussi
court à un bord : les `key_held_secrets` plus récents (1 000 si les réglages du serveur ne disent pas
autre chose), trouvés quelle que soit la façon dont ils sont collés. Un plus ancien est trouvé là où
rien ne lui est collé, ou là où un côté est libre et qu'il a sept caractères ou plus ; un plus long,
partout où un côté est libre. Augmentez `key_held_secrets` pour une clé qui voit beaucoup de secrets
courts, comme un jeton de session dans chaque ligne de journal : chacun occupe environ 2,2 Ko sur
chaque instance, et chaque texte que la clé masque ou restaure est parcouru pour le chercher, ce qui
ralentit un peu ses appels. Mesurée comme ci-dessous avec un jeton de session propre à chaque ligne
(une valeur de plus à garder dans chacune), une nouvelle clé qui en gardait 1 000 a masqué 73 lignes
par seconde et en a restauré 1 935, contre 75 et 2 103 sans aucun secret gardé. Une
valeur factice collée de sorte qu'aucun de ses mots ne soit isolé n'est pas trouvée non plus ; le
masquage n'en produit jamais. Comme partout dans Latet, une valeur est reconnue à nouveau à partir de
quatre caractères, le nom d'une personne à partir de trois
([valeurs connues](../concepts/detection/#valeurs-connues)).

**Ce que conserve un appel qui échoue.** Un appel `mask` qui échoue pendant le masquage (un 507, une
validation qui n'atteint pas PostgreSQL) ne conserve aucune de ses nouvelles valeurs quand il tenait
pour elles l'une des transactions de l'instance, trois à la fois avec le `database_pool` par défaut :
l'espace de la clé les oublie. Quand celles-ci étaient prises, l'appel a conservé chaque nouvelle
valeur au fur et à mesure, et celles-ci restent dans le coffre-fort de la clé. Une erreur après le masquage, dans la relecture locale ou en préparant la
réponse, survient après la validation : les nouvelles valeurs de l'appel restent aussi. Masquer à
nouveau les textes est sans risque dans tous les cas : une valeur que le coffre-fort a conservée garde
sa valeur factice, et une valeur qu'il a oubliée en reçoit de nouveau une. Un agent de la même clé qui
a utilisé entre-temps une valeur oubliée par la passerelle a déjà envoyé sa valeur factice, qu'aucune
restauration ultérieure ne remplacera.

## Limites

- **Le coffre-fort.** Le coffre-fort d'une clé contient les `vault_limit` valeurs du serveur (200 000
  si les réglages du serveur ne disent pas autre chose), ou une limite propre :
  `latet server keys limit "Shop" 5000000` (de 1 000 à 100 000 000 ; `default` lui rend celle du
  serveur). Un coffre-fort plein refuse avec 507 l'appel `mask` qui ajouterait une valeur : rien de
  l'appel n'est renvoyé, rien ne sort en clair. L'état du serveur liste les identités à 90 % de leur
  limite, et la métrique `latet_vault_near_limit` les compte : relevez donc une limite avant qu'elle
  soit atteinte. Chaque instance compte les nouvelles valeurs d'un appel avant de les valider : deux
  instances, ou un appel et un agent de la même clé, peuvent dépasser la limite des nouvelles valeurs
  d'un appel chacun avant que la valeur suivante soit refusée.
- **Un appel** prend au plus 1 000 textes et l'`apps_request_mb` du serveur (8 Mio si ses réglages ne
  disent pas autre chose, jamais plus que son `max_request_mb`) : un appel attend en mémoire le thread
  de la clé, et `latet-client` découpe une liste plus longue en appels de 8 Mio au plus. Les textes dont les mots nomment plus de 100 000 valeurs ou valeurs factices
  du coffre-fort sont refusés avec 422, sans rien masquer : envoyez moins de textes par appel.
- **Appels simultanés.** Une clé a au plus `requests_in_flight` requêtes d'applications sur une
  instance (16 si les réglages ne disent pas autre chose) ; au-delà, 429 avec `Retry-After`.

## À quelle vitesse

Les appels d'une clé s'exécutent l'un après l'autre sur une instance, dans le fil de son espace ; les
appels de clés différentes, et ceux qui arrivent sur des instances différentes, s'exécutent côte à
côte. Pour aller plus vite avec une clé, faites tourner plus d'instances derrière le répartiteur de
charge.

Mesuré sur une instance (Intel i9-10850K, 64 Go, Windows 10, PostgreSQL dans Docker Desktop sur la
même machine), avec des lignes de journal d'environ 200 caractères, chacune avec une adresse e-mail et
une adresse IPv4 publique, dont la moitié nomme un client déjà vu, 500 lignes par appel en `data` ; et
avec des documents d'environ 20 Ko de prose contenant dix valeurs chacun, un par appel :

| Le coffre-fort de la clé | Masquage, lignes par seconde | Restauration, lignes par seconde | Un document, masquage / restauration |
| --- | ---: | ---: | ---: |
| Nouveau, apprend ses valeurs au fil de l'eau | 189 | 2 691 | 77 / 28 ms |
| 200 000 valeurs | 440 | 1 691 | 87 / 30 ms |
| 1 000 000 de valeurs | 410 | 1 498 | 95 / 32 ms |

Le masquage est limité par les détecteurs : sur un fil, avec toutes les valeurs en mémoire et sans
base de données, le moteur masque environ 900 lignes de ce type par seconde. Une clé nouvelle écrit
aussi chaque nouvelle valeur dans PostgreSQL. Une instance garde au plus `database_pool` connexions à
PostgreSQL (10 si les réglages du serveur ne disent pas autre chose) pour le travail de toutes les
identités, et laisse trois dixièmes d'entre elles (trois sur dix) être tenues par des appels qui
conservent leurs nouvelles valeurs dans une transaction chacun ; les autres conservent chaque valeur au
fur et à mesure, plus lentement. Augmentez `database_pool` pour une instance qui masque pour beaucoup
de nouvelles clés à la fois, et avec lui le `max_connections` de PostgreSQL
([exploitation du serveur](../concepts/server-operations/#connexions-limites-et-confiance)).
`scripts/bench_apps.py` dans le dépôt mesure l'un de vos serveurs de la même façon.

## Mettre à jour un serveur

Après une mise à jour qui change la façon dont les mots d'un coffre-fort sont indexés, la première
instance qui construit l'espace d'une clé les refait avant de répondre au premier appel de la clé :
environ 40 secondes pour 200 000 valeurs et quatre minutes et demie pour 1 000 000 sur la machine
ci-dessus. Un appel qui arrive entre-temps attend, et une instance construit quatre espaces à la fois :
quelques grosses clés retardent donc les premiers appels des autres clés sur cette instance. Le journal
des modifications dit quand une version le fait ; envoyez alors à chaque grosse clé un appel, avec un
délai d'attente assez long, avant que ses programmes ne redémarrent.

Tant que des instances de deux versions tournent côte à côte, une valeur qu'écrit une instance plus
ancienne est trouvée par les plus récentes tant qu'elles la gardent en mémoire, mais pas dans
PostgreSQL avant que l'espace de la clé soit reconstruit. Quand l'API pour les applications est
utilisée, arrêtez chaque instance plus ancienne avant que les nouvelles démarrent, même quand les
tables ne changent pas
([mettre à jour](../concepts/server-operations/#mettre-à-jour-et-ajouter-des-instances)).
