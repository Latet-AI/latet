---
title: Journal des modifications
description: Les modifications notables de Latet, les plus récentes d'abord.
---

Latet suit le [versionnage sémantique](https://semver.org). Les modifications qui ne sont pas encore
dans une version sont listées sous **Non publié** ; chaque version liste ses modifications, les plus
récentes d'abord.

Cette page reprend les modifications non publiées et les versions à partir de la 0.10.2. Les versions
précédentes, de la 0.10.0 en arrière, sont dans le
[journal des modifications en anglais](../../changelog/#0100-2026-10-27).

## Non publié

Ce qui vient ensuite est dans la [feuille de route](../roadmap/) : un refus clair de la part du mode
auto, un programme d'installation signé pour Windows, vos propres modèles de noms, d'autres agents, et
la documentation dans d'autres langues.

## 0.11.0 (2026-11-05)

Latet 0.11.0 bloque les requêtes par défaut lorsqu'une partie du chemin, des paramètres de requête,
des en-têtes ou du corps n'est ni inspectée ni explicitement autorisée. Un corps vide ne suffit pas
pour autoriser une requête. Les identifiants nécessaires du fournisseur et les composants signés sont
préservés selon les règles définies. Vérifiez vos routes et vos exceptions limitées avant la mise à
niveau ; les appels auparavant envoyés sans inspection peuvent désormais être bloqués. Le tableau de
bord indique les parties non inspectées explicitement autorisées. Un refus ne dit à l'agent que rien
n'a été envoyé que lorsque rien n'est parti : quand une seconde tentative est refusée après qu'une
première a atteint le fournisseur, l'agent apprend qu'aucune autre tentative n'a été envoyée et que
la première a pu arriver. [Passer à la 0.11](../getting-started/upgrading-to-0-11/) dit ce qu'il faut
vérifier d'abord, et `latet doctor` liste ce qui s'arrêtera.

- **La même chose sur un ordinateur et sur un serveur.** Un ordinateur refuse maintenant ce qu'un
  serveur Latet refusait : une requête dont Latet ne lit pas le corps, un contenu sous une forme qu'il
  ne lit pas, une trame WebSocket qui n'est pas un tour. Les définitions d'outils de Claude Code sont
  aussi vérifiées sur un ordinateur. Sur un ordinateur, le corps d'une requête est limité à 32 Mio.
- **Les appels sans corps.** La liste des modèles et la lecture d'un modèle passent chez chaque
  fournisseur que Latet connaît, profils d'inférence de Bedrock compris, et leurs paramètres de
  requête sont lus. Tout autre appel est refusé, sans nommer aucune de vos routes : fichiers,
  téléversements, lots, audio, génération d'images, modération, une réponse enregistrée, les lectures
  d'un cache de Gemini.
- **Les en-têtes et les paramètres de requête.** Chacun est lu selon une règle propre. Un en-tête
  qu'aucun profil ne nomme est refusé, et son nom n'est donné qu'à l'agent. Les en-têtes de
  télémétrie et de traçage des SDK (`x-stainless-*`, `traceparent`, `amz-sdk-*` du SDK d'AWS,
  `x-goog-api-client`), les identifiants des sous-agents de Claude Code et les valeurs qu'il copie de
  son environnement (`x-claude-code-agent-id`, `x-claude-remote-session-id`...), ainsi que
  `x-codex-turn-metadata` et `x-openai-subagent` de Codex, sont retirés et comptés dans le tableau de
  bord, et un `401` ou un `403` qui suit le dit ([la liste](../getting-started/upgrading-to-0-11/#les-en-têtes-que-latet-retire)). Sur une requête que Latet ne
  fait que vérifier, le corps part toujours octet pour octet. Un corps de requête compressé part
  compressé à nouveau à partir de ce que Latet a lu, jamais tel qu'il est arrivé : un nom de fichier
  dans son en-tête gzip ou des octets après ses données compressées n'atteignent donc jamais le
  fournisseur ; sur une requête que Latet ne fait que vérifier, un corps compressé est refusé, avec
  des mots qui disent de l'envoyer non compressé. Un paramètre de requête qu'aucun profil ne nomme est refusé, et son
  nom n'est donné qu'à l'agent. Une requête refusée pour un en-tête, un paramètre de requête ou sa
  route figure dans l'historique avec cette catégorie, sans en-tête ; un WebSocket refusé ainsi est
  fermé avec le code 1008 et une raison qui dit la même chose. Les en-têtes que
  vous définissez pour un agent (`ANTHROPIC_CUSTOM_HEADERS`, `http_headers` de Codex,
  `COPILOT_PROVIDER_HEADERS`) sont refusés tant qu'une exception ne les nomme pas.
- **Chaque membre du corps.** Un membre qu'aucun profil ne nomme est refusé, avec l'endroit où il se
  trouve, jamais son nom ; sur une requête que Latet ne fait que vérifier (Claude Code, Bedrock,
  Vertex), un nouveau membre qui contient du texte passe une fois vérifié, affiché comme *seulement
  vérifiée*. `client_metadata` de Codex est retiré avant que la requête soit masquée. Les
  identifiants de modèle passent sous les formes documentées par leurs fournisseurs ; un ARN de
  Bedrock, un projet Vertex, le nom de modèle propre à une passerelle ou une `api-version` est d'abord
  listé dans `request_resources`. Sur OpenRouter, un modèle passe sous la forme `anthropic/`,
  `openai/` ou `google/` suivi d'un modèle de ce fournisseur, ou `openrouter/auto` ; le modèle d'un
  autre fournisseur y est aussi listé d'abord.
- **Ce que le fournisseur a renvoyé.** Les signatures du raisonnement, le raisonnement chiffré, les
  compactages et les résultats de recherche web passent quand Latet les a vus arriver du fournisseur
  dans cette conversation, sur n'importe quelle instance d'un serveur ; après un redémarrage du daemon
  d'un ordinateur, une règle intégrée laisse passer les champs chiffrés du fournisseur lui-même,
  seulement dans les tours du modèle et seulement vers l'adresse du fournisseur. Une conversation par
  une passerelle d'entreprise qui relaie les signatures du raisonnement est refusée après un
  redémarrage.
- **Les outils et le code de Google pour Gemini.** Le code que Google exécute pour le modèle et ce
  que ce code a affiché, ainsi que les entrées et les sorties des outils intégrés de Google (Google
  Search, contexte d'URL, Maps, File Search), sont désormais masqués, comme l'appel d'une fonction
  et son résultat ; la 0.10 les laissait tels qu'ils avaient été écrits. La réflexion du modèle
  reste toujours telle que le modèle l'a écrite.
- **Images et documents.** Une requête avec une image n'est envoyée qu'une fois que Latet en a lu le
  texte : sans la reconnaissance de texte, ou avec `ocr: off`, elle est refusée avec des mots qui
  nomment `latet ocr install`. `images: allow` n'envoie plus une image que Latet n'a pas lue, et les
  documents, l'audio et les images désignées par leur adresse sont refusés. Sur une requête que Latet
  ne fait que vérifier, chaque octet d'une image doit être expliqué : la position d'une photo, les
  notes du fabricant d'un appareil photo, une miniature, une seconde image et la vidéo d'une photo
  animée sont refusées, avec la façon de les retirer, et une image WebP d'origine est refusée ; les
  codes QR ne sont pas décodés. Les requêtes que Latet réécrit envoient les images réécrites, sans
  leurs métadonnées.
  Sur un serveur dont la reconnaissance de texte n'est pas disponible (elle n'a pas pu se charger),
  `latet_ocr_unread_total{reason="unavailable"}` compte une fois chaque requête ou tour WebSocket
  refusé, quel que soit le nombre d'images qu'il porte.
- **Un projet où Latet est désactivé n'envoie rien.** Ses requêtes sont refusées, en HTTP et par un
  WebSocket, avec des mots qui disent comment y réactiver Latet ou joindre directement le fournisseur.
  `latet project off` et l'interrupteur du tableau de bord le disent d'abord.
  `latet serve --passthrough` n'existe plus : l'option cachée s'arrête avec un message.
- **Projets de confiance et déclarés.** Un dossier qu'une requête nomme (`x-latet-project`, le dossier de
  travail qu'écrivent Codex, OpenCode et Gemini CLI) ne fait qu'ajouter de la protection : les
  assouplissements et les exceptions de requête de son projet ne s'appliquent pas, et un projet
  déclaré qui est désactivé est refusé. Un projet est de confiance par le lien du plugin de Claude Code, qui
  prend fin après 30 minutes sans appel, ou par une clé de lancement. Les appels de Claude Code avant
  le premier appel du plugin sont jugés avec vos seules règles.
- **Clés de lancement.** `latet claude`, `latet codex`, `latet opencode`, `latet gemini` et
  `latet copilot` donnent à l'agent une clé secrète de ce lancement, dans son propre réglage
  d'en-têtes, que Latet vérifie et retire ; Codex sur ses fournisseurs intégrés, Codex avant la 0.115,
  OpenCode avant la 1.19.5, Gemini CLI en mode ACP, Copilot CLI hors de Chat Completions et
  `latet run` n'en passent aucune, et la commande qui les lance
  le dit en une phrase. Une clé valide dans la sortie d'un outil est masquée comme le jeton de Latet.
  Le tableau de bord montre pour chaque requête si son projet était de confiance ou déclaré, et son
  lancement : *Lancé par Latet, clé vérifiée*, *Lancé par Latet, clé manquante*, *Lié par le plugin
  Claude Code* ou *Non lancé par Latet* ; `latet doctor` nomme les agents vus sans leur clé, et un
  Codex, un OpenCode ou un Gemini CLI trop ancien pour en porter une.
- **Exceptions de requête.** `request_exceptions` nomme exactement ce qu'elle laisse passer (un
  en-tête, un paramètre de requête ou un membre du corps, sur une destination et un profil de
  requête), pour chacune de vos requêtes ou, sous un lien de confiance, pour un projet. Chacune attend votre
  approbation, dure 30 jours par défaut et 90 au plus, avec un avertissement sept jours avant sa fin,
  ou est rendue permanente exprès, marquée comme telle et listée par `latet doctor` et
  `latet server status`. Une révision de profil qui change ce qu'une exception nomme la suspend
  jusqu'à ce que vous l'approuviez de nouveau. Dans la 0.11.0, aucune exception ne s'applique à un
  WebSocket, ni à un membre du corps dans un élément du fournisseur ou la source d'une image, et aucune
  ne nomme une route entière : le tableau de bord n'en propose aucune là. Avec l'historique désactivé, les requêtes que
  chacune a laissées passer sont comptées en mémoire, et additionnées entre les instances d'un
  serveur. Un serveur tient dans PostgreSQL un index des exceptions approuvées de chaque personne, que
  `latet server status` lit : identifiants, types de portée, fins et états seulement, jamais ce
  qu'une exception nomme ni sa description.
- **Couverture visible.** Chaque échange du tableau de bord dit comment sa requête a été couverte :
  bloquée, chaque partie lue, ou laissée passer par une règle ou une exception nommée, avec les
  en-têtes et les membres que son profil a retirés et son profil de requête. Un échange enregistré
  avant la 0.11 n'est jamais affiché comme vérifié : il dit *Enregistrée par une version antérieure de
  Latet. La part de cette requête qu'elle a lue n'a pas été enregistrée : elle n'est donc pas indiquée
  comme vérifiée.* Pas plus qu'un échange qui a laissé passer une partie sans la lire, ni une
  vérification du plugin Claude Code.
- **Ce que gardent les journaux.** Les journaux, les métadonnées de l'historique et les
  enregistrements des décisions d'un serveur gardent des catégories fixes et des modèles de route,
  jamais le nom d'un en-tête ou d'un outil, une valeur ni le chemin d'une requête tel qu'il a été
  envoyé.
- `latet doctor`, et le premier démarrage du daemon en 0.11 dans son journal, listent par type et par
  nombre ce que la 0.11 arrête et que la 0.10 envoyait : les projets où Latet est désactivé, la
  reconnaissance de texte non installée ou désactivée, `--passthrough` dans une entrée qui démarre
  Latet, et les appels de votre historique qu'aucun profil ne couvre.

- Les dossiers des projets dans vos réglages sont enregistrés sous une forme canonique : barres
  obliques inverses et lettre de lecteur en majuscule sous Windows, et aucun séparateur final
  (`C:/work/shop/` est enregistré comme `C:\work\shop`). L'identifiant de chaque projet ne change pas,
  pas plus que les réglages qui s'y appliquent.

- Le coffre-fort d'une personne sur un serveur est gardé entier en mémoire quand il contient 35 000
  valeurs ou moins au moment où son espace est construit (environ 1,6 Ko par valeur sur chaque
  instance et chaque worker qui sert la personne, comptés dans la mémoire de l'espace) : les textes
  de la personne sont masqués sans demander leurs valeurs à PostgreSQL, environ deux fois plus vite
  qu'en les cherchant. Au-delà de 35 000 valeurs, l'espace d'une personne garde en mémoire les valeurs
  que la personne a utilisées en dernier (`key_working_values`) et trouve toutes les autres dans
  PostgreSQL, comme le fait celui d'une clé d'API : l'espace d'une personne de 200 000 valeurs occupe
  24 Mo, là où celui de la 0.10.0 en occupait 495. Dans les deux cas, chaque requête demande d'abord
  à PostgreSQL ce que les autres instances ont changé, de sorte qu'une valeur gardée ou oubliée sur
  une instance est vue par la requête suivante sur n'importe quelle autre
  ([Exploitation du serveur](../concepts/server-operations/)).
- **Un serveur masque plus vite.** Les détecteurs passent moins de motifs sur chaque texte, les textes
  d'un appel de l'API pour les applications sont lus ensemble, et une instance trouve les valeurs
  d'une clé d'API ou d'une personne grâce aux empreintes des mots de son coffre-fort gardées en
  mémoire (environ 15 Mo pour un million de valeurs), en ne demandant à PostgreSQL que les mots qui
  peuvent être une valeur ou une valeur factice. Mesuré sur une instance de la machine de la
  documentation, une clé d'API de 200 000 valeurs masque 871 lignes de journal par seconde et en
  restaure 2 960, là où la 0.10.0, mesurée de la même façon, en masquait 350 et en restaurait 2 114 ;
  une de 1 000 000 de valeurs 773 et 2 967, contre 304 et 2 014 ; une nouvelle clé en masque 191,
  contre 153. L'objectif de 1 000 lignes par seconde à ces tailles n'est pas atteint
  ([À quelle vitesse](../applications/#à-quelle-vitesse)).
- **La mise à jour arrête le serveur une fois.** La 0.11 ajoute à chaque ligne de mot d'un
  coffre-fort une place pour son empreinte et pour le numéro de la modification qui l'a écrite en
  dernier : arrêtez
  chaque instance et chaque worker, lancez `latet server migrate` une fois et démarrez-les avec la
  0.11. Un coffre-fort gardé par la 0.10 reçoit ses empreintes après le redémarrage, en arrière-plan,
  une fois, une instance à la fois, pendant que ses requêtes et ses appels sont servis par la
  recherche dans PostgreSQL, plus lentement, aucun refusé ; chaque instance construit ensuite son
  index des empreintes, en 1,5 seconde pour 1 000 000 de valeurs
  ([Mettre à jour un serveur](../applications/#mettre-à-jour-un-serveur)).
- `latet server migrate`, et `latet server init` sur des tables pour lesquelles une migration est
  due, ne changent rien tant qu'une instance du serveur est en vie, de la 0.10 ou de la 0.11, ou tant
  que Valkey ne répond pas : ils disent combien d'instances sont en vie et de quelle version, et se
  terminent avec 2. Une instance de la 0.10 compte comme en vie pendant 30 secondes après son arrêt.
  Une fois les migrations appliquées, ils interrogent de nouveau, et quand une instance est devenue en
  vie entre-temps ils le disent et se terminent avec une erreur (`migrate` 1, `init` 2) : une instance
  de la 0.10 laissée en marche sur les tables de la 0.11 continuerait à servir des requêtes sans les
  lire comme le fait la 0.11.
- `latet server status` liste une instance de Latet 0.10 encore en vie à côté de celles de la 0.11,
  par son identifiant, comme Latet 0.10 ou antérieure, et dit quand sa liste a été coupée à sa borne
  (`instances: the list is partial, cut at its bound: an instance may be alive and not listed`, et
  `instances_whole` false dans sa réponse).
- **Lire chaque partie d'une requête coûte du temps.** Mesuré sur une instance, en médiane, par
  rapport à la même version sans cette lecture : une petite requête de discussion prend environ 1 ms
  de plus sur un ordinateur (2,02 ms contre 0,94) et sur un serveur (6,99 ms contre 5,95) ; une
  requête de 4 000 emplacements prend 3,8 fois plus de temps sur un ordinateur (362 ms contre 96) et
  1,4 fois sur un serveur ; une requête de 500 textes déjà masqués et d'un nouveau 2,8 fois sur un
  ordinateur (36 ms contre 13) et 1,16 fois sur un serveur.
- `latet server run --workers N` (ou le réglage `workers`) exécute N instances du serveur sur une machine, sur
  le même port, chacune dans son propre processus, pour qu'un serveur utilise davantage de ses cœurs. Sous
  Linux, chaque worker écoute sur le port avec un socket à lui, et le noyau répartit les nouvelles connexions
  entre eux selon un hachage des adresses et des ports de chaque connexion : de nombreuses connexions se
  répartissent également, quelques connexions longues, comme celles d'un répartiteur de charge, peuvent encore
  tomber inégalement. Le processus qui lance les workers garde le port tout le temps, et seuls des programmes
  qui tournent sous l'utilisateur du serveur peuvent y écouter à côté d'eux : cet utilisateur n'exécute donc que
  Latet. Mesuré sous Linux par l'API pour les applications, huit workers masquent 6,28 fois ce qu'en masque un
  seul et restaurent 5,21 fois, et chacun occupe de 185 à 227 Mo avec l'espace d'une clé d'API de 200 000
  valeurs. Plusieurs workers sont refusés sous Windows, et sous macOS ils partagent un socket ([Exploitation du
  serveur](../concepts/server-operations/), Plusieurs instances sur une machine).
- Corrigé : `latet upgrade` ne nommait, dans ce qu'il installait, que la détection des noms et le texte
  des images, et uv, qui installe un outil avec exactement les composants optionnels que nomme son
  exigence, retirait les autres : un serveur installé avec `latet[server]` qu'il mettait à jour perdait
  psycopg et redis, et `latet server run` refusait alors de démarrer. La mise à jour nomme maintenant
  chaque composant optionnel qu'a le Latet installé, comme les liste l'enregistrement d'uv de son
  installation (là où cet enregistrement manque ou ne peut pas être lu, chaque composant dont les
  paquets sont tous installés), à côté de la détection des noms et du texte des images ; là où ces deux
  ne peuvent pas être installés, les autres restent. `--to` et la fenêtre propre à la mise à jour sous
  Windows les gardent aussi, de même que les scripts d'installation, relancés sur un Latet installé
  avec d'autres composants, comme uv les liste (un uv trop ancien pour les lister n'en donne aucun,
  comme avant).

  Une mise à jour exécute le code de la version installée, donc un Latet jusqu'à la 0.10.4 ne nomme
  encore que la détection des noms et le texte des images en passant à cette version. Un serveur en
  0.10.4 ou antérieure prend donc cette version une fois par un autre moyen : la commande avec laquelle
  il a été installé, qui nomme `server` et ses dossiers `UV_*`, ou le script d'installation récupéré de
  nouveau sur GitHub et lancé avec ces variables. Un serveur dont la mise à jour a déjà retiré `server`
  le récupère avec cette commande ([Exploitation du serveur](../concepts/server-operations/), Mettre à
  jour un Latet installé comme service système). Là où le composant manque, `latet server run` donne
  maintenant cette commande avec les autres composants dedans, et `latet upgrade` et le Dépannage
  disent quels composants nomme `LATET_SOURCE` pour une installation à la main.
- Corrigé : `latet upgrade` installait dans le dossier d'outils propre à uv, quel que soit le dossier
  du Latet qui tourne : un serveur installé avec un `UV_TOOL_DIR` à lui, comme le dit Exploitation du
  serveur, n'était jamais mis à jour, et un second Latet était installé et démarré dans le dossier
  personnel du compte qui lançait la commande. `latet upgrade` s'arrête maintenant là avant que rien ne
  change, et affiche la commande à lancer à la place, avec les dossiers qu'il a trouvés. Exploitation
  du serveur a une nouvelle section sur la mise à jour d'un Latet installé comme service système, et
  l'Installation y renvoie un serveur.

## 0.10.4 (2026-11-04)

- Corrigé : sous Windows, la détection locale des noms pouvait cesser de trouver des noms quand
  Latet tournait dans une application installée comme paquet, comme l'application de bureau de
  Claude. Windows garde les fichiers d'une telle application dans un dossier propre à l'application
  (`...\Packages\Claude_...\LocalCache\Local\latet`) et les montre à l'emplacement habituel : le
  dossier du modèle de noms pouvait donc se trouver à l'emplacement habituel alors que ses fichiers
  étaient dans le dossier de l'application, y compris quand on l'atteignait par un lien vers
  l'emplacement habituel. ONNX Runtime, à partir de la version 1.24 (une installation reçoit
  maintenant la 1.30.0), refusait alors de charger le modèle ("External data path escapes model
  directory"), et les requêtes continuaient avec les noms laissés en clair : seul le journal du
  daemon disait que le modèle local de noms n'avait pas pu être chargé. Latet ouvre maintenant le
  modèle depuis l'endroit où se trouve vraiment son fichier, et les noms sont de nouveau trouvés.
  Dans tous les autres cas, le modèle se charge comme avant.

## 0.10.3 (2026-10-29)

- Corrigé : sur un serveur Latet, un enregistrement des réglages qui vérifie les noms des destinations
  (depuis le tableau de bord ou l'administration) lançait la recherche du nom suivant après qu'une
  recherche avait épuisé tout le temps de l'enregistrement, quand l'horloge du système n'avait pas
  encore avancé (vu sous Windows). Il n'en lance plus aucune : ces noms sont vérifiés quand le serveur
  se connecte, comme l'étaient déjà les noms qui ne répondent pas à temps.
- La page [sécurité](../security/) explique comment Latet regroupe les requêtes en conversations : les
  en-têtes et les champs qu'il lit, le début de l'historique qu'il utilise quand il n'y en a pas,
  pourquoi deux discussions qui commencent de la même façon peuvent partager une conversation et
  restaurer les valeurs l'une de l'autre, et que chaque identité d'un serveur a son propre coffre-fort
  et ses propres conversations.
- Une version n'est publiée qu'une fois que la CI complète de son commit exact sur `main` a réussi : le
  workflow de publication le vérifie avant de construire quoi que ce soit, et s'arrête sinon. La
  nouvelle page [Publier Latet](../releasing/) décrit les étapes, les guides de contribution y
  renvoient, et le modèle de pull request demande quelles vérifications ont été lancées, qui a relu la
  modification et où sont les preuves.

## 0.10.2 (2026-10-28)

- Corrigé : un secret de moins de douze caractères (`password=1234`, `token=ab12f3`) recevait une
  valeur factice de sa propre forme, quatre chiffres ou six chiffres hexadécimaux, qu'un texte
  ultérieur pouvait contenir par hasard dans un identifiant ou un nombre. L'unmask de l'API des
  applications, qui lit chaque valeur factice du coffre-fort d'une clé, y mettait alors le secret : une
  ligne de journal qui ne l'avait jamais contenu revenait modifiée (`request_id=347bf309420a2290` en
  `347bf573020a2290`), et son mask prenait la valeur factice trouvée dans un mot pour une valeur à
  part entière. Un tel secret reçoit maintenant une valeur factice plus longue des mêmes sortes, ses
  signes à leur place : quinze chiffres pour un nombre (son signe et sa virgule décimale gardés, pour
  qu'un nombre JSON reste un nombre), douze chiffres hexadécimaux pour une valeur hexadécimale, sinon
  douze lettres et chiffres. Un secret de douze caractères ou plus, et les données personnelles,
  gardent des valeurs factices de leur forme. Une valeur factice courte donnée auparavant (huit
  caractères ou moins) reste, comme toute valeur factice. Une conversation, sur un ordinateur ou un
  serveur, la remplace comme avant, y compris à l'intérieur d'un mot, puisqu'elle ne restaure que les
  valeurs factices qu'on lui a montrées. L'unmask de l'API des applications ne la remplace que là où
  elle est un mot entier, sans lettre, chiffre ni trait de soulignement à côté : là où la valeur d'un
  tel secret a été masquée dans un mot ou un nombre plus long (`postgres` dans `postgresql://`,
  `1234` dans `order_id=9912343`), unmask y laisse sa valeur factice. Une valeur factice de 9 à 11
  caractères est remplacée comme avant partout. Un reconnaisseur à vous de la classe `secret` garde la
  valeur factice que donne sa règle, de la forme de la valeur ; une de huit caractères ou moins est
  remplacée comme une valeur factice courte donnée auparavant. Mettez à jour chaque instance d'un
  serveur : une qui exécute encore la 0.10.0 donne des valeurs factices courtes aux nouveaux secrets
  courts, et celles-ci restent aussi.
- Corrigé : une requête que Latet refuse avant de la masquer (son masquage échoue ou, sur un serveur,
  son corps serait décodé au-delà de `max_request_mb`) apparaissait dans l'Historique comme envoyée
  sans lecture, avec une erreur, alors que rien n'avait été envoyé. Elle apparaît maintenant comme
  bloquée et non envoyée, avec sa raison dans les mots de Latet et la taille de son corps, et sur un
  serveur la métrique des arrêts (`latet_agent_stops_total`) la compte.
- Corrigé : un texte masqué avant que le coffre-fort n'apprenne une valeur autour d'une valeur déjà
  masquée en lui laissait le reste de cette valeur en clair quand il était masqué de nouveau. Un IBAN
  collé à des lettres des deux côtés (`MarioIT60X0542811101000000123456Ferri`) qui contenait un secret
  connu du coffre-fort n'avait que ce secret masqué ; une fois qu'un texte ultérieur avait appris
  l'IBAN entier au coffre-fort, le même texte masqué de nouveau comme il l'avait été (par un appel
  ultérieur aux applications, dans une nouvelle conversation, ou dans le même appel ou la même
  requête) montrait encore les autres caractères de l'IBAN. Un nom appris entier (`Mario Rossi`) autour
  d'un nom de famille masqué auparavant (`Rossi`) laissait `Mario` en clair de la même façon. La
  vérification finale ne l'arrêtait pas, puisqu'aucune valeur entière n'était en clair. Un tel texte
  est maintenant masqué de nouveau, entièrement. Le texte d'une conversation qui n'est jamais parti (sa
  requête bloquée ou abandonnée) est masqué à neuf aussi ; un texte qui est parti est renvoyé tel qu'il
  est parti, comme avant.
