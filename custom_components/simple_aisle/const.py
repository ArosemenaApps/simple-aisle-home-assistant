"""Constants for the Simple Aisle integration."""

from __future__ import annotations

DOMAIN = "simple_aisle"
API_BASE = "/api/simple_aisle/v1"
PROTOCOL_VERSION = 1
STORAGE_VERSION = 1

HEADER_TOKEN = "X-Simple-Aisle-Token"

# Pairing
PAIR_CODE_TTL = 600  # seconds
PAIR_MAX_FAILURES = 5
PAIR_FAILURE_WINDOW = 600

# Queue
LEASE_SECONDS = 90
OPS_PER_SYNC = 100
DEFER_MAX_SECONDS = 7 * 24 * 3600
DEFER_MAX_ATTEMPTS = 50
# A device that just deferred an op isn't offered it again for this long, so it
# can't use up the op's attempts before another device gets a turn.
DEFER_COOLDOWN_SECONDS = 60
# After a device applies an op, later ops for the same entry go to that device
# for this long: the others may not have its change from iCloud yet.
AFFINITY_SECONDS = 600
DONE_RETENTION_SECONDS = 14 * 24 * 3600
TOMBSTONE_RETENTION_SECONDS = 90 * 24 * 3600
POLL_SECONDS = 60
WAIT_MAX_SECONDS = 25

# Options
CONF_PURCHASED_ACTION = "purchased_action"
PURCHASED_COMPLETE = "complete"
PURCHASED_REMOVE = "remove"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_NOTIFY_WHEN_WAITING = "notify_when_waiting"
NOTIFY_WAIT_SECONDS = 3600
NOTIFY_MIN_INTERVAL = 3600

# Ops
OP_ADD = "add"
OP_EDIT = "edit"
OP_REMOVE = "remove"
OP_COMPLETE = "complete"
OP_UNCOMPLETE = "uncomplete"
OP_MOVE = "move"

STATE_PENDING = "pending"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

RESULT_APPLIED = "applied"
RESULT_DUPLICATE = "duplicate"
RESULT_NOOP = "noop"
RESULT_DEFERRED = "deferred"
RESULT_REJECTED = "rejected"

LINK_ACTIVE = "active"
LINK_SUSPENDED = "suspended"
LINK_MISSING = "list_missing"

NEEDS_ACTION = "needs_action"
COMPLETED = "completed"

SIGNAL_UPDATED = f"{DOMAIN}_updated"
