# Agent instructions

## Sources and project context

- `docs/PRODUCT.md`: approved requirements and acceptance criteria. Label assumptions; do not change approved criteria to fit incomplete work.
- `README.md`: introduction and getting started. `docs/OWNER_GUIDE.md`: operation, verification, and recovery.
- Issues/PRs: tasks, temporary discussion, verification evidence, and review history. Use a local handoff when unavailable.
- This file: durable constraints and development commands. Update the authoritative source when its facts change; link instead of duplicating.

Read what is relevant to the task. Human-facing docs default to English unless the
owner requests otherwise; agent instructions and technical references use English.

This is independent local infrastructure, not desktop-companion code. Runtime:
Python 3.12+, aiohttp, stdlib SQLite. Preserve loopback-only routing, no redirects
or environment proxies, per-request positive local Ollama checks, and no cloud
fallback. Persist metadata only; never log request/response content, headers or
unsanitized backend errors. Unknown usage is null, never zero. Streaming must use
bounded buffering, retain cancellation/error outcomes, and avoid counting repeated
metrics. The gateway does not grant application consent or prove backend drain.
Only explicit configured clients/aliases/model pairs are accepted. No automatic
model downloads, GPU/driver changes, or daemon reconfiguration.

## Authority and Git

Choose implementation and development strategy autonomously within the authorized
task. Routine reversible edits, checks, local commits, documentation maintenance,
and in-scope review fixes need no extra approval. Ask only for missing decisions or
authorization affecting material product/scope or governance commitments,
authorization boundaries, destructive actions, security/privacy/data guarantees,
important permissions, significant costs, or external publication/deployment.
Existing authorization counts.

Keep `main` stable; use task branches or worktrees. Preserve unrelated
edits, data, and history, including staged/untracked work; never discard, stash,
stage, or commit unrelated work. Keep commits/PRs focused and traceable. Follow
repository conventions for branch names and merge method.

Remote creation, pushes, access/protection changes, publishing, and deployment
need authorization covering them. Authorization to open a PR covers its branch
publication, not merge. The owner may authorize merge in the initial task or after
PR review; do not request granted approval again. Once authorized, the agent may
merge and clean up merged task branches, respecting permissions and protections.

## Third-party code and attribution

- Agents may freely research existing open-source projects, algorithms, architectures, and implementation techniques.
- Prefer existing libraries over copying implementations when practical.
- Before incorporating any third-party material into the project, including code, images, fonts, datasets, binaries, and dependencies, verify and document its license and compatibility with the intended use, or another valid legal basis covering that use.
- Preserve all legally required copyright, license, and attribution notices.
- Record all incorporated third-party materials, relevant licenses or other legal bases, and sources in an appropriate location, such as `THIRD_PARTY_NOTICES.md`; preserve existing notice conventions.
- If a third-party material's licensing is unknown or incompatible, keep it out until a compatible license, permission from the relevant rights holder, or a documented applicable legal exception establishes a valid basis for the intended use. Obtain explicit human approval before relying on such permission or exception; project-owner approval alone does not supply third-party rights.
- Original implementations inspired by general ideas do not require formal attribution, but significant technical influences may be acknowledged voluntarily.
- Before submitting a PR or delivering a local handoff, report any new third-party code, assets, dependencies, or unresolved licensing concerns.
- Never claim licensing compliance has been verified when it has not.

## Checks and acceptance

Run relevant commands from `the repository root`:

```sh
uv sync --frozen
uv run --frozen python -m unittest discover -s tests -v
uv build --no-build-isolation
```

Scale verification to consequences. Persistent data, money, authentication,
sensitive data, and destructive migrations need affected failure/recovery evidence;
use disposable data. Critical safety/security release needs a qualified human's
validation plan. Structural checks, tests, and AI review do not prove correctness.

When implementation and relevant local checks are complete, create a regular PR
ready for review; use a draft only for incomplete work or an explicit need for
early feedback. Completed work needs no draft-to-ready transition. Without remote authorization,
deliver a local diff/report. Use `.github/pull_request_template.md` for what/why, owner
verification steps, actual checks/CI/review results, and remaining risks or gaps.

Relevant CI must pass before merge; unavailable CI needs an explicit owner decision.
Allow CI and configured automated review, including Codex Code Review when
available, to run. Evaluate findings, fix confirmed correctness/security/data-integrity
issues, and justify declining stylistic or
unnecessary changes. Substantive revisions need affected checks and renewed review
when prior conclusions no longer hold; avoid endless review loops. AI comments are
not formal approving reviews or merge authorization. Do not require an independent
approving reviewer when none is eligible. Evidence must cover the delivered revision.
