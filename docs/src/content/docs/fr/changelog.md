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
auto, les outils MCP masqués avant que Claude Code ne les lise, un programme d'installation signé
pour Windows, vos propres modèles de noms, d'autres agents, et la documentation dans d'autres
langues.

## 0.15.1 (2026-11-22)

- **Une synchronisation demandée ne reste plus en attente.** Sur un serveur avec Latet Enterprise,
  une synchronisation demandée (**Synchroniser maintenant**, ou `latet server terms run`) au moment
  où commençait une synchronisation de la même source, comme celle que lance une modification des
  sources, pouvait rester affichée comme demandée : cette synchronisation lisait la source mais se
  terminait avant que la demande soit enregistrée, si bien qu'aucune ne prenait la demande avant la
  synchronisation suivante de la source à son intervalle, et `latet server terms run` attendait sa
  minute entière et affichait *still running*. Désormais, une synchronisation commencée au moment
  de la demande, ou après, y répond, quelle que soit celle des deux qui est enregistrée en premier.
  Les termes synchronisés et masqués ont toujours été les bons.

## 0.15.0 (2026-11-21)

Latet 0.15.0 apporte Latet Enterprise : sur un serveur Latet, les fonctionnalités dont l'équipe de
sécurité d'une entreprise a besoin, dans le même paquet et la même image que l'édition Community, et
désactivées sans fichier de licence. Sur un serveur, les tables changent, si bien que ses instances
s'arrêtent une fois pour la migration : lisez **Mettre à jour**, à la fin de cette liste, avant de
mettre à jour un serveur.

- **Latet Enterprise.** Les fonctionnalités Enterprise tournent sur un
  [serveur Latet](../getting-started/server/), activées par un fichier de licence. Leur code se
  trouve dans les dossiers `ee` du dépôt, sous la Latet Enterprise License, que le paquet et l'image
  contiennent à côté de la Latet Sustainable Use License. Le fichier de licence est signé par le
  concédant de la licence : `latet server license install` l'installe, et le serveur le vérifie hors
  ligne, avec la clé publique du concédant, intégrée à Latet. Il couvre un nombre d'utilisateurs :
  chaque personne activée et chaque clé d'API activée compte pour un. Une entreprise peut demander
  un essai de 32 jours à [info@latet.ai](mailto:info@latet.ai). Quand une licence se termine, le
  serveur le dit 30 jours avant, tout continue de fonctionner pendant 30 jours de plus, puis les
  rapports, les événements et la synchronisation des termes se mettent en pause. Un essai n'a pas
  ces 30 jours de plus : une fois terminé, ses réglages Enterprise peuvent être consultés, mais pas
  modifiés. Ni l'un ni l'autre ne réduit la protection : les règles verrouillées restent en vigueur,
  et rien ne part sans être masqué. Voir [Éditions et licence](../enterprise/).
- **Règles verrouillées.** Une entreprise fixe sur son serveur un plancher sous lequel aucune
  personne et aucune clé d'API ne peut descendre : des directives au format d'un fichier de règles,
  définies pour toutes les instances avec `latet server policy set`. `latet policy check` lit
  un fichier avant sa mise en place et dit ce qu'il verrouille. Une modification sous le plancher est
  refusée, avec le nom du réglage qu'elle assouplit, et dans le tableau de bord, chaque réglage que
  fixe l'entreprise affiche un cadenas et *Défini par votre entreprise*. Avec
  `exceptions: company`, seules les exceptions de l'entreprise comptent. Voir
  [Règles verrouillées](../enterprise/locked-rules/).
- **Authentification unique (SSO), groupes et rôles.** Les personnes se connectent au tableau de
  bord avec leur compte d'entreprise par OpenID Connect (Microsoft Entra ID, Okta, Google, Keycloak
  et d'autres) ; le fournisseur d'identité crée, modifie et désactive leurs comptes et pousse ses
  groupes par SCIM 2.0 ; cinq rôles (Utilisateur, Responsable de groupe, Sécurité, Auditeur,
  Administrateur), donnés à des personnes ou à des groupes sur la page **Personnes et groupes**,
  décident qui voit et modifie quoi ; et les personnes ou les groupes qu'un administrateur choisit
  contrôlent chaque clé d'API. Voir [Connexion, groupes et rôles](../enterprise/sign-in/).
