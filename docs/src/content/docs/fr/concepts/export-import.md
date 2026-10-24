---
title: Exportation et importation
description: Vos règles dans un fichier pour un collègue, et toute votre installation dans un fichier chiffré pour une autre machine.
---

Deux choses différentes peuvent voyager :

- **Vos règles, pour un collègue :** termes, reconnaisseurs, politiques et le reste, dans un fichier
  YAML. Une fois importées, elles s'ajoutent à celles du collègue.
- **Votre installation, pour une autre machine :** vos réglages et, si vous le demandez, le coffre-fort
  avec sa clé, dans un seul fichier chiffré.

## Vos règles, pour un collègue

```sh
latet rules export acme-rules.yaml     # here
latet rules import acme-rules.yaml     # on your colleague's machine
```

Ou **Exporter les règles** et **Importer des règles** en haut de la page **Règles** du tableau de
bord : l'importation affiche ce qu'elle ferait avant que quoi que ce soit ne change. Les tableaux
*Termes*, *Reconnaisseurs* et *Valeurs autorisées* ont leur propre *Exporter* et *Importer*, pour
cette liste seule : les termes et les valeurs en CSV, les reconnaisseurs en fichier de règles. Une
telle exportation garde les mêmes précautions : un reconnaisseur dont les exemples contiennent une
valeur que Latet a vraiment vue reste dehors, et une cellule CSV qu'un tableur exécuterait comme
une formule est écrite après une apostrophe. Une telle importation passe par le même aperçu,
ajoute sans jamais remplacer, et retient de la même façon ce qui baisse la protection ; le tableau
*Reconnaisseurs* prend un fichier de reconnaisseurs seuls.

**Ce que contient le fichier :**
- les termes, les reconnaisseurs, les politiques et les règles de champ ;
- les types de données personnelles, et les pays dont les numéros d'identité et de téléphone sont
  reconnus ;
- le profil, les adresses IP privées, la détection locale des noms, le mode données strict avec les
  fichiers et les adresses web qu'il couvre, le garde-fou anti-extraction, les images et la
  reconnaissance de texte ;
- les exceptions : les valeurs et les motifs à ne jamais masquer, les règles de secrets désactivées,
  les outils que couvre le mode données strict ;
- les [consignes](../llm-review/#vos-consignes) du relecteur local.

Jamais l'identité, le port, les dossiers de projets ou les serveurs de cette machine.

Un fichier de règles pèse 2 Mo au plus. Au-delà de 5 000 termes, 500 reconnaisseurs, 500 règles de
champ, 5 000 valeurs ou 500 motifs à ne jamais masquer, seuls les premiers sont lus, et l'importation
le dit. Un fichier fait par un Latet plus récent est refusé.

**Les termes sont des valeurs en clair.** Le nom d'un client parmi vos termes figure tel quel dans le
fichier : envoyez le fichier comme vous enverriez les noms eux-mêmes. Un reconnaisseur dont les
exemples contiennent des valeurs que Latet a réellement rencontrées est laissé de côté, puisque les
exemples doivent être inventés.

**Une importation ajoute de la protection, et celle-ci s'applique aussitôt :**
- les nouveaux termes, reconnaisseurs et règles de champ sont ajoutés, et les vôtres restent ;
- une valeur plus stricte que la vôtre est adoptée : un profil plus élevé, `redact` pour un type que
  vous masquez ;
- un reconnaisseur qui porte le nom ou le type de l'un des vôtres, ou une règle pour un champ qui a
  déjà une règle chez vous, est laissé de côté : vous gardez ce que vous aviez.

**Ce qui réduirait la protection est listé et laissé de côté :** les exceptions, une valeur plus
faible, les consignes du relecteur, un reconnaisseur qui laisse passer ce qu'il trouve
(`policy: allow`), un terme ou une règle de champ d'un type que vos politiques laissent passer (ce
qu'il trouve resterait en clair), un champ laissé en clair. `--all`, ou *Les appliquer aussi* dans
le tableau de bord, les applique également. Si c'est un agent ou un script qui lance l'importation,
`--all` fait de toute l'importation une seule modification, qui attend votre approbation comme toute
[modification qui réduit la protection](../agents-and-settings/) : ce qui ajoute de la protection
attend avec elle, et rien de l'importation ne s'applique tant que vous ne l'avez pas approuvée.
`--dry-run` indique seulement ce que ferait une importation.

```text
$ latet rules import acme-rules.yaml
Would add:
  terms: Fonderia Brambilla, Banca Ligure
  recognizer badge-number
  policy for IBAN: redact (yours: mask)
Yours already, or kept as yours:
  recognizer ticket: yours, with the same name, is kept
Left out, it would lower protection:
  values never to mask: acme-dev
--all applies them too: they wait for your approval.
saved C:\Users\you\AppData\Roaming\latet\config.yaml
Rules reloaded.
```

**Ce qui s'applique, c'est la réponse du daemon qui le dit.** La commande indique d'abord ce qu'elle
ajouterait, puis enregistre `config.yaml`, demande au daemon de le recharger et indique ce qu'il en
est : *Rules reloaded.* (code de sortie `0`), une modification qui attend votre approbation (`3`),
ou, si le daemon n'a pas répondu, que le fichier est enregistré et qu'il n'est pas confirmé qu'il
s'applique (`1` : `latet reload` l'applique dès que le daemon tourne). La commande ne lit pas les
dépôts dotés de leur propre `latet.yaml` que Latet a vus : le daemon y pèse l'importation quand il
recharge, et un ajout qui réduit la protection dans l'un d'eux attend lui aussi votre approbation.

