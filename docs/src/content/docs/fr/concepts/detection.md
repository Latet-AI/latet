---
title: Détection
description: Comment Latet reconnaît les secrets et les données personnelles, entièrement sur votre machine.
---

La détection s'exécute sur votre machine, ou sur le serveur Latet auquel votre ordinateur est
connecté : aucun service de détection dans le cloud n'est jamais appelé. Plusieurs détecteurs lisent
chaque morceau de texte que contient une requête. Cette
page les explique avec des exemples ; la [référence des détecteurs](../../reference/detectors/) liste
chaque détecteur et chaque règle de secrets.

## Secrets

Les secrets sont masqués dans tous les [profils](../profiles/).

Les identifiants de Latet sont inclus : `LATET_PERSONAL_TOKEN` (`latet_pat_`), `LATET_COMPUTER_KEY`
(`latet_ck_`), `LATET_API_KEY` (`latet_key_`) et `LATET_ADMIN_TOKEN` (`latet_adm_`),
chaque préfixe suivi de 43 caractères
compatibles avec les URL (`A-Z`, `a-z`, `0-9`, `_`, `-`). Dans `https://<host>/u/<route-key>`, les
16 à 128 caractères compatibles avec les URL de la clé de routage sont masqués comme `LATET_ROUTE_KEY` ; l'hôte et les autres
segments du chemin restent. Cela s'applique sur votre ordinateur et dans un espace serveur, y compris
aux dumps d'environnement, au fichier `settings.json` de Claude Code et au fichier `config.toml` de
Codex. Les appels aux outils locaux retrouvent les valeurs réelles. Un appel à un outil externe qui
contient une valeur factice est arrêté lorsque Latet peut l'intercepter (voir [Périmètre de protection](../protection-scope/)).

