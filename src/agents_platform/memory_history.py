"""Derived history: bounded passages, extractive digests, exact/FTS/vector retrieval.

Raw Sources are authoritative. Derived rows carry source hashes; retrieval rejects
stale rows. Embeddings happen outside transactions and never gate mandatory memory.
"""

import hashlib
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from openai import AsyncOpenAI
from sqlalchemy import String, cast, delete, exists, func, literal_column, or_, select
from sqlalchemy.dialects.postgresql import insert
from temporalio import activity

from agents_platform.models import (
    ActivityEvent,
    Case,
    CaseCoordination,
    MemoryPassage,
    MemoryQuery,
    MemorySummary,
    RecordEvidence,
    RecordRequirement,
    RecordsRequest,
    Source,
    Task,
)

PROFILE = "text-embedding-3-small:1536:v1"
LOG = logging.getLogger(__name__)
ENGLISH = literal_column("'english'::regconfig")


def query_hash(query):
    return hashlib.sha256((PROFILE + query).encode()).hexdigest()


def source_hash():
    return func.encode(func.sha256(func.convert_to(cast(Source.content, String), "UTF8")), "hex")


def passages(content):
    # Boundaries and overlap are explicit, reproducible and small enough for the demo.
    text = json.dumps(content, ensure_ascii=False, sort_keys=True)
    return [text[i : i + 1600] for i in range(0, len(text), 1400)]


async def embed(texts, settings):
    if settings.agent_mode != "openai" or not os.getenv("OPENAI_API_KEY"):
        return None, 0
    async with AsyncOpenAI(timeout=20, max_retries=0) as client:
        result = await client.embeddings.create(
            model="text-embedding-3-small",
            dimensions=1536,
            input=texts,
        )
    return [
        d.embedding for d in sorted(result.data, key=lambda d: d.index)
    ], result.usage.total_tokens


async def cached_query(session, tenant_id, case_id, query):
    return await session.scalar(
        select(MemoryQuery).where(
            MemoryQuery.tenant_id == tenant_id,
            MemoryQuery.case_id == case_id,
            MemoryQuery.query_hash == query_hash(query),
        )
    )


async def prepare_query(database, settings, tenant_id, case_id, query):
    """Caller authorizes case before requesting an embedding. No DB lock spans the API."""
    async with database.sessions() as session:
        if await cached_query(session, tenant_id, case_id, query):
            return True
    try:
        vectors, _ = await embed([query], settings)
    except Exception as exc:
        LOG.warning("Query embedding unavailable (%s); using keyword search", type(exc).__name__)
        return False
    if vectors:
        async with database.sessions.begin() as session:
            await session.execute(
                insert(MemoryQuery)
                .values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    case_id=case_id,
                    text=query,
                    query_hash=query_hash(query),
                    embedding=vectors[0],
                )
                .on_conflict_do_nothing()
            )
    return bool(vectors)


def history_filters(
    tenant_id, case_id, provider_id=None, recorded_after=None, recorded_before=None
):
    """Provider links come from saved events/requests, never names or model guesses.

    Date bounds concern source recording time, not treatment or fact-validity time.
    """
    filters = [Source.tenant_id == tenant_id, Source.case_id == case_id]
    if recorded_after:
        filters.append(Source.created_at >= recorded_after)
    if recorded_before:
        filters.append(Source.created_at < recorded_before)
    if provider_id:
        request_scope = [
            RecordsRequest.tenant_id == tenant_id,
            RecordsRequest.case_id == case_id,
            RecordsRequest.provider_contact_id == provider_id,
        ]
        event_link = exists(
            select(ActivityEvent.id)
            .join(
                RecordsRequest,
                (RecordsRequest.task_id == ActivityEvent.task_id)
                & (RecordsRequest.tenant_id == ActivityEvent.tenant_id),
            )
            .where(
                *request_scope,
                ActivityEvent.tenant_id == tenant_id,
                ActivityEvent.case_id == case_id,
                ActivityEvent.payload["source_id"].astext == cast(Source.id, String),
            )
        )
        evidence_link = exists(
            select(RecordEvidence.id)
            .join(RecordRequirement, RecordRequirement.id == RecordEvidence.requirement_id)
            .join(RecordsRequest, RecordsRequest.id == RecordRequirement.request_id)
            .where(
                *request_scope,
                RecordEvidence.tenant_id == tenant_id,
                RecordEvidence.case_id == case_id,
                RecordEvidence.source_id == Source.id,
            )
        )
        # Tool sources have a backend-generated task path, even before a task event exists.
        tool_link = exists(
            select(RecordsRequest.id).where(
                *request_scope,
                Source.source_key.startswith(
                    "tool/" + cast(RecordsRequest.task_id, String) + "/", autoescape=False
                ),
            )
        )
        filters.append(or_(event_link, evidence_link, tool_link))
    return filters


