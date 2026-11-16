# Final demo walkthrough

Open [Interview live](http://localhost:8000/?firm=c51532c8-5015-53f6-81c3-c1c9e6f646c4), refresh, and use **CASE-101**.
All outreach is simulated. Wait for each agent update before the next step.

## 1. Medical records agent

**Add task** → paste:

> Collect all general medical records and surgical notes for Jordan Lee from Example Hospital, for all dates. Tell me about missing items or anything that needs my help.

**Review supervisor plan → Approve & start**. Open the records child using **Subtasks**. Wait for **Waiting**.

**Simulate call** → speaker **Example Hospital** → paste:

> Please call us back in three days.

**Save simulated call**. Show the summary and follow-up time. No review is needed.

**Simulate provider response** → paste:

> General medical records are attached. Surgical notes are missing. Please provide an updated authorization.

Attach **01-general-records-40-pages.pdf** → **Save response**.
Wait for **Records received**, **Surgical notes outstanding**, and the authorization review.

In that review:

1. Upload **02-updated-authorization.txt**.
2. Check **I reviewed this document…**.
3. Set the response deadline **four days from today, at 09:00**.
4. Reason: **Reviewed this fictional authorization. Continue for the missing surgical notes.**
5. **Record decision**. Wait for **Waiting**.

**Simulate provider response** again → paste:

> The surgical notes are attached. General records were supplied earlier.

Attach **03-surgical-notes-2-pages.pdf** → **Save response**.
Wait for **Completed** with both items received. You do not need to wait three days to upload.

## 2. Client check-in agent

**Add task** → same case → paste:

> Check in with Jordan Lee in two minutes. Ask how they feel and whether they need help. Report meaningful changes. One check-in only.

**Review supervisor plan → Approve & start**. Open the client check-in task, directly or through **Subtasks**.
Wait about **two minutes** for **Sent**.

**Simulate client reply**:

- Message: **My pain is worse. Please ask someone from the firm to call me.**
- Pain: **7**. Daily function: **Worse**.
- Check **Client requests a teammate's help**.

**Save simulated reply**. Wait for review.
Choose **I have handled this report**.
Reason: **Reviewed and arranged teammate follow-up**.
**Record decision** → wait for **Completed**. Only one check-in is sent.

## 3. Show the platform

On a completed child, open **Executions**:

- **Open LLM trace** → **llm.call → Preview**: input, output, model, tokens.
- **Open execution trace**: context, model, tools, database, result and timing.
- [Temporal](http://localhost:8233): search the task UUID; show timer and signal history.
- **Activity & comments**: show the saved source summary, review decision and completion.
- **Operations**: show pending work and failures.
- **Agents**: explain that a new agent adds a contract and adapter, then the supervisor discovers it.

Langfuse: **admin@hellocounsel.local**; password is `OBS_USER_PASSWORD` in `.env.observability`.
For the audit journal: [API docs](http://localhost:8000/docs) → **Authorize** with `local-development-only` → **GET /v1/firms/{tenant_id}/audit/events**. Firm: `c51532c8-5015-53f6-81c3-c1c9e6f646c4`; filter by task UUID. Show actor, action, outcome and hashes.

## Files

[Open demo files](/Users/admin1/Documents/Codex/2026-11-15/in-x20/outputs/presentation-demo).
The PDFs are fictional test documents. Attach them only at the steps above.
