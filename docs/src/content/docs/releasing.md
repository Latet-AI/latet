---
title: Releasing Latet
description: Publish a version only after the exact main commit has passed the complete CI workflow.
---

This procedure covers Latet's `v*` tags and `.github/workflows/release.yml`. Keep one public
commit per version, including its code, tests, version numbers and documentation in all five languages.

## Merge, wait for CI, then tag

1. Prepare and review the version as one public commit. Keep the package and Claude plugin version
   numbers aligned; the release build checks them against the tag.
2. Merge that commit onto `main`. Record its full commit SHA.
3. Wait for the **complete CI workflow** (`.github/workflows/ci.yml`) triggered by that push to
   `main` to finish with **success** for that exact SHA. Check the workflow result, including every
   matrix job and the server tests. A successful pull-request run or individual job is insufficient.
4. Create the version tag at that exact commit and push it only when publication is authorized.
   Lightweight and annotated tags are supported; an annotated tag is checked against its peeled commit.
5. Check the release workflow result. Keep the tag SHA and the successful main CI run URL with the
   release verification record. The gate's job summary records the accepted run and attempt.

If the commit changes, repeat the main CI check for the new SHA before tagging. Preserve the single
public version commit; do not add a separate public commit solely to record successful checks.

## What the release gate requires

Before builds, attestations or publication, the gate resolves the actual CI workflow ID from its file,
checks that the tag commit belongs to `main`, and reads the exact SHA's `push` runs on `main`.
It selects the latest run and refreshes its current attempt. Only `completed` with `success` passes.
The later jobs check out the verified commit SHA and depend on the gate.

Missing, queued, running, waiting, failed, cancelled, skipped or neutral results block the release.
API or authentication failures, inconsistent responses and incomplete pagination also block it.
The gate refuses inventories at GitHub's 1,000-result filtered-search ceiling. It does not wait or retry
until a favourable result appears. Investigate the failed check, let the exact main commit's full CI
succeed, then explicitly rerun the release when authorized. Do not move a published tag to bypass a failure.

The result is evidence at the time of the gate check; a later CI rerun can change the recorded run's state.
A green gate confirms that CI passed, not that every possible defect has been excluded.

## A verification record for each version

Link a short English verification summary from the release notes, with a versioned Markdown file
and shared source/evidence manifests attached outside Git. Finalize the record after the public
commit exists and its complete main CI succeeds; after the authorized tag and release, manually
attach the files and add the release-note links. The current release workflow does not generate
these verification files. Keep one public commit per version: the external record can name its
exact SHA without putting that SHA inside a file that determines it.

Include:

- Version, full public commit and peeled tag SHA; separately, the private source actually tested
  (commit or base plus retained patch/manifest). A manifest lists paths and content hashes for the
  tested code, tests, harness and configuration. Record all differences from the public source,
  exclusions and any required rechecks; a private test result does not become a public-SHA result.
  Keep canonical Git-blob and actual execution-byte inventories separate for clean and dirty runs;
  record EOL/filter transformations and differences, without normalizing the executed-byte hashes.
- Actual review participants or identifiable roles and report references, reviewed scope, review kind
  and whether an independent human reviewed it. Separate automated-agent review is not human review.
  List closed findings with recheck evidence and open findings with severity and remaining work.
- Suite commands, environment, results and skips; fuzz configuration IDs, harness/source, seeds or
  corpus, target/actual counts and completed/planned configurations. Mark failed, incomplete and
  not-run work explicitly. Count campaigns do not establish performance measurements.
- Exact CI repository, workflow path, event/ref, head SHA, run ID, attempt URL, whole-workflow
  status/conclusion and observation time. Check final `push`/`main` CI separately before tagging;
  PR checks, local results and publication success cannot replace it.
- Tested limits: mocked upstream or direct space-runtime tests versus authenticated end-to-end
  tests, omitted configurations and unavailable evidence. Private logs are not publicly inspectable
  evidence; provide a safe public extract when possible and state what readers cannot verify.

Keep the summary and manifests outside the commit they identify. Give evidence files checksums and
revision identifiers; preserve prior revisions and explain corrections. A link or checksum alone is
not proof of an independent review or immutable storage. Use synthetic data and safe extracts, never
credentials or real traffic. Write the per-version report in English only, with shared technical
manifests; the release guide and other public documentation remain in all five languages. A template
entry or checkbox alone proves nothing. A report is evidence only for its stated source and scope;
list incomplete campaigns and pending publication steps explicitly.

## Separate client releases

The Python client's `sdk-python-v*` tags use `.github/workflows/sdk-python-release.yml` and independent
versioning. That workflow is outside this gate; do not treat its publication as evidence of successful main CI.
