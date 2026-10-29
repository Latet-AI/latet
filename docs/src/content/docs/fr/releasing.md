---
title: Publier Latet
description: Publier une version seulement après la réussite du workflow CI complet sur le commit exact de main.
---

Cette procédure concerne les tags `v*` de Latet et `.github/workflows/release.yml`. Conservez un seul
commit public par version, avec le code, les tests, les numéros de version et la documentation dans les cinq langues.

## Fusionner, attendre la CI, puis créer le tag

1. Préparez et faites examiner la version sous la forme d'un seul commit public. Alignez les versions
   du paquet et du plugin Claude ; la compilation de publication les compare au tag.
2. Fusionnez ce commit dans `main`. Notez son SHA complet.
3. Attendez que **l'ensemble du workflow CI** (`.github/workflows/ci.yml`), déclenché par ce push sur
   `main`, se termine avec **succès** pour ce SHA exact. Vérifiez le résultat du workflow, y compris
   tous les jobs de la matrice et les tests du serveur. Une pull request ou un job réussi ne suffit pas.
4. Créez le tag de version sur ce commit exact et poussez-le seulement lorsque la publication est autorisée.
   Les tags légers et annotés sont acceptés ; pour un tag annoté, le commit sous-jacent est vérifié.
5. Vérifiez le résultat du workflow de publication. Conservez le SHA du tag et l'URL de la CI réussie sur
   main dans le dossier de vérification. Le résumé du contrôle indique l'exécution et la tentative acceptées.

Si le commit change, vérifiez à nouveau la CI de main pour le nouveau SHA avant de créer le tag. Conservez
l'unique commit public de version ; n'ajoutez pas de commit public uniquement pour consigner des contrôles réussis.

## Exigences du contrôle de publication

Avant toute compilation, attestation ou publication, le contrôle identifie le workflow CI par son fichier,
vérifie que le commit du tag appartient à `main` et lit les exécutions `push` sur `main` de ce SHA exact.
Il choisit l'exécution la plus récente et relit sa tentative actuelle. Seul `completed` avec `success` est accepté.
Les jobs suivants utilisent le SHA du commit vérifié et dépendent de ce contrôle.

Les résultats absents, en file, en cours, en attente, échoués, annulés, ignorés ou neutres bloquent la publication.
Les erreurs d'API ou d'authentification, les réponses incohérentes et la pagination incomplète la bloquent aussi.
Le contrôle refuse les listes atteignant la limite de 1 000 résultats des recherches filtrées de GitHub.
Il n'attend pas et ne réessaie pas jusqu'à obtenir un résultat favorable. Examinez l'échec, attendez la réussite
de toute la CI sur ce commit exact de main, puis relancez explicitement la publication lorsqu'elle est autorisée.
Ne déplacez pas un tag publié pour contourner un échec.

Le résultat constitue une preuve au moment du contrôle ; une relance ultérieure de la CI peut modifier l'état
de l'exécution consignée. Un contrôle vert confirme la réussite de la CI, pas l'absence de tout défaut possible.

## Un bilan de vérification par version

Liez depuis les notes de version un court bilan de vérification en anglais, avec un fichier Markdown
identifié par version et des manifestes communs des sources et des preuves joints hors de Git.
Finalisez le bilan après la création du commit public et la réussite de toute sa CI sur main ; après
le tag et la publication autorisés, joignez les fichiers manuellement et ajoutez les liens dans les
notes de version. Le workflow de publication actuel ne génère pas ces fichiers de vérification.
Conservez un seul commit public par version : le bilan externe peut citer son SHA exact sans
l'inscrire dans un fichier qui détermine ce même SHA.

Incluez :

- La version, le SHA complet du commit public et celui du commit sous-jacent au tag ; séparément, les
  sources privées réellement testées (commit ou base avec patch/manifeste conservé). Un manifeste
  énumère les chemins et empreintes du contenu du code, des tests, du harnais et de la configuration
  vérifiés. Consigner tous les écarts avec les sources publiques, les exclusions et les vérifications
  à refaire ; un résultat privé ne devient pas un résultat pour le SHA public.
  Séparez les inventaires des blobs Git canoniques et des octets réellement exécutés, pour les copies
  de travail propres comme modifiées ; consignez les conversions EOL/filtres et les différences,
  sans normaliser les empreintes des octets exécutés.
- Les participants réels aux revues ou leurs rôles identifiables et références aux rapports, le
  périmètre, le type de revue et l'existence d'une revue humaine indépendante. La revue d'un agent
  automatisé distinct n'est pas humaine. Lister les constats clos avec les preuves de nouvelle
  vérification, ainsi que les constats ouverts avec leur gravité et le travail restant.
- Les commandes des suites, l'environnement, les résultats et les tests ignorés ; les identifiants
  des configurations fuzz, le harnais et les sources, les graines ou corpus, les nombres visés/réels
  et les configurations terminées/prévues. Signaler explicitement les travaux échoués, incomplets et
  non exécutés. Les campagnes de comptage ne constituent pas des mesures de performances.
- Le dépôt CI exact, le chemin du workflow, l'événement/ref, le SHA, l'identifiant d'exécution, l'URL
  de la tentative, l'état/conclusion du workflow entier et l'heure d'observation. Vérifier séparément
  la CI finale `push`/`main` avant le tag ; les contrôles PR, résultats locaux et publications réussies
  ne la remplacent pas.
- Les limites testées : upstream simulé ou tests directs des runtimes d'espaces face aux tests
  authentifiés de bout en bout, configurations omises et preuves indisponibles. Les logs privés ne
  sont pas consultables publiquement ; fournir si possible un extrait public sûr et préciser ce que
  les lecteurs ne peuvent pas vérifier.

Conserver le bilan et les manifestes hors du commit qu'ils identifient. Donner aux fichiers de preuves
des sommes de contrôle et des identifiants de révision ; préserver les révisions précédentes et expliquer
les corrections. Un lien ou une somme de contrôle ne prouve à lui seul ni une revue indépendante ni un
stockage immuable. Utiliser des données synthétiques et des extraits sûrs, jamais des identifiants secrets
ou du trafic réel. Rédigez le bilan par version uniquement en anglais, avec des manifestes techniques
communs ; le guide de publication et le reste de la documentation publique restent dans les cinq langues.
Une entrée du modèle ou une case cochée ne prouve rien. Un bilan ne vaut que pour les sources et le
périmètre indiqués ; signalez explicitement les campagnes incomplètes et les étapes de publication en attente.

## Publications distinctes du client

Les tags `sdk-python-v*` du client Python utilisent `.github/workflows/sdk-python-release.yml` et des versions
indépendantes. Ce workflow est hors de ce contrôle ; sa publication ne prouve pas la réussite de la CI sur main.
