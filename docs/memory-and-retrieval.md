# Memory and context retrieval

Reviewed against the code on 14 November 2026. This is the current implementation, including the focused retrieval service.

## Three kinds of information

| Kind | Storage and selection | Authority |
|---|---|---|
| Current operational state | Task/domain state, approved instruction revisions, open obligations and applicable task/case holds; selected directly with SQL | Current controls and responsibilities, checked by backend code |
| Original evidence | Sources and activity events, linked to case/task identities | What was received or observed; not automatically verified truth |
| Supporting history | Derived passages, embeddings and a source-linked digest | Searchable evidence; cannot grant permission or prove completion |

Mandatory memory always loads, regardless of age or similarity. A months-old open restriction does not need to win a vector search. Each activation records selected instruction/item revisions, source IDs and a fingerprint. The specialist checks task version and memory again before a simulated action and final commit; the supervisor also checks its saved plan/task/memory state.

Approved instructions have immutable revision history. An operator publishes a revision with a reason. Task obligations and stop-contact items require explicit resolution or an authorized replacement; holds do not expire automatically. Task memory now uses normalized `task_memory_items` rows, with active, archived and scheduled items kept separate in the briefing. Review dates never lift a hold. Only optional informational notes can expire automatically. Whole-case or provider-specific case holds apply to existing and future tasks. A free-text “stop” comment does not automatically create a typed restriction. Resolving an item records the operator's reason; it does not establish that consent is legally sufficient.

## Indexing and search

A durable background workflow reads saved sources in batches, creates passages and refreshes the case digest. It polls roughly every 15 seconds, faster while draining a backlog, and resumes after restarts. Derived text commits before embedding calls; embedding failures retain keyword search and retry with backoff.

Current choices:

- Source content is serialized as JSON and split into 1,600-character passages with 200-character overlap. These are simple cuts, not document-section-aware chunks.
- Embeddings use `text-embedding-3-small`, 1,536 dimensions. Scripted mode does not generate fake embeddings. Changing the embedding profile requires reindexing/migration.
- Exact source ID/key lookup works before indexing. PostgreSQL full-text search and exact vector-distance ranking operate within the authorized firm/case. There is a text-search index; approximate vector indexes are not used.
- Rank fusion combines keyword and vector candidate lists. Vector search needs both source and query embeddings; missing embeddings leave keyword search working.
- Passages and digests carry source hashes. Changed/missing sources invalidate their derived copies until rebuilt.
- The versioned digest contains excerpts from eight recent sources, with coverage and references. It is not a generated summary of the whole case.

Current time filters use **source recording time**, not symptom/treatment dates or when a fact became true. The system does not yet reconcile contradictory historical claims into a dated current-fact model.

## Agent-requested context

Both specialist and supervisor SDK paths receive platform-owned read-only tools, separately from published domain tools:

| Tool | Use |
|---|---|
| `retrieve_context` | Ask a specific historical question, optionally filtering by an authorized case provider and recording-date interval |
| `read_evidence` | Expand a source; continue long content with `next_offset` and `expected_hash` |

Identity comes from the execution, not agent-supplied firm/case arguments. Provider filters require a case participant with the medical-provider role and use saved event/request, evidence or backend tool-source links. Unlinked sources are excluded by a provider filter; provider names do not establish those links.

Retrieval considers up to 12 fused candidates, removes identical passage text within each source, and returns at most six passages within the requested response budget. Separate reports with the same wording retain their own source IDs, dates and event links. This is rank fusion and duplicate removal, not a learned reranker. Results include source IDs/hashes, timestamps, linked event IDs, applied filters, search method, unindexed-source count and truncation. An empty or partially indexed result is not proof that something never happened.

Reads can expand unindexed sources. Their offsets refer to characters in serialized source JSON. Supplying the returned hash prevents continuation across a changed revision. The prototype caps read offsets at one million characters.

Requests are serialized and capped per activation. Invocation arguments/results are saved in the existing tool ledger; identical repeated requests reuse their saved response. Retrieval does not create new Sources from its results. Historical citations remain separate from domain completion evidence.

## One budget before every model call

The full starting snapshot remains saved on the execution. A model-facing copy is packed before each SDK call, including later calls after tools have added history.

The filter counts instructions, tool schemas, structured-output schema and messages using `o200k_base`, with extra message/serialization reserves. This is an estimate, not provider billing-token accounting. Other model families require tokenizer validation.

