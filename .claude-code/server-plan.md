# aidev as a shared service — plan

Goal: any Imagen developer can tag a Jira ticket `aidev` and get a reviewed PR. Today only one person can, because the pipeline assumes it runs on that person's laptop.

## Decisions

| Area | Decision |
|---|---|
| Audience | Imagen developers only (one Jira, one GitHub org, one Slack). External customers are out of scope |
| Bot identity | LADIS: the shared GitHub user, Jira account and Slack bot. aidev uses its own fine-grained GitHub token on it, so it can be revoked without touching other infrastructure |
| Requester | Jira assignee, reporter as fallback. The assignee's comments are prioritised; other people's comments still reach Claude |
| Shared with internal-claude | Directory (Slack, Jira, GitHub mapping), notifications, service auth and observability, built inside internal-claude. The aidev Runner is a separate service that calls it |
| Directory | Slack members listed with emails; Jira accountId found by email search (Jira hides emails in responses but searches them); exact display-name match as fallback; unresolved people get a ticket comment, never a guess. Cached in Postgres |
| Execution | One container per Job. Interactive Claude in tmux inside it. `status.json` is the only completion/needs-input signal. Replies from Jira are wrapped as `Human reply from Jira: ...` and pasted, so they can never start with a command character |
| Operators | `docker exec -it <job> tmux attach` to watch or wake a Job |
| Requesters | A web page reads `status.json` and the session transcript for their own Jobs |
| Orchestration | One long-running Runner service replaces the two cron scripts and the lockfile. Postgres (docker-compose now, same DB on the server). Polling Jira org-wide: `labels = aidev` in an allowed list of projects |
| Capacity | A global cap (the only value that differs between laptop and server), 3 running Jobs per Requester, FIFO queue with an `aidev-queued` label and a position comment. A Job waiting for a human stops its container and frees its slot; it resumes with `claude --resume` from a per-Job volume |
| Repos | Allowlist of `repo:KEY` → git URL, base branch, review skill, required checks, auto-merge allowed. A bare mirror cache plus a fresh `git clone --reference` per Job. No label, an unknown label or several labels: the Job does not start; the ticket goes to `aidev-stuck` with a comment saying no repo was mentioned and which labels are valid, and the Requester gets a DM |
| Secrets | AWS Secrets Manager, fetched by the Runner at Job start, injected as environment variables. Egress allowlist. Container is destroyed at the end. Inside: Claude and Codex auth, the aidev GitHub token, read-only Jira, Notion and Figma access. Outside, in the Runner: Jira comments, labels and status moves, Slack, merges |
| Slack context | Not available in the container. The ticket author pastes what matters into the ticket |
| Cross-ticket context | No shared filesystem. Jira (ticket, linked tickets, Epic) and Notion are durable. The Runner collects named files from a Job's `.claude-code/` and attaches them to the ticket or its Epic |
| Reply channels | Jira comments and the PR. Slack is notification only |
| Review skill | `/land-pr`, named once in `claude.review_skill` |
| Rollout | Requester allowlist. Start with one person on real projects, add developers once there are no bugs |

## Milestones

- **M0 — identity and routing on the current pipeline**: PRs by LADIS with a new aidev token; org-wide pickup with the Requester rule; the strict repo-label rule with stuck state, comment and DM.
- **M1 — containers**: one container per Job, secrets from Secrets Manager, egress allowlist, per-Job volume.
- **M2 — Runner service**: replaces cron and the lockfile; Postgres; global and per-Requester caps; queue; stop-and-resume on needs-input; usage records.
- **M3 — shared API in internal-claude**: Directory, notifications, auth and observability; Slack DMs; the Requester web view. Everything runs on one laptop and looks like the real server.
- **M4 — move to the dedicated server**: only three changes — the host, the Claude backend (subscription login to Bedrock through an IAM role) and the Codex key (personal login to a company key). Optional: a credential-injecting proxy so containers hold no real secrets.

## Known risks in M0–M3

- Claude and Codex run on one person's personal logins for other developers' jobs: a terms-of-use grey area, and their rate limits are shared. This is why M4 exists.
- The laptop must stay awake and online.
- Notion and Figma access must be read-only; the Notion key's scope is unverified and Figma MCP read access needs approval.
- Run the secret-read prompt-injection test on a throwaway ticket before adding a second developer.
- Ofek-style accounts that Jira cannot find by email fall back to name matching, then to ticket-only notification.

## Open items

- Confirm the Notion key is read-only.
- Get the Figma MCP read access approved.
- Decide the global cap value for the laptop.
- Rewrite the ticket skill: "attach it to the Epic" instead of "put it in `.claude-code/`".
- Remove the macOS-only pieces: Keychain token, `osascript` notifications, absolute home paths in the cron scripts and the ticket skill.
