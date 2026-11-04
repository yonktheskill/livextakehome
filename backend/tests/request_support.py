"""Explicit inbox acknowledgement for legacy-action database fixtures.

These tests exercise leases/receipts/confirmations, constructing unassociated
legacy actions directly. They intentionally do not simulate booking semantics;
test_requests covers durable task interpretation using the real accept API.
"""

from app.db.models import Run
from app.db.repository import accept as raw_accept
from app.db.requests import request_context, resolve_requests


def accept_interpreted(user, content, request_id):
    accepted = raw_accept(user, content, request_id)
    run = Run.get_by_id(accepted["run_id"])
    if run.interrupted or run.revision != run.conversation.revision:
        return accepted
    for inbox in request_context(run.id)["inbox"]:
        resolve_requests(
            run.id,
            inbox["message_id"],
            [
                {
                    "relation": "dismiss",
                    "reason": "Legacy-action fixture acknowledgement; "
                    "semantic task behavior is covered in test_requests",
                }
            ],
        )
    return accepted
