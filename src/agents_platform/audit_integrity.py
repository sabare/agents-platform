"""Hash-chain verification against an optional independently stored checkpoint."""

import hashlib


def verify(connection, stream, expected=None):
    # Caller must use a repeatable-read transaction for a consistent head + rows.
    head = connection.execute(
        "SELECT last_sequence,last_hash,anchor_sequence,anchor_hash "
        "FROM audit.heads WHERE stream_id=%s",
        (stream,),
    ).fetchone()
    if not head:
        raise ValueError("Audit stream not found")
    sequence, previous = head[2], head[3]
    rows = []
    checkpoint_seen = expected is None or (
        expected["sequence"] == sequence and expected["hash"] == previous
    )
    for seq, wire, prev, digest in connection.execute(
        "SELECT sequence,event::text,previous_hash,event_hash FROM audit.events "
        "WHERE stream_id=%s ORDER BY sequence",
        (stream,),
    ):
        calculated = hashlib.sha256((previous + "\n" + wire).encode()).hexdigest()
        if seq != sequence + 1 or prev != previous or digest != calculated:
            raise ValueError(f"Audit integrity failure at sequence {seq}")
        sequence, previous = seq, digest
        rows.append({"sequence": seq, "event_json": wire, "previous_hash": prev, "hash": digest})
        if expected and expected["sequence"] == seq and expected["hash"] == digest:
            checkpoint_seen = True
    if (sequence, previous) != head[:2]:
        raise ValueError("Audit head mismatch (possible tail deletion)")
    if not checkpoint_seen:
        raise ValueError("External checkpoint missing or changed")
    return {
        "stream_id": str(stream),
        "sequence": sequence,
        "hash": previous,
        "anchor_sequence": head[2],
        "anchor_hash": head[3],
    }, rows
