---
title: Règles verrouillées
description: Des directives que votre entreprise fixe sur son serveur Latet, en dessous desquelles aucune personne et aucune clé d'API ne peut descendre.
---

Avec Latet Enterprise, une entreprise fixe un **plancher** sur son [serveur Latet](../../getting-started/server/) :
des réglages en dessous desquels aucune personne et aucune clé d'API ne peut descendre. Chaque personne garde ses
propres réglages et peut toujours ajouter de la protection. Une modification qui passerait sous le plancher est
refusée, avec un message indiquant que le réglage est défini par l'entreprise ; elle est refusée, et non mise en attente
d'approbation, puisque la personne ne peut pas l'approuver. Une modification n'est refusée que pour ce qu'elle
abaisse elle-même : des réglages qui étaient déjà sous le plancher n'empêchent pas de modifier autre chose.

## Le fichier de directives

Les directives sont un fichier de règles, au format qu'écrit `latet rules export` (voir
[Exportation et importation](../../concepts/export-import/)) : une entreprise peut donc partir d'une exportation :

```yaml
latet_rules: 1
exceptions: company
profile: max
terms:
  - {value: Project Falcon, type: PROJECT}
  - {value: Northwind Traders, type: CLIENT}
policies: {IBAN: redact}
ocr: auto
```

**Ce que dit le fichier est verrouillé, et ce qu'il omet reste au choix de chaque personne.** Avec ce fichier, personne ne peut choisir
un profil inférieur à `max` ni cesser de lire le texte des images, Project Falcon et Northwind Traders sont toujours
masqués, les IBAN sont toujours caviardés, et seules les exceptions de l'entreprise comptent (elle n'en fixe aucune
ici) ; chacun peut tout de même ajouter ses propres termes, reconnaisseurs et valeurs plus strictes. Pour verrouiller
un réglage à la valeur par défaut de Latet, écrivez-le dans le fichier.

- **Le profil apporte ce qui l'accompagne :** avec `profile: max`, les numéros d'identification de chaque pays, les
  adresses IP privées, le mode données strict et la règle de max pour les images sont verrouillés eux aussi.
- **Les listes sont un minimum :** les termes, reconnaisseurs, règles de champ, types de données personnelles, pays et
  régions téléphoniques des directives, ainsi que les fichiers et les adresses web que couvre le mode données strict,
  sont toujours en vigueur, et ceux de chaque personne s'y ajoutent. Un terme, un reconnaisseur ou une règle de champ
  d'un type que les directives autorisent (`policies: {CLIENT: allow}` et un terme de type `CLIENT`) est en revanche
  une exception de l'entreprise : il n'est ajouté aux réglages d'aucune personne, et une personne qui masque cette
  valeur par un terme ou un reconnaisseur qui lui est propre continue de la masquer.
- **Les reconnaisseurs et les règles de champ de l'entreprise s'utilisent à côté de ceux de chaque personne :** un
  reconnaisseur ou une règle de champ propre à une personne n'est jamais mis hors service par ceux de l'entreprise. Quand un
  reconnaisseur d'une personne a le nom ou le type d'un reconnaisseur de l'entreprise, celui de l'entreprise est
  utilisé sous un nom ou un type qui lui est propre ; quand une règle de champ d'une personne a le nom d'une règle de
  l'entreprise, les deux sont fusionnées, et le champ est masqué.
- **Les consignes pour le relecteur local** contenues dans les directives lui sont toujours données, avant celles de
  la personne.
- **Dans chaque projet aussi :** Latet reste activé dans chaque projet, et les réglages propres à un projet ne le
  font jamais passer sous le plancher.