Un `config.yaml` ou le `latet.yaml` d'un projet peut aussi être importé : ce qu'il contient et qui
n'est pas une règle (le port, l'identité...) est laissé de côté, et l'importation le signale.

### Les règles d'un projet

Les règles partagées d'un projet voyagent déjà avec lui : son [`latet.yaml`](../projects/), que le
tableau de bord écrit quand vous les modifiez, est dans le dépôt, et quiconque le clone les a.

Vos propres réglages pour un projet, comme les noms de clients que vous gardez hors du dépôt, ou ses
exceptions, forment un fichier à part :

```sh
latet rules export shop-rules.yaml --project C:\dev\shop
latet rules import shop-rules.yaml --project D:\work\shop
```

Dans le tableau de bord, la page **Règles** d'un projet les propose aussi, sous
*Vos réglages pour ce projet* : *Exporter ces réglages* et *Importer des réglages*.

### Sur un serveur Latet

Sur une machine connectée à un [serveur Latet](../server/), vos règles sont dans votre espace sur
le serveur, et `latet rules export` et `import` refusent de s'exécuter. **Exporter les règles** et
**Importer des règles** en haut de **Règles** dans votre tableau de bord sur le serveur fonctionnent
comme ci-dessus, sur vos propres règles : une équipe partage ses règles sous forme de fichier, et
chacun l'importe. Le `latet.yaml` d'un projet ne s'y applique pas : importez-le de la même façon
pour utiliser ses règles.

Un administrateur exporte et importe les règles d'une clé d'API :

```sh
latet server --server https://latet.acme.lan rules --key "Billing CI" export billing-rules.yaml
latet server --server https://latet.acme.lan rules --key "Billing CI" import team-rules.yaml
```

Les règles définies pour tout le monde sur un serveur, que personne ne peut assouplir, sont prévues
pour l'édition Enterprise.

## Votre installation, pour une autre machine

```sh
latet backup latet.backup --vault    # here
latet restore latet.backup           # on the other machine
```

`latet backup` demande deux fois une phrase de passe (au moins 12 caractères : quelques mots sont
faciles à retenir et difficiles à deviner), et écrit un seul fichier :

| | Sans `--vault` | Avec `--vault` |
| --- | --- | --- |
| Vos réglages (`config.yaml`) | oui | oui |
| Les certificats auxquels le relecteur local fait confiance | oui | oui |
| Le coffre-fort et sa clé racine | non | oui |
| L'historique du tableau de bord | non | avec `--history` |