| Setting | Default |
|---|---:|
| `CONTEXT_INPUT_TOKENS` | 12,000 estimated input tokens per call |
| `CONTEXT_OUTPUT_TOKENS` | 1,500 output tokens per call |
| `CONTEXT_RETRIEVAL_CALLS` | 4 retrieval/read calls per activation |
| Retrieval/read `max_tokens` | 1,800; allowed range 256–2,500 |

Packing removes duplicated instructions, reduces optional historical passages/digest/recent events, and replaces large supporting source bodies with excerpts and expansion references. All retrieval/read results remain available when they fit. Only when the input still exceeds the budget do older evidence bodies become source references, oldest first. Their IDs, hashes, dates, event links and read offsets remain available for expansion. The newest evidence result, call/output pairs and domain-action results remain intact. Usage records show estimated tokens, selected initial source IDs, omissions and compaction.

Mandatory memory, task/domain state and unread event payloads are not silently shortened. If essential information or retained tool history cannot fit, the agent requests operator review. A large unread backlog can therefore block work; automatic backlog partitioning is not implemented. Per-call budgets supplement existing model-turn/activation limits; they are not a total run-cost budget.

## Operator API

The authenticated operator routes use the same service as agent tools. Prefix all paths below with `/v1/firms/{firm}`. Full schemas are in [Swagger](http://localhost:8000/docs).

| Route | Purpose |
|---|---|
| `GET/POST /tasks/{task}/memory` | Read/add/resolve task memory |
| `GET/POST /instructions/{instruction}/revisions` | Read history/publish an approved instruction revision |
| `GET /cases/{case}/memory` | Case memory version, restrictions and digest |
| `POST /cases/{case}/restrictions` | Add/resolve whole-case or provider-specific holds |
| `GET /cases/{case}/memory/search?q=...` | Basic scoped historical search |
| `GET /cases/{case}/sources/{source}` | Read an original source |
| `POST /cases/{case}/context/retrieve` | Question-specific, filtered, budgeted evidence |
| `POST /cases/{case}/context/read` | Bounded source expansion |

Memory/restriction writes require `Idempotency-Key` and the current `expected_version`. Instruction publication uses `expected_revision`. Resolution requires an item ID and reason text. Task memory permits at most 100 open, unexpired items; archived history does not consume this limit. History is paginated independently of the complete current-control load. Policy/hold changes currently fan out to affected tasks in one transaction.

Example retrieval body:

```json
{
  "question": "Why was MRI reference ZQ731 blocked?",
  "provider_id": "<case provider UUID>",
  "recorded_after": "2026-03-01T00:00:00Z",
  "recorded_before": "2026-04-01T00:00:00Z",
  "max_tokens": 1800
}
```

The lower date bound is inclusive and the upper bound exclusive; timestamps require timezones. To read a returned source, send `source_id`, optional `offset`, `expected_hash` and `max_tokens` to the read endpoint.

Core files: `memory.py`, `memory_api.py`, `memory_history.py`, `context.py` under `src/agents_platform/`. Broader temporal memory, retention/deletion and retrieval-quality work are in the [roadmap](roadmap.md).


## Read the code for the demo

The two former context modules are combined in **`context.py`**. No additional storage or framework is introduced.

| Part | Responsibility |
|---|---|
| `pack_context` / `ModelBudget` | Fit a model-facing copy into the input budget; preserve mandatory state and keep evidence while it fits |
| `RetrievalRequest` / `EvidenceRequest` | Small validated inputs; no caller-supplied firm or case identity |
| `RetrievalService` | Search and read within the trusted case, with dates, provider filters and source references |
| `ExecutionRetrieval` | Expose both SDK tools, limit calls and save requests/results in the existing audit ledger |

Follow one request: agent asks a question → execution supplies firm/case identity → service searches indexed history → cited results are saved and returned → budget checks the next model call. The original snapshot and full tool results remain saved even if the model-facing copy is shortened.

The other memory files have separate jobs: `memory.py` loads current controls; `memory_history.py` indexes/searches historical evidence; `memory_api.py` exposes operator routes. They remain separate to keep current controls, background indexing and HTTP handling easy to locate.


## Maintaining current memory

See [Memory lifecycle](memory-lifecycle.md) for exact-duplicate reuse, explicit
supersession, effective/review dates, instruction retirement and bounded history.
The existing indexing worker also performs bounded lifecycle sweeps. Action-time
checks independently honor effective and expiry dates, even if the sweep is late.
Both agent retrieval tools label historical memory sources with their current
status, active flag and replacement reference. Retrieved text cannot revive a
resolved or superseded restriction. This is an explicit operator-managed lifecycle;
automatic natural-language contradiction resolution remains unimplemented.