async def search_history(
    session,
    tenant_id,
    case_id,
    query,
    limit=6,
    *,
    broad=False,
    provider_id=None,
    recorded_after=None,
    recorded_before=None,
):
    """Scope is enforced in every SQL lane, including exact identifier lookups."""
    filters = history_filters(tenant_id, case_id, provider_id, recorded_after, recorded_before)
    exact_source = (
        await session.execute(
            select(Source, source_hash())
            .where(
                *filters,
                or_(cast(Source.id, String) == query, Source.source_key == query),
            )
            .limit(1)
        )
    ).first()
    if exact_source:
        source, digest = exact_source
        return {
            "query": query,
            "profile": PROFILE,
            "method": "exact",
            "results": [
                {
                    "source_id": str(source.id),
                    "passage_id": None,
                    "source_hash": digest,
                    "kind": source.kind,
                    "recorded_at": source.created_at.isoformat(),
                    "text": json.dumps(source.content, ensure_ascii=False, sort_keys=True)[:1600],
                    "lanes": ["exact"],
                    "score": 1.0,
                }
            ],
        }
    scoped = (
        select(MemoryPassage, Source)
        .join(
            Source,
            (Source.id == MemoryPassage.source_id)
            & (Source.tenant_id == MemoryPassage.tenant_id)
            & (Source.case_id == MemoryPassage.case_id),
        )
        .where(
            MemoryPassage.tenant_id == tenant_id,
            MemoryPassage.case_id == case_id,
            MemoryPassage.source_hash == source_hash(),
            *filters,
        )
    )
    tsv = func.to_tsvector(ENGLISH, MemoryPassage.text)
    lexical_query = " OR ".join(dict.fromkeys(re.findall(r"\w+", query)[:30])) if broad else query
    tsquery = func.websearch_to_tsquery(ENGLISH, lexical_query or query)
    lexical = (
        await session.execute(
            scoped.where(tsv.op("@@")(tsquery))
            .order_by(func.ts_rank_cd(tsv, tsquery).desc(), MemoryPassage.id)
            .limit(30)
        )
    ).all()
    exact = (
        await session.execute(
            scoped.where(or_(cast(Source.id, String) == query, Source.source_key == query))
            .order_by(MemoryPassage.position)
            .limit(10)
        )
    ).all()
    cached = await cached_query(session, tenant_id, case_id, query)
    semantic = []
    if cached:
        # Exact nearest-neighbor over the authorized case subset; no global ANN filter loss.
        semantic = (
            await session.execute(
                scoped.where(MemoryPassage.embedding.is_not(None))
                .order_by(
                    MemoryPassage.embedding.cosine_distance(cached.embedding), MemoryPassage.id
                )
                .limit(30)
            )
        ).all()
    scores, rows, lanes = defaultdict(float), {}, defaultdict(list)
    for name, hits in (("exact", exact), ("keyword", lexical), ("vector", semantic)):
        for rank, (passage, source) in enumerate(hits, 1):
            scores[passage.id] += (10 if name == "exact" else 1) / (60 + rank)
            rows[passage.id] = (passage, source)
            lanes[passage.id].append(name)
    ranked = sorted(scores, key=lambda k: (-scores[k], str(k)))
    if semantic:
        # Many weak keyword+vector hits can otherwise crowd out the best semantic-only
        # answer. Keep that candidate visible even under a small context/tool budget.
        anchor = semantic[0][0].id
        ranked = [anchor] + [key for key in ranked if key != anchor]
    results = []
    for key in ranked[:limit]:
        passage, source = rows[key]
        results.append(
            {
                "source_id": str(source.id),
                "passage_id": str(passage.id),
                "source_hash": passage.source_hash,
                "kind": source.kind,
                "recorded_at": source.created_at.isoformat(),
                "text": passage.text,
                "lanes": lanes[key],
                "score": scores[key],
            }
        )
    return {
        "query": query,
        "profile": PROFILE,
        "method": "hybrid-rrf" if cached and semantic else "exact+keyword",
        "selection": "semantic anchor then rank fusion" if semantic else "keyword rank",
        "results": results,
    }


