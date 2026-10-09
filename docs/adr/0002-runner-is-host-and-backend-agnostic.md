# The Runner must not know which host or which Claude backend it runs on

Phase 1 runs on a developer's computer with a personal Claude login; the dedicated server will use Bedrock. To keep the move a deployment and not a rewrite, the Claude backend (subscription login or Bedrock role), the Codex credential, host paths and the capacity caps are configuration only. Jobs run in containers with secrets from Secrets Manager in both phases.

We accepted running other developers' Jobs on a personal login in Phase 1 (a terms-of-use grey area, shared rate limits) in exchange for testing the real architecture before buying infrastructure. The alternative was to build for the laptop first and containerise later, which would have meant a second migration and no real test of the server design.
