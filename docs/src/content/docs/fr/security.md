---
title: Sécurité et modèle de menace
description: Ce contre quoi Latet protège, ce contre quoi il ne protège pas, et comment il se protège lui-même.
---

:::note[Une couche de défense]
Latet masque ce que ses détecteurs reconnaissent, et certains agents et certaines interfaces sont couverts
par conception plutôt que testés en conditions réelles (la page de chaque agent dit lesquels). N'en faites
pas votre seule protection pour des données qui ne doivent jamais quitter votre machine.
:::

## Ce que Latet protège

Les secrets et les données personnelles reconnaissables dans votre espace de travail et vos
conversations : clés d'API, jetons, mots de passe, clés privées, identifiants dans les chaînes de
connexion, adresses e-mail, numéros de téléphone, IBAN, numéros de carte, numéros d'identité nationale,
d'identification fiscale et de TVA, adresses IP, les termes que vous ajoutez, et l'identité de cette
machine. La [page sur la détection](../concepts/detection/) dit exactement ce qui est reconnu.

## Frontières de confiance

Pour un déploiement connecté, la [frontière de confiance du serveur](../concepts/server/)
s'applique aussi : les originaux peuvent atteindre le serveur et les vues authentifiées du
tableau de bord. Les administrateurs gèrent la configuration du serveur et les réglages qu'il
partage, comme ses destinations et son relecteur ; empreintes, identifiants et liaisons d'agents
restent sur les machines. Imposez séparément
les restrictions d'accès aux fournisseurs si le serveur doit être le point de contrôle
de l'entreprise. Un serveur ne prend pas les marqueurs d'une requête comme la preuve que le
plugin de Claude Code a été exécuté : il vérifie aussi les descriptions, les schémas et les
exemples des outils.

- **Votre machine** est de confiance : l'agent, ses outils, le plugin et le daemon de Latet, et vos
  fichiers.
- **Le fournisseur du modèle** ne se voit pas confier ces valeurs. Latet ne présume rien de la façon dont
  les requêtes sont journalisées, conservées ou utilisées une fois arrivées : il garde donc les originaux
  hors de sa portée.
- **Le réseau** entre les deux est protégé par TLS, comme d'habitude.

## Conversations, projets et restitution

Les identifiants de conversation regroupent les requêtes ; ils n'authentifient pas une personne et
ne créent pas de coffre. La passerelle utilise le premier en-tête non vide dans cet ordre :
`x-claude-code-session-id`, `session-id`, `session_id`, `thread-id`, `x-session-id`. Pour la Responses
API viennent ensuite un `previous_response_id` connu, puis un `prompt_cache_key` de type chaîne.
Ces noms sont recherchés dans l'espace de l'identité courante du serveur ou dans l'instance locale.

Sans l'un de ces identifiants, Latet dérive une clé du début de l'historique de la requête, en
utilisant seulement les 8 192 premiers caractères de sa sérialisation compacte. Des chats distincts
dont le début est identique, ou qui ne diffèrent qu'après ce préfixe, peuvent donc partager une
conversation. Il s'agit de la réutilisation de la même entrée, pas d'une collision cryptographique
de hachage. Cela regroupe les requêtes successives des clients sans état, mais ne permet pas de
déterminer quel chat logique le client voulait désigner.

Dans cette conversation partagée, la réponse peut restituer des valeurs enregistrées par les deux
chats. Par exemple, si le fournisseur répond dans le chat B avec un substitut émis précédemment dans
le chat A, Latet peut restituer localement l'original de A. Des identifiants explicites distincts
évitent ce regroupement accidentel si le client les utilise de façon cohérente. Ils ne constituent
pas une frontière d'autorisation. Dans le texte des requêtes HTTP ordinaires, un substitut collé par
le client est masqué comme une nouvelle valeur littérale ; il ne désigne pas l'original qu'il
représentait. Une réponse du fournisseur contenant un substitut inconnu ne donne pas accès aux
originaux d'un autre espace du serveur.

