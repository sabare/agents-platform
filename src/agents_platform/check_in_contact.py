"""One contact decision shared by direct, series and monthly check-ins."""

import hashlib
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from agents_platform.models import (
    Case,
    CaseContact,
    ClientCheckIn,
    ClientReport,
    ClientReview,
    MonthlyCheckInOccurrence,
    MonthlyCheckInSchedule,
    Task,
)

TERMINAL = {"COMPLETED", "CANCELLED", "FAILED"}


def now():
    return datetime.now(timezone.utc)


async def contact_lock(session, tenant_id, case_id, client_id):
    identity = f"check-in-contact/{tenant_id}/{case_id}/{client_id}"
    key = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def contact_policy(session, tenant_id, case_id, client_id):
    # A live schedule wins; otherwise retain the latest explicitly configured spacing policy.
    return await session.scalar(
        select(MonthlyCheckInSchedule)
        .where(
            MonthlyCheckInSchedule.tenant_id == tenant_id,
            MonthlyCheckInSchedule.case_id == case_id,
            MonthlyCheckInSchedule.client_contact_id == client_id,
        )
        .order_by(
            MonthlyCheckInSchedule.status.in_(["ACTIVE", "PAUSED"]).desc(),
            MonthlyCheckInSchedule.created_at.desc(),
            MonthlyCheckInSchedule.id.desc(),
        )
        .limit(1)
    )


async def assess_contact(session, tenant_id, case_id, client_id, *, at=None, current_task_id=None):
    at = at or now()
    case = await session.get(Case, case_id)
    if not case or case.tenant_id != tenant_id or case.status != "OPEN":
        return {"code": "CASE_CLOSED", "reason": "The case is closed; do not contact the client."}
    participant = await session.scalar(
        select(CaseContact).where(
            CaseContact.tenant_id == tenant_id,
            CaseContact.case_id == case_id,
            CaseContact.contact_id == client_id,
        )
    )
    if not participant or not {"CLIENT", "PATIENT"}.intersection(participant.roles):
        return {
            "code": "CLIENT_SCOPE",
            "reason": "Client membership or role changed; operator review is required.",
        }
    policy = await contact_policy(session, tenant_id, case_id, client_id)
    horizon = None
    if policy:
        horizon = at.astimezone(ZoneInfo(policy.timezone)).date() + timedelta(
            days=policy.minimum_gap_days
        )
    review = await session.scalar(
        select(ClientReview)
        .join(
            ClientCheckIn,
            (ClientCheckIn.task_id == ClientReview.task_id)
            & (ClientCheckIn.tenant_id == ClientReview.tenant_id),
        )
        .where(
            ClientReview.tenant_id == tenant_id,
            ClientReview.case_id == case_id,
            ClientCheckIn.client_contact_id == client_id,
            ClientReview.status == "OPEN",
        )
        .order_by(ClientReview.created_at, ClientReview.id)
        .limit(1)
    )
    if review and review.task_id != current_task_id:
        return {
            "code": "OPEN_REVIEW",
            "reason": "An earlier client report still requires human review.",
            "covering_task_id": str(review.task_id),
        }
    pending = (
        await session.execute(
            select(ClientCheckIn, Task)
            .join(
                Task,
                (Task.id == ClientCheckIn.task_id) & (Task.tenant_id == ClientCheckIn.tenant_id),
            )
            .where(
                ClientCheckIn.tenant_id == tenant_id,
                ClientCheckIn.case_id == case_id,
                ClientCheckIn.client_contact_id == client_id,
                Task.status.not_in(TERMINAL),
            )
            .order_by(
                ClientCheckIn.monthly_occurrence_id.is_not(None),
                ClientCheckIn.scheduled_at,
                Task.created_at,
                Task.id,
            )
        )
    ).all()
    contenders = []
    for checkin, task in pending:
        if checkin.monthly_occurrence_id and checkin.sent_at is None:
            occurrence = await session.get(MonthlyCheckInOccurrence, checkin.monthly_occurrence_id)
            schedule = (
                await session.get(MonthlyCheckInSchedule, occurrence.schedule_id)
                if occurrence
                else None
            )
            if (
                not occurrence
                or occurrence.expires_at <= now()
                or not schedule
                or schedule.status != "ACTIVE"
            ):
                # Disarmed automation must not prevent an explicit manual assignment.
                continue
        if (
            policy
            and checkin.sent_at is None
            and task.id != current_task_id
            and checkin.scheduled_at.astimezone(ZoneInfo(policy.timezone)).date() >= horizon
        ):
            continue  # A distant future assignment does not cover today's monthly check-in.
        contenders.append((checkin, task))
    pending = contenders
    for checkin, task in pending:
        if task.id == current_task_id:
            continue
        if checkin.sent_at is not None:
            return {
                "code": "PENDING_REPLY",
                "reason": "An existing check-in is awaiting reply, review or completion.",
                "covering_task_id": str(task.id),
            }
    winner = pending[0] if pending else None
    if winner and winner[1].id != current_task_id:
        return {
            "code": "PENDING_TASK",
            "reason": "A check-in is already scheduled; use that task instead of another message.",
            "covering_task_id": str(winner[1].id),
        }
    if not policy:
        return {"code": "ELIGIBLE", "reason": "No overlapping client interaction."}
    scope = (
        ClientCheckIn.tenant_id == tenant_id,
        ClientCheckIn.case_id == case_id,
        ClientCheckIn.client_contact_id == client_id,
    )
    sent_query = select(ClientCheckIn).where(*scope, ClientCheckIn.sent_at.is_not(None))
    if current_task_id:
        sent_query = sent_query.where(ClientCheckIn.task_id != current_task_id)
    sent = await session.scalar(
        sent_query.order_by(ClientCheckIn.sent_at.desc(), ClientCheckIn.id).limit(1)
    )
    report_query = select(ClientReport).where(
        ClientReport.tenant_id == tenant_id,
        ClientReport.case_id == case_id,
        ClientReport.client_contact_id == client_id,
    )
    report = await session.scalar(
        report_query.order_by(ClientReport.reported_at.desc(), ClientReport.id).limit(1)
    )
    interactions = []
    if sent:
        interactions.append((sent.sent_at, str(sent.task_id), "sent check-in"))
    if report:
        report_checkin = await session.get(ClientCheckIn, report.check_in_id)
        interactions.append((report.reported_at, str(report_checkin.task_id), "client reply"))
    if not interactions:
        return {
            "code": "ELIGIBLE",
            "reason": "No previous check-in or reply.",
            "minimum_gap_days": policy.minimum_gap_days,
        }
    last_at, task_id, kind = max(interactions, key=lambda i: i[0])
    zone = ZoneInfo(policy.timezone)
    eligible = datetime.combine(
        last_at.astimezone(zone).date() + timedelta(days=policy.minimum_gap_days), time.min, zone
    ).astimezone(timezone.utc)
    info = {
        "last_interaction_at": last_at.isoformat(),
        "interaction_kind": kind,
        "covering_task_id": task_id,
        "eligible_after": eligible.isoformat(),
        "minimum_gap_days": policy.minimum_gap_days,
        "timezone": policy.timezone,
    }
    if at < eligible:
        return {
            "code": "RECENT_CONTACT",
            "reason": (
                f"A recent {kind} already covers this client. "
                f"Wait at least {policy.minimum_gap_days} "
                f"local calendar days; next eligible date is {eligible.astimezone(zone).date()}."
            ),
            **info,
        }
    return {"code": "ELIGIBLE", "reason": "Contact-spacing requirement satisfied.", **info}
