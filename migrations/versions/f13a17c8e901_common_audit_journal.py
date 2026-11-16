"""Common append-only audit journal; existing business tables remain authoritative."""

# ruff: noqa: E501 -- readable PostgreSQL statements
import sqlalchemy as sa
from alembic import op

revision = "f13a17c8e901"
down_revision = "6e8b9252f1d6"
branch_labels = None
depends_on = None


def upgrade():
    storage, capture = SQL.split("-- Database triggers cover", 1)
    if op.get_bind().scalar(sa.text("SELECT to_regclass('audit.events')")) is None:
        op.execute(storage)
    # A downgrade retains the journal. Reattach capture after business tables return.
    op.execute("-- Database triggers cover" + capture)


def downgrade():
    # Detach capture so older table layouts can migrate, but retain all audit evidence.
    for table, trigger in (
        ("activity_events", "audit_business"),
        ("human_decisions", "audit_decisions"),
        ("tool_invocations", "audit_tools"),
        ("executions", "audit_executions"),
        ("outbox_messages", "audit_effects"),
        ("sources", "audit_approvals"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON public.{table}")
    op.execute("DROP FUNCTION IF EXISTS audit.capture_business()")


SQL = r"""
CREATE EXTENSION IF NOT EXISTS pgcrypto;
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='agents_audit_owner') THEN
    CREATE ROLE agents_audit_owner NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='agents_audit_writer') THEN
    CREATE ROLE agents_audit_writer NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='agents_audit_reader') THEN
    CREATE ROLE agents_audit_reader NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='agents_audit_maintainer') THEN
    CREATE ROLE agents_audit_maintainer NOLOGIN;
  END IF;
END $$;
CREATE SCHEMA audit AUTHORIZATION agents_audit_owner;
REVOKE ALL ON SCHEMA audit FROM PUBLIC;
CREATE TABLE audit.heads (
  stream_id uuid PRIMARY KEY,
  last_sequence bigint NOT NULL DEFAULT 0,
  last_hash text NOT NULL DEFAULT repeat('0',64),
  anchor_sequence bigint NOT NULL DEFAULT 0,
  anchor_hash text NOT NULL DEFAULT repeat('0',64),
  retention_days integer NOT NULL DEFAULT 365 CHECK(retention_days BETWEEN 1 AND 36500),
  legal_hold boolean NOT NULL DEFAULT false
);
CREATE TABLE audit.events (
  stream_id uuid NOT NULL,
  sequence bigint NOT NULL,
  event_id uuid NOT NULL UNIQUE,
  event jsonb NOT NULL,
  previous_hash text NOT NULL,
  event_hash text NOT NULL,
  PRIMARY KEY(stream_id, sequence)
);
CREATE INDEX audit_events_task ON audit.events ((event->>'tenant_id'), (event->>'task_id'),sequence);
CREATE INDEX audit_events_type ON audit.events ((event->>'event_type'), (event->>'recorded_at'));
ALTER TABLE audit.heads OWNER TO agents_audit_owner;
ALTER TABLE audit.events OWNER TO agents_audit_owner;

CREATE FUNCTION audit.append_event(input jsonb) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, audit AS $$
DECLARE
  stream uuid := COALESCE((input->>'tenant_id')::uuid,'00000000-0000-0000-0000-000000000000');
  identity uuid := COALESCE((input->>'id')::uuid, gen_random_uuid());
  h audit.heads;
  e jsonb;
  hash text;
BEGIN
  IF input->>'schema_version' IS DISTINCT FROM '1'
     OR input->>'outcome' IS NULL
     OR input->>'outcome' NOT IN ('ATTEMPTED','ALLOWED','DENIED','SUCCEEDED','FAILED','REJECTED','CACHED','PROPOSED')
     OR COALESCE(length(input->>'event_type'),0) NOT BETWEEN 1 AND 100
     OR COALESCE(length(input->>'action'),0) NOT BETWEEN 1 AND 200
     OR COALESCE(length(input->>'actor_ref'),0) NOT BETWEEN 1 AND 200
     OR COALESCE(length(input->>'reason_code'),0) NOT BETWEEN 1 AND 100
     OR jsonb_typeof(input->'details') IS DISTINCT FROM 'object'
     OR octet_length(input::text)>16384 THEN
    RAISE EXCEPTION 'Invalid audit event contract' USING ERRCODE='22023';
  END IF;
  -- Strict envelope: arbitrary payloads must stay in their existing evidence tables.
  IF EXISTS (SELECT FROM jsonb_object_keys(input) k WHERE k NOT IN
    ('id','schema_version','event_type','occurred_at','tenant_id','case_id','task_id','execution_id',
     'actor_ref','action','resource_type','resource_id','outcome','reason_code','policy_version',
     'request_id','trace_id','span_id','attempt_id','details')) THEN
    RAISE EXCEPTION 'Unknown audit event field' USING ERRCODE='22023';
  END IF;
  INSERT INTO audit.heads(stream_id) VALUES(stream) ON CONFLICT DO NOTHING;
  SELECT * INTO h FROM audit.heads WHERE stream_id=stream FOR UPDATE;
  IF EXISTS(SELECT FROM audit.events WHERE event_id=identity) THEN
    IF EXISTS(SELECT FROM audit.events WHERE event_id=identity
              AND event @> (input || jsonb_build_object('id',identity))) THEN
      RETURN identity;
    END IF;
    RAISE EXCEPTION 'Audit event ID reused with different data' USING ERRCODE='22023';
  END IF;
  e := input || jsonb_build_object('id',identity,'recorded_at',clock_timestamp(),
    'occurred_at',COALESCE(input->>'occurred_at',clock_timestamp()::text),
    'retained_until',clock_timestamp()+make_interval(days=>h.retention_days));
  hash := encode(public.digest(convert_to(h.last_hash || E'\n' || e::text,'UTF8'),'sha256'),'hex');
  INSERT INTO audit.events VALUES(stream,h.last_sequence+1,identity,e,h.last_hash,hash);
  UPDATE audit.heads SET last_sequence=h.last_sequence+1,last_hash=hash WHERE stream_id=stream;
  RETURN identity;
END $$;
ALTER FUNCTION audit.append_event(jsonb) OWNER TO agents_audit_owner;
REVOKE ALL ON FUNCTION audit.append_event(jsonb) FROM PUBLIC;
GRANT USAGE ON SCHEMA audit TO agents_audit_writer,agents_audit_reader,agents_audit_maintainer;
GRANT EXECUTE ON FUNCTION audit.append_event(jsonb) TO agents_audit_writer;
GRANT SELECT ON audit.events,audit.heads TO agents_audit_reader;

-- Defense in depth against accidental grants. Only the isolated owner functions may mutate.
CREATE FUNCTION audit.prevent_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
 IF current_user <> 'agents_audit_owner' THEN
   RAISE EXCEPTION 'Audit records are append-only' USING ERRCODE='42501';
 END IF;
 IF TG_OP='UPDATE' THEN RETURN NEW; END IF;
 RETURN OLD;
END $$;
CREATE TRIGGER audit_no_changes BEFORE UPDATE OR DELETE ON audit.events
 FOR EACH ROW EXECUTE FUNCTION audit.prevent_mutation();
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit.events
 FOR EACH STATEMENT EXECUTE FUNCTION audit.prevent_mutation();
REVOKE ALL ON FUNCTION audit.prevent_mutation() FROM PUBLIC;

-- Maintenance requires a separate role, never granted to the API/worker.
CREATE FUNCTION audit.set_retention(stream uuid, days integer, hold boolean) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,audit AS $$ BEGIN
 IF days NOT BETWEEN 1 AND 36500 OR hold IS NULL THEN RAISE EXCEPTION 'Invalid retention policy'; END IF;
 INSERT INTO audit.heads(stream_id) VALUES(stream) ON CONFLICT DO NOTHING;
 UPDATE audit.heads SET retention_days=days,legal_hold=hold WHERE stream_id=stream;
 PERFORM audit.append_event(jsonb_build_object('schema_version',1,'tenant_id',stream,
   'event_type','retention.changed','actor_ref',session_user,'action','set_retention',
   'outcome','SUCCEEDED','reason_code','POLICY_UPDATED','details',jsonb_build_object('retention_days',days,'legal_hold',hold)));
END $$;
CREATE FUNCTION audit.prune(stream uuid, through_sequence bigint, expected_hash text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,audit AS $$
DECLARE h audit.heads; actual text; removed bigint;
BEGIN
 SELECT * INTO h FROM audit.heads WHERE stream_id=stream FOR UPDATE;
 IF NOT FOUND OR h.legal_hold THEN RAISE EXCEPTION 'Audit stream missing or on legal hold'; END IF;
 SELECT event_hash INTO actual FROM audit.events WHERE stream_id=stream AND sequence=through_sequence;
 IF actual IS NULL OR actual<>expected_hash OR through_sequence<=h.anchor_sequence THEN
   RAISE EXCEPTION 'Archive checkpoint does not match';
 END IF;
 IF EXISTS(SELECT FROM audit.events WHERE stream_id=stream AND sequence<=through_sequence
           AND (event->>'retained_until')::timestamptz>clock_timestamp()) THEN
   RAISE EXCEPTION 'Audit retention period has not ended';
 END IF;
 DELETE FROM audit.events WHERE stream_id=stream AND sequence<=through_sequence;
 GET DIAGNOSTICS removed = ROW_COUNT;
 UPDATE audit.heads SET anchor_sequence=through_sequence,anchor_hash=actual WHERE stream_id=stream;
 PERFORM audit.append_event(jsonb_build_object('schema_version',1,'tenant_id',stream,
   'event_type','retention.pruned','actor_ref',session_user,'action','prune',
   'outcome','SUCCEEDED','reason_code','EXPIRED_PREFIX','details',jsonb_build_object(
     'anchor_sequence',through_sequence,'anchor_hash',actual,'removed',removed)));
 RETURN removed;
END $$;
ALTER FUNCTION audit.set_retention(uuid,integer,boolean) OWNER TO agents_audit_owner;
ALTER FUNCTION audit.prune(uuid,bigint,text) OWNER TO agents_audit_owner;
REVOKE ALL ON FUNCTION audit.set_retention(uuid,integer,boolean),audit.prune(uuid,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION audit.set_retention(uuid,integer,boolean),audit.prune(uuid,bigint,text) TO agents_audit_maintainer;

-- Database triggers cover ORM and Core SQL alike, atomically with committed work.
CREATE FUNCTION audit.capture_business() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,audit AS $$
DECLARE
 r jsonb:=to_jsonb(NEW); ctx jsonb:=COALESCE(NULLIF(current_setting('app.audit_context',true),''),'{}')::jsonb;
 typ text; action text; outcome text:='SUCCEEDED'; reason text:='COMMITTED'; d jsonb:='{}';
 task text:=r->>'task_id'; caseid text:=r->>'case_id'; actor text:=COALESCE(ctx->>'actor_ref','platform');
BEGIN
 IF TG_TABLE_NAME='activity_events' THEN
   typ:=CASE WHEN r->>'event_type'='records.human_follow_up_sent'
     THEN 'external_effect.result' ELSE 'business.event' END;
   action:=r->>'event_type'; actor:=r->>'actor_ref';
   d:=jsonb_build_object('event_id',r->>'id','producer',r->>'producer');
 ELSIF TG_TABLE_NAME='human_decisions' THEN
   typ:='approval.decision'; action:=r->>'action'; actor:=r->>'actor';
   SELECT task_id::text INTO task FROM public.human_requests WHERE id=(r->>'request_id')::uuid;
   d:=jsonb_build_object('request_id',r->>'request_id','source_id',r->>'source_id',
      'reviewed_fingerprint',r->>'reviewed_fingerprint');
 ELSIF TG_TABLE_NAME='tool_invocations' THEN
   typ:='tool.committed'; action:=r->>'tool_name';
   d:=jsonb_build_object('invocation_id',r->>'id');
 ELSIF TG_TABLE_NAME='executions' THEN
   IF TG_OP='UPDATE' AND NEW.status=OLD.status AND NEW.attempt=OLD.attempt THEN RETURN NEW; END IF;
   typ:='execution.result'; action:='execute';
   outcome:=CASE WHEN NEW.status='FAILED' THEN 'FAILED' WHEN NEW.status='STALE' THEN 'REJECTED'
                 WHEN NEW.status='RUNNING' THEN 'ATTEMPTED' ELSE 'SUCCEEDED' END;
   d:=jsonb_build_object('status',NEW.status,'attempt',NEW.attempt,'error_type',NEW.error_type);
 ELSIF TG_TABLE_NAME='outbox_messages' THEN
   IF TG_OP='INSERT' OR (NEW.status=OLD.status AND NEW.attempts=OLD.attempts) THEN RETURN NEW; END IF;
   typ:='external_effect.result'; action:=NEW.destination;
   outcome:=CASE WHEN NEW.status='DELIVERED' THEN 'SUCCEEDED' WHEN NEW.status='PENDING' THEN 'FAILED' ELSE 'REJECTED' END;
   SELECT case_id::text,task_id::text INTO caseid,task FROM public.activity_events WHERE id=NEW.event_id;
   d:=jsonb_build_object('outbox_id',NEW.id,'event_id',NEW.event_id,'status',NEW.status,'attempt',NEW.attempts);
 ELSIF TG_TABLE_NAME='sources' THEN
   IF NEW.kind NOT IN ('ASSIGNMENT_APPROVAL','ASSIGNMENT_PLAN_APPROVED')
      AND NEW.source_key NOT LIKE 'assignment-approved/%' THEN RETURN NEW; END IF;
   typ:='approval.decision'; action:='approve_assignment'; actor:='local-operator';
   d:=jsonb_build_object('source_id',NEW.id);
 END IF;
 PERFORM audit.append_event(jsonb_build_object('schema_version',1,'event_type',typ,'action',action,
  'tenant_id',r->>'tenant_id','case_id',caseid,'task_id',task,
  'execution_id',CASE WHEN TG_TABLE_NAME='executions' THEN r->>'id' ELSE r->>'execution_id' END,
  'actor_ref',actor,'resource_type',TG_TABLE_NAME,'resource_id',r->>'id',
  'outcome',outcome,'reason_code',reason,'request_id',ctx->>'request_id','trace_id',ctx->>'trace_id',
  'span_id',ctx->>'span_id','attempt_id',ctx->>'attempt_id','details',d));
 RETURN NEW;
END $$;
ALTER FUNCTION audit.capture_business() OWNER TO agents_audit_owner;
REVOKE ALL ON FUNCTION audit.capture_business() FROM PUBLIC;
GRANT SELECT ON public.human_requests,public.activity_events TO agents_audit_owner;
CREATE CONSTRAINT TRIGGER audit_business AFTER INSERT ON public.activity_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit.capture_business();
CREATE CONSTRAINT TRIGGER audit_decisions AFTER INSERT ON public.human_decisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit.capture_business();
CREATE CONSTRAINT TRIGGER audit_tools AFTER INSERT ON public.tool_invocations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit.capture_business();
CREATE CONSTRAINT TRIGGER audit_executions AFTER INSERT OR UPDATE ON public.executions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit.capture_business();
CREATE CONSTRAINT TRIGGER audit_effects AFTER UPDATE ON public.outbox_messages DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit.capture_business();
CREATE CONSTRAINT TRIGGER audit_approvals AFTER INSERT ON public.sources DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit.capture_business();
"""
