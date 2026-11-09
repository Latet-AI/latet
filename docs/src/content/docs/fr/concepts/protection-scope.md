---
title: Ce qui est protégé et ce qui ne l'est pas
description: Les limites de Latet. Lisez cette page avant de vous y fier.
---

Latet réduit ce que votre agent divulgue au fournisseur du modèle. Ce n'est pas une garantie que rien
de sensible ne quitte jamais votre machine. Cette page liste ce qui est couvert et ce qui ne l'est
pas.

## Protégé

Quand un [détecteur](../detection/) reconnaît une valeur, Latet la remplace par une valeur factice
avant que la requête protégée n'atteigne le fournisseur du modèle. Avec un serveur, les
données originales peuvent d'abord atteindre ce serveur. Cela couvre :

- **Dans Claude Code (mode client) :** les prompts que vous saisissez, le prompt système, les fichiers
  `CLAUDE.md` et le reste du contexte qu'ajoute Claude Code, les pièces jointes, les prompts des
  skills, les descriptions des outils, et chaque résultat d'outil : contenu des fichiers, sortie des
  commandes, sortie des hooks. Une vérification finale lit ensuite les textes de chaque requête avant
  son envoi. Ce que le modèle a écrit n'arrête rien quand cela revient exactement tel que le modèle l'a
  écrit, ce que Latet sait par les réponses qu'il a transmises et par les appels d'outils que le plugin
  a fait remettre en place ; tout autre texte à la place du modèle est lu comme le vôtre. Sur un
  ordinateur, le daemon garde cela en mémoire : après un redémarrage, un texte que le modèle a écrit
  plus tôt dans la conversation est lu comme le vôtre, et un texte qui contient une valeur que Latet
  masque arrête la requête ; commencez alors une nouvelle conversation. Un serveur Latet le garde pour
  chaque instance, pendant une heure après sa dernière utilisation.
- **En mode passerelle :** le prompt système ou les instructions, et chaque message de la
  conversation : votre texte, les réponses précédentes du modèle, les appels d'outils et les résultats
  d'outils. L'entrée des embeddings aussi. Une vérification finale lit ensuite ces textes avant
  l'envoi de la requête, comme en mode client.

Les réponses passerelle, flux et arguments d'outils sont restaurés là où Latet s'exécute :
sur la machine en local, sur le serveur pour une passerelle connectée. Le plugin Claude Code
restaure les arguments d'outils et le texte interactif à l'écran sur la machine ; stdout de
`claude -p` contient toujours des valeurs factices. OCR/NER s'exécutent là où tourne le moteur
Latet concerné ; un relecteur configuré peut recevoir du texte original.

Les clés d'un document JSON, comme la sortie d'un outil, sont masquées comme ses valeurs : une adresse
e-mail, un numéro de téléphone, un secret, un nom ou l'un de vos termes écrit comme une clé est
remplacé par la valeur factice qu'il a partout, et une clé où le modèle écrit une valeur factice est
remise en place pour l'agent. Une règle de champ et le mode données strict trouvent toujours une
valeur par sa clé telle qu'elle est arrivée, et une clé qui ne fait que nommer un champ, comme `id`
ou `email`, reste telle quelle.

Avec le [relecteur local](../llm-review/) activé, un modèle de langage sur votre machine lit aussi ces
textes avant leur départ, et masque les noms, les informations de santé et les autres valeurs
qu'aucun détecteur ne trouve, dans les clés d'un document JSON aussi. Une clé écrite comme le nom d'un
champ, en lettres minuscules, chiffres, `_` et `-` (`exit_code`, mais aussi `mario_rossi`), ne lui est
pas montrée : les détecteurs la lisent comme ils lisent chaque clé.

## Chaque partie d'une requête

Latet lit chaque partie d'une requête avant de l'envoyer, sur un ordinateur comme sur un serveur, et
n'envoie rien dont il n'a pas rendu compte :

