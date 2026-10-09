# Identity and notifications live in internal-claude; the aidev Runner is a separate service

aidev needs to resolve a Jira user to a Slack user and a GitHub login, and to send Slack notifications. internal-claude already has the Slack app and its scopes, GitHub-to-Slack matching, SQS workers, service auth and observability. We build the shared Directory and notification API there, and the aidev Runner calls it.

We did not run aidev inside internal-claude's api-server because that server is a custom agent loop on AWS Bedrock with a fixed tool set, not Claude Code. aidev needs the real `claude` and `codex` CLIs, git, `gh` and plugins such as the review skill, and porting it would be a rebuild without Codex. Keeping the Runner separate also means a long, heavy Job cannot starve the chat bot, and each service deploys on its own schedule.

Consequence: aidev depends on a service in another repository, so changes to the Directory API need to stay backwards compatible.