async def latest_summary(session, tenant_id, case_id):
    row = await session.scalar(
        select(MemorySummary)
        .where(MemorySummary.tenant_id == tenant_id, MemorySummary.case_id == case_id)
        .order_by(MemorySummary.revision.desc())
        .limit(1)
    )
    if not row:
        return None
    references = row.content["entries"]
    current = dict(
        (
            await session.execute(
                select(Source.id, source_hash()).where(
                    Source.tenant_id == tenant_id,
                    Source.case_id == case_id,
                    Source.id.in_([UUID(i["source_id"]) for i in references]),
                )
            )
        ).all()
    )
    if any(current.get(UUID(i["source_id"])) != i["source_hash"] for i in references):
        return None  # Never serve a digest with stale/missing evidence.
    return {
        "id": str(row.id),
        "revision": row.revision,
        "created_at": row.created_at.isoformat(),
        **row.content,
    }


async def historical_context(session, tenant_id, case_id, query):
    return {
        "authority": "Historical evidence only; never permission, policy or proof of completion.",
        "retrieval": await search_history(session, tenant_id, case_id, query, broad=True),
        "summary": await latest_summary(session, tenant_id, case_id),
    }


async def refresh_summary(session, tenant_id, case_id):
    # Serialize revisions for this case; independent of agent/business state revisions.
    await session.scalar(
        select(Case)
        .where(Case.tenant_id == tenant_id, Case.id == case_id)
        .with_for_update(key_share=True)
    )
    rows = (
        await session.execute(
            select(Source, source_hash())
            .where(Source.tenant_id == tenant_id, Source.case_id == case_id)
            .order_by(Source.created_at.desc(), Source.id.desc())
            .limit(8)
        )
    ).all()
    total = await session.scalar(
        select(func.count())
        .select_from(Source)
        .where(Source.tenant_id == tenant_id, Source.case_id == case_id)
    )
    content = {
        "kind": "EXTRACTIVE",
        "source_count": total,
        "covered_recent_sources": len(rows),
        "is_complete_history": total <= 8,
        "entries": [
            {
                "source_id": str(s.id),
                "source_hash": h,
                "kind": s.kind,
                "recorded_at": s.created_at.isoformat(),
                "excerpt": json.dumps(s.content, ensure_ascii=False, sort_keys=True)[:280],
            }
            for s, h in reversed(rows)
        ],
    }
    digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
    previous = await session.scalar(
        select(MemorySummary)
        .where(MemorySummary.tenant_id == tenant_id, MemorySummary.case_id == case_id)
        .order_by(MemorySummary.revision.desc())
        .limit(1)
    )
    if not previous or previous.content_hash != digest:
        session.add(
            MemorySummary(
                tenant_id=tenant_id,
                case_id=case_id,
                revision=previous.revision + 1 if previous else 1,
                content_hash=digest,
                content=content,
            )
        )


