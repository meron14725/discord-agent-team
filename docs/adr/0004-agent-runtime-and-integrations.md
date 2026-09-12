# ADR 0004: Agent runtime and integration boundary

## Status

Accepted for the current single-owner deployment.

## Decision

The control plane remains Python, PostgreSQL, Discord Gateway, and typed internal HTTP contracts. Model turns run through `codex exec` in non-interactive mode with JSON Schema output inside disposable Docker Sandboxes. The host worker launches the CLI as a subprocess; user text never becomes shell syntax or command arguments.

This is a supported Codex automation pattern, not an interactive terminal workaround. OpenAI documents `codex exec` for scripts and CI, `--output-schema` for machine-readable decisions, read-only sandboxes by default, and reuse of saved CLI authentication. ChatGPT-managed authentication is treated as an advanced trusted-runner setup. API-key or workload-identity based SDK services remain the later production option if operating requirements outgrow the subscription-backed local runner.

n8n is an optional integration edge. It may handle schedules, notifications, CRM/email/calendar flows, and human-in-the-loop forms. It must call a narrow authenticated controller endpoint and must not receive GitHub publisher credentials, Discord administrator tokens, database credentials, Docker access, or the authority to bypass approval state. The Discord Gateway stays the inbound chat path because it supplies real-time message events and reconnect/resume behavior.

The workflow maintains distinct roles. The coordinator selects a response, specialist delegation, formal task, or clarification. Selected specialists perform separate model turns concurrently, bounded to three turns. Upstream, downstream, and SRE conversations use separate one-slot worker processes and separate recovery journals. Formal tasks continue through upstream specification, owner approval, downstream implementation, independent upstream review, and trusted CI.

## Product workflow

The controller supplies `prompts/company-memory.md` to conversational agents. It records the current thesis, narrow wedge, stage, must-build and must-not-build boundaries, evidence goal, and decision rights. Project work should optimize for one evidence-producing slice before expanding scope. Browser-facing projects may later add Playwright MCP journey checks after its tool permissions and evidence format are separately approved.

## Sources

- [Reference adoption notes](../references/agentic-company-inspirations.md)
- [OpenAI Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Discord Gateway documentation](https://docs.discord.com/developers/events/gateway)
- [n8n Discord node](https://docs.n8n.io/integrations/builtin/app-nodes/n8n-nodes-base.discord/)
- [From SaaS Idea to Agentic Build](https://www.brainstron.ai/blog/from-idea-to-agentic-build-solo-founder-workflow)
- [Claw-Empire](https://github.com/GreenSheep01201/claw-empire)
- [Claude-to-IM Skill](https://github.com/op7418/Claude-to-IM-skill)
- [Just Talk To It](https://steipete.me/posts/2025/just-talk-to-it)
- [Shipping at Inference-Speed](https://steipete.me/posts/2025/shipping-at-inference-speed)
- [OpenAI Symphony orchestration specification](https://openai.com/index/open-source-codex-orchestration-symphony/)

## Consequences

The local shell scripts are launch wrappers around a typed worker service. They are acceptable process entrypoints on the trusted Mac; launchd owns the supervisor, and the supervisor owns worker health checks, coordinated restarts, and logs. Scaling to ten roles should add role configuration and a bounded worker pool rather than one operating-system process per persona.
