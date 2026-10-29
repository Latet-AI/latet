---
title: Contribuer
description: Préparer un environnement de développement, lancer les vérifications, tenir la documentation à jour et ajouter une langue.
---

Avant de vous lancer dans un travail important, ouvrez une issue pour en discuter.

Pour publier une version, suivez [Publier Latet](../releasing/) : fusionnez, attendez toute la CI
sur le commit exact de main, puis créez le tag.

## Environnement de développement

Il vous faut [uv](https://docs.astral.sh/uv/) ; il installe la version de Python qu'utilise le
projet.

```sh
git clone https://github.com/Latet-AI/latet.git
cd latet
uv sync
```

Exécutez Latet depuis cette copie locale du dépôt avec `uv run` :

```sh
uv run latet serve       # the daemon in the foreground, with its log in the terminal
uv run latet claude      # Claude Code with the plugin from src/latet/claude_plugin
uv run latet doctor
```

Pour garder une instance de test à l'écart d'un Latet que vous utilisez pour de bon, définissez
`LATET_DATA_DIR` et `LATET_CONFIG` vers un dossier temporaire et choisissez un autre port dans ce
`config.yaml`.

`latet serve --passthrough` transmet le trafic sans le masquer, et `--capture <folder>` écrit chaque
échange dans des fichiers locaux (avec les en-têtes d'authentification caviardés), ce qui est utile
pour étudier un protocole. Les captures contiennent le contenu des conversations : ne les incluez
jamais dans un commit (c'est pour cette raison que git ignore `notes/captures/`).

## Vérifications

Lancez-les avant d'ouvrir une pull request. La CI les lance dans ses jobs de lint et de test, les
tests sous Windows, macOS et Linux avec plus d'exemples pour les tests de propriétés
(`HYPOTHESIS_PROFILE=ci`). Elle lance aussi `uv run python scripts/gen_notices.py --check`, les tests
du serveur, et les vérifications du plugin, du tableau de bord et de la documentation décrites
ci-dessous, et elle construit les installateurs et l'image de conteneur :

```sh
uv run pytest
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pyright
uv run python scripts/gen_reference.py --check
```

- **Pages de référence.** Les pages [CLI](../reference/cli/), [configuration](../reference/configuration/)
  et [détecteurs](../reference/detectors/) sont générées à partir du code. Après avoir modifié une
  commande, une clé de configuration ou un détecteur, régénérez-les avec
  `uv run python scripts/gen_reference.py`. Ne les modifiez jamais à la main.
- **Plugin Claude Code.** `claude plugin validate src/latet/claude_plugin` valide le plugin
  avec une version installée compatible. `claude plugin test`, lancé dans
  `src/latet/claude_plugin/`, n'est pas disponible dans tous les environnements installés ;
  utilisez-le uniquement si celui-ci expose la commande de test.
- **Faux positifs.** `tests/unit/test_fp_budget.py` passe les détecteurs sur la bibliothèque standard
  de Python, qui ne contient aucun vrai secret, avec un budget par profil qui ne peut que baisser.
  `uv run python scripts/fp_corpus.py` affiche ce qui se déclenche sur un corpus plus large, règle
  par règle.
- **Avis de tiers.** Après avoir modifié les dépendances, lancez `npm ci` dans `dashboard/` et dans
  `docs/`, puis régénérez-les avec `uv run python scripts/gen_notices.py`. Ajoutez `--check` pour
  vérifier que le fichier est à jour sans le réécrire ; la CI lance cette vérification.

Règle de base : les tests n'utilisent que des données synthétiques. N'incluez jamais dans un commit
de vrais secrets, de vraies données personnelles ou des captures de trafic.

## Tests du serveur

Utilisez Docker et le script existant pour des services de test isolés : PostgreSQL,
Valkey et PostgreSQL avec TLS. Le premier démarrage télécharge les images absentes :

```sh
uv run python scripts/server-services.py up
uv run python scripts/server-services.py env
```

Définissez `LATET_TEST_DATABASE_URL`, `LATET_TEST_VALKEY_URL` et `LATET_TEST_TLS_DATABASE_URL`
affichés dans le shell de test, avec sa syntaxe. Ils désignent des services de test jetables,
jamais des données de production. Définissez `LATET_TEST_REQUIRE_SERVICES=1`, puis lancez :

```sh
uv run pytest -q -m server
```

Chaque test utilise son propre schéma de base et préfixe Valkey. Le job CI `server`
fournit et exige les services : un service indisponible doit provoquer un échec plutôt
qu'un test ignoré en silence. Arrêtez vos services créés par le script avec
`uv run python scripts/server-services.py down` ; coordonnez les services partagés avec leur responsable.

Les tests d'agents réels sont facultatifs, avec `LATET_AGENT_TESTS=1` et
`uv run pytest -m agents`. Ils exécutent des programmes tiers installés contre des
fournisseurs fictifs locaux, mais ces programmes peuvent contacter leurs propres hôtes
externes. Utilisez des dossiers personnels isolés et des identifiants synthétiques ;
les fixtures locales ne garantissent pas l'absence de trafic externe. Les tests marqués
`live` utilisent les modèles locaux de détection des noms et de reconnaissance de texte : ils sont
ignorés là où ces modèles ne sont pas installés, comme en CI.

## Tableau de bord

Le tableau de bord se trouve dans `dashboard/` (React, Vite et [i18next](https://www.i18next.com)) et
nécessite Node.js 22.12 ou plus récent.

```sh
cd dashboard
npm ci
npm run dev:mock    # the dashboard with sample data, no daemon needed
npm run lint
npm run check:i18n  # every key the code uses is in en.json, and every language has its keys
npm test
npm run build       # writes the files the daemon serves into src/latet/web/
```

Une nouvelle chaîne va dans `en.json` et, traduite, dans les fichiers des autres langues, dans la même
modification : `check:i18n` veut les mêmes clés dans toutes les langues.

## Le tutoriel

Les chapitres de `latet tutorial` sont des fichiers YAML dans `src/latet/tutorial/chapters/` : le
prompt et l'explication de chaque étape, la réponse du modèle fictif, et les vérifications qui
doivent être satisfaites après elle. Les tests jouent chaque chapitre du début à la fin avec le vrai
masquage, deux fois, si bien qu'un changement de Latet qui rend un chapitre inexact les fait
échouer : modifiez le chapitre en même temps que le comportement. Ils refusent aussi un chapitre qui
nomme un côté de l'écran (sur un écran étroit, la page empile ses panneaux) ou qui contient une
valeur qu'un scanner de secrets prendrait pour la clé d'un fournisseur.

Depuis une copie locale du dépôt, `uv run latet tutorial` a besoin du tableau de bord compilé
(`npm run build` dans `dashboard/`). Le mode simulé du tableau de bord joue aussi les chapitres, avec
`?mock-tutorial`, à partir de sa propre copie dans `dashboard/src/mock/tutorial.ts` : modifiez-la en
même temps que le chapitre.

## Documentation

Le site se trouve dans `docs/` et il est construit avec [Starlight](https://starlight.astro.build).
Il vous faut Node.js 22.12 ou plus récent.

```sh
cd docs
npm ci
npm run dev     # http://localhost:4321/
npm run build   # static site in docs/dist/
```

Les pages sont des fichiers Markdown ou MDX dans `docs/src/content/docs/`. La barre latérale est
définie dans `docs/astro.config.mjs`.

**L'anglais est la version de référence.** Une pull request qui change ce que fait Latet met à jour
les pages anglaises concernées, dans la même pull request : une nouvelle option, une nouvelle
commande ou un nouveau comportement est documenté en anglais avant d'être fusionné. Les traductions
suivent les pages anglaises, jamais l'inverse. Une pull request peut ne modifier que les pages
anglaises : les mainteneurs mettent les autres langues à jour.

**Les captures d'écran du tableau de bord** sont faites par un script, à partir de ses données
d'exemple, dans toutes les langues et les deux thèmes : après une modification visible sur l'une
d'elles, lancez `npm run screenshots` dans `dashboard/` (`npm run screenshots -- live it` n'en refait
que quelques-unes). La première fois, `npx playwright install chromium` télécharge le navigateur
qu'il utilise. Une page MDX en affiche une avec `<Screenshot name="live" alt="…" />`, après avoir
importé `docs/src/components/Screenshot.astro` comme le fait `dashboard.mdx`.

## Comment ajouter une langue

### Documentation

La documentation existe en cinq langues : l'anglais à la racine, qui sert de référence, et
l'italien, l'allemand, le français et l'espagnol sous `/it/`, `/de/`, `/fr/` et `/es/` (les
`locales` de `docs/astro.config.mjs`). Une page pas encore traduite affiche la page anglaise, avec un
avertissement. Les pages de référence générées à partir du code restent en anglais dans toutes les
langues.

- **Traduire une page**, c'est la créer sous `docs/src/content/docs/<lang>/`, au même chemin que la
  page anglaise : `concepts/how-it-works.mdx` devient `it/concepts/how-it-works.mdx`. Ses imports et
  les chemins de ses images prennent un `../` de plus, puisque le fichier se trouve un dossier plus
  bas.
- **Les liens entre les pages restent relatifs**, si bien qu'une page traduite les garde dans sa
  langue. Un titre traduit change d'ancre, donc les liens qui y mènent changent aussi :
  `scripts/check_docs_links.py` trouve ceux qui ont été oubliés (lancez-le depuis la racine du dépôt
  après `npm run build` ; la CI le lance aussi).
- **Les textes d'interface** qu'ajoute Latet se trouvent dans `docs/src/content/i18n/<lang>.json`, et
  les libellés de la barre latérale dans les `translations` de `docs/astro.config.mjs`. L'interface
  propre de Starlight est déjà traduite.
- **Une autre langue**, c'est une entrée de plus dans `locales`, son
  `docs/src/content/i18n/<lang>.json` et ses `translations` de la barre latérale.

Lancez `npm run build` et vérifiez le résultat avec `npm run preview`.

### Tableau de bord

Le tableau de bord existe en anglais, en italien, en allemand, en français et en espagnol. Pour
ajouter une autre langue (ici le portugais) :

1. Copiez `dashboard/src/locales/en.json` vers `pt.json` et traduisez les valeurs. Gardez les clés et
   les `{{variables}}`, et ajoutez chaque forme plurielle dont la langue a besoin (`key_many` en
   portugais).
2. Lancez `npm run check:i18n` : il liste les clés manquantes ou en trop, les variables qui diffèrent
   et les formes plurielles qui manquent au fichier.
3. Déclarez la langue dans `dashboard/src/i18n.ts`, avec son nom dans sa propre langue
   (`LANGUAGE_NAMES`), et ajoutez-la aux langues qu'attend `dashboard/src/i18n.test.ts`. Ce fichier de
   test prend le portugais pour une langue que le tableau de bord n'a pas (`pt-BR`,
   `docsUrl(…, 'pt')`) : changez ces cas pour une autre langue. Tant que la documentation n'existe pas
   dans la nouvelle langue, faites mener `docsUrl` aux pages en anglais pour elle ; dès qu'elle existe,
   ajoutez la langue à `LANGUAGES` dans `dashboard/scripts/screenshots.mjs`.

Le tableau de bord s'ouvre dans la langue choisie dans son menu, sinon dans la première des langues
du navigateur qu'il propose, sinon en anglais. `dashboard/README.md` donne les détails.

### Tutoriel

Les textes d'un chapitre dans une autre langue se trouvent dans
`src/latet/tutorial/chapters/<lang>/<chapter>.yaml` : ses titres, ses explications, ses prompts, les
répliques de l'agent et les tours du modèle, étape par étape, chacun avec les espaces réservés de son
texte anglais. Le reste du chapitre, ses valeurs et ses vérifications, vient du fichier anglais.
Quiconque modifie les textes anglais d'un chapitre les modifie dans toutes les langues. Les tests
jouent chaque chapitre dans chaque langue, et soumettent chaque langue à ses propres règles
éditoriales (`tests/unit/tutorial/test_chapters.py`). Une nouvelle langue a aussi besoin de ses propres
règles dans ce fichier, dans `ON_SCREEN` et `JARGON`, sinon les tests échouent.

Une pull request peut ne modifier que les textes anglais d'un chapitre. Un texte qui gagne ou perd
un espace réservé demande la même modification dans les autres langues, sinon les tests échouent ;
les mainteneurs traduisent le reste.

## Comment ajouter un pays

Les numéros d'identité et d'identification fiscale nationaux se trouvent dans
`src/latet/engine/detectors/ids/regions/`, un module par pays, chacun étant un tuple d'`IdSpec`. Les
numéros de TVA se trouvent dans `ids/vat.py`.

1. Écrivez `regions/<cc>.py` (le code ISO 3166-1 alpha-2) : pour chaque numéro, un motif décrivant
   la façon dont il s'écrit, un validateur, un générateur de valeurs factices valides au même
   format, ses mots-clés, et les cas où un mot-clé est requis (`context` : `never`, `bare`, `code`
   ou `always`). Écrivez le validateur d'après la spécification publique de l'autorité émettrice.
   python-stdnum est une référence réservée aux tests, sous licence LGPL : n'en copiez jamais le
   code.
2. Déclarez le module dans `regions/__init__.py`, et le pays dans `ID_REGIONS`, dans
   `src/latet/config.py`.
3. Ajoutez un format et le module stdnum, s'il y en a un, à `tests/unit/test_ids_oracle.py`, et un
   mot-clé dans les cas de `test_ids.py` si nécessaire. Le test oracle vérifie 200 valeurs factices
   par type et compare le validateur à stdnum sur des numéros voisins de numéros valides ; listez-y
   les différences voulues.
4. Ajoutez le libellé du type dans les cinq langues du tableau de bord,
   `dashboard/src/locales/<lang>.json` (`rules.pii.types`), à `PII_TYPES` dans
   `dashboard/src/lib/labels.ts` et sa description à `PII_HELP` dans
   `dashboard/src/pages/RulesPage.tsx` ; ajoutez aussi le type et le pays au catalogue de la maquette
   (`PII_CATALOG` et `id_regions_available` dans `dashboard/src/mock/fixtures.ts`, `ID_TYPES` dans
   `dashboard/src/mock/builder.ts`). Lancez ensuite `uv run python scripts/gen_reference.py` pour les
   tableaux de référence.
5. Lancez le budget de faux positifs (`tests/unit/test_fp_budget.py`) : aucune règle de numéro
   d'identité ne doit se déclencher sur l'échantillon de la bibliothèque standard.

## Comment mesurer un relecteur local

Quel modèle local fonctionne le mieux comme [relecteur local](../concepts/llm-review/#quelle-vitesse-et-quel-modèle)
reste une question ouverte, et vos mesures sont les bienvenues. Faites tourner un modèle sur cette
machine (llama.cpp, Ollama ou LM Studio), faites pointer `LATET_LLM_URL` et `LATET_LLM_MODEL` vers
lui, et lancez le benchmark sur ses 93 cas inventés :

```sh
uv run python scripts/llm_review_bench.py run --label <model> --variants A
uv run python scripts/llm_review_bench.py report
```

Envoyez ce qu'il a écrit dans `benchmarks/llm-review/` (vos lignes dans `results/`, et `tables.md`)
dans une pull request, avec votre carte graphique ou votre processeur, le serveur et la
quantification du modèle. `benchmarks/llm-review/README.md` explique comment les chiffres publiés ont été
obtenus.

## Licence

Latet est distribué sous la Latet Sustainable Use License, son Enterprise Edition sous la Latet
Enterprise License, et Latet est aussi proposé sous des licences commerciales
([LICENSING.md](https://github.com/Latet-AI/latet/blob/main/LICENSING.md)). Les contributions sont
acceptées selon l'[accord de licence des contributeurs](https://github.com/Latet-AI/latet/blob/main/CLA.md) :
vous conservez vos droits d'auteur, et votre contribution peut faire partie de Latet sous toutes ces
licences. Pour donner votre accord, cochez la case
"I agree to the Latet contributor license agreement (CLA.md)." dans le modèle de votre première
pull request.