class MemoryActivities:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings

    @activity.defn
    async def index_memory(self):
        """Restart-safe indexing/lifecycle maintenance; never repeats a business tool."""
        from agents_platform.memory_lifecycle import advance_lifecycles

        lifecycle_transitions = await advance_lifecycles(self.database)
        scopes = set()
        async with self.database.sessions.begin() as session:
            rows = (
                await session.execute(
                    select(Source, source_hash())
                    .where(
                        ~exists(
                            select(MemoryPassage.id).where(
                                MemoryPassage.source_id == Source.id,
                                MemoryPassage.tenant_id == Source.tenant_id,
                                MemoryPassage.source_hash == source_hash(),
                            )
                        )
                    )
                    .order_by(Source.created_at, Source.id)
                    .limit(30)
                )
            ).all()
            for source, digest in rows:
                scopes.add((source.tenant_id, source.case_id))
                await session.execute(
                    delete(MemoryPassage).where(
                        MemoryPassage.tenant_id == source.tenant_id,
                        MemoryPassage.source_id == source.id,
                    )
                )
                for position, passage in enumerate(passages(source.content)):
                    session.add(
                        MemoryPassage(
                            tenant_id=source.tenant_id,
                            case_id=source.case_id,
                            source_id=source.id,
                            source_hash=digest,
                            position=position,
                            text=passage,
                        )
                    )
            await session.flush()
            for tenant, case in sorted(scopes, key=lambda s: str(s)):
                await refresh_summary(session, tenant, case)
        # Indexing lexical passages and summaries commits before any external request.
        current_time = datetime.now(timezone.utc)
        async with self.database.sessions() as session:
            pending = (
                await session.scalars(
                    select(MemoryPassage)
                    .where(
                        MemoryPassage.embedding.is_(None),
                        or_(
                            MemoryPassage.embedding_retry_at.is_(None),
                            MemoryPassage.embedding_retry_at <= current_time,
                        ),
                    )
                    .order_by(MemoryPassage.created_at, MemoryPassage.id)
                    .limit(30)
                )
            ).all()
        tokens, embedded, error = 0, 0, None
        if pending:
            try:
                vectors, tokens = await embed([p.text for p in pending], self.settings)
            except Exception as exc:
                vectors, error = None, type(exc).__name__
                LOG.warning("Memory embeddings unavailable (%s)", error)
            async with self.database.sessions.begin() as session:
                for i, old in enumerate(pending):
                    row = await session.get(MemoryPassage, old.id, with_for_update=True)
                    if (
                        row is None
                        or row.source_hash != old.source_hash
                        or row.embedding is not None
                    ):
                        continue
                    if vectors:
                        row.embedding, row.embedding_error = vectors[i], None
                        embedded += 1
                    else:
                        row.embedding_attempts += 1
                        row.embedding_error = error or "EmbeddingsDisabled"
                        row.embedding_retry_at = current_time + timedelta(
                            seconds=min(3600, 10 * 2 ** min(row.embedding_attempts, 8))
                        )
        # Cache objective embeddings for hybrid activation reads without network calls.
        if self.settings.agent_mode == "openai":
            async with self.database.sessions() as session:
                tasks = (
                    await session.scalars(
                        select(Task)
                        .where(
                            Task.kind == "AGENT",
                            ~exists(
                                select(MemoryQuery.id).where(
                                    MemoryQuery.tenant_id == Task.tenant_id,
                                    MemoryQuery.case_id == Task.case_id,
                                    MemoryQuery.text == Task.objective,
                                )
                            ),
                        )
                        .order_by(Task.created_at)
                        .limit(5)
                    )
                ).all()
                coords = (
                    await session.scalars(
                        select(CaseCoordination)
                        .where(
                            ~exists(
                                select(MemoryQuery.id).where(
                                    MemoryQuery.tenant_id == CaseCoordination.tenant_id,
                                    MemoryQuery.case_id == CaseCoordination.case_id,
                                    MemoryQuery.text
                                    == CaseCoordination.objective_json["objective"].astext,
                                )
                            )
                        )
                        .order_by(CaseCoordination.created_at)
                        .limit(5)
                    )
                ).all()
            for task in tasks:
                if not await prepare_query(
                    self.database, self.settings, task.tenant_id, task.case_id, task.objective
                ):
                    break
            for coord in coords:
                query = coord.objective_json.get("objective")
                if query:
                    if not await prepare_query(
                        self.database, self.settings, coord.tenant_id, coord.case_id, query
                    ):
                        break
        return {
            "lifecycle_transitions": lifecycle_transitions,
            "indexed_sources": len(rows),
            "embedded_passages": embedded,
            "embedding_tokens": tokens,
            "embedding_error": error,
            "profile": PROFILE,
        }
