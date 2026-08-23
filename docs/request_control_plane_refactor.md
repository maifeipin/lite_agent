# Lite Agent request control-plane refactor

Branch: `codex/request-control-plane-refactor`

This is one pragmatic major refactor, not a new workflow framework. It joins the
request selector, LLM invocation/accounting, plan execution, inspections,
structured tasks, cost-aware budgets, and reliable output delivery at the few
boundaries they already share.

## Design rule

Every task has a cost. Spend more only when expected benefit, uncertainty, or
risk justifies it. Manual configuration remains the primary control surface.

- A low-value deterministic task should use a cheap model or no Worker model.
- A high-value ambiguous task may use a strong author/reviewer or committee.
- Token budget controls how long a task may continue; model price does not
  silently decide permissions.
- A model review is advice. Deterministic policy is the hard boundary, and an
  experienced user can override only findings explicitly marked overrideable.
- A change that provides neither immediate value nor a clean extension point is
  outside this refactor.

## Resulting architecture

```text
IM / API / Dashboard
        |
        +-- simple request ------------------------------+
        |   optional model/output directives             |
        |                                                 v
        +-- complex TaskSpec -> preflight -> review -> execute
                                  |             |
                                  | hard rules  +-- Planner / direct tool DAG
                                  |             +-- manual / once / repeat
                                  v
RequestSelector -> ModelRouter -> LLMGateway -> ModelInvoker -> provider
                      |              |
                      |              +-> ExecutionLedger (all auxiliary calls)
                      +-> configured model/cost tier

complete answer -> channel limit -> summary -> HedgeDoc / email / SQLite
```

`ModelRouter` still owns model configuration and client selection.
`ModelInvoker` still owns OpenAI-compatible versus Gemini protocol details.
`LLMGateway` is intentionally thin: it connects an Invoker to the existing
Ledger so Planner, reviewer, committee, memory and title calls do not bypass
usage accounting. It is not another provider abstraction.

## Simple requests

Natural IM/API requests continue to work as one prompt. Existing model override
syntax chooses a configured model. Output destination can be selected without
changing code:

```text
用 gemini-pro 优化这条旅行路线 [output=email]
总结这份报告 [output=hedgedoc]
```

Supported request values are `auto`, `email`, `hedgedoc`, `sqlite`, and
`inline`. `store` is accepted as an alias for `sqlite`. The API accepts the same
value in `output_delivery`. Explicit Chinese phrases such as “完整回复发到邮件”
and “上传到 HedgeDoc” are also recognized. A HedgeDoc result is labelled as a
public link before delivery. Explicit `email` failure falls back directly to
SQLite; it never silently publishes the private answer to HedgeDoc.

## Complex TaskSpec requests

The Dashboard now provides persistent TaskSpec CRUD, editing, validation,
review, approval, immediate execution, and manual/one-time/repeated scheduling.
Uploaded JSON receives a fresh task identity and must contain the current
canonical policy snapshot.

The editable contract contains:

- objective, context, assumptions, required inputs, constraints, acceptance;
- complexity and model policy (recommended tier, preferred/allowed model,
  whether the user locked the choice, cost advice behavior);
- network mode, citation requirement and minimum sources;
- capabilities and approved plan nodes;
- max total tokens, steps, wall time and parallelism;
- side-effect approval and schedule;
- output format, summary/preview mode and full-output destination;
- failure, retry and partial-result behavior.

The immutable policy is supplied by code and embedded in every TaskSpec for
transparency. Preflight always overlays and verifies the canonical copy.
Hard blockers include policy mismatch, unavailable models/tools/capabilities,
missing required input, invalid budgets/schedules, unapproved side effects,
forbidden networking, suspected secrets, and invalid output policy.

The high-value author/reviewer may produce findings, but those findings cannot
weaken deterministic checks. Advisory findings can be acknowledged with a user
rationale. A deterministic blocker cannot be acknowledged away. Editing after
validation changes the content digest and requires revalidation.

## Cost and execution capacity

Model cost and execution capacity are related through TaskSpec budgets, without
hard-coding provider names:

- `preferred_model` / `allowed_models` constrain model choice.
- `recommended_tier` expresses low/standard/high expected value.
- `max_total_tokens` is the real spend/compute budget.
- `max_steps` is an independent safety ceiling.
- the orchestrator derives the effective step allowance from the approved
  budget and configured model capacity.

This permits a long local/free-model task to run for many steps when the user
sets a large token and step budget, while a paid high-value task can deliberately
use a stronger model with a tighter execution envelope. Permission and
side-effect rules never become looser because a model is cheap.

For one-prompt IM/API work, `task_routing.simple_model` is the manually
configured low-cost default. An explicit user model directive always wins and
may only add a non-blocking cheaper-model suggestion; it never changes the
requested model silently.

## Plan execution and inspections

