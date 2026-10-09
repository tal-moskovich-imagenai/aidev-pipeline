# aidev

Autonomous pipeline that turns Jira tickets tagged `aidev` into reviewed pull requests, using Claude Code and Codex, for all Imagen developers.

## Language

**Ticket**:
A Jira issue tagged `aidev` that the pipeline works on.

**Job**:
One run of the pipeline against one Ticket, from pickup to review hand-off.

**Bot account**:
LADIS: the shared bot identity (GitHub user, Jira account and Slack bot, also used by other internal infrastructure). It authors every PR and posts every Jira comment and label change the pipeline makes. aidev uses its own token on this identity.

**Requester**:
The Imagen developer on whose behalf a Job runs: the Ticket's Jira assignee, or the reporter if nobody is assigned. Distinct from the Bot account, which is the PR author. The Requester's comments are prioritised, but other people's comments still reach Claude.
_Avoid_: user, owner, author

**Directory**:
The mapping between one person's Slack user, Jira account and GitHub login. Lives in internal-claude, not in aidev.

**Runner**:
The aidev service that runs Jobs with the real Claude Code and Codex CLIs. Calls internal-claude for the Directory and for notifications.

**Review skill**:
The single skill (`claude.review_skill`) run after a PR exists to simplify, review, fix and watch CI.