- **Formats connus.** Le jeu de règles par défaut de [gitleaks](https://github.com/gitleaks/gitleaks)
  (222 règles) est intégré à Latet et s'exécute sur [RE2](https://github.com/google/re2), avec les
  mots-clés, les seuils d'entropie et les listes d'autorisation qu'utilise gitleaks. Il couvre les
  clés d'API et les jetons de nombreux fournisseurs, comme GitHub, Anthropic, OpenAI, AWS, Stripe ou
  Slack.
- **Valeurs affectées à des noms sensibles**, dans les fichiers `.env`, la configuration et le code.
  C'est le nom qui décide, quelle que soit l'apparence de la valeur : `DB_PASSWORD=...`,
  `"api_key": "..."` et `$env:TOKEN = ...` sont masqués ; `APP_NAME=shop` et `DEBUG=true` ne le sont
  pas. Les espaces réservés comme `changeme`, `${DB_PASSWORD}` ou `<your-key>` ne sont pas touchés,
  pas plus que les références dans le code comme `process.env.API_KEY`.
- **Clés privées** : tout le corps d'un bloc de clé privée PEM, OpenSSH ou PGP. Les lignes `BEGIN` et
  `END` restent visibles, pour que le modèle sache toujours qu'une clé se trouve là.
- **Identifiants** : le mot de passe dans `postgres://user:password@host/db` et d'autres URL, les JSON
  Web Tokens, et les identifiants `Bearer` ou `Basic`. Dans une chaîne de connexion (une variable
  `connectionString`, une colonne `connection_string`), seuls les identifiants sont masqués : les
  valeurs de `Password=`, `PWD=`, `AccountKey=` et autres, ou le mot de passe de son URL. Le serveur,
  l'utilisateur et la base de données restent, comme dans `Server=db;User Id=app;Password=…;`.

Par exemple, un fichier `.env` tel qu'il est sur votre disque :

```sh title=".env"
APP_NAME=shop
SMTP_HOST=mail.northwind-legal.com
SMTP_PASSWORD=hunter2hunter
PAYMENTS_API_KEY=pk_9f2c4e7a1b3d5f8e0a6c
```

et ce que reçoit le modèle quand l'agent le lit :

```sh
APP_NAME=shop
SMTP_HOST=mail.northwind-legal.com
SMTP_PASSWORD=jeeepo3ixhbkw
PAYMENTS_API_KEY=pk_3a8d1f6b0e9c2a7d4b5e
```

Le nom d'hôte n'est pas un secret et reste tel quel. Le mot de passe et la clé sont remplacés par des
valeurs factices de la même longueur et du même format (les vôtres seront différentes).

### Espaces réservés

Une valeur qui ne fait que tenir la place d'un secret n'est pas touchée : dans une affectation, comme
mot de passe d'une URL, dans une colonne secrète et comme identifiant d'une chaîne de connexion
(`User Id={{username}};Password={{password}};`). La valeur entière doit avoir l'une de ces formes,
quelle que soit la casse :

| Forme | Exemples |
| --- | --- |
| Entre chevrons ou entre crochets | `<password>`, `<your-key>`, `[password]` |
| Une variable ou un gabarit | `${DB_PASSWORD}`, `$DB_PASSWORD`, `%DB_PASSWORD%`, `{{password}}`, `{env:DB_PASSWORD}`, `{file:./secret}` |
| *Your* et un nom | `YOUR_PASSWORD`, `your-api-key`, `your.token` |
| Un mot qui tient lieu de valeur | `changeme`, `change_me`, `password`, `passwd`, `secret`, `token`, `placeholder`, `example`, `redacted`, `todo`, `tbd` |
| Remplissage | `xxx`, `***`, `...`, `---`, `___` |
| Aucune valeur | `null`, `none`, `nil`, `undefined`, `empty`, `true`, `false` |

`changeme123` n'est pas un espace réservé : ce pourrait être un vrai mot de passe. Dans une
affectation, une référence dans le code n'est pas touchée non plus : `process.env.API_KEY`,
`os.environ["TOKEN"]`, `os.getenv(...)`, `config.`, `settings.`. Pour laisser en clair un espace
réservé d'une autre forme, ajoutez-le aux valeurs à ne jamais masquer
([listes d'autorisation](../profiles/#listes-dautorisation)), dans vos réglages pour un projet s'il appartient
à un projet.

Une valeur faite de symboles seuls n'est pas touchée non plus, quel que soit ce qui la trouve et
quelle que soit sa longueur : `!!!!!!!!` comme mot de passe, `--------------------` après `Bearer`,
une correspondance de l'un de vos [reconnaisseurs](../custom-recognizers/) ou une valeur que signale
le [relecteur local](../llm-review/). La valeur factice garde les symboles de l'original : elle
serait l'original lui-même. Une clé de routage de Latet fait exception : chaque caractère de sa
valeur factice est généré de nouveau, même lorsque l'original ne contient que `_` ou `-`.

## Données personnelles

Les données personnelles sont masquées dans les profils `balanced` et `max`. Chaque correspondance est
validée avant d'être masquée, ce qui limite les fausses alertes dans le code :

| Type | Exemple | Comment une correspondance est confirmée |
| --- | --- | --- |
| Adresse e-mail | `emily.carter@northwind-legal.com` | Le domaine de premier niveau est vérifié dans la liste de l'IANA, fournie avec Latet pour fonctionner hors ligne |
| Numéro de téléphone | `+44 20 7946 0958` | Analysé et validé avec [phonenumbers](https://github.com/daviddrysdale/python-phonenumbers), un portage de libphonenumber de Google |
| IBAN | `GB82WEST12345698765432` | Longueur propre au pays et somme de contrôle mod-97 |
| Numéro de carte | `4111 1111 1111 1111` | Préfixe de l'émetteur et somme de contrôle de Luhn |
| Numéro d'identité national et numéro fiscal | `SSN NNN-NN-NNNN`, `DNI NNNNNNNNA` | Chiffres de contrôle, et un mot-clé là où le numéro seul pourrait être n'importe quoi (voir plus bas) |
| Numéro de TVA | `DENNNNNNNNN`, `CHE-NNN.NNN.NNN MWST` | Le préfixe du pays et les chiffres de contrôle de ce pays |
| Adresse IP | `151.101.1.69` | Les adresses publiques ; les privées seulement en `max` ou avec `include_private_ips` |
| Adresse Bitcoin | `bc1q...` | Somme de contrôle Base58Check ou bech32 |

Les reconnaisseurs d'adresses e-mail, d'IBAN, de cartes, de codice fiscale et de partita IVA sont
repris de [Presidio](https://github.com/data-privacy-stack/presidio).

### Numéros d'identité et numéros fiscaux par pays

Latet connaît les numéros d'identité nationaux et les numéros fiscaux de 15 pays : l'Allemagne, la
Belgique, le Canada, l'Espagne, les États-Unis, la Finlande, la France, l'Italie, la Lettonie, les
Pays-Bas, la Pologne, le Portugal, le Royaume-Uni, la Suède et la Suisse. `id_regions` dans
`config.yaml` choisit les pays (`IT`, `US` et `GB` par défaut) ; `latet global-install` propose le
vôtre, et le profil `max` les prend tous. La page **Règles** du tableau de bord liste les numéros de
chaque pays.

Beaucoup de ces numéros ne sont qu'une suite de neuf ou onze chiffres, comme d'innombrables ID dans
le code. Chacun est donc confirmé par ses chiffres de contrôle, et ceux qui pourraient encore être
n'importe quoi ont besoin d'un mot-clé à proximité : *SSN* ou *social security* à côté d'un numéro de
sécurité sociale américain, *BSN* à côté d'un numéro de service citoyen néerlandais, *Steuer-ID* à
côté d'un numéro d'identification fiscale allemand. Le mot-clé compte sur la même ligne, à quelques
caractères de distance, ou dans l'en-tête de la colonne où se trouve le numéro (tableaux CSV, TSV et
Markdown) :

```text
name,ssn,city
Jane,NNN-NN-NNNN,Boston        ← found: the column is called ssn
```

Le DNI et le NIE espagnols, le numéro AVS suisse et le code d'identité personnel finlandais portent
une lettre de contrôle ou une structure caractéristique : ils n'ont donc pas besoin de mot-clé. Un
numéro n'est jamais pris à l'intérieur d'un numéro plus long, d'une version, d'un nombre décimal ou
d'un chemin. La [référence des détecteurs](../../reference/detectors/) donne la liste complète, avec
la structure et la vérification de chaque numéro.

Les numéros de TVA sont reconnus pour les 27 pays de l'UE, l'Irlande du Nord, le Royaume-Uni, la
Suisse et la Norvège, quoi que dise `id_regions` : ils portent toujours le préfixe de leur pays.

Certaines choses ne sont volontairement pas touchées :

- les adresses e-mail des domaines de documentation `example.com`, `example.net` et `example.org`, ou
  sous `.example`, `.test`, `.invalid` et `.localhost`, et les expéditeurs système comme `git@` ou
  `noreply@` ;
- dans le code et les sorties d'outils, les suites de chiffres sans `+`, `00`, espace ni parenthèses,
  qui sont bien plus souvent des ID, des dates ou des numéros de build que des numéros de téléphone.
  Dans vos propres messages, les règles sont plus souples. Les numéros sans préfixe international ne
  comptent que pour les pays de `phone_regions` (`IT`, `US` et `GB` par défaut) ;
- les numéros de version qui ressemblent à des adresses IP, comme `v1.2.3.4`.

### Noms

Les noms dans du texte libre, comme dans *Emily Carter a écrit ceci*, demandent un modèle
statistique. Avec le [modèle local de noms](../../getting-started/installation/) facultatif installé,
Latet trouve lui-même les noms et les adresses postales ; le [relecteur local](../llm-review/), un
modèle de langage sur votre machine ou sur un serveur à vous, les trouve aussi, avec les informations
de santé et les autres valeurs qu'aucun motif ne décrit. Sans l'un ni l'autre, un nom n'est masqué
que s'il s'agit de votre propre nom d'auteur git (voir plus bas), de l'un de vos propres termes ou
d'une valeur déjà présente dans le coffre-fort.

Prenons ce prompt :

```text
Hi, I'm Emily Carter and I live at 42 Mill Lane, Bristol BS1 4DJ.
```

Avec le modèle de noms, dans le profil `balanced`, le modèle dans le cloud reçoit une version comme
celle-ci :

```text
Hi, I'm Hannah Mitchell and I live at 45 Moore Lane, Derby YN4 9CP.
```

Votre agent voit toujours le vrai nom et la vraie adresse quand la réponse revient.

Ce que cherche le modèle de noms dépend du [profil](../profiles/) :

- **`balanced`** : les noms complets (deux mots ou plus) et les adresses postales, dans ce que vous
  saisissez et joignez et dans le texte lu dans les images. Une ville ou un code postal ne compte
  qu'à côté d'une rue. Le code, les sorties d'outils et le texte système ne sont pas lus.
- **`max`** : aussi les noms d'un seul mot et les villes, et également le texte système et les
  sorties d'outils.
- **`secrets`** : jamais.

Une fois trouvé, un nom rejoint les [valeurs connues](#valeurs-connues) et il est ensuite masqué
partout, sorties d'outils comprises, dans tous les profils, `secrets` aussi.

Le modèle de noms fait de son mieux, sans garantie :

- **Des noms lui échappent.** Les noms peu courants, les initiales et les noms écrits en minuscules
  sont ceux qui lui échappent le plus souvent. Les salutations et les titres placés devant un nom
  (*Hi Paul*, *Dr. Sarah Mitchell*) sont laissés en dehors de ce qui est masqué.
- **Il prend certains mots pour des noms.** Les mots techniques comme *Redis* ou *Postgres* sont
  filtrés, et `balanced` exige au moins deux mots.
- **Il est lent sur les longs textes.** Il lit environ 700 jetons par seconde sur le processeur d'un
  ordinateur de bureau, dans un fil d'exécution à part, pour que les autres requêtes ne l'attendent
  pas. Un texte de plus de 4 096 jetons (8 192 en `max`), ou de plus de 16 000 caractères
  (64 000 en `max`), n'est pas lu, et la requête est signalée dans le tableau de bord.

## Vos propres termes

Ajoutez les mots dont vous seul savez qu'ils sont sensibles : noms de clients, noms de code de
projets, domaines internes. Ils sont masqués dans tous les profils, partout où ils apparaissent :

```yaml title="config.yaml"
terms:
  - value: Project Falcon
    type: PROJECT
  - value: Acme Logistics
    type: CLIENT
```

Avec ces termes, *"Project Falcon démarre avec Acme Logistics en mai"* parvient au modèle sous une
forme comme *"Consolidated Margie démarre avec Northwind Adatum en mai"*. Par défaut, un terme
correspond quelle que soit la casse, et seulement en mot entier ; `case_sensitive` et `whole_word`
changent cela. Vous pouvez aussi ajouter des termes sur la page **Règles** du tableau de bord, ou avec
**Toujours masquer** sur n'importe quelle valeur d'une requête.

Un terme a besoin d'au moins une lettre ou un chiffre : la valeur factice garde les symboles de
l'original, donc des symboles seuls, comme `--` ou `###`, ne peuvent pas être masqués. Le tableau de
bord et `latet config` refusent un tel terme. Un terme écrit à la main dans `config.yaml` ou dans le
`latet.yaml` d'un projet est laissé de côté, et la page **Règles** et `latet config validate` le
signalent.

Un terme compte au plus 8 192 caractères : un terme plus long est cherché à part, et un terme
d'environ un million de lettres ne peut pas être cherché du tout. Le tableau de bord, `latet config`
et un import refusent un terme plus long. Un terme écrit à la main dans `config.yaml` est laissé de
côté, et `latet reload`, le journal du daemon et `latet config validate` disent quelle entrée ; un
terme dans le `latet.yaml` d'un projet est laissé de côté lui aussi, et la page du projet le dit.

Pour un format plutôt qu'une valeur, comme tous les numéros de badge écrits `AB-123456`, écrivez un
[reconnaisseur personnalisé](../custom-recognizers/) : un motif, des chiffres de contrôle et des
mots-clés, testé sur des exemples inventés.

## L'identité de cette machine

En `balanced` et en `max`, Latet détecte qui vous êtes et où vous êtes à chaque démarrage, et le
masque :

- votre nom d'utilisateur du système dans les chemins de votre répertoire personnel :
  `C:\Users\anna\shop\app.py`, `/home/anna/...` ou `/Users/anna/...`. Un nom d'utilisateur de six
  caractères ou plus est masqué partout où il apparaît ;
- votre nom d'hôte ;
- le nom et l'adresse e-mail de votre auteur git, tirés de `git config --global`, et le domaine de
  cette adresse quand c'est celui de votre entreprise (`studio-rossi.it`, pas `gmail.com`), partout où
  il apparaît : `git.studio-rossi.it` est masqué aussi.

Comme tout est détecté de nouveau à chaque démarrage, une nouvelle adresse e-mail git ou un nouveau
nom d'utilisateur est masqué sans aucune action de votre part. La section `identity` de la
[configuration](../../reference/configuration/) désactive ce masquage, ou fixe les valeurs : une fois
que vous les y avez écrites, Latet utilise celles-ci.

## Valeurs connues

Une fois qu'une valeur a été détectée, Latet la retient dans le coffre-fort. Ses occurrences suivantes
sont masquées même là où les détecteurs ne la reconnaîtraient pas d'eux-mêmes : par exemple le mot de
passe de votre `.env`, mentionné plus tard dans un message de commit. Deux sortes de valeurs ne sont
pas retenues ainsi : une valeur que le mode données strict ou une règle de champ masque dans un
tableau ou du JSON ne reste masquée que dans sa structure
([données de production](../production-data/#mode-données-strict)), et une valeur d'un type réglé sur
`redact` n'est pas conservée du tout. Les valeurs connues sont aussi ce que la vérification finale
cherche dans les requêtes sortantes.

Une valeur est reconnue de nouveau à partir de quatre caractères, le nom d'une personne à partir de
trois (*Ugo*, *Ada*), et seulement telle qu'elle est écrite : *Ugo* n'est pas *ugo*, ni une partie
d'*Ugolino*.

Une valeur est aussi trouvée là où elle est écrite avec des séquences d'échappement : `\u00f2` ou
`\\u00f2` dans du JSON que Latet ne lit pas comme un document (un bloc de code, des lignes JSON, un
document contenu dans une chaîne), des octets dans un journal (`\xc3\xb2`) ou dans un chemin que git
met entre guillemets (`\303\262`), un encodage d'URL (`%C3%B2`, `%20`, ou `+` dans une requête ou un
formulaire), ou une référence de caractère HTML (`&#242;`, `&ograve;`). Il en va de même pour vos
termes et l'identité de cette machine et, là où une séquence écrit un caractère, pour les données
personnelles et les valeurs de vos reconnaisseurs : une adresse écrite `mario.rossi%40acme.it` est
donc trouvée elle aussi (un secret ou une valeur d'une structure n'est trouvé que tel qu'il est
écrit). Une valeur trouvée ainsi est conservée telle qu'elle est écrite, comme une valeur à part
avec sa propre valeur factice, écrite avec les mêmes séquences là où elles font partie du texte :
`Paolo%20Fontana` dans un lien, `\'` dans une chaîne entre apostrophes. L'agent récupère la valeur
exactement telle qu'elle était écrite : un lien reste un lien, et une modification retrouve sa
ligne. Là où le modèle écrit la valeur factice telle qu'elle se lit, `Paolo Fontana`, l'agent récupère
la valeur telle qu'elle se lit.

## Quand deux détecteurs trouvent le même texte

Les correspondances qui se chevauchent sont fusionnées en une seule, qui les couvre toutes : une
valeur n'est donc jamais remplacée à moitié. Le type le plus fort donne son nom au résultat : d'abord
les secrets, puis vos termes et l'identité de cette machine, puis les données personnelles. Vos termes
et les valeurs connues qui se chevauchent sont aussi trouvés chacun : si *Anna Maria* et *Maria
Rossi* sont toutes deux connues, le nom *Anna Maria Rossi* est masqué en entier. Les chevauchements
sont cherchés jusqu'à une limite dans chaque texte : au-delà, le reste du texte est masqué comme
avant, une valeur après l'autre.

Si un détecteur autre que celui des données personnelles et le modèle local de noms échoue (secrets,
vos termes, identité, valeurs connues), le masquage échoue et la requête est bloquée : rien ne part
sans vérification. Si le détecteur des données personnelles ou le modèle local de noms échoue, il est
ignoré et la requête est signalée dans le tableau de bord.

## Pourquoi RE2

RE2 trouve les correspondances en temps linéaire : une entrée hostile ou simplement énorme ne peut
donc pas déclencher un retour arrière catastrophique des expressions régulières et bloquer votre
agent.

## Faux positifs et faux négatifs

Chaque détecteur fait un compromis entre les valeurs manquées et les fausses alertes. Les
[profils](../profiles/) choisissent où se placer dans ce compromis, et les
[listes d'autorisation](../profiles/#listes-dautorisation) corrigent les fausses alertes une à une. Pour voir
comment un texte serait masqué, collez-le dans le **Banc d'essai** du tableau de bord : rien de ce
qui y est saisi n'est conservé ni envoyé à un modèle.