Chaque identité du serveur possède son coffre et ses enregistrements de conversation. Des en-têtes,
identifiants de réponse, clés de cache ou débuts identiques ne franchissent pas cette frontière.
Les mêmes octets d'un substitut peuvent exister dans des coffres séparés ; la restitution utilise
l'association du coffre destinataire. Sur un ordinateur, les projets d'une même instance Latet
partagent son coffre : lorsque cette fonction est prise en charge,
`x-latet-project` sélectionne les réglages du projet, pas un stockage séparé ni une nouvelle
conversation. Des dossiers de données et des coffres configurés séparément offrent des stockages
séparés ; travailler dans des dépôts différents ne suffit pas. Voir [Projets](../concepts/projects/)
et [Latet sur un serveur](../concepts/server/).

## Dans le périmètre

- La divulgation accidentelle de valeurs reconnaissables au fournisseur, par les prompts, les fichiers,
  les sorties d'outils et l'historique des conversations.
- Les requêtes qui partiraient avec une valeur sensible en clair en mode client, par exemple par un chemin
  que le plugin ne couvre pas : la vérification finale du plugin et le daemon les bloquent.
- Les pages web qui tentent d'utiliser le daemon local (DNS rebinding, requêtes intersites).

## Hors du périmètre

- **Les valeurs que les détecteurs manquent.** Voir
  [Ce qui est protégé et ce qui ne l'est pas](../concepts/protection-scope/).
- **Une machine compromise.** Un logiciel malveillant, d'autres utilisateurs de votre compte, ou un plugin
  ou un agent malveillant peuvent lire vos fichiers directement. Latet n'est pas un bac à sable.
- **Un agent déterminé à réduire la protection de Latet, ou amené à le faire par la ruse.** Une
  modification des réglages qui réduit la protection attend votre approbation, et les commandes qui
  désactivent Latet refusent de s'exécuter dans un agent : un garde-fou contre les erreurs, pas un verrou.
  Un agent s'exécute sous votre compte : un agent résolu à contourner ce garde-fou, ou trompé par une
  injection de prompt, peut donc lire ce que lit Latet et approuver sa propre modification par l'interface
  locale du daemon. Une vraie frontière exige que Latet s'exécute sous un autre utilisateur, ou sur une
  autre machine ([Latet sur un serveur](../concepts/server/)). Voir
  [Les agents et vos réglages](../concepts/agents-and-settings/).
- **L'exfiltration par les outils.** Les originaux sont remis en place dans les arguments des appels
  d'outils pour que le travail de l'agent soit correct sur le disque. Une instruction introduite par
  injection de prompt pourrait amener l'agent à passer une valeur factice à une commande qui envoie des
  données sur le réseau, comme `curl` ; l'original remis en place partirait alors par cette commande.
  Latet garde les valeurs factices dans les outils web et les connecteurs claude.ai, mais une commande
  shell ou un serveur MCP local reçoit la vraie valeur. Gardez activées les demandes de permission de
  votre agent pour les commandes réseau.
- **L'inférence à partir du contexte.** Les valeurs factices gardent le format de l'original, et le texte
  qui les entoure peut révéler à quoi sert une valeur.
- **Les interfaces que Latet ne peut pas voir** : les sessions dans le cloud, Chat et Cowork de Claude
  Desktop, Cursor, et les résultats des connecteurs claude.ai, qui reviennent par les serveurs
  d'Anthropic.

## Comment Latet se protège lui-même

**Le daemon et le tableau de bord**

- Ils n'écoutent que sur `127.0.0.1` : rien sur votre réseau ne peut donc les atteindre.
- Les requêtes dont l'en-tête `Host` n'est pas une adresse locale sont rejetées, ce qui déjoue le DNS
  rebinding.
- Les requêtes qui viennent d'une page web (avec un en-tête `Origin`, ou un `Sec-Fetch-Site` intersite)
  sont refusées sur les routes du proxy.
- Le tableau de bord exige un lien de connexion à usage unique fourni par `latet dashboard`, puis un
  cookie de session `HttpOnly`, `SameSite=Strict`, et chaque modification exige un en-tête que les autres
  sites ne peuvent pas envoyer. Voir le
  [guide du tableau de bord](../dashboard/#la-confidentialité-du-tableau-de-bord-lui-même).
- Le plugin Claude Code communique avec le daemon au moyen d'un jeton aléatoire qui commence par
  `latet_brg_`, conservé dans `bridge.token` dans le dossier de données (un jeton créé par une version
  précédente n'a pas de préfixe et fonctionne comme avant). Pour le remplacer par un jeton qui a le
  préfixe, exécutez `latet stop`, supprimez `bridge.token` dans le
  [dossier de données](../getting-started/uninstall/), exécutez `latet start` (il en crée un nouveau)
  puis `latet agents apply`, qui écrit le nouveau jeton dans les réglages de vos agents. Sans ce jeton,
  le daemon refuse de masquer, de remettre en place ou de vérifier quoi que ce soit.
- **Vos agents atteignent le daemon en http simple sur `127.0.0.1`, et c'est voulu.** Ce trafic
  n'atteint jamais une carte réseau : aucune autre machine, pas même sur votre Wi-Fi, ne peut le voir. Un
  programme sur votre machine qui pourrait le lire (avec des droits d'administrateur, ou sous votre
  compte) pourrait lire directement le coffre-fort et vos fichiers : le chiffrer ne l'arrêterait donc pas.
  Il faudrait pour cela installer dans le système d'exploitation votre propre autorité de certification,
  dont la clé, si elle était volée, permettrait à quelqu'un de se faire passer pour n'importe quel site
  web sur votre machine. Ce qui quitte votre machine est chiffré : les requêtes vers votre fournisseur,
  telles que l'agent les envoie, et vers le relecteur local sur une autre machine (ci-dessous).

**Le tutoriel**

- `latet tutorial` fait tourner son bac à sable à l'intérieur de la commande, sur `127.0.0.1` et un port
  libre, protégé comme le tableau de bord : un lien à usage unique, puis un cookie qui lui est propre, qui
  ne remplace jamais la connexion à votre tableau de bord.
- Le bac à sable a ses propres données, ses propres réglages et son propre coffre-fort, dans un dossier
  temporaire, et aucune de vos clés ni aucun de vos jetons. Son unique route mène à un modèle fictif dans
  le même processus : aucune requête n'atteint donc un fournisseur ; ce qui en sortirait, comme une
  destination ou le serveur d'un relecteur local, y est refusé. Voir
  [Tutoriel](../getting-started/tutorial/#un-bac-à-sable).

**Le relecteur local sur une autre machine**

- Il reçoit les textes originaux : il est donc joint en `https://`. Hors de votre réseau local, rien
  d'autre n'est accepté.
- Pour un serveur à vous doté d'un certificat auto-signé, la confiance repose sur son empreinte, que vous
  comparez une fois avec celle du serveur (`latet llm trust`, ou *C'est la même : lui faire confiance*
  dans le tableau de bord). Dès lors, seul ce certificat est accepté, vérifié avant l'envoi de tout texte,
  et aucune autorité n'est ajoutée au système d'exploitation.
- Sur votre réseau local, `http://` est encore accepté, pour les serveurs configurés auparavant : le
  tableau de bord, `latet doctor` et le journal du daemon avertissent que les textes traversent votre
  réseau sans chiffrement.

**Le fichier d'un projet**

- Un `latet.yaml` dans un dépôt ne peut qu'ajouter de la protection : un profil plus strict, davantage de
  termes et de reconnaisseurs. Ce qui réduirait la protection (des valeurs à ne jamais masquer, des règles
  désactivées, Latet désactivé) est ignoré : un dépôt que vous clonez ne peut donc pas affaiblir vos
  réglages. Voir [Projets](../concepts/projects/#le-fichier-du-projet).
- Ses reconnaisseurs sont des expressions régulières exécutées par RE2, en temps linéaire par rapport au
  texte, et la taille du fichier est plafonnée : un fichier hostile ne peut pas paralyser le masquage.

**Vos valeurs**

- Le coffre-fort chiffre chaque original sur le disque avec AES-GCM. La clé est dérivée d'une clé racine
  propre à chaque installation, conservée dans le magasin d'identifiants de votre système d'exploitation
  (le Gestionnaire d'identification de Windows, le Trousseau de macOS, le trousseau de clés de
  l'environnement de bureau sous Linux), ou dans un fichier privé, `root.key`, quand il n'y en a pas.
- Les valeurs factices sont dérivées avec HMAC de cette clé propre à l'installation : sans la clé,
  personne ne peut remonter d'une valeur factice à l'original.
- Une valeur factice n'est remise en place que dans la conversation à laquelle elle a été montrée : une
  autre session ne peut donc pas se servir du coffre-fort comme d'un oracle.
- L'historique du tableau de bord garde les requêtes telles qu'elles ont quitté votre machine, déjà
  masquées (celles qui ont été bloquées, telles qu'elles seraient parties), chiffrées avec la même clé,
  pendant 14 jours par défaut. Réglez `history: metadata` (comptages et durées uniquement) ou
  `history: off` dans la [configuration](../reference/configuration/) pour en garder moins.

**Sauvegardes**

`latet backup` écrit [votre installation](../concepts/export-import/#votre-installation-pour-une-autre-machine)
dans un seul fichier :
- **La clé vient de votre phrase secrète.** Elle est dérivée avec Argon2id (128 Mio de mémoire, 3 passes)
  et un sel aléatoire.
- **Le corps est scellé par segments.** C'est un flux tar découpé en segments de 1 Mio, chacun scellé avec
  AES-256-GCM. Le nonce d'un segment est son numéro et le fait qu'il soit ou non le dernier, et l'en-tête
  du fichier (son format, ainsi que les paramètres et le sel de la clé) est authentifié avec chaque
  segment.
- **Ce qui est refusé, avant que quoi que ce soit du fichier ne soit utilisé :**
  - une autre phrase secrète ;
  - un segment modifié, déplacé, supprimé ou ajouté ;
  - un fichier tronqué, ou un en-tête modifié ;
  - des paramètres de clé au-delà de ce que Latet accepte (un fichier ne peut pas lui faire utiliser toute
    la mémoire) ;
  - tout contenu autre que les fichiers attendus.
- **Rien n'est en clair sur le disque.** Les bases de données sont copiées telles quelles (leurs valeurs
  scellées), la clé racine n'existe qu'à l'intérieur du flux chiffré, et le fichier est écrit en entier,
  lisible par vous seul.
- **Avec `--vault`, le fichier contient vos vraies valeurs et la clé qui y donne accès :** gardez-le comme
  vous garderiez les valeurs.

`latet upgrade` copie, avant d'installer une version, le coffre-fort, l'historique et les réglages
dans `backups/before-<version>-<heure>` dans le dossier de données de Latet
([Mettre à jour](../getting-started/installation/#mettre-à-jour)) :
- **Scellées comme le coffre-fort.** Les bases de données sont copiées telles quelles, leurs valeurs
  scellées avec la clé de cette machine, qui n'est pas copiée ; le dossier est lisible par vous
  seul.
- **Elles suivent ce que vous retirez**, tant que Latet 0.9.10 ou une version plus récente tourne.
  *Oublier* retire une valeur de chaque copie, effacer
  l'historique efface aussi le leur, et leur historique n'est pas gardé plus longtemps que le
  vôtre. Une copie qui ne peut pas être nettoyée est supprimée, ou mise de côté et plus jamais
  proposée.
- **Trois au plus, et revenir en arrière vous appartient.** Remettre une copie n'est proposé que
  dans un terminal à vous, et un agent ne peut pas lancer `latet upgrade --to` vers une version
  plus ancienne.

**Le relecteur local** (facultatif, [désactivé tant que vous ne l'activez pas](../concepts/llm-review/))

- Il s'exécute sur votre propre serveur : sur cette machine, ou sur une autre de vos machines une fois que
  vous l'avez confirmé (`remote: true`), en https hors de votre réseau local. Il lit le texte original : ne
  le faites donc tourner que sur un serveur que vous contrôlez.
- Il peut ajouter à ce qui est masqué, jamais rien retirer ; ce qu'il trouve est vérifié par rapport au
  texte (une valeur qu'il invente, ou une valeur à l'intérieur d'un espace réservé, est écartée).
- Une sortie d'outil contenant des instructions qui lui sont destinées peut au pire lui faire trouver
  moins de choses ; les détecteurs s'appliquent quand même, et une réponse vide à un texte qui s'adresse
  au relecteur n'est pas considérée comme fiable (la requête est arrêtée).
- La clé d'API de son serveur est conservée dans le magasin d'identifiants du système d'exploitation (ou
  provient d'une variable ou d'un fichier), n'est envoyée qu'à ce serveur, et n'est jamais écrite dans les
  journaux, dans le tableau de bord ni dans config.yaml. Le tableau de bord peut l'enregistrer ou la
  supprimer, jamais l'afficher.
- Ce qu'il a trouvé est mémorisé, scellé, dans la base de données du coffre-fort, comme les originaux du
  coffre-fort.
- On ne lui montre les images que si vous activez cette option (`llm_review.images`) : les montrer à un
  serveur sur une autre machine attend votre approbation quand c'est un agent qui le demande.

**Images** (avec la [reconnaissance de texte](../concepts/images/) facultative)

- Les images sont lues sur votre machine ; les modèles sont téléchargés une seule fois, à une révision
  fixée, chaque fichier étant vérifié par rapport à un SHA-256 fixé, puis ils s'exécutent hors ligne.
- Les images telles qu'elles ont été envoyées sont conservées dans l'historique du tableau de bord,
  scellées, uniquement avec `history: full` (10 Mo au plus chacune), et disparaissent avec leur échange.
- Ce que vous décidez sur une image dans le Relecteur en direct, et une image que le relecteur local a
  retenue, sont mémorisés, scellés, dans la base de données du coffre-fort, par un hachage à clé de
  l'image.
- Une image qui ne peut pas être lue est retenue, jamais envoyée telle quelle.

**Trafic**

- Les en-têtes d'identification (`Authorization`, clés d'API, cookies et autres) sont transmis inchangés,
  mais jamais écrits dans les journaux ni dans le tableau de bord : seule leur longueur est enregistrée.
  Les en-têtes propres à Latet (`x-latet-*`, dont l'un nomme un de vos dossiers) ne quittent jamais votre
  machine.
- Une exception, quand vous utilisez Amazon Bedrock avec votre connexion AWS : Claude Code n'envoie pas
  d'identifiants, et Latet signe chaque requête pour Amazon (SigV4) avec votre profil AWS. Il lit le
  profil comme le fait l'AWS CLI, garde les identifiants en mémoire seulement, ne les écrit jamais nulle
  part, et ne signe que les requêtes vers Amazon Bedrock. Les identifiants qui ne se trouvent que dans
  votre shell (aws-vault, clés exportées) sont transmis par `latet claude` pour sa seule session : Latet
  les oublie quand cette session se termine, même si `latet claude` est tué, et à leur propre expiration.
  Une route qui signe porte une clé dérivée du propre jeton de Latet : un autre utilisateur de votre
  machine, ou un programme qui ne peut pas lire vos fichiers, ne peut donc pas faire signer des requêtes en
  votre nom ; un programme qui s'exécute sous votre compte peut lire ce jeton, comme il peut lire vos
  fichiers AWS. La clé n'apparaît jamais dans un journal. Voir
  [Points de terminaison d'entreprise](../agents/company-endpoints/#amazon-bedrock).
- Un dépôt ne peut pas décider où vont vos requêtes : Latet lit la route d'un agent dans vos propres
  réglages, jamais dans ceux d'un projet, et le plugin Claude Code arrête les prompts quand les réglages
  d'un projet fixent où vont les requêtes, ou un proxy pour elles, même via Latet. Une adresse écrite de
  sorte que deux programmes puissent la lire différemment (une barre oblique inverse, un espace, un nom
  d'utilisateur à l'intérieur) est refusée, jamais devinée. Quand le plugin ne peut pas vérifier la route
  d'une session, les prompts de celle-ci sont arrêtés.
- Les requêtes de Claude Code, ou qui portent un jeton d'abonnement Claude, ne sont jamais réécrites,
  seulement vérifiées.
- Quand le masquage échoue, la requête est refusée au lieu d'être envoyée : un corps que Latet ne peut pas
  décoder, un détecteur requis qui échoue, ou une valeur qu'il ne peut pas remplacer aboutissent tous à une
  erreur qui indique que rien n'a été envoyé. Dans Claude Code, quand le daemon est injoignable, le plugin
  retient le contenu.
- Le TLS vers la destination est vérifié avec le magasin de certificats de votre système d'exploitation :
  un proxy d'entreprise dont le certificat racine y est installé continue donc de fonctionner.
- La capture brute des échanges pour le débogage est désactivée par défaut. Quand vous l'activez
  (`latet serve --capture <folder>`), les en-têtes d'identification sont caviardés, mais les captures
  contiennent toujours le contenu des conversations : gardez-les sur votre machine.

Le daemon n'établit aucune connexion réseau, si ce n'est pour transmettre les requêtes de votre agent au
fournisseur (ou au point de terminaison d'entreprise) que votre agent utilise déjà et, quand vous
l'activez, pour interroger le relecteur local, sur cette machine ou sur une autre de vos machines.
Quand il signe pour Amazon Bedrock avec un
profil SSO, l'AWS CLI qu'il exécute peut contacter le service de connexion d'AWS pour renouveler votre
connexion, comme lorsque vous l'utilisez vous-même. Les commandes n'accèdent au réseau que pour ce que vous
demandez : `latet ner install` et `latet ocr install` téléchargent leurs modèles depuis huggingface.co, et
`latet upgrade` cherche la dernière version sur GitHub et la télécharge. Il n'y a ni télémétrie ni
statistiques d'utilisation, ni dans le daemon, ni dans le tableau de bord, ni sur ce site de
documentation.

**Sur un serveur Latet** ([Latet sur un serveur](../concepts/server/))

- L'administration ne répond qu'en HTTPS, à un programme : une requête venant d'une page web est
  refusée, et un jeton d'administration n'est vérifié qu'après que son adresse a passé une limite de
  tentatives refusées.
- Le serveur ne garde aucun secret qu'il émet : seulement un hachage à clé de chaque jeton, clé et code.
  La clé de chaque espace est scellée avec la clé principale du serveur, gardée à part des sauvegardes de
  la base de données.
- Chaque appel d'administration est enregistré dans un journal chaîné, lectures et refus compris ;
  `latet server log verify` vérifie la chaîne.
- Le jeton que détiennent les agents d'une machine ouvre le pont du plugin et le proxy de l'espace,
  jamais le tableau de bord : celui-ci demande aussi la clé de la machine, qu'aucun agent ne reçoit.

## Signaler une vulnérabilité

Merci de ne pas ouvrir de ticket public. Utilisez le signalement privé des vulnérabilités de GitHub sur le
[dépôt de Latet](https://github.com/Latet-AI/latet/security).
