# Memory and retrieval

Think of **memory as the filing cabinet** and **context as the briefing an agent
gets for one turn**. Both live in the existing PostgreSQL system; the model does
not retain an endless private conversation.

## What is stored

| Information | Storage | How it reaches the agent |
|---|---|---|
| Current firm/provider instructions | `instructions`, with `instruction_revisions` history | Loaded directly for the applicable scope |
| Task holds, obligations and notes | `task_memory_items` | Effective open controls load directly; notes are optional |
| Case-wide/provider-specific holds | `case_restrictions` | Applicable effective open holds load directly |
| Work and progress | Tasks, case coordination, check-ins and series tables | Current task/case state loads directly |
| Original evidence and changes | `sources` and `activity_events` | New relevant events/sources and explicit evidence reads |
| Searchable history | `memory_passages`, `memory_summaries`, query embeddings | Selected search results and a small source-linked digest |

Scopes overlap: firm rules, provider instructions within that firm, case holds and
task-specific responsibilities. There is no blanket "most specific wins" rule.
Task parent/child structure is not automatic inheritance of every memory item.
A provider identity never authorizes reading another firm's case.

## What loads automatically

When an activation starts, application code prepares current work state, applicable
instructions, effective open restrictions/obligations, new events, and supporting
history. Specialist context concerns its task; the supervisor gathers case work.

Multiple model calls inside that activation use the saved starting snapshot, packed
to fit each call. The system does not reread the entire database before every model
call. It rechecks current versions/memory before specialist actions and commits;
the supervisor checks its plan/task/memory state before committing proposals.
Changes can invalidate a decision and require a fresh activation.

An old restriction remains important until explicitly resolved or replaced.
Monthly coordination also reads current contact history directly: scheduled/pending
tasks, actual sends and attributed replies. Those checks are not dependent on vector
search; a recent manual check-in can suppress a due calendar slot. See
[monthly check-ins](monthly-check-ins.md).

Obsolete instructions can be retired. These operations and their audit history are
explained in [memory lifecycle](memory-lifecycle.md).

## Ingestion: making incoming information searchable

1. Intake code saves a reply, update or other source with firm/case identity.
2. A durable background worker reads saved sources in batches.
3. It serializes source JSON and cuts it into 1,600-character passages with
   200 characters of overlap. Each passage retains its original source reference/hash.
4. Keyword search can use those passages. When enabled, embeddings add meaning search.
5. A small digest keeps excerpts from the eight latest sources, with links and coverage.

The digest is not a complete AI-written case summary. Chunk boundaries are simple
text cuts, not document sections. The worker normally polls every 15 seconds and
speeds up for a backlog. Embedding failures leave keyword search working and retry
with backoff. Exact source lookup works before indexing.

Embeddings use `text-embedding-3-small`, 1,536 dimensions, stored through `pgvector`.
Scripted mode does not invent embeddings. Changed sources invalidate stale derived
passages/digests until rebuilt.

## The two agent tools

**`retrieve_context` finds relevant passages.** The agent supplies a question and
optional provider/recording-date filters. Execution code supplies the trusted firm
and case IDs. The service validates access, then searches by exact source ID/key,
PostgreSQL full-text matching and, when embeddings exist, cosine-distance similarity.
It combines rankings using reciprocal rank fusion, removes duplicate text within
a source, and returns up to six passages from at most 12 candidates within budget.
There is no learned reranker. Vector ranking is exact over the authorized subset.

**`read_evidence` opens an original source.** The agent supplies its source ID.
The service checks case access and returns a bounded portion of the saved JSON.
`next_offset` continues long content; `expected_hash` prevents continuing across a
changed source. Offsets are characters in serialized JSON, capped at one million.

Results include source IDs, hashes, recording times and linked events. Historical
memory sources also carry their current lifecycle state. Search reports incomplete
indexing/truncation; an empty result does not prove an event never happened.
Arguments/results are saved in the tool ledger, and identical requests in an
execution reuse the saved response. The default limit is four calls per activation.

Date filters use **when a source was recorded**, not when a symptom started or a
fact became true. Automatic reconciliation of contradictory historical claims is
still unbuilt. Current lifecycle metadata does not change those search semantics.

## Who decides what matters?

Application rules select mandatory current controls. Search ranking selects
historical passages that match a question. The agent interprets the supplied
information and can ask for more. A relevance score cannot lift a restriction,
approve an action or prove the work is complete.

## Keeping the briefing small

Defaults are 12,000 estimated input tokens per call, 1,500 output tokens and four
retrieval/read calls per activation. Each context response defaults to 1,800 tokens
and can request 256–2,500. The budget includes instructions, schemas and messages.

Packing removes optional notes/history and shortens supporting sources first.
Older evidence bodies can become references that the agent can reopen. Current
hard controls, essential state, unread events and domain-action results are not
silently discarded. If they cannot fit, the system asks for operator review.
Full starting snapshots and saved tool results remain available for inspection.
These are per-call limits, not a total run-cost budget.

For routes, fields and code locations, see [development](development.md) and the
[generated API reference](http://localhost:8000/docs).
