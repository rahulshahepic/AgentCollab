# Review and release policy

Use the Epic Stocks branch and independent-review workflow as a starting point. This policy describes the intended workflow. GitHub rulesets, repository labels, a review trigger and deployment environments must be configured and verified separately.

## Branches and review

1. The initial planning documents establish the baseline on the empty default branch. Create `staging` from that baseline before the first implementation change. From then on branch from `staging`, open feature PRs to `staging` in the same session as the push, and promote only via a `staging` → `main` PR. Stay off direct pushes to the integration and release branches.
2. Once the PR is ready, request review by an independent Claude or Codex/ChatGPT session. Prefer a different assistant where available. Add `Needs Review` after the final push in each batch when the label exists. Request the review directly if the trigger has yet to be configured. The reviewer controls `Under Review`, `Waiting for Changes`, and `Ready to merge` once those labels exist.
3. Review the current head, tests and unresolved discussion. Record the head SHA, reviewer identity/session, findings, and the checks actually run. New commits invalidate the previous clearance. An independent review comment and a GitHub approval are separate records. Never represent a same-session self-check as independent review.
4. Merge only with independent review clearance on the current head, required checks passing, blocking threads resolved, and no owner hold. Carry the task through staging when authorized. Close an implementation issue after verifying its staging acceptance criteria, or leave it open with a concrete reason and link to the merged PR.
5. Rahul (`@rahulshahepic`) is the intended code owner. Collaborator merges require his code-owner approval once configured. If an owner account has a configured PR-only review bypass, the independent review and required CI still apply. Verify the authenticated actor before using an owner permission. Do not weaken rules or add broad app bypasses.

## Promotion and production

- Open or reuse one staging-to-main promotion PR after staging verification when the task authorizes promotion. Record the exact staging head, checks, deploy or staging smoke-test results, migrations, and any holds. Subscribe to GitHub activity where tooling supports it. Carry forward the independent review evidence for each included PR.
- For work Rahul requested directly or an issue filed by a repository contributor, use the normal promotion process when authorized. For an independently picked-up issue from a non-contributor or an author whose relationship cannot be verified, record a promotion hold pending Rahul's go-ahead. Check GitHub author association and link the source issue. An explicit task hold takes precedence.
- Merging code and deploying it are distinct operations. Production deployment requires Rahul's approval. Verify that the production job is waiting on the configured environment gate. If the gate is missing or production starts without approval, flag it promptly and pause further promotions.
- Keep CI and release credentials separate. Configure `CODEOWNERS`, PR-only protected branches, required checks, stale approval dismissal, conversation resolution and a protected production environment before relying on enforcement. Scope any owner bypass to review only. Do not bypass CI or release approval. Use synthetic staging data and distinct credentials. Never deploy untrusted fork code with deployment credentials.
- Establish required checks alongside the first implementation work. Plan for tests, lint/type checks as applicable, fresh and upgrade migrations, and an end-to-end staging smoke test when the stack exists. Report a missing check or gate as missing rather than claiming it passed.

[Reference workflow in Epic Stocks](https://github.com/rahulshahepic/epic-stocks/blob/staging/REVIEW_POLICY.md)