Planner tool exposure reuses `RequestSelector`. A confident subset is passed to
the Planner; uncertain requests preserve the full-tool fallback. A planned node
with exactly one registered tool and valid JSON arguments can execute directly,
without starting a Worker LLM. Ambiguous nodes retain the Worker path.

Direct execution is manually controlled with
`task_routing.direct_tool_execution`. Inspection/data-analysis work is routed to
the configured flash tier. Committee and genuine complex-reasoning routes keep
their stronger configured models.

The three previously unmapped tools now have narrow selector domains:

- explicit committee language selects `ops_decision`;
- RSS node status selects `ops_rss_node_status` read-only;
- explicit Meili index synchronization additionally selects `sync_meili`.

## Output completeness

Output generation and output transport are separate failure domains.

### Provider output-limit recovery

All Invokers normalize provider output-limit reasons to `length`. The Runtime
retries once when a step remains. If partial answer text exists, it asks the
model to continue from the interruption and joins both pieces. If no answer text
exists (the observed GLM case where reasoning consumed the output budget), the
retry reuses the original request.

The recovery request is configured per model in `conf.d/llm.json`:

```json
"output_recovery": {
  "retry_kwargs": {"thinking": {"type": "disabled"}}
}
```

OpenAI-compatible services can instead use provider-specific `extra_body`; a
Gemini native model can use `thinking_budget` or `thinking_level`. The Runtime
does not branch on Ark, DeepSeek, Qwen, OpenAI, or Gemini names. A model that
cannot reduce thinking can still continue once using its configured supported
arguments.

### Complete-output delivery

Delivery policy lives in a new `conf.d/output_delivery.json`, not in `llm.json`,
because it is model-independent. Copy the example and set the recipient:

```bash
cp conf.d/output_delivery.json.example conf.d/output_delivery.json
```

Default long-output behavior:

1. Generate a compact summary with the configured low-cost `summary_model`.
2. Preserve the complete response at the first successful destination.
3. Default fallback order: public HedgeDoc, email, local SQLite.
4. Email reuses the deployed `mail-statement-parser` credentials and tries QQ,
   163, Outlook, then Gmail.
5. SQLite returns a 16-character archive id. Admin API retrieval is
   `GET /agent/api/v1/output-archive/{id}`.
6. If every archive path fails, the complete text still remains in session
   history and the channel receives an explicit warning instead of silent
   truncation.

Non-streaming API replies remain inline unless the request explicitly chooses
another destination. IM limits are conservative; WeChat uses 2000 characters
before archiving because its actual transport limits bytes and context sends.

## Gate 2 shadow review

The VPS shadow sample contained 13 requests (Aug 14–21): 9 subset selections
(69.2%), 4 uncertain/full fallbacks (30.8%), no empty selections, average 2.89
tools for subset requests, and no over-15 fallback. Domains were todo 3, web 3,
media 2, billing 1. Logs intentionally contain no raw request.

This is enough for a small, reversible Gate 2 canary, not enough for broad
automatic enablement. Keep the environment switches independent:

```text
LITE_AGENT_SELECTOR_SHADOW=1
LITE_AGENT_SELECTOR_ENABLED=0
```

Then enable only a small traffic slice, watch miss/fallback and tool-call errors,
and roll back by setting `LITE_AGENT_SELECTOR_ENABLED=0`. Shadow may continue
during the canary.

## Compatibility and deliberate non-goals

- Existing simple IM/API behavior and Selector `None / [] / subset` semantics
  remain.
- Guest tool filtering and deterministic permission checks remain final.
- Existing `config.json` HedgeDoc and billing credential locations are reused.
- No generic workflow DSL, policy language, plugin framework, automatic model
  benchmark, provider credential migration, or commercial-grade queue was added.
- Scheduling supports only the existing useful forms: manual, once, `HH:MM`, or
  `*/N * * * *`.
- Dashboard visual verification is intentionally left to the owner; backend and
  contract tests cover the API behavior.

## Rollout checklist

1. Deploy with Selector in Shadow and direct tool execution disabled.
2. Copy `output_delivery.json.example`; set email recipient and enable email if
   desired. Confirm HedgeDoc is acceptable as a public destination.
3. Verify GLM output-limit recovery with the current default model.
4. Run one long WeChat response and confirm summary plus complete link/archive.
5. Run one explicit `[output=email]` request and force the first SMTP account to
   fail to verify provider fallback.
6. Run one inspection TaskSpec and verify flash routing and Ledger records.
7. Enable direct deterministic nodes, then a small Gate 2 selector canary.

## Change size

The requirements expanded beyond the original selector/Gateway merge to include
the TaskSpec editor/scheduler and reliable output delivery. The final production
change is intentionally concentrated in four new modules plus one Dashboard
module; tests are separate. Use `git diff --stat` on the PR for the exact final
count. The main maintenance cost is TaskSpec validation/storage, not provider or
channel branching; adding a model or destination does not require modifying the
execution loop.