- **Avec le coffre-fort**, l'autre machine produit les mêmes valeurs factices que celle-ci, se
  souvient de chaque valeur trouvée jusqu'ici (par les détecteurs, le relecteur local, vous dans le
  Relecteur en direct), et continue de remettre en place les vraies valeurs dans les conversations
  commencées ici. Le fichier contient alors vos vraies valeurs, chiffrées : gardez-le là où vous les
  garderiez.
- **Sans lui**, l'autre machine produit ses propres valeurs factices.
- **Jamais dans le fichier :** la clé d'API du relecteur local, le jeton du bridge (entre le plugin
  Claude Code et le daemon), les journaux, le modèle de noms local, les modèles de reconnaissance de
  texte et les réglages de vos agents. Chaque machine a les siens.

`latet restore` lit et vérifie d'abord tout le fichier, affiche ce qu'il contient et ce qui, dans ses
réglages, réduit la protection par rapport aux vôtres, et vous demande confirmation. Ensuite :

- **Cette machine garde ce qui lui est propre :** son port, son identité (ajoutée à celle de la
  sauvegarde, pour que les deux soient masquées), et rien de `capture_dir` ni de `api_key_file`, qui
  sont des chemins de l'autre machine.
- **Des dossiers de projets à un autre endroit :** `--paths C:\Users\anna\dev=D:\work` déplace ceux qui
  se trouvent sous le premier dossier vers le second. Les dossiers qui n'existent pas ici sont listés.
- **Un coffre-fort déjà présent ici :** si le coffre-fort de cette machine a ses propres valeurs, il
  n'est remplacé qu'avec `--replace`. Elles sont d'abord sauvegardées dans `before-restore-<date>.latet`
  à côté du coffre-fort, avec la même phrase de passe, pour que `latet restore` puisse les ramener.
- **Le daemon** est arrêté avant et redémarré après, et rien ne le démarre pendant une restauration.
  Une restauration interrompue (un plantage, une coupure de courant) le laisse arrêté jusqu'à ce que
  `latet restore --abort` remette les choses en place.
- **Ce qu'avait cette machine** (le coffre-fort, l'historique, `config.yaml`) reste dans un dossier
  `before-restore-` daté, à côté du coffre-fort.

Ensuite : `latet llm key` pour la clé d'API du relecteur, `latet ner install` pour la détection locale
des noms, `latet ocr install` pour le texte des images (d'ici là, les images partent telles quelles,
signalées *Non inspecté*, ou ne partent pas du tout avec `images: mask`), et `latet global-install`
(ou `latet claude`) pour vos agents.

**Dans votre terminal uniquement.** Les deux commandes refusent de s'exécuter dans un agent : elles
manipulent votre coffre-fort et sa clé. `latet restore` a aussi besoin d'un terminal pour vous poser
ses questions, et refuse quand la clé racine provient de `LATET_ROOT_KEY` ou de `LATET_ROOT_KEY_FILE`,
qu'une clé restaurée ne pourrait pas remplacer.

Sans la phrase de passe, personne ne peut lire le fichier, vous compris. Un fichier modifié, tronqué
ou assemblé à partir de morceaux d'autres fichiers est refusé avant que quoi que ce soit de son contenu
ne soit utilisé : voir [Sécurité](../../security/#comment-latet-se-protège-lui-même).

### Dans Docker

Le coffre-fort et sa clé se trouvent dans le volume `latet-data`, les réglages dans `latet-config` :
copier les deux volumes déplace tout. Avec les commandes :

```sh
docker exec -it latet latet backup /data/latet.backup --vault
docker cp latet:/data/latet.backup .
```

Sur l'autre machine, avec son conteneur arrêté :

```sh
docker compose stop
docker cp latet.backup latet:/data/
docker compose run --rm -it latet latet restore /data/latet.backup
docker compose start
```

Si le conteneur prend sa clé dans `LATET_ROOT_KEY_FILE`, `latet restore` refuse : déplacez plutôt les
volumes `latet-data` et `latet-config` et le fichier de clé.