- **Le chemin** doit correspondre à une route que connaît l'un des profils de requête de Latet, et
  chacune de ses parties doit avoir sa forme : l'identifiant d'un modèle, une région, une ressource que
  vous avez listée. Tout autre appel est refusé, un appel sans corps compris.
- **Les paramètres de requête** sont nommés par le profil, et chaque valeur doit avoir sa forme. Un
  paramètre qu'aucun profil ne nomme est refusé.
- **Les en-têtes.** Un identifiant est envoyé tel qu'il est arrivé, jamais journalisé ni enregistré.
  Tout autre en-tête doit avoir la forme que lui donne son agent, ou il est lu par la vérification
  finale quand il peut contenir du texte libre (`user-agent`). Les en-têtes qu'ajoutent les SDK et le
  traçage (`x-stainless-*`, `traceparent`...), et ceux des agents eux-mêmes qui peuvent contenir du
  texte libre (les identifiants d'un sous-agent de Claude Code, les métadonnées de tour de Codex...),
  sont retirés et comptés dans le tableau de bord : [Les en-têtes que Latet retire](../../getting-started/upgrading-to-0-11/#les-en-têtes-que-latet-retire) les énumère
  par agent. Tout autre en-tête est refusé, et son nom n'est donné qu'à l'agent, sauf sur une requête
  que Latet ne fait que vérifier (Claude Code, Bedrock, Vertex) : là, la vérification finale lit un
  nouvel en-tête `anthropic-*` ou une nouvelle valeur d'`anthropic-beta`, qui passe ensuite, et un
  nouvel en-tête `x-claude-code-*` est retiré.
- **Le corps.** Ses textes sont masqués, ou vérifiés là où Latet ne fait que vérifier la requête. Tout
  autre membre doit avoir sa forme (l'identifiant d'un modèle, le nom d'un outil, un réglage) et il est
  lu par la vérification finale là où il contient du texte. Un membre qu'aucun profil ne nomme est
  refusé, et l'agent apprend où il se trouvait, jamais son nom.

Dans le tableau de bord, chaque échange dit si chaque partie de sa requête a été lue, ou ce qui a
laissé passer une partie : *seulement vérifiée* (une valeur que la vérification finale a lue sans
devoir la masquer), *autorisée par une règle* (l'une des règles intégrées de Latet, comme pour les
champs chiffrés que le fournisseur lui-même renvoie), ou une
[exception](../../getting-started/upgrading-to-0-11/#approuver-une-exception-exacte) que vous avez
approuvée. Un refus dit à l'agent que rien n'a été envoyé.
Quand une seconde tentative est refusée après qu'une première a atteint le fournisseur (une requête à
Bedrock signée de nouveau après l'expiration de sa connexion AWS), l'agent apprend qu'aucune autre
tentative n'a été envoyée et que la première a pu arriver, jamais que rien n'a été envoyé.

Deux choses restent à l'écart : ce qu'un agent envoie sans l'adresse de Latet, que Latet ne voit
jamais, et le [relecteur local](../llm-review/), qui reçoit les textes d'origine parce que vous l'avez
configuré ainsi.

## Non protégé

**Valeurs que les détecteurs ne reconnaissent pas.** Latet ne masque que ce qu'il détecte. Un secret
dans un format inhabituel, coupé sur plusieurs lignes ou encodé (par exemple en base64 sous un nom
ordinaire) peut passer. Les noms et les adresses postales dans du texte libre ne sont trouvés qu'avec
le [modèle local de noms](../../getting-started/installation/) facultatif ou le
[relecteur local](../llm-review/), tous deux sans garantie ; sans eux, seuls votre propre nom d'auteur
git, vos termes et les valeurs déjà présentes dans le coffre-fort sont masqués. Les dates de
naissance ne sont pas détectées. La détection a toujours des faux négatifs.

**Images.** Avec la [reconnaissance de texte](../images/) facultative, le texte d'une capture d'écran
est lu sur votre machine et chaque valeur trouvée est recouverte par sa valeur factice. C'est sans
garantie, comme pour les noms : le texte qu'elle ne lit pas part tel quel. Sans elle, une requête qui
porte une image n'est pas envoyée. Sur une requête que Latet ne fait que vérifier, comme celles de
Claude Code, la position d'une photo, les notes du fabricant d'un appareil photo et une image dans
l'image sont refusées aussi
([Les photos et leurs métadonnées](../images/#les-photos-et-leurs-métadonnées)). Les codes QR et les
codes-barres ne sont pas décodés : les données qu'ils contiennent partent avec l'image.

**PDF et autres documents, images désignées par leur adresse, extraits audio.** Leur contenu n'est pas
lu, donc une requête qui en porte un n'est pas envoyée, sur un ordinateur comme sur un serveur, quoi
que dise `images`. Donnez plutôt le texte à l'agent. Les pages d'un navigateur, lues comme du texte, sont masquées comme toute sortie d'outil
([données de production](../production-data/)) ; ses captures d'écran sont des images.

**Les outils web, les sous-agents et les connecteurs claude.ai gardent les valeurs factices
(Claude Code).** WebFetch, WebSearch, les sous-agents et les connecteurs claude.ai (Gmail,
Calendar...) reçoivent les valeurs factices que le modèle a écrites, pas les originaux, car leurs
arguments quittent votre machine ou démarrent une autre conversation avec un modèle. Une recherche
web sur un nom masqué chercherait quelqu'un d'autre, et un connecteur à qui l'on demande d'envoyer un
e-mail à `emily.carter@northwind-legal.com` recevrait l'adresse factice : un appel d'outil web ou de
connecteur qui contient une valeur factice est donc arrêté avant de s'exécuter. Le modèle vous pose
la question, et *Non sensible* dans le tableau de bord laisse la valeur sortir en clair. Les résultats
des connecteurs claude.ai reviennent par les serveurs d'Anthropic, où le connecteur s'exécute : ce
qu'ils contiennent s'y trouve déjà avant que Latet puisse le masquer.

**Outils qu'exécute le fournisseur (agents en mode passerelle).** Une recherche web ou un serveur MCP
distant que le fournisseur exécute lui-même, comme Codex peut le faire, travaille sur ce que le modèle
a écrit : la valeur factice. Latet ne voit un tel appel qu'après son exécution, dans l'historique de
la requête suivante : le tableau de bord le signale (*Valeur factice à un outil externe*), avec
*Non sensible* pour chaque valeur, pour que la fois suivante le modèle utilise la vraie.

**Environnements que Latet ne voit pas :**

- Chat et Cowork dans Claude Desktop, connecté à un compte Claude ;
- les sessions dans le cloud, qui s'exécutent sur l'infrastructure du fournisseur plutôt que sur
  votre machine ;
- Cursor, Windsurf et les autres outils dont les requêtes au modèle partent des serveurs de leur
  éditeur ([la liste](../../agents/other-tools/#ce-que-latet-ne-peut-pas-protéger)) ;
- les suggestions inline de GitHub Copilot et les prédictions de modifications de Zed, qui
  viennent des propres services de leurs éditeurs ;
- OpenCode connecté avec ChatGPT (dans la [feuille de route](../../roadmap/)) ;
- ce que les outils propres à un agent envoient quand ils s'exécutent, comme une API de recherche
  web, une base de données ou une base vectorielle dans le cloud : ils reçoivent les vraies valeurs
  (voir [Frameworks pour agents](../../agents/openai-compatible/#frameworks-pour-agents)) ;
- tout agent ou environnement que vous n'avez pas configuré pour utiliser Latet.

Quand Claude Desktop est connecté à un compte Claude, son onglet Code masque le contenu avec le
plugin, mais l'application ignore `ANTHROPIC_BASE_URL` : il n'y a pas de vérification du daemon
derrière le plugin, ni de trace dans le tableau de bord. Sur une passerelle tierce, sans connexion à
un compte Claude, Chat, Cowork et Code envoient chaque requête par Latet : Latet vérifie chacune
d'elles et arrête celle qui transporterait en clair une valeur sensible, mais ne masque que dans
l'onglet Code, avec le plugin. Voir [Claude Code](../../agents/claude-code/#couverture) et
[Claude Desktop sur une passerelle](../../agents/other-tools/#claude-desktop-sur-une-passerelle).

**Contexte et métadonnées.** Les noms de fichiers, les chemins, la structure du code, les commentaires
et la logique métier sont envoyés tels quels, à part les valeurs que Latet y détecte, comme votre nom
d'utilisateur dans le chemin de votre répertoire personnel. Les valeurs factices gardent la forme de
l'original (longueur, format, préfixe ; un secret de moins de douze caractères reçoit une valeur
factice plus longue), et le texte qui les entoure peut révéler ce qu'est une valeur ("le mot de
passe de la base de données de production").

**Ce qu'un document JSON contient là où le masquage n'atteint pas.** Un numéro de carte écrit comme
un nombre JSON, que le profil `balanced` ne masque pas, est trouvé par la vérification finale, qui
arrête alors la requête : l'agent en apprend la raison, et le tableau de bord montre où se trouve la
valeur. Tant qu'une telle sortie reste dans la conversation, chacune de ses requêtes est arrêtée :
commencez une nouvelle conversation, et demandez les données à l'agent sous une autre forme. La
valeur factice tirée pour une clé n'est jamais une autre clé de son objet ; un appel d'outil dont
l'entrée contient une valeur et sa valeur factice comme deux clés, qui n'en feraient plus qu'une une
fois masquées, arrête la requête, car un objet ne contient une clé qu'une fois, et deux clés de ce
genre restent telles que le modèle les a écrites quand Latet remet en place sa réponse pour l'agent.
Un objet JSON qui donne deux fois une clé est lu avec sa dernière copie, et part réécrit avec cette
seule copie, pour que la copie que Latet n'a pas lue ne parte jamais ; une requête que Latet se
contente de vérifier, comme celles de Claude Code, est arrêtée quand c'est son propre corps qui donne
deux fois une clé.

**Appels avec des clés en conflit dans l'historique.** Conserver les deux clés dans un appel restauré
préserve ses données, mais le masquer à nouveau peut rendre les clés identiques. C'est une limite
documentée : la requête suivante et toutes celles qui conservent cet appel sont refusées. Commencez
une nouvelle conversation, ou retirez ou reformulez l'appel avec les contrôles d'historique pris en
charge par votre client. Réessayer la requête inchangée ne rétablit pas la conversation.

**Arguments d'outils en flux.** Latet retient les arguments JSON jusqu'à la fin de l'appel, puis
restaure tout le document sans fusionner des clés distinctes. Le texte ordinaire continue d'arriver
en flux. Les canaux JSON retenus partagent un même budget de réponse limité, qui comprend les
métadonnées et la croissance lors de la reconstruction et de la sérialisation. Un dépassement ou des
arguments incomplets ou non valides terminent le flux par une erreur. Un appel Anthropic dont
l'entrée initiale n'est pas vide ne peut pas fournir une seconde source JSON non vide d'arguments.
Un appel interrompu avant sa fin n'est pas enregistré comme sortie complète du modèle.

**Enregistrements acceptés de la sortie du modèle.** Après avoir validé un lot de réponses, Latet
accepte les enregistrements de la sortie et met leur sauvegarde en attente avant de renvoyer la
sortie du lot. La sauvegarde se fait en arrière-plan : l'acceptation ne prouve ni la remise au client
ni que les données sont déjà sauvegardées. Une annulation avant l'acceptation ne publie aucun
enregistrement ; après l'acceptation, leur sauvegarde peut se terminer même si la remise est annulée.

Dans une instance, la passerelle de chaque identité admet au plus 32 MiB de ces enregistrements et
256 lots pour l'ensemble de ses sessions, en comptant le travail en attente et en cours.
Le texte masqué et remis en place, les
identifiants des réponses et le coût des structures d'enregistrement sont comptés. Cela borne le
nouveau travail retenu, pas la mémoire totale du processus (RSS) : la conservation prolongée de
données en clair et l'interpréteur consomment toujours de la mémoire. Si l'admission est pleine ou
fermée, ou si la mise en attente échoue, la sortie du lot est refusée. Le texte déjà transmis en
flux ne peut pas être retiré.

Les enregistrements deviennent visibles plus tard. Les requêtes mises en attente ensuite sur le
même processus de travail de la passerelle suivent sa tentative d'enregistrement. Sur un serveur
Latet, cela ne garantit ni que Valkey les a écrits, ni qu'une autre instance les voit, ni leur ordre
par rapport au travail du plugin de Claude Code. L'arrêt peut supprimer les enregistrements en attente. Une erreur de sauvegarde après la
remise produit un diagnostic à fréquence limitée, avec seulement une catégorie fixe et le type
d'exception ; elle ne peut annuler ni la remise ni les effets d'une transaction. Les enregistrements
manquants restent soumis aux règles habituelles de recherche dans la session, de masquage et de refus.

**Champs de la requête hors de la conversation.** En mode passerelle, les descriptions des outils, les
schémas de leurs paramètres et les exemples de leur entrée sont masqués comme la conversation, de même
que le schéma d'une sortie structurée, une sortie prédite et ce qu'une requête dit de la personne pour
qui elle est faite (`user`, `safety_identifier`, `metadata`). Les noms des outils, ce qui permet au
fournisseur d'exécuter un outil à lui (l'adresse et les en-têtes d'un serveur MCP distant, la position
d'une recherche web) et les autres réglages de la requête doivent chacun avoir la forme que leur
donne leur profil, et sont lus par la vérification finale là où ils contiennent du texte ; un réglage
qu'aucun profil ne nomme arrête la requête ([Chaque partie d'une requête](#chaque-partie-dune-requête)).
Dans Claude Code, le plugin masque les descriptions des outils, et la vérification finale lit
`metadata`, le schéma d'une sortie structurée et les schémas des paramètres des outils (voir plus
bas). Un client autre que Claude Code sur une voie
que Latet se contente de vérifier (la connexion d'un abonnement, Bedrock, Vertex) n'a pas de plugin :
la vérification finale lit aussi les descriptions, les paramètres et les exemples de ses outils, et
arrête une requête qui y laisserait sortir une valeur en clair.

**Les outils de Claude Code.** Le plugin masque les descriptions des outils, y compris celles de
`tool_addition`. Latet ne prend pas les marqueurs de la requête comme la preuve que le plugin a été
exécuté, sur un ordinateur comme sur un serveur : sa vérification finale lit la description de chaque outil, le schéma de
ses paramètres et les exemples de son entrée, comme pour un client sans plugin, et arrête une requête
qui y laisserait sortir une valeur en clair. Un outil MCP dont le schéma nomme l'adresse d'un client
arrête donc les requêtes de Claude Code, tant que la valeur n'est pas retirée du schéma
ou autorisée dans vos réglages. Une définition d'outil sous une forme que Latet ne lit pas est
refusée, comme pour tout client. Les vérifications des métadonnées, des sorties structurées et des
schémas de repli décrites ici s'appliquent toujours.

Pour Anthropic, la lecture des sorties structurées couvre aussi les descriptions, clés et valeurs
des schémas dans chaque `fallbacks[].output_config.format`. La réécriture masque les schémas
sensibles ; dans les requêtes seulement vérifiées, ils sont refusés et les octets sûrs restent
inchangés. Les modèles de secours qui ne précisent qu'un modèle restent pris en charge. Un serveur
dont la protection est active refuse les conteneurs de secours et schémas de sortie mal formés.

**Ce que Latet ne lit pas.** Latet refuse ce qu'il ne lit pas, sur un ordinateur comme sur un
serveur, avec une erreur qui dit pourquoi. Il refuse un appel qu'aucun profil ne nomme : files ou
batches, par exemple, ou une méthode que la route n'accepte pas. Il refuse un contenu sous une forme que Latet ne lit pas : un prompt
de complétion donné en jetons ; une variable de prompt, une définition d'outil ou le format d'une
réponse d'une sorte qu'il ne connaît pas ; une partie de contenu, un bloc d'un message ou un élément
de l'entrée d'une sorte qu'il ne connaît pas, dans les chat completions comme dans les messages
d'Anthropic et la Responses API, ou qui n'est pas un objet ; un champ que Latet lit donné sous une
autre forme, comme l'entrée d'un appel d'outil qui n'est pas un objet ou un prompt système qui n'est
ni du texte ni des blocs ; et, dans la Responses API, l'audio d'une conversation en direct (`audio`,
`output_audio`) et ce qui a été entendu dans un extrait audio. Il refuse une trame WebSocket qui
n'est pas un tour (`response.create` ; `response.cancel` passe) et, sur une requête qu'il se contente
de vérifier (Claude Code, Bedrock, Vertex), une image dont il n'a pas pu lire le texte, quoi que dise
`images`. Ce que le fournisseur a produit et qui lui est rendu tel qu'il l'a produit passe quand Latet
l'a vu arriver du fournisseur dans cette conversation : le raisonnement du modèle et ses signatures,
une compaction, les appels des propres outils du fournisseur et leurs résultats. Là où Latet ne l'a
pas vu (le daemon d'un ordinateur a redémarré, par exemple), une règle intégrée laisse passer les
champs chiffrés du fournisseur lui-même, seulement dans les tours du modèle et seulement vers
l'adresse du fournisseur, jamais par une passerelle d'entreprise ; le tableau de bord les affiche
comme *autorisée par une règle*. Il en va de même pour ce qui ne contient rien, comme une référence à
un élément précédent, et pour une requête sans corps sur une route qu'un profil nomme, par exemple la
liste des modèles.

Latet refuse aussi les messages chat portant `name`, les blocs Anthropic `search_result` et les blocs
document portant `title` ou `context`, même dans les résultats d'outils, sur un ordinateur comme sur un
serveur. Ces formes facultatives ne sont pas prises en charge, même avec des valeurs vides ou null. Un
document sans titre ni contexte est refusé comme tout document.

**Relecture du mode auto de Claude Code.** Le relecteur du mode auto reçoit votre nom d'utilisateur
du système et les chemins de vos répertoires personnel et de travail, ainsi que tout nom d'hôte ou
auteur git présent dans votre CLAUDE.md ou dans l'action, et aucun plugin ne peut les masquer. Par
défaut, le daemon arrête ces relectures : ces informations restent sur votre machine, et le mode auto
ne peut pas approuver d'actions de lui-même. `auto_mode_review` laisse passer chaque information vers
le relecteur, et vers lui seul, ou donne au relecteur un nom d'utilisateur factice ; voir
[Claude Code](../../agents/claude-code/).

**Votre propre machine.** Latet n'est pas un bac à sable : il ne protège pas contre les logiciels
malveillants, les autres utilisateurs de votre compte ou un agent compromis. Les outils que l'agent
exécute sur votre machine, serveurs MCP locaux compris, reçoivent les vraies valeurs, pour que les
fichiers et les commandes fonctionnent : une commande qui envoie des données sur le réseau envoie la
vraie valeur. Exécutez l'agent dans un bac à sable quand il en a besoin.

**Règles ajoutées en cours de conversation (Claude Code).** Imaginons que vous ajoutiez
`Project Falcon` comme terme personnalisé au milieu d'une conversation. Le nouveau contenu est masqué
à partir de là, mais les tours précédents, déjà parvenus chez Anthropic avec le nom en clair, sont
renvoyés avec chaque requête. Latet les signale dans le tableau de bord au lieu de les bloquer : ils
ont déjà été envoyés, et les bloquer arrêterait la conversation. Commencez une nouvelle conversation
pour les laisser derrière vous.

**Données envoyées auparavant.** Tout ce qui a été envoyé avant l'installation de Latet ou, avant la
0.11, depuis un projet où il était désactivé, se trouve déjà chez le fournisseur.

La page [Sécurité et modèle de menace](../../security/) explique le raisonnement qui sous-tend ces
limites.
