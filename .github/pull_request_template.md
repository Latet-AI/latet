## What it changes

<!-- What the pull request does and why. The issue it closes, if there is one: "Closes #123". -->

## Verification

<!-- Use synthetic test data only. Link evidence; a checkbox alone is not proof. Keep short entries here
and link a longer record when needed. Say "not run", "incomplete", "failed", "passed", or
"not applicable (reason)" explicitly; include skipped tests and reasons even in a passing suite.
Never invent a reviewer or a result. Use truthful review roles/record IDs for automated reviews,
without assistant, model or provider signatures/attribution in the PR description. -->

- **Source and scope:** full tested commit SHA (or base SHA + retained patch/file manifest for a
  dirty tree); exact files/behaviour covered and excluded. Identify any later changes.
- **Review:** actual human reviewer or identifiable review role/record; kind (author self-check,
  separate automated agent, maintainer human, external human); reviewed source/scope and report link.
  Independent human review: yes/no/not performed, with basis. Separate automated review is not human review.
- **Findings:** closed IDs with fix/recheck evidence; open IDs with severity and remaining action,
  or explicitly none found in the stated scope. Pending review is not approval.

| Check | Exact source, command/selection and environment | Result and evidence |
| --- | --- | --- |
| Suites / manual checks | OS/runtime, backend, relevant flags and mocks | Status; pass/fail/skip counts, reasons; log/report |
| Fuzz (or not applicable) | Harness/source, configuration IDs, seed/corpus, target and actual counts | Completed/planned configurations; failures, incomplete/not-run rows; evidence |
| CI | Repository, workflow path, event/ref, full run head SHA, run ID/attempt URL | Whole-workflow status/conclusion; matrix/server scope and omissions |

<!-- Fuzz counts are correctness coverage, not performance measurements. Disclose mocked upstream
and direct space tests separately from authenticated end-to-end tests. Private tested source, PR
head/merge-test source and final public squash SHA may differ: record each accurately. Final push/main
CI must pass for the exact public commit before tagging; PR checks cannot establish that result.
No final commit/tree SHA goes inside a file that determines that same SHA. The English per-version
summary and shared technical manifests are external to the commit, as explained in
https://docs.latet.ai/releasing/. -->

## Contributor license agreement

Your first pull request accepts the [contributor license agreement](https://github.com/Latet-AI/latet/blob/main/CLA.md):
you keep your copyright, and your contribution can be part of Latet under every license it is offered
under.

- [ ] I agree to the Latet contributor license agreement (CLA.md).
