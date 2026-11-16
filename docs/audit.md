# Consistent audit events

This adds one journal beside the existing business tables. It does not replace tasks,
activity events, tool results, sources or human decisions. Those tables still hold the
full evidence. The journal holds safe references and a common account of what happened.

## Design and sources

| Source | Applied here |
|---|---|
| [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html) | Common actor/action/resource/outcome fields, failed attempts, safe metadata and access restrictions |
| [Schneier and Kelsey, Secure Audit Logs (1999)](https://www.schneier.com/academic/archives/1999/05/secure_audit_logs_to.html) | Treat integrity as a separate problem; add chained hashes and external checkpoints. This implementation does not implement the paper's forward-secure construction. |
| [NIST SP 800-53 Rev. 5, AU-9 and AU-11](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-53r5.pdf) | Protect records and define retention explicitly; no claim of certification or legally required retention period |
| [PostgreSQL privileges](https://www.postgresql.org/docs/17/ddl-priv.html) and [safe definer functions](https://www.postgresql.org/docs/17/sql-createfunction.html) | Separate application and audit-owner roles; fixed function search paths; revoke public execution |

## Common contract

`AuditEvent` in `src/agents_platform/audit.py` defines schema version 1:

- Identity: event ID, occurrence time, tenant/case/task/execution IDs.
- Authority: actor, action, resource, outcome, reason code and policy version.
- Correlation: request, trace/span and tool-attempt IDs.
- Details: small operational metadata or evidence references, never raw tool arguments,
  prompts, reports, credentials or exception messages.

PostgreSQL validates the envelope and adds recording time and `retained_until`.
`audit.events` stores the event, tenant-stream sequence, previous hash and current hash.
There are no foreign keys to business rows, so deleting business data does not delete
its audit references. Events before this migration remain in their original tables;
they are not backfilled with invented authority or attempt history. A migration downgrade
detaches business capture but preserves the journal. Upgrading reattaches it; stop application
processes during migrations. No audit evidence is dropped by downgrade.

| Event | Captured at |
|---|---|
| `authorization.decision` | Every protected API request, specialist backend tool gate, retrieval scope gate and supervisor proposal validation |
| `tool.attempt` / `tool.result` | Shared SDK tool wrapper, including validation failures, cached calls and rejected results |
| `tool.committed` | Database trigger on saved tool invocations |
| `approval.decision` | Human decisions and approved assignment sources |
| `external_effect.result` | Committed simulated contact, human-authorized follow-up and outbox delivery result |
| `external_effect.attempt` | Outbox dispatcher before delivery |
| `execution.result` / `action.result` | Execution transitions, activity exceptions and failed HTTP requests |
| `business.event` | Existing activity events, using safe event IDs rather than copying payloads |

Tools that propose delegation are labelled `PROPOSED`; they are not recorded as having
created tasks. Backend delegation validation has its own authorization decision.
An allowed gate followed by an adapter error remains distinct from a denial.

Successful business records are captured by **deferred database triggers**, in the same
transaction. This covers both ORM and raw SQL writes and takes the audit-chain lock after
business work. Explicit effect authorization also commits with the effect. Tool attempt
and failure records use separate short transactions so business rollback cannot erase them.
A crash may leave an attempt without a result; that means the outcome is unknown. Audit
failure stops a tool before invocation; business audit failure rolls back its transaction.
This journal is durable evidence, unlike best-effort OpenTelemetry export.

## Permissions and local setup

Compose's migration service now installs the schema and provisions `agents_app`. API and
worker use that non-superuser login. They can execute `audit.append_event` and read the
journal, but cannot insert directly, update, delete, truncate, disable triggers, assume the
audit-owner role or run retention maintenance. The owner is a separate `NOLOGIN` role.
Definer functions use a fixed search path and have no public execution grant.

For host development, after `alembic upgrade head`:

```sh
python scripts/setup_audit.py
```

This saves a generated application password in `.env`, sets `DATABASE_URL` to the
restricted login, and preserves the owner URL as `MIGRATION_DATABASE_URL`. Alembic uses
the migration URL unless `DATABASE_URL` is explicitly supplied in the process environment.
Re-running setup preserves the password. Compose also supports a development-only fallback
password for a fresh stack; generated credentials are preferable.

The existing shared operator key still grants all-firm access. These database permissions
protect audit integrity; they do not introduce per-user or per-firm authorization.

## Read, verify and export

Use Swagger's protected `GET /v1/firms/{tenant_id}/audit/events`. It supports `task_id`,
`case_id`, `after` and `limit` (maximum 500). Responses include hashes and sequence numbers.
The sequence is the database append order, not the original event occurrence order.

```sh
python scripts/audit_journal.py TENANT_UUID
python scripts/audit_journal.py TENANT_UUID --export work/audit-export.jsonl
python scripts/audit_journal.py TENANT_UUID --checkpoint work/audit-export.jsonl.checkpoint.json
```

Export refuses to overwrite existing files. It saves each event's PostgreSQL JSON text
and a checkpoint. Verification uses a consistent database snapshot, checks every link and
the stored head, and can compare an earlier checkpoint. Copy the export and checkpoint to
storage controlled separately from the application and database administrator.

## Retention and limits

The demo default is **365 days**, chosen for development rather than a legal requirement.
A separate `agents_audit_maintainer` role can call:

```sql
SELECT audit.set_retention('TENANT_UUID', 365, true); -- legal hold on
SELECT audit.set_retention('TENANT_UUID', 365, false);
-- After verified export and expiry, supply the archived prefix's sequence/hash:
SELECT audit.prune('TENANT_UUID', ARCHIVED_SEQUENCE, 'ARCHIVED_HASH');
```

Changing the policy only affects new events; it cannot shorten existing events' saved
retention deadlines. A legal hold blocks pruning. Pruning only removes an expired prefix
whose final hash matches the supplied checkpoint, preserves that hash as an anchor, and
records the maintenance action. No automatic deletion job is enabled. The maintenance
role must be granted separately by an administrator; the API and worker do not have it.

A database superuser can still bypass protections and rewrite the whole chain. A checkpoint
kept on the same host is not independent protection. Signed checkpoints, automatic off-host
archival, WORM storage, production identities and archival restore drills remain future work.
Real external providers will also need durable send intents and receipt/reconciliation
records; current contact tools are simulated, while Temporal delivery is real.

The per-tenant chain serializes audit appends. It is suitable for this slice; sustained high
volume would need measurement and likely smaller streams or an independent audit sink.