- **Rapports, journal d'administration et événements vers votre SIEM.** Le serveur compte les
  requêtes, les valeurs masquées et les requêtes arrêtées par groupe, projet, agent, clé d'API et
  type de valeur, jamais une valeur elle-même : la page **Rapports** les montre, avec des exports CSV
  et JSON, et la page **Journal d'administration** montre ce que l'administration du serveur a fait.
  Les événements et les entrées du journal partent vers le HTTP Event Collector de Splunk, vers des
  webhooks JSON ou vers syslog sur TLS. Un événement `stopped` donne pour raison l'une des valeurs de
  la liste *Pourquoi une requête a été arrêtée* de la page des rapports, `settings-changed` compris : une
  règle de SIEM écrite d'après cette liste en tient compte. Voir
  [Rapports et journal d'administration](../enterprise/reports/) et
  [Événements vers votre SIEM](../enterprise/siem/).
- **Approbations centralisées.** L'équipe de sécurité de l'entreprise décide des modifications qui
  réduisent la protection, à la place de la personne qui les a demandées ; celle-ci voit qui décide
  et peut retirer la modification, mais pas l'approuver ; les modifications d'une clé d'API vont aux
  autres personnes qui contrôlent la clé. Si l'entreprise le choisit, l'équipe de sécurité décide
  aussi des textes du Relecteur en direct. Un administrateur les active avec
  `latet server approvals set`. Voir [Approbations centralisées](../enterprise/approvals/).
- **Termes synchronisés.** Le serveur lit à intervalles réguliers les listes de clients, de
  collègues et de noms de code de projets d'une entreprise, depuis un fichier ou une adresse web
  (CSV, JSON ou YAML), Microsoft Entra ID ou un annuaire LDAP comme Active Directory, et les masque
  pour chaque personne et chaque clé d'API qu'il sert, sans que personne n'ait à les saisir. LDAP
  demande le nouveau composant optionnel `ldap`, que l'image contient. Voir
  [Synchronisation des termes](../enterprise/term-sync/).
- **Les connexions propres de Latet Enterprise, à chaque adresse.** Quand la connexion à une adresse
  échoue, le fournisseur d'identité, une sortie SIEM (Splunk, un webhook ou syslog), une source de
  termes à une adresse web, Microsoft Entra ID et un annuaire LDAP sont joints à l'adresse suivante
  que la résolution de leur nom a vérifiée, comme l'est depuis la 0.12.1 une destination propre d'une
  personne. Une fois une connexion établie, aucune autre adresse n'est essayée.
- **Ce qu'un serveur Community montre de l'édition.** `latet server status` termine la ligne de
  chaque instance par son édition, *Community Edition* ou *Enterprise Edition*, et les réglages
  partagés de l'API d'administration indiquent aussi `edition`. Quand un serveur Community
  refuse une règle définie pour tout le monde, il explique qu'avec l'édition Enterprise ces règles
  sont les directives de l'entreprise, définies avec `latet server policy set`, et indique aussi
  comment partager des règles à la main.
- **Les réponses d'un serveur aux agents et aux programmes, dans un bac à sable pour un
  navigateur.** Sur un serveur, chaque réponse sous une adresse pour les modèles (`/u/…`) et chaque
  réponse de l'API pour les applications portent
  `Content-Security-Policy: default-src 'none'; sandbox` : un navigateur qui en ouvre une n'y exécute
  aucun script et ne la traite jamais comme une page du serveur. Un cookie qu'une destination définit
  n'atteint le client que pour cette adresse, et seulement s'il ne porte pas le nom d'un des cookies
  de Latet ; le `Clear-Site-Data` d'une destination n'est plus transmis. Les clients HTTP ignorent
  ce nouvel en-tête.
- **L'image du Relecteur en direct, telle qu'elle part.** Une image en attente dans le Relecteur en
  direct est redessinée quand ses textes sont masqués de nouveau pendant qu'elle attend, après une
  modification des règles ou du coffre-fort, si bien que *Telle qu'elle part* montre ce qui part ;
  auparavant, elle gardait l'image dessinée au moment où son texte avait été mis en attente. Une
  image en attente est aussi servie au tableau de bord avec le type qu'ont réellement ses octets, PNG,
  JPEG, WebP ou GIF, jamais avec celui que sa requête déclarait, sous une politique de sécurité qui
  la place dans un bac à sable.
- **Des tests chronométrés plus stables.** Les deux tests qui mesurent le temps que met Latet à lire
  une ligne de paires `key=value` aussi longue qu'un texte peut l'être vérifient désormais que la
  ligne entière prend moins de 30 fois le temps d'un dixième de la ligne : une lecture dont la durée
  croît avec la longueur y parvient, une lecture dont la durée croît avec son carré non, quelle que
  soit la vitesse de la machine. Une machine de CI lente ne les fait plus échouer.
- **Hermes Agent avec la connexion ChatGPT.** Trois de ses requêtes étaient refusées depuis la
  0.11.0 et passent désormais, chacune sous sa forme exacte : une conversation commencée depuis sa
  ligne de commande ou sa passerelle (un identifiant de session fait de `local-` et de 64 chiffres
  hexadécimaux), la résidence des données d'un espace de travail ChatGPT
  (`x-openai-internal-codex-residency`, deux lettres minuscules ou `no_constraint`, aux seules
  adresses de ChatGPT) et une conversation reprise dont les réponses portent leur phase
  (`commentary`, `partial_answer` ou `final_answer`, sur les seuls messages du modèle). Toute autre
  valeur reste refusée. Voir [Hermes Agent](../agents/autonomous-agents/#avec-la-connexion-chatgpt).
- **Un secret devant un guillemet échappé.** Dans la sortie d'un outil qui contient un document
  JSON, un secret écrit sans guillemets juste devant un guillemet que le document écrit avec un
  échappement (`token=…\"`) était appris avec la barre oblique inverse, et sa valeur factice prenait
  la place de la barre : le document ne se lisait plus. Les barres sont désormais lues comme faisant
  partie du guillemet : seul le secret est masqué, et le document reste entier.
- **Un jeton bearer suivi de points.** Les points après un jeton, là où un texte l'abrège
  (`Bearer <token>...`) ou termine une phrase avec lui, ne sont plus lus comme en faisant partie
  quand ce qui les précède est un jeton entier et non un espace réservé comme `YOUR_API_KEY` : le
  jeton garde son unique valeur factice, et une valeur factice suivie de `...` n'arrête plus une
  requête.
- **Un nom dans la condition d'un appel.** Dans du code, `f(access_token=fallback or "")` désigne
  une variable, comme déjà `f(token=fallback)` : il n'est plus masqué comme un secret. Une valeur qui
  contient un chiffre est masquée comme avant.
- **Un secret découvert plus tard, dans l'historique de la conversation.** Quand une sortie
  ultérieure montre qu'une valeur citée au passage par une sortie précédente est un secret (un jeton
  après `Bearer`), la sortie précédente est masquée à nouveau à partir de la requête suivante, une
  seule fois, au lieu d'être renvoyée en clair. Son texte change pour le fournisseur, si bien que son
  cache de la conversation repart de là, une fois. Les autres valeurs partent toujours comme elles
  étaient parties. Les requêtes de Claude Code ne sont pas concernées : Latet les vérifie et ne les
  réécrit jamais.
- **Claude Code 2.1.323.** Chacune de ses requêtes était refusée, avec *unknown member in message*,
  et rien n'était envoyé. Ses trois nouveaux membres passent désormais, chacun seulement tel que
  Claude Code l'envoie : un message système dans la conversation qui fixe l'effort à partir de son
  tour (`output_config` avec `effort` seul), l'édition du contexte qui garde la réflexion des tours
  précédents (`context_management`, `clear_thinking_20251015` avec `keep: "all"`) et l'effort
  `xhigh`, sa valeur par défaut et l'une des cinq valeurs que prend `--effort`. L'effort `xhigh`
  passe sur toute requête à l'API d'Anthropic. Une exception de requête approuvée auparavant pour un
  membre sous `context_management` ou pour l'`output_config` d'un message, ou pour la `phase` d'un
  message sur l'API Responses d'OpenAI, attend à nouveau une approbation : Latet lit désormais ces
  membres selon leurs propres règles.
- **Un champ qu'aucun profil de requête ne prévoit encore, lu et vérifié.** Un agent cessait de
  fonctionner derrière Latet chaque fois qu'une nouvelle version de cet agent ajoutait un membre à
  ses requêtes, jusqu'à ce qu'une version de Latet nomme ce membre. Latet lit désormais un tel membre
  en entier, sur chaque requête, celles qu'il ne fait que vérifier comme celles qu'il réécrit, le
  vérifie avec la même rigueur qu'un texte qu'il masque, et l'envoie tel quel quand rien en lui ne
  serait masqué et qu'il ne nomme ni fichier, ni adresse, ni conversation enregistrée, ni modèle : un
  mode, un indicateur, un nombre ou un jeton court passe, de même qu'un objet ou une liste de
  ceux-ci. Une adresse web, un fichier, une pièce jointe, du texte libre dans un élément propre au
  fournisseur, des données encodées ou une valeur sensible arrêtent encore la requête, avec les mots
  d'avant. Un tel membre n'est jamais masqué, et son nom n'est jamais enregistré : le tableau de bord
  indique *un champ qu'aucun profil de requête ne prévoit encore, lu et vérifié* parmi ce qui a
  laissé passer une requête, et une valeur trouvée dans l'un d'eux sous le type *Nouveau membre de la
  requête*. `unknown_members: refuse` les refuse comme avant, dans vos réglages ou sur la page
  **Règles** ; les réglages d'un projet et le `latet.yaml` de son dépôt ne peuvent que les refuser,
  et avec Latet Enterprise les directives d'une entreprise peuvent verrouiller `refuse`.
  `latet doctor` et `latet server status` disent quelle valeur est en vigueur. Voir
  [Nouveaux membres des requêtes](../getting-started/upgrading-to-0-11/#nouveaux-membres-des-requêtes).
- **Le schéma d'un outil MCP, nommé.** Quand une valeur en clair dans le schéma des paramètres
  d'un outil MCP arrête les requêtes de Claude Code, où le plugin de Latet ne peut pas la masquer,
  l'erreur nomme l'outil et dit quoi faire : laisser passer la valeur si elle n'est pas sensible, ou
  désactiver le serveur. Le tableau de bord montre le bloc comme *Schéma des paramètres d'outil*,
  avec sa propre explication. Voir [Dépannage](../troubleshooting/).
- **CLAUDE.md et votre adresse e-mail, avec Claude Code 2.1.322 et les versions suivantes.** Dans une
  session interactive, Claude Code lit le contexte du premier message (vos fichiers CLAUDE.md, votre
  mémoire, votre adresse e-mail) avant de démarrer le plugin de Latet, et garde cette réponse pour
  toute la conversation. Le plugin le retenait alors, car il ne savait pas encore où vont les
  requêtes, et Claude travaillait toute la session sans lui ; rien n'était envoyé en clair.
  Désormais, le plugin le fait relire par Claude Code dès qu'il le sait, et le premier message le
  porte masqué. Avec `claude -p`, le contexte des hooks SessionStart, qui arrivait pendant que le
  plugin démarrait encore, l'attend quelques secondes au lieu d'être retenu.
- **La connexion de Claude Code au démarrage.** Au démarrage, Claude Code ouvre sa connexion par un
  appel `HEAD` à `/api/hello`, sans corps et sans identifiants, et n'en lit pas la réponse. Latet le
  refusait, puisqu'aucun profil ne le nommait : rien n'était envoyé, mais chaque démarrage laissait
  dans *Sessions* une requête bloquée, affichée comme `/other`, et `latet doctor` le citait parmi
  les appels qu'aucun profil ne couvre. Il passe désormais tel qu'il est arrivé, sous un profil à
  lui, `anthropic.hello.1` : seulement en `HEAD`, seulement vers la destination d'Anthropic, sans
  paramètre de requête ni corps, et seulement avec les en-têtes qu'un profil nomme.
- **Un secret fait d'un mot et de signes seuls.** Un secret fait d'un mot suivi de signes seuls,
  comme un jeton bearer (`Bearer abcdefghijk.........`, `Bearer abc---------`) ou une valeur après
  `token =`, ne pouvait recevoir aucune valeur factice : Latet prenait le mot pour un préfixe
  public, comme `sk-ant-` en est un, et le gardait avec les signes, si bien que la seule valeur
  factice du secret était le secret lui-même. La requête était refusée, comme chaque requête
  suivante de la même conversation ; rien n'était envoyé. Ses lettres sont désormais remplacées,
  comme celles de tout secret.
- **Une adresse et les mots autour.** Quand le modèle de noms n'était pas sûr que la ville faisait
  partie d'une adresse comme `via Garibaldi 12 a Bologna`, la rue s'arrêtait sur le mot avant la
  ville, et la valeur factice de la rue emportait ce mot avec elle (`via Mariani 90 p Bologna`). Au
  début d'une phrase, les mots avant la rue (`Abito in via Garibaldi 12`) étaient masqués avec elle.
  La rue est désormais masquée seule, et les mots autour restent tels quels.
- **La valeur factice d'un secret ne garde que le préfixe du fournisseur.** La valeur factice d'un
  secret gardait jusqu'à 24 lettres, chiffres et séparateurs du début de la valeur. Dans un jeton
  dont la partie aléatoire contient `-`, `_` ou `.`, cela allait jusque dans la partie aléatoire, et
  dans une valeur affectée à un nom (`my-company-prod-…`), cela gardait les mots de la valeur : ces
  caractères partaient chez le fournisseur du modèle dans la valeur factice. Une valeur factice ne
  garde désormais que le préfixe que le fournisseur met devant chaque clé de ce type (`ghp_`,
  `sk-ant-api03-`, `AKIA`, et le `latet_pat_` de Latet), et tire à nouveau chaque autre lettre et
  chaque autre chiffre ; un secret de tout autre type ne garde aucune de ses lettres ni aucun de ses
  chiffres, seulement ses signes (`-`, `_`, `.`) à leur place. Une valeur factice tirait aussi un
  chiffre là où la valeur a un chiffre, une majuscule là où elle a une majuscule et une minuscule là
  où elle a une minuscule : elle montrait ainsi à quelles places un mot de passe comme
  `Summertime2024`, ou une clé, a un chiffre, une majuscule ou une minuscule. Désormais, chaque suite
  de lettres et de chiffres entre des signes est tirée, à chaque place, parmi toutes les sortes de
  caractères qu'elle contient : une suite de chiffres hexadécimaux parmi les seize (`9f2c4e…`),
  `Summertime2024` parmi les 62 lettres et chiffres ; un de vos reconnaisseurs avec `class: secret`
  garde la forme qu'il documente (`surrogate: shape` : la même sorte de caractère à chaque place).
  Une valeur factice déjà dans votre coffre-fort reste
  telle quelle, pour qu'une conversation en cours voie la même valeur factice ; seule une valeur
  retirée avec *Oublier* reçoit une valeur factice de la nouvelle sorte quand elle revient.
- **Le port de Latet, réservé sous Windows.** Hyper-V, WSL et Docker réservent des blocs de ports à
  chaque démarrage de Windows, dans la plage de ports dynamiques de Windows ; là où un programme a
  fait descendre cette plage au-dessous de 49152, un bloc peut prendre le port 5283. Le daemon ne
  pouvait alors pas démarrer jusqu'au prochain redémarrage, et le conseil que donnait Latet,
  `latet global-install`, ne servait à rien quand le même bloc occupait aussi ses ports de repli. La
  nouvelle commande `latet reserve-port` réserve le port de Latet avec une seule confirmation d'un
  administrateur, de sorte que Windows ne peut plus le prendre ; là où Windows l'occupe déjà, elle
  fait passer Latet sur un port libre, avec les agents que `latet global-install` a configurés, et
  réserve celui-ci. Elle demande avant de changer quoi que ce soit, un agent ne peut pas la lancer,
  et `latet reserve-port --remove` retire les ports qu'elle a réservés. Le script d'installation pour
  Windows la propose à la fin là où Windows peut prendre le port ; `latet start`, `latet claude` et
  les autres lanceurs la nomment là où Windows occupe le port ; `latet doctor` la nomme aussi, avec
  un échec là où Windows occupe le port et un avertissement là où Windows peut le prendre ; et
  `latet global-uninstall` propose de retirer les réservations. `latet global-install` cherche
  désormais un port libre au-delà des ports que Windows occupe, et non plus seulement de 5284 à
  5335, et un port qui se trouve seulement dans une exclusion de port d'un administrateur ou dans
  une plage IPv6 n'est plus pris pour un port que Windows occupe. Voir
  [Le port 5283 est déjà utilisé](../troubleshooting/#le-port-5283-est-déjà-utilisé).
- **Les secrets dans les sessions enregistrées de vos agents.** La nouvelle commande `latet leak`
  lit les sessions que Claude Code, Codex, OpenCode, Copilot CLI, Gemini CLI, Qwen Code et Hermes
  Agent ont enregistrées sur cette machine, avec les détecteurs de Latet, et liste les secrets
  qu'elles contiennent en trois listes : en clair, sans signe que Latet les ait remplacés, donc
  peut-être arrivés chez le fournisseur du modèle ; remplacés par Latet, dont la valeur factice est
  partie à leur place ; et caviardés. Un secret montre son type, un indice qui ne garde que le
  préfixe public de son fournisseur (`ghp_…`), les agents, les sessions, les dossiers et les dates,
  jamais sa valeur ; les clés d'un fournisseur, à remplacer en premier, viennent en tête. La commande
  ne modifie rien et n'a pas besoin du réseau, et le coffre-fort de Latet n'en apprend rien. Voir
  [Secrets dans les sessions enregistrées](../concepts/leaks/).
- **Les versions allemande, française, italienne et espagnole, relues.** La documentation et le
  tableau de bord en allemand, en français, en italien et en espagnol ont été relus phrase par phrase
  en regard de l'anglais. Là où ils disaient autre chose que l'anglais, ou moins, ou davantage, ils
  ont été corrigés, ainsi que leurs termes et leurs tournures.
- **La recherche de la documentation en italien, en allemand et en français.** Dans la
  documentation en italien, en allemand et en français, la recherche répondait en anglais
  (*17 results for eccezione*) : Starlight ne donne ses mots à la recherche qu'en espagnol, et les
  traductions de Pagefind n'atteignent jamais une page construite ainsi. Elle répond désormais dans
  la langue de la page (*17 risultati per eccezione*).
- **Dépendances.** Le composant optionnel `server` apporte PyJWT, qui vérifie le jeton d'ID d'une
  connexion avec un compte d'entreprise ; le nouveau composant optionnel `ldap` installe ldap3 et
  pyasn1, pour la synchronisation des termes depuis LDAP ; et httpcore, qu'apportait déjà httpx,
  devient une dépendance à part entière. Les mentions des composants tiers les citent.
- **Mettre à jour.**
  - **Un serveur : les tables changent.** La 0.15.0 ajoute les tables de l'édition Enterprise à la
    base de données de chaque serveur, vides et inutilisées sans licence, si bien que les instances
    ne peuvent pas être remplacées une à une. Faites une copie de la base avec `pg_dump` et lancez
    `latet server migrate --check` avec la 0.15.0, qui indique que les tables changent ; puis arrêtez
    toutes les instances, lancez `latet server migrate` une fois avec la 0.15.0 et démarrez les
    instances avec cette version, comme le montre
    [Exploitation du serveur](../concepts/server-operations/#mettre-à-jour-et-ajouter-des-instances).
    Dans l'exemple Compose, `ghcr.io/latet-ai/latet:latest` est désormais la 0.15.0 : récupérer cette
    image demande les mêmes étapes. Parmi ses migrations figure la 018 (`server_notes`), où le
    serveur note qu'il a enregistré la nouvelle valeur par défaut pour les nouveaux membres des
    requêtes, ci-dessous : `latet server migrate` l'applique avec les autres. Pour revenir en
    arrière, restaurez la copie et démarrez la 0.12.1.
  - **Une machine :** `latet upgrade` (dans Docker, `docker compose pull` puis
    `docker compose up -d`), et rien d'autre : l'édition Enterprise ne concerne que les serveurs.
  - **Sans licence, l'édition Enterprise ne change rien :** aucune page Enterprise et aucun réglage
    Enterprise. Un script qui lit `latet server status` trouve l'édition à la fin de la ligne de
    chaque instance.
  - **Nouveaux membres des requêtes : chaque installation, serveurs compris, passe à `check` ;
    indiquez `refuse` pour garder le refus d'aujourd'hui.** À partir de la 0.15.0, un membre d'une
    requête qu'aucun profil de requête ne nomme encore est lu et vérifié, et non plus refusé, sans
    approbation. Pour garder le refus, indiquez `unknown_members: refuse` : dans `config.yaml` sur
    une machine, dans les réglages d'une personne ou d'une clé d'API sur un serveur, ou pour tous
    dans les directives de l'entreprise avec Latet Enterprise. Sur une machine, `latet doctor` le
    dit tant que `config.yaml` n'a pas cette clé ; `latet server status` donne la valeur du serveur
    sur une ligne à part, le premier démarrage de la 0.15.0 d'un serveur écrit une entrée
    `setting.defaulted` dans le journal d'administration, et une approbation qui ramène des réglages
    de `refuse` à `check` écrit `setting.loosened`.
  - **Pour essayer Latet Enterprise :** demandez un essai à [info@latet.ai](mailto:info@latet.ai),
    et, pour la synchronisation des termes depuis LDAP, installez le composant optionnel `ldap`.
  - **Les mots des clés ne sont pas refaits :** la 0.15.0 ne refait les mots d'aucune clé d'API.

## 0.12.1 (2026-11-09)

- **Une destination jointe à ses autres adresses.** Sur un serveur, une destination propre d'une
  personne dont le nom a plusieurs adresses est jointe à la suivante, dans l'ordre donné par le
  système, quand la connexion à la première est refusée ou ne se fait pas à temps, avant que rien de
  la requête ne soit envoyé. Un système peut donner d'abord l'adresse IPv6 d'un nom (Windows le fait
  pour `localhost`), et un fournisseur qui ne répond qu'à son adresse IPv4 est alors joint là ; une
  connexion encore ouverte vers l'une d'elles sert en premier, si bien qu'une adresse qui refuse ne
  coûte son temps qu'une fois tant que cette connexion reste ouverte. Seules les adresses vérifiées
  lors de cette même résolution sont essayées, huit au plus, dans le temps dont dispose une
  connexion ; une requête qui en a joint une n'est jamais envoyée à une autre, et un nom dont une
  adresse quelconque est dans le réseau du serveur est toujours refusé.
- **Une restauration refusée pendant un changement de réglages n'est plus journalisée.** Une
  restauration par l'API des applications était refusée (`503`, *send it again*) quand les réglages
  de la clé commençaient à être appliqués pendant qu'on y répondait. Elle ne renvoyait rien, mais
  laissait quand même une entrée `apps.unmasked` dans le journal d'administration, et sa nouvelle
  tentative une deuxième. Une restauration refusée dès son arrivée, parce que les réglages étaient
  déjà en train d'être appliqués, n'en laissait aucune. L'entrée n'est maintenant écrite que pour
  une restauration qui reçoit sa réponse, si bien que chaque entrée est une restauration qui a rendu
  ses textes. Une analyse, un masquage ou une restauration refusés pendant qu'on y répondait ne sont
  plus comptés non plus dans `latet_apps_texts_total` et `latet_apps_values_total`.

## 0.12.0 (2026-11-09)

- **Une requête dont les réglages changent avant son envoi.** Une requête dont les réglages changent
  entre sa décision et son envoi est refusée comme une requête dont les réglages ont changé pendant
  sa préparation : son agent apprend qu'il faut l'envoyer à nouveau et l'historique enregistre un
  refus, là où elle recevait avant un `500` *Latet could not send this request as it was decided* et
  était enregistrée comme un échange. Une requête préparée et envoyée pendant qu'un enregistrement est
  encore en train d'être appliqué ne part plus avec des réglages à moitié appliqués : elle est
  préparée à nouveau une fois qu'ils sont tous en place, ou refusée de la même façon. Un
  enregistrement qui échoue en cours de route remet les réglages d'avant ; si même ceux-ci ne peuvent
  pas être remis, chaque requête est refusée de la même façon jusqu'à ce qu'un enregistrement suivant
  réussisse ou que Latet redémarre.
- **Nouveaux en-têtes de Claude Code.** Sur une requête que Latet ne fait que vérifier (Claude Code,
  Bedrock, Vertex), un en-tête `anthropic-*` qu'aucun profil ne nomme encore, ou une valeur
  d'`anthropic-beta` d'une forme nouvelle, passe une fois lu par la vérification finale, signalé par
  *seulement vérifiée*, comme le fait déjà un nouveau membre du corps qui contient du texte ; un nouvel
  en-tête `x-claude-code-*` est retiré et compté. Claude Code continue de fonctionner avec une version
  qui en ajoute un avant que Latet ne le nomme. Un secret ou l'une de vos valeurs dans un tel en-tête
  est toujours refusé.
- **Mettre à jour un serveur depuis la 0.10.** Calculer les empreintes d'un coffre-fort conservé par
  la 0.10 n'occupe plus l'une des deux constructions d'index d'une instance : les index des autres
  identités sont construits pendant ce temps, et seules les identités dont les empreintes ne sont
  pas encore toutes calculées sont servies par la recherche dans PostgreSQL, comme le disent les
  étapes de la mise à jour.
- **Python 3.14.** Latet fonctionne aussi sous Python 3.14, sur lequel il est testé comme sur 3.12
  et 3.13. Une nouvelle installation sur un ordinateur sans Python reçoit 3.14 ; `latet upgrade` ne
  fait passer un Latet déjà installé à 3.14 que là où un 3.14 est déjà présent.

## 0.11.1 (2026-11-06)

- **Les en-têtes d'indication de Claude Code.** Réglé pour envoyer ses en-têtes d'indication aux
  passerelles (`CLAUDE_CODE_GATEWAY_HINT_HEADERS=1`), Claude Code n'est plus refusé après une
  compaction ou un appel d'outil : `x-claude-code-compaction` et `x-claude-code-context-compacted`
  passent, limités à leurs trois valeurs, et `x-claude-code-prev-tool-durations`, qui nomme les outils
  exécutés, est retiré et compté, comme les identifiants de ses sous-agents.
- **Une requête envoyée pendant que les réglages changent.** Une requête préparée pendant que ses
  réglages étaient appliqués à nouveau (un enregistrement, ou sur un serveur une modification
  partagée) est préparée à nouveau selon les nouveaux, comme Latet le faisait déjà quand seules les
  règles changeaient, au lieu d'être refusée avec *could not account for every part of this request*.
  Une requête dont la route a changé entre-temps, ou dont les réglages ont changé après qu'elle a été
  préparée, n'est toujours pas envoyée, et son agent apprend que les réglages ont changé et qu'il faut
  l'envoyer à nouveau ; l'historique, le journal et le tableau de bord disent la même chose, sous une
  raison qui lui est propre.
- **Une réponse diffusée en entier est enregistrée en entier.** Une réponse diffusée qui a atteint son
  client en entier est enregistrée en entier même quand le client ferme la connexion juste après,
  comme l'était déjà un tour WebSocket ; avant, l'historique la montrait comme une erreur, sans son
  texte.
- **Un WebSocket que le fournisseur refuse figure dans l'historique.** Un WebSocket dont le
  fournisseur refuse (`401`, `403`) ou redirige la poignée de main, ou dont le fournisseur est
  injoignable, est enregistré dans l'historique comme la même réponse sur HTTP ; avant, il ne laissait
  aucune trace.
- **L'index des exceptions de requête du serveur.** Un espace construit sur plusieurs workers ou
  instances à la fois ne réécrit plus à chaque fois l'index des exceptions de requête de l'identité : il
  n'est écrit que s'il a changé, par un seul à la fois, si bien que le journal n'avertit plus qu'il n'a
  pas été écrit, et `latet server status` ne liste jamais une exception révoquée entre-temps.
- **Une route disparue entre deux tentatives AWS.** Quand une route Bedrock est retirée entre les deux
  tentatives AWS d'une requête (la première ayant reçu un jeton expiré), l'agent apprend qu'aucune
  autre tentative n'a été envoyée et que la première a pu arriver, au lieu du seul *no upstream for
  this path*.
- **Qui a approuvé une exception de requête.** Une exception approuvée dont l'`issuer` (qui l'a
  approuvée) a été changé à la main attend à nouveau une approbation, même quand son heure
  d'approbation a aussi été avancée ; avant, cette heure avancée la laissait en vigueur avec le
  nouvel `issuer`.

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
