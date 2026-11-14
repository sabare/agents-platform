# Roadmap and design decisions

Reviewed 14 November 2026. Everything under “Remaining work” is unfinished; the other guides describe the implementation. This is a priority proposal, not a delivery schedule.

## Remaining work

| Priority | Work | What is still missing |
|---|---|---|
| 1 | Broader human intervention | Client-report acknowledgement/guidance and same-task resumption are implemented. Records/objective review resolution, takeover, per-user reviewer identity and correction/re-review flows remain unfinished. |
| 2 | Retrieval quality and changing facts | Better document/section boundaries, question-specific reranking, explicit fact revisions with effective/recorded time, conflict handling and a representative labelled benchmark. Existing search filters source recording time. |
| 3 | Broader workflow support | Recurring check-in series, reply corrections/late responses, richer history pagination, generic intake and explicit supervisor delegation beyond records. |
| 4 | Real integrations | Messaging/provider portals, document intake, durable outbound action intents and reconciliation when delivery outcome is uncertain. Database-only simulated sends do not solve real delivery duplicates. |
| 5 | Production access and operations | Firm-user authentication/permissions, stronger tenant protection/RLS, retention and deletion propagation, monitoring, workload isolation and load/reliability testing. |

Other scale limits: task memory permits 100 open, unexpired items independently of archived history; policy/hold publication wakes affected tasks in one transaction; indexing uses a shared backlog; very large essential context requests review. Larger firms will need batched fan-out, backlog partitioning and safe task/context partitioning. Exact vector ranking is intentional until case-size and recall measurements justify approximate indexes.

The next retrieval evaluation should contain corrections, delayed information, ambiguous dates, similarly named providers, multi-source questions and unknown answers. Measure evidence recall, outdated-answer errors, citation accuracy, downstream decisions, latency, indexing lag and tokens separately. A seven-month test fixture does not prove seven months of live operation.

## Decisions we are keeping

| Decision | Reason |
|---|---|
| One codebase, API and worker processes | Clear logical boundaries without introducing unnecessary deployment services |
| PostgreSQL for business state; Temporal for progression | Saved responsibilities and durable waiting solve different problems |
| One workflow per executable task; one coordinator per case | Specialists can wait/recover independently while case plans persist |
| Supervisor proposes; backend validates and applies | A stale or invalid model proposal must not mutate assignments |
| Version-pinned definitions and explicit adapter identity | New behavior must not silently change existing multi-month work |
| Mandatory state through SQL; history through retrieval | Old restrictions must remain effective without depending on similarity search |
| Source-backed evidence and separate completion checks | A summary or retrieved historical statement does not establish task completion |
| Request context when needed, with a shared budget | Stored history can grow while model inputs remain bounded |
| Retain current PostgreSQL/pgvector search | A graph database or extra memory framework needs demonstrated benefit first |

Information scopes include platform/firm rules, provider and firm-provider instructions, case state, parent work and task state. These overlap rather than form one universal tree. Provider identity never authorizes sharing private case information across firms. Current instructions are not resolved by a blanket “most specific wins” rule; automatic natural-language conflict resolution remains unfinished.

## Research references

These sources informed the design. They are references, not claims that their full systems are implemented here or that their benchmark results transfer to legal workflows.

| Source | Idea retained |
|---|---|
| [LongMemEval](https://arxiv.org/html/2410.10813v2) | Test time-sensitive retrieval, knowledge updates and knowing when evidence is insufficient |
| [Zep temporal-memory paper](https://arxiv.org/html/2501.13956v1) | Keep original episodes and source-backed facts; distinguish when information applied from when it was recorded |
| [Anthropic Contextual Retrieval](https://www.anthropic.com/research/contextual-retrieval) | Preserve a chunk's document/entity context and combine keyword and meaning search |
| [Anthropic context engineering](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents) | Small working context and tools that load supporting material when needed |
| [RAPTOR](https://arxiv.org/abs/2401.18059) / [GraphRAG](https://arxiv.org/abs/2404.16130) | Multi-level summaries can help broad overview questions; they do not replace scope or time rules |
| [Magentic-One](https://arxiv.org/html/2411.04468v1) | Separate case planning and progress checks from specialist execution |
| [pgvector filtering](https://github.com/pgvector/pgvector#filtering) | Measure filtered recall before switching to approximate vector indexes |

Future research should update this focused list and the relevant decision rather than adding another overlapping proposal document. Keep one owner for each topic: architecture, memory/retrieval, development or roadmap; the root README is the entry point.