- **Nouveaux membres des requêtes :** `unknown_members: refuse` refuse, pour chaque personne et chaque clé d'API, un
  membre d'une requête qu'aucun profil de requête ne nomme encore, comme Latet le faisait avant la 0.15.0, un texte ou
  une liste de textes hors d'un élément du fournisseur passant toujours, sur une requête que Latet ne fait que vérifier
  (Claude Code, Bedrock, Vertex), une fois lu par la vérification finale ([Nouveaux membres des
  requêtes](../../getting-started/upgrading-to-0-11/#nouveaux-membres-des-requêtes)) ; `unknown_members: check` ne
  verrouille rien, puisque chacun peut les refuser.

`latet policy check policy.yaml` lit un fichier avant qu'il soit mis en place : ce qu'il verrouille, ce qu'il laisse à
chaque personne, de qui les exceptions comptent, et ce qui n'a pas pu y être lu. Un réglage qui contient une erreur, ou
qui est écrit sans valeur (`ocr:` seul), n'est pas lu : il ne verrouille donc rien. La commande n'ouvre aucune
connexion, et se termine avec le code 0 quand tout va bien, 1 quand quelque chose dans le fichier n'est pas lu, et 2
quand le fichier ne peut pas être lu du tout.

## Exceptions

Les valeurs et les motifs à ne jamais masquer, les règles de secrets désactivées et ce qui laisse des valeurs en clair
(un reconnaisseur dont la politique est `allow`, `allow` pour un type dans `policies`, une règle de champ avec
`mask: false`) sont les exceptions. La clé `exceptions` des directives dit de qui elles comptent :

- **`personal`** (par défaut) : chaque personne peut toujours marquer une valeur comme **Non sensible** pour
  elle-même, et garde ses propres choix `allow`, sauf pour les types des termes que l'entreprise masque.
- **`company`** : seules les exceptions des directives comptent. Celles qui sont propres à une personne sont refusées
  quand elle les ajoute, et non utilisées là où elles existaient déjà : ses valeurs et motifs à ne jamais masquer, ses
  règles de secrets désactivées, ses reconnaisseurs qui laissent en clair ce qu'ils trouvent, ses `allow` pour un
  type que l'entreprise n'autorise pas, et ses règles de champ avec `mask: false`. Une règle de champ qui lui est
  propre, d'un type que l'entreprise autorise (`policies: {URL: allow}` dans les directives, et une règle de champ
  personnelle de type `URL`), est aussi une exception, car les valeurs du champ resteraient en clair, même là où le mode
  données strict les masquerait : elle est refusée quand elle l'ajoute, elle n'est pas utilisée là où elle existait
  déjà, et **Règles** dit combien des siennes sont laissées de côté ainsi. Là où un tel type est masqué dans l'un de
  ses projets, la règle de champ est gardée, et son type est masqué partout où elle aurait laissé le champ en clair. Un
  terme personnel d'un tel type est gardé, et enregistré quand elle l'ajoute : il ne laisse jamais en clair rien de ce
  que l'entreprise masque (un terme de l'entreprise, ce que trouvent un reconnaisseur ou une règle de champ de
  l'entreprise, les données personnelles) ni le domaine d'une adresse, partout où le masquage le masquerait sans ses
  termes. Entre ses propres termes, la correspondance la plus longue l'emporte, comme sans directives. Son identité
  (son nom, ses noms d'utilisateur, ses adresses e-mail et ses noms d'hôte), d'un type que l'entreprise autorise, est
  gardée de la même façon : elle ne laisse jamais en clair ce que le masquage masquerait sans elle. Là où son compte
  est lié au fournisseur d'identité de l'entreprise (SCIM, ou l'authentification unique) et où l'entreprise laisse les
  noms (`PERSON`) en clair, son nom est celui que donne le fournisseur là où ses propres réglages laissent aussi les
  noms en clair, dans chacun de ses projets ; sinon son propre nom reste utilisé, et il est masqué partout où les noms
  le sont. Dans les deux cas, le changer depuis ses réglages est refusé ; une machine ne le renseigne que là où aucun
  n'est défini.

Les termes de l'entreprise d'un type qu'elle n'autorise pas restent masqués, tels que l'entreprise les a écrits. Ils
sont verrouillés dans le masquage lui-même : un terme est masqué quels que soient la valeur à ne jamais masquer, le
motif, le reconnaisseur ou la politique `allow` qu'une personne définit, et personne ne peut en marquer un comme **Non
sensible**. Quand une personne a autorisé un texte plus long qui contient un terme, le reste demeure comme elle l'a
autorisé, et le terme est masqué. Une valeur de `exceptions` que Latet ne peut pas lire, ou une clé `exceptions` sans
valeur (`exceptions:` seul), compte comme `company`, la plus stricte, et `latet policy check` le dit.

Avec `personal`, seuls les termes de l'entreprise sont protégés de cette façon. Les exceptions propres à une personne
peuvent encore laisser en clair ce que trouve un reconnaisseur ou une règle de champ de l'entreprise : un texte plus
long qu'elle a autorisé et qui le contient, ou un terme, un reconnaisseur ou une règle de champ personnels d'un type
qu'elle autorise. Pour l'exclure, utilisez `exceptions: company`.

Les exceptions de requête (les [exceptions
exactes](../../getting-started/upgrading-to-0-11/#approuver-une-exception-exacte) qui laissent passer un en-tête, un
paramètre de requête ou un membre du corps) suivent la même clé. Avec `personal`, celles d'une personne sont les
siennes : elle les approuve comme sans directives, et on décide de celles d'une clé d'API comme de ses autres
modifications.
Avec `company`, elles sont celles de l'entreprise : qui en demande une la propose, et elle attend l'équipe de sécurité
dans **Approbations de l'entreprise**, quelle que soit la personne qui décide du reste de ses modifications ; sa propre
approbation, ou celle des personnes qui contrôlent une clé d'API, n'en accorde aucune. Cela vaut aussi pour celles
approuvées avant que les directives disent `company`, permanentes comprises : dès que le serveur applique les
directives, elles n'accordent rien, le tableau de bord affiche chacune comme *Pas en vigueur : l'équipe de sécurité de
votre entreprise décide des exceptions de requête, et n'a pas approuvé celle-ci*, et `latet server status` la liste
comme `forbidden` parmi les exceptions qui n'accordent rien à présent, une fois que son espace a appliqué les
directives : un espace qui n'a été ouvert sur aucune instance depuis le changement des directives y montre ses exceptions telles
qu'elles étaient jusqu'à ce qu'il soit reconstruit, tandis que ses requêtes sont déjà jugées selon les nouvelles
directives. Demandée à nouveau, une exception attend l'équipe de sécurité ; ces exceptions comptent de nouveau dès que
l'entreprise n'en décide plus : quand les directives reviennent à `personal` ou sont supprimées, quand la licence est
supprimée, ou quand une licence qui n'inclut pas les règles verrouillées est installée. Sans les approbations
centralisées dans la licence, personne ne peut en approuver une : une exception demandée attend une approbation qui ne
peut que la rejeter (qui l'a demandée peut la retirer, et sa propre approbation est refusée avec un message indiquant
que l'équipe de sécurité en décide), et elle n'accorde rien. Pour que les personnes puissent avoir des exceptions de requête avec
`company`, la licence doit comprendre `approvals` ; sans cela, `company` verrouille quand même toutes les autres
exceptions. L'enregistrement des réglages d'une clé d'API par l'administration approuve toujours les exceptions qu'il
ajoute. Avec l'une ou l'autre valeur, une exception en attente n'accorde rien et n'est jamais refusée comme passant sous
les directives : ce que les directives jugent, c'est son approbation, quand elle vient.

## Sur un serveur Latet

Avec [Latet sur un serveur](../../getting-started/server/), un administrateur fixe les directives depuis n'importe
quelle machine qui dispose du jeton d'administration du serveur :

```sh
latet policy check policy.yaml
latet server policy set policy.yaml
```

`latet server policy set` lit d'abord le fichier, et ne change rien s'il ne peut pas être lu. Ce qu'il ne peut pas
lire dans le fichier est signalé avant tout envoi, et il demande confirmation : un réglage qui contient une
erreur est laissé de côté en entier, si bien qu'un seul terme mal formé laisse tous les termes du fichier hors des
directives. Il demande aussi confirmation avant de fixer des directives qui verrouillent moins de réglages que celles
du serveur, qui permettent de nouveau à chaque personne d'ajouter des exceptions là où seules celles de l'entreprise
comptaient, ou qui remplacent des directives qu'il ne peut pas lire ici ; dans votre propre terminal, il demande
confirmation, sinon `--yes` indique que vous êtes d'accord. Il fixe le fichier à la place des directives avec
lesquelles il l'a comparé : si quelqu'un modifie entre-temps les directives du serveur, sur la page **Directives** ou
avec un autre `latet server policy set`, le serveur refuse le changement, et la commande relit les directives, les
compare de nouveau et redemande confirmation. Un agent IA est refusé : fixer ou supprimer les directives revient à une
personne. Le `PUT` de l'API d'administration refuse un fichier dont un réglage est laissé de côté en entier (400, en
nommant le réglage), sauf si la requête dit aussi `"accept_notices": true`. Un `PUT` qui indique aussi `"etag"`, les
directives qu'il remplace telles que le `GET` les a données (`null` quand aucune n'est conservée), ou un `DELETE` dont
le corps indique de même les directives qu'il retire, est refusé avec 409 quand ce ne sont plus les directives
conservées.

Chaque personne et chaque clé d'API du serveur est relevée aux directives : chaque instance relève chaque espace dès
qu'elle est prévenue, et vérifie de nouveau toutes les 10 secondes, de sorte qu'une instance qui a manqué le message
dispose des directives en 10 secondes au plus. Le serveur les garde dans sa base de données, leur texte scellé avec la clé
principale (les termes qu'elles contiennent sont des valeurs), et consigne chaque modification dans le journal
d'administration, avec les réglages qu'elle verrouille et jamais une valeur. `latet server policy show` dit quand et
par qui elles ont été fixées, ce qu'elles fixent et si elles sont en vigueur, et `latet server policy show --file
policy.yaml` réécrit leur texte dans un fichier, pour le modifier. `latet server policy remove` les retire, après
avoir demandé confirmation : la commande retire les directives qu'elle a lues, et si quelqu'un modifie entre-temps
les directives du serveur, le serveur refuse le retrait, et la commande relit les directives et redemande
confirmation.

Avec `roles` dans la licence, les personnes du rôle Sécurité les fixent aussi depuis le tableau de bord
([Connexion, groupes et rôles](../sign-in/)) : sur la page **Directives**, elles en modifient le texte, puis choisissent
**Enregistrer** ou **Supprimer**. Un texte que le serveur ne peut pas lire est refusé avec la raison, et les directives
en vigueur restent. Un enregistrement nomme les directives qu'il remplace : si quelqu'un les a modifiées depuis
l'ouverture de leur texte, il est refusé, et la page montre les directives telles qu'elles sont maintenant. Le texte des
directives contient les termes et les valeurs de l'entreprise : tant que le serveur demande la connexion d'entreprise
(`company_changes: sso`, la valeur par défaut une fois l'authentification unique configurée), le lire et le modifier
demandent une session ouverte avec le compte d'entreprise. Chaque modification figure dans le journal d'administration,
au nom de la personne qui l'a faite.

**Les directives s'appliquent sur chaque instance au fur et à mesure de sa mise à jour.** Une instance d'une version
antérieure, ou sans l'édition Enterprise, n'applique aucune directive. Fixez les directives une fois que chaque
instance exécute cette version, ou fixez-les de nouveau à ce moment-là : `latet server policy set` et le `PUT` de
l'API d'administration avertissent, en disant combien d'instances actives ne les appliquent pas. Voir
[Mettre à jour et ajouter des instances](../../concepts/server-operations/#mettre-à-jour-et-ajouter-des-instances).

Tant que les directives verrouillent des termes (l'un de leurs termes d'un type qu'elles n'autorisent pas) ou disent
`exceptions: company`, le serveur lit chaque texte d'un appel à l'[API pour les applications](../../reference/apps-api/)
séparément, comme le faisait Latet 0.10, là où il lit sinon les textes d'un appel ensemble : le masquage reste le même,
et un appel de nombreux textes prend plus de temps. Les termes synchronisés font de même tant que l'un d'eux est masqué
([Synchronisation des termes](../term-sync/)).

Les réglages partagés (`latet server config`, `latet server rules`) restent tels qu'un administrateur les fixe : ce
qui s'y trouve sous les directives est relevé dans chaque espace, comme les réglages propres à une personne. Fixer les
réglages d'une clé d'API sous les directives est refusé : `latet server config --key ... set` se termine avec le
code 2.

## Ce que voient les personnes

- **Dans le tableau de bord**, un réglage que fixe l'entreprise affiche un cadenas et **Défini par votre entreprise**
  dans **Règles** et dans les règles d'un projet. Une personne apprend ce qui est verrouillé et combien il y a de
  termes, d'exceptions et de règles, par exemple que 2 termes de l'entreprise restent masqués ; jamais les termes, ni
  aucune autre valeur de l'entreprise. Sur un serveur, la page **Directives** est destinée au rôle Sécurité, qui y lit
  et y modifie leur texte, et aux auditeurs, qui y lisent ce qu'elles verrouillent ; tant que la licence n'a pas
  `roles`, tout le monde lit ce qu'elles verrouillent. Les administrateurs lisent les directives avec
  `latet server policy show`.
- **Une modification refusée nomme le réglage :** `Set by your company, not changed: profile: from max to balanced`.
  Le tableau de bord répond par un conflit (409) et montre le refus là où la modification a été faite, et dans le
  terminal la commande se termine avec le code 2. Une seule modification qui abaisse les réglages de plus de 20
  dossiers de projet est refusée en entier : modifiez moins de dossiers à la fois.
- **Vos propres réglages restent comme vous les avez définis :** ce qui est sous le plancher est relevé pendant son
  utilisation, et vos réglages gardent ce que vous avez écrit : ils s'appliquent tels quels si les directives sont
  retirées.
- **`latet server status`** a une ligne pour les directives : quand elles ont été fixées (en UTC), et un
  avertissement quand elles ne sont pas en vigueur ou ne peuvent pas être lues.

## La licence

Les directives sont en vigueur avec une licence Latet Enterprise qui inclut les règles verrouillées
([Éditions et licence](../)). La protection ne baisse jamais : quand la licence expire, que son nombre d'utilisateurs
est dépassé, qu'elle est en pause ou qu'un essai se termine, les directives restent en vigueur exactement comme elles sont. Fixer ou retirer
les directives d'un serveur demande une licence qui peut encore gérer les réglages Enterprise : quand un essai se
termine, les directives sont figées, et `latet server policy show` les montre encore, jusqu'à l'installation d'une
licence.

Les directives cessent d'être en vigueur dans deux cas seulement : quand la licence est retirée, et quand on installe
une licence qui ne liste pas les règles verrouillées. `latet server license remove` et `latet server license install`
le disent, demandent confirmation d'abord et refusent un agent IA ; les directives du serveur sont conservées, et
sont de nouveau en vigueur avec une licence qui liste les règles verrouillées.

## Les limites

- **Seulement sur un serveur Latet :** l'édition Enterprise est celle d'un serveur. Latet sur une machine sans
  serveur est l'édition Community, quoi qu'on y installe, et n'a pas de directives.
- **Une machine connectée à un serveur Latet** passe par le masquage du serveur, selon les directives du serveur.
- **Une instance qui ne peut pas lire les directives :** des directives conservées mais illisibles (créées par une
  version plus récente, ou scellées avec une autre clé principale) laissent une instance qui avait déjà lu des
  directives appliquer les dernières qu'elle a lues : ses espaces restent sur ce plancher plus ancien, qui peut être
  plus bas que le nouveau, et `latet server status` dit que les nouvelles directives ne peuvent pas être lues. Une
  instance qui n'a jamais lu de directives, ou qui n'a pas encore lu la licence, ne sert aucune personne ni aucune clé
  d'API tant qu'elle ne le peut pas : la requête est refusée par un 503 qui dit de réessayer.
  `latet server policy remove` répare des directives qui ne peuvent pas être lues ; les fixer de nouveau avec une
  version que chaque instance exécute les répare aussi.
- **La base de données est de confiance :** quiconque peut écrire dans la base PostgreSQL du serveur peut supprimer la ligne
  `ee_policy`, ou en remettre une plus ancienne. Le plancher baisse alors, ou revient aux directives plus anciennes,
  sur chaque instance, et aucune entrée du journal d'administration ne le dit. Les entrées `policy.changed` du
  journal, chacune avec l'etag des directives fixées, sont la trace à laquelle comparer les directives en vigueur
  (`latet server policy show`) ; laissez l'accès en écriture à cette base au seul serveur.
- **Le texte dans les images :** des directives qui verrouillent `terms` mais pas `ocr` et `images` laissent une
  personne libre de désactiver la lecture du texte dans les images, et un terme de l'entreprise dans une image n'est
  alors pas lu. Verrouillez aussi `ocr: auto` et `images` pour que les termes couvrent les images.
- **Un agent pointé directement vers un fournisseur d'IA** n'est masqué par personne. Un serveur n'est le point de
  contrôle de l'entreprise que lorsque son réseau ne permet d'atteindre les fournisseurs que depuis le serveur
  ([Latet sur un serveur](../../concepts/server/)).
