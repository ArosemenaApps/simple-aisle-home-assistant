"""The Simple Aisle sync engine: pure Python, no Home Assistant imports.

Everything the sync rules promise is decided here, so it
can be tested against a fake list backend. The Home Assistant side
(`ha_backend.py`) only reads and writes native to-do lists.

State is one JSON-serialisable dict, saved through the `save` callback after
every change and before anything is acknowledged.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import hashlib
import hmac
import secrets
from typing import Any, Protocol
import uuid

from .const import (
    COMPLETED,
    DEFER_MAX_ATTEMPTS,
    DEFER_MAX_SECONDS,
    DONE_RETENTION_SECONDS,
    LEASE_SECONDS,
    LINK_ACTIVE,
    LINK_MISSING,
    LINK_SUSPENDED,
    NEEDS_ACTION,
    OP_ADD,
    OP_COMPLETE,
    OP_EDIT,
    OP_MOVE,
    OP_REMOVE,
    OP_UNCOMPLETE,
    OPS_PER_SYNC,
    PAIR_CODE_TTL,
    PAIR_FAILURE_WINDOW,
    PAIR_MAX_FAILURES,
    PURCHASED_COMPLETE,
    RESULT_APPLIED,
    RESULT_DEFERRED,
    RESULT_DUPLICATE,
    RESULT_NOOP,
    RESULT_REJECTED,
    STATE_CANCELLED,
    STATE_DONE,
    STATE_FAILED,
    STATE_PENDING,
    TOMBSTONE_RETENTION_SECONDS,
)


class SyncError(Exception):
    """A request the engine refuses. `code` is sent to the app."""

    def __init__(self, code: str, message: str = "", status: int = 400) -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status


@dataclass(slots=True)
class Item:
    """One native to-do item as the backend sees it."""

    uid: str
    summary: str
    status: str = NEEDS_ACTION
    description: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {"summary": self.summary, "description": self.description or None, "status": self.status}


class ListBackend(Protocol):
    """Native to-do lists. Implemented with Home Assistant services."""

    async def get_items(self, entity_id: str) -> list[Item] | None:
        """All items, or None if the list doesn't exist (never a partial list)."""

    def supports_description(self, entity_id: str) -> bool: ...

    async def add(self, entity_id: str, summary: str, description: str | None, known_uids: set[str]) -> str:
        """Adds an item and returns its uid."""

    async def update(
        self, entity_id: str, uid: str, *, summary: str | None = None,
        description: str | None = None, status: str | None = None,
    ) -> None: ...

    async def remove(self, entity_id: str, uids: list[str]) -> None: ...


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_secret(nbytes: int) -> str:
    return base64.b32encode(secrets.token_bytes(nbytes)).decode().rstrip("=")


def empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "instance_id": str(uuid.uuid4()),
        "household_id": None,
        "household_name": None,
        "devices": {},
        "links": {},
        "mappings": {},
        "known": {},
        "ops": [],
        "next_seq": 1,
        "journal": [],
        "stats": {},
    }


@dataclass
class Settings:
    purchased_action: str = PURCHASED_COMPLETE


@dataclass
class _PairCode:
    digest: str
    expires_at: float


@dataclass
class _Failures:
    times: list[float] = field(default_factory=list)


class Engine:
    """The integration's memory and rules. All mutations hold `self.lock`."""

    def __init__(
        self,
        state: dict[str, Any],
        backend: ListBackend,
        *,
        now: Callable[[], float],
        save: Callable[[dict[str, Any]], Awaitable[None]],
        settings: Settings | None = None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.state = state
        self.backend = backend
        self.now = now
        self._save = save
        self.settings = settings or Settings()
        self.on_change = on_change
        self.lock = asyncio.Lock()
        self._codes: list[_PairCode] = []
        self._failures = _Failures()
        self._waiters: list[asyncio.Future[None]] = []
        for key, value in empty_state().items():
            state.setdefault(key, value)

    # ------------------------------------------------------------------
    # Persistence and notification

    async def _commit(self) -> None:
        await self._save(self.state)
        if self.on_change:
            self.on_change()

    def _wake_waiters(self) -> None:
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)
        self._waiters.clear()

    async def wait_for_ops(self, after_seq: int, timeout: float) -> int:
        """Long-poll: returns once an op newer than `after_seq` exists, or on timeout."""
        if self.state["next_seq"] - 1 > after_seq:
            return self.state["next_seq"] - 1
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter, timeout)
        except TimeoutError:
            pass
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
        return self.state["next_seq"] - 1

    # ------------------------------------------------------------------
    # Pairing and devices

    def create_pair_code(self) -> tuple[str, float]:
        now = self.now()
        self._codes = [c for c in self._codes if c.expires_at > now]
        code = new_secret(16)
        expires = now + PAIR_CODE_TTL
        self._codes.append(_PairCode(hash_secret(code), expires))
        return code, expires

    async def pair(
        self, code: str, *, device_name: str, household_id: str, household_name: str | None,
        app_version: str | None,
    ) -> dict[str, Any]:
        now = self.now()
        self._codes = [c for c in self._codes if c.expires_at > now]
        digest = hash_secret(code.strip().upper())
        match = next((c for c in self._codes if hmac.compare_digest(c.digest, digest)), None)
        if match is None:
            self._failures.times = [t for t in self._failures.times if t > now - PAIR_FAILURE_WINDOW]
            self._failures.times.append(now)
            if len(self._failures.times) >= PAIR_MAX_FAILURES:
                self._codes.clear()
                self._failures.times.clear()
            raise SyncError("invalid_code", "That pairing code is wrong or has expired.", 403)
        self._codes.remove(match)  # single use, even if what follows fails
        try:
            uuid.UUID(household_id)
        except (ValueError, TypeError) as err:
            raise SyncError("bad_request", "household_id must be a UUID") from err
        async with self.lock:
            bound = self.state["household_id"]
            if bound and bound.lower() != household_id.lower():
                raise SyncError(
                    "household_mismatch",
                    "This Home Assistant is already connected to a different Simple Aisle household.",
                    409,
                )
            self.state["household_id"] = household_id.lower()
            if household_name:
                self.state["household_name"] = household_name[:100]
            token = new_secret(32)
            device_id = str(uuid.uuid4())
            self.state["devices"][device_id] = {
                "name": (device_name or "Device")[:80],
                "token_hash": hash_secret(token),
                "created_at": now,
                "last_seen": now,
                "app_version": (app_version or "")[:40],
            }
            await self._commit()
        return {
            "device_id": device_id,
            "token": token,
            "instance_id": self.state["instance_id"],
            "household_id": self.state["household_id"],
        }

    def device_for_token(self, token: str | None) -> str | None:
        if not token:
            return None
        digest = hash_secret(token)
        for device_id, device in self.state["devices"].items():
            if hmac.compare_digest(device["token_hash"], digest):
                return device_id
        return None

    async def revoke(self, device_id: str) -> bool:
        async with self.lock:
            removed = self.state["devices"].pop(device_id, None) is not None
            for op in self.state["ops"]:
                if op.get("lease_device") == device_id:
                    op["lease_device"] = None
                    op["lease_until"] = None
            if removed:
                await self._commit()
            return removed

    def _touch(self, device_id: str, app_version: str | None = None) -> None:
        device = self.state["devices"].get(device_id)
        if device:
            device["last_seen"] = self.now()
            if app_version:
                device["app_version"] = app_version[:40]

    def _check_household(self, household_id: str | None) -> None:
        bound = self.state["household_id"]
        if not household_id or not bound or bound != household_id.lower():
            raise SyncError(
                "household_mismatch",
                "This device's Simple Aisle household isn't the one connected to Home Assistant.",
                409,
            )

    # ------------------------------------------------------------------
    # Mappings

    def _mapping(self, entry_id: str) -> dict[str, Any] | None:
        return self.state["mappings"].get(entry_id.lower())

    def _entry_for_uid(self, entity_id: str, uid: str) -> str | None:
        for entry_id, mapping in self.state["mappings"].items():
            if mapping.get("entity_id") == entity_id and mapping.get("uid") == uid:
                return entry_id
        return None

    def _link_for_entity(self, entity_id: str) -> dict[str, Any] | None:
        return next((l for l in self.state["links"].values() if l["entity_id"] == entity_id), None)

    def _set_mapping(self, entry_id: str, **fields: Any) -> dict[str, Any]:
        mapping = self.state["mappings"].setdefault(
            entry_id.lower(),
            {"store_id": None, "entity_id": None, "uid": None, "version": 0, "tombstoned": False},
        )
        mapping.update(fields)
        return mapping

    # ------------------------------------------------------------------
    # The op queue

    def _pending_ops(self) -> list[dict[str, Any]]:
        return [op for op in self.state["ops"] if op["state"] == STATE_PENDING]

    def _has_pending_op(self, entry_id: str) -> bool:
        entry_id = entry_id.lower()
        return any(op.get("entry_id") == entry_id for op in self._pending_ops())

    def _undelivered_add(self, entity_id: str, uid: str) -> dict[str, Any] | None:
        return next(
            (
                op for op in self._pending_ops()
                if op["type"] == OP_ADD and op["entity_id"] == entity_id and op["uid"] == uid
                and not op.get("lease_device") and not op.get("delivered")
            ),
            None,
        )

    def _enqueue(self, op_type: str, *, store_id: str, entity_id: str, uid: str | None,
                 entry_id: str | None, data: dict[str, Any] | None = None, op_id: str | None = None) -> dict[str, Any]:
        op_id = op_id or str(uuid.uuid4())
        op = {
            "seq": self.state["next_seq"],
            "op_id": op_id,
            "type": op_type,
            "store_id": store_id,
            "entity_id": entity_id,
            "uid": uid,
            "entry_id": (entry_id or op_id).lower(),
            "data": data or {},
            "created_at": self.now(),
            "state": STATE_PENDING,
            "lease_device": None,
            "lease_until": None,
            "attempts": 0,
        }
        self.state["next_seq"] += 1
        self.state["ops"].append(op)
        self._wake_waiters()
        return op

    def pending_count(self) -> int:
        return len(self._pending_ops())

    def oldest_pending_age(self) -> float | None:
        pending = self._pending_ops()
        if not pending:
            return None
        return self.now() - min(op["created_at"] for op in pending)

    def _prune(self) -> None:
        now = self.now()
        self.state["ops"] = [
            op for op in self.state["ops"]
            if op["state"] == STATE_PENDING or now - op.get("finished_at", now) < DONE_RETENTION_SECONDS
        ]
        self.state["mappings"] = {
            entry_id: mapping for entry_id, mapping in self.state["mappings"].items()
            if not mapping.get("tombstoned") or now - mapping.get("tombstoned_at", now) < TOMBSTONE_RETENTION_SECONDS
        }

    def reset_leases(self) -> None:
        for op in self.state["ops"]:
            op["lease_device"] = None
            op["lease_until"] = None

    # ------------------------------------------------------------------
    # Watching HA lists

    async def observe(self, entity_id: str) -> int:
        """Reads a linked list and queues what changed in HA. Returns ops queued."""
        async with self.lock:
            queued = await self._observe_locked(entity_id)
            if queued:
                await self._commit()
            return queued

    async def observe_all(self) -> int:
        async with self.lock:
            total = 0
            for link in list(self.state["links"].values()):
                total += await self._observe_locked(link["entity_id"])
            if total:
                await self._commit()
            return total

    async def _observe_locked(self, entity_id: str) -> int:
        link = self._link_for_entity(entity_id)
        if link is None or link["state"] == LINK_SUSPENDED:
            return 0
        items = await self.backend.get_items(entity_id)
        if items is None:
            if link["state"] != LINK_MISSING:
                link["state"] = LINK_MISSING
                await self._commit()
            return 0
        if link["state"] == LINK_MISSING:
            link["state"] = LINK_ACTIVE
        return self._diff(link, items)

    def _text(self, entity_id: str, item: Item) -> dict[str, Any]:
        """What an op says about an item's text. A list with descriptions always
        sends one ("" when empty), so the app can tell "cleared" from "this
        list has no descriptions" and never wipes a note because of the latter."""
        description = (item.description or "") if self.backend.supports_description(entity_id) else None
        return {"summary": item.summary, "description": description}

    def _diff(self, link: dict[str, Any], items: list[Item]) -> int:
        entity_id = link["entity_id"]
        store_id = link["store_id"]
        known: dict[str, dict[str, Any]] = self.state["known"].setdefault(entity_id, {})
        queued = 0
        current = {item.uid: item for item in items}

        for item in items:
            before = known.get(item.uid)
            entry_id = self._entry_for_uid(entity_id, item.uid)
            if before is None:
                if item.status == NEEDS_ACTION:
                    op = self._enqueue(OP_ADD, store_id=store_id, entity_id=entity_id, uid=item.uid, entry_id=None,
                                       data=self._text(entity_id, item))
                    self._set_mapping(op["entry_id"], store_id=store_id, entity_id=entity_id, uid=item.uid,
                                      version=0, tombstoned=False)
                    queued += 1
                known[item.uid] = item.snapshot()
                continue

            text_changed = (before["summary"] != item.summary
                            or (before.get("description") or None) != (item.description or None))
            status_changed = before["status"] != item.status

            if entry_id is None:
                # An item Simple Aisle doesn't track (left over at link time, or
                # completed when it appeared). Un-checking it puts it on the list.
                if status_changed and item.status == NEEDS_ACTION:
                    op = self._enqueue(OP_ADD, store_id=store_id, entity_id=entity_id, uid=item.uid, entry_id=None,
                                       data=self._text(entity_id, item))
                    self._set_mapping(op["entry_id"], store_id=store_id, entity_id=entity_id, uid=item.uid,
                                      version=0, tombstoned=False)
                    queued += 1
                known[item.uid] = item.snapshot()
                continue

            if text_changed:
                pending_add = self._undelivered_add(entity_id, item.uid)
                if pending_add is not None:
                    pending_add["data"].update(self._text(entity_id, item))
                else:
                    self._enqueue(OP_EDIT, store_id=store_id, entity_id=entity_id, uid=item.uid, entry_id=entry_id,
                                  data=self._text(entity_id, item))
                    queued += 1
            if status_changed:
                mapping = self._mapping(entry_id)
                if item.status == COMPLETED:
                    purchase_id = str(uuid.uuid4())
                    self._enqueue(OP_COMPLETE, store_id=store_id, entity_id=entity_id, uid=item.uid,
                                  entry_id=entry_id, data={"purchase_id": purchase_id}, op_id=purchase_id)
                    if mapping is not None:
                        mapping["ha_purchase_id"] = purchase_id
                else:
                    reverse = mapping.pop("ha_purchase_id", None) if mapping else None
                    if mapping is not None:
                        mapping["tombstoned"] = False
                    self._enqueue(OP_UNCOMPLETE, store_id=store_id, entity_id=entity_id, uid=item.uid,
                                  entry_id=entry_id, data={**self._text(entity_id, item),
                                                           "reverse_purchase_id": reverse})
                queued += 1
            known[item.uid] = item.snapshot()

        for uid in [uid for uid in known if uid not in current]:
            before = known.pop(uid)
            entry_id = self._entry_for_uid(entity_id, uid)
            if entry_id is None:
                continue
            mapping = self._mapping(entry_id)
            if before["status"] == NEEDS_ACTION:
                pending_add = self._undelivered_add(entity_id, uid)
                if pending_add is not None:
                    # Added and deleted in HA before any phone saw it.
                    for op in self._pending_ops():
                        if op["entry_id"] == pending_add["entry_id"] and not op.get("lease_device"):
                            op["state"] = STATE_CANCELLED
                            op["finished_at"] = self.now()
                    self.state["mappings"].pop(entry_id, None)
                    continue
                self._enqueue(OP_REMOVE, store_id=store_id, entity_id=entity_id, uid=uid, entry_id=entry_id)
                queued += 1
            # Cleared after completion: the completion is already queued.
            if mapping is not None:
                mapping["uid"] = None
        return queued

    # ------------------------------------------------------------------
    # The app's sync request

    async def sync(self, device_id: str, request: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            self._check_household(request.get("household_id"))
            self._touch(device_id, request.get("app_version"))
            now = self.now()
            confirmed_results = self._apply_results(device_id, request.get("results") or [])

            items_by_entity: dict[str, list[Item] | None] = {}
            for link in list(self.state["links"].values()):
                if link["state"] == LINK_SUSPENDED:
                    continue
                await self._observe_locked(link["entity_id"])
                items_by_entity[link["entity_id"]] = await self.backend.get_items(link["entity_id"])

            confirmed_tombstones: list[str] = []
            if request.get("state_complete"):
                await self._apply_store_states(request.get("stores") or [])
                confirmed_tombstones = await self._apply_tombstones(request.get("tombstones") or [], items_by_entity)
                await self._apply_upserts(request.get("entries") or [], items_by_entity)
                self.state["stats"]["last_full_sync"] = now
                self.state["stats"]["last_full_sync_device"] = device_id

            ops = self._lease(device_id)
            self.state["stats"]["last_sync"] = now
            self.state["stats"]["last_sync_device"] = device_id
            self._prune()
            await self._commit()
            return {
                "ops": [self._public_op(op) for op in ops],
                "results_confirmed": confirmed_results,
                "tombstones_confirmed": confirmed_tombstones,
                "links": self.public_links(),
                "pending_ops": self.pending_count(),
                "next_seq": self.state["next_seq"] - 1,
                "server_time": now,
            }

    def _lease(self, device_id: str) -> list[dict[str, Any]]:
        now = self.now()
        leased: list[dict[str, Any]] = []
        for op in sorted(self._pending_ops(), key=lambda o: o["seq"]):
            if len(leased) >= OPS_PER_SYNC:
                break
            holder = op.get("lease_device")
            if holder and holder != device_id and (op.get("lease_until") or 0) > now:
                continue
            op["lease_device"] = device_id
            op["lease_until"] = now + LEASE_SECONDS
            op["delivered"] = True
            leased.append(op)
        return leased

    @staticmethod
    def _public_op(op: dict[str, Any]) -> dict[str, Any]:
        return {
            "op_id": op["op_id"],
            "seq": op["seq"],
            "type": op["type"],
            "store_id": op["store_id"],
            "entry_id": op["entry_id"],
            "created_at": op["created_at"],
            **{k: v for k, v in op["data"].items() if v is not None},
        }

    def _apply_results(self, device_id: str, results: list[dict[str, Any]]) -> list[str]:
        confirmed: list[str] = []
        by_id = {op["op_id"]: op for op in self.state["ops"]}
        now = self.now()
        for result in results:
            op_id = str(result.get("op_id", ""))
            confirmed.append(op_id)
            op = by_id.get(op_id)
            if op is None or op["state"] != STATE_PENDING:
                continue
            status = result.get("status")
            if status == RESULT_DEFERRED:
                op["lease_device"] = None
                op["lease_until"] = None
                op["attempts"] = op.get("attempts", 0) + 1
                op.setdefault("deferred_since", now)
                op["last_reason"] = str(result.get("reason") or "")[:80]
                if op["attempts"] >= DEFER_MAX_ATTEMPTS or now - op["deferred_since"] > DEFER_MAX_SECONDS:
                    op["state"] = STATE_FAILED
                    op["finished_at"] = now
                continue
            op["finished_at"] = now
            op["result"] = status
            op["applied_by"] = device_id
            if status == RESULT_REJECTED:
                op["state"] = STATE_FAILED
                op["last_reason"] = str(result.get("reason") or "")[:80]
                continue
            op["state"] = STATE_DONE
            result_entry = str(result.get("entry_id") or "").lower() or None
            if op["type"] in (OP_ADD, OP_UNCOMPLETE) and result_entry and result_entry != op["entry_id"]:
                self._remap(op, result_entry, duplicate=status == RESULT_DUPLICATE)
        return confirmed

    def _remap(self, op: dict[str, Any], result_entry: str, *, duplicate: bool) -> None:
        """The app put this HA item on an entry other than the provisional one."""
        old = self.state["mappings"].pop(op["entry_id"], None)
        uid = old.get("uid") if old else op.get("uid")
        existing = self._mapping(result_entry)
        if duplicate and existing and existing.get("uid") and existing.get("uid") != uid \
                and existing.get("entity_id") == op["entity_id"]:
            # Already on that list as another HA item: this one is the duplicate.
            self.state.setdefault("pending_removals", []).append({"entity_id": op["entity_id"], "uid": uid})
            return
        self._set_mapping(result_entry, store_id=op["store_id"], entity_id=op["entity_id"], uid=uid, tombstoned=False)
        for later in self._pending_ops():
            if later["entry_id"] == op["entry_id"]:
                later["entry_id"] = result_entry

    async def _apply_store_states(self, stores: list[dict[str, Any]]) -> None:
        for store in stores:
            store_id = str(store.get("store_id", "")).lower()
            link = self.state["links"].get(store_id)
            if link is None:
                continue
            if store.get("name"):
                link["store_name"] = str(store["name"])[:100]
            if store.get("eligible") is False and link["state"] != LINK_SUSPENDED:
                await self._remove_mapped_items(link)
                link["state"] = LINK_SUSPENDED
                link["suspended_reason"] = str(store.get("reason") or "not_eligible")[:40]
            elif store.get("eligible") is True and link["state"] == LINK_SUSPENDED \
                    and link.get("suspended_reason") != "user":
                link["state"] = LINK_ACTIVE
                link.pop("suspended_reason", None)

    async def _remove_mapped_items(self, link: dict[str, Any]) -> None:
        entity_id = link["entity_id"]
        uids = [m["uid"] for m in self.state["mappings"].values()
                if m.get("entity_id") == entity_id and m.get("uid")]
        if uids:
            try:
                await self.backend.remove(entity_id, uids)
            except Exception:  # noqa: BLE001 - the list may be gone; mappings still go
                pass
        known = self.state["known"].get(entity_id, {})
        for uid in uids:
            known.pop(uid, None)
        for entry_id in [e for e, m in self.state["mappings"].items() if m.get("entity_id") == entity_id]:
            self.state["mappings"].pop(entry_id)
        for op in self._pending_ops():
            if op["entity_id"] == entity_id:
                op["state"] = STATE_CANCELLED
                op["finished_at"] = self.now()

    async def _apply_tombstones(
        self, tombstones: list[dict[str, Any]], items_by_entity: dict[str, list[Item] | None]
    ) -> list[str]:
        confirmed: list[str] = []
        for tomb in tombstones:
            entry_id = str(tomb.get("entry_id", "")).lower()
            mapping = self._mapping(entry_id)
            if mapping is None or mapping.get("tombstoned"):
                confirmed.append(entry_id)
                continue
            if self._has_pending_op(entry_id):
                continue  # resent next sync, after the HA change is applied
            entity_id = mapping.get("entity_id")
            uid = mapping.get("uid")
            items = items_by_entity.get(entity_id) if entity_id else None
            present = items is not None and uid is not None and any(i.uid == uid for i in items)
            if present:
                if tomb.get("reason") == "purchased" and self.settings.purchased_action == PURCHASED_COMPLETE:
                    await self.backend.update(entity_id, uid, status=COMPLETED)
                    self._record_known(entity_id, uid, status=COMPLETED)
                else:
                    await self.backend.remove(entity_id, [uid])
                    self.state["known"].get(entity_id, {}).pop(uid, None)
                    mapping["uid"] = None
            mapping["tombstoned"] = True
            mapping["tombstoned_at"] = self.now()
            confirmed.append(entry_id)
        return confirmed

    def _record_known(self, entity_id: str, uid: str, **fields: Any) -> None:
        known = self.state["known"].setdefault(entity_id, {})
        snapshot = known.setdefault(uid, {"summary": "", "description": None, "status": NEEDS_ACTION})
        snapshot.update(fields)

    async def _apply_upserts(
        self, entries: list[dict[str, Any]], items_by_entity: dict[str, list[Item] | None]
    ) -> None:
        for entry in entries:
            entry_id = str(entry.get("entry_id", "")).lower()
            store_id = str(entry.get("store_id", "")).lower()
            link = self.state["links"].get(store_id)
            if not entry_id or link is None or link["state"] != LINK_ACTIVE:
                continue
            entity_id = link["entity_id"]
            items = items_by_entity.get(entity_id)
            if items is None:
                continue
            version = int(entry.get("version") or 0)
            mapping = self._mapping(entry_id)
            if mapping is not None:
                if mapping.get("tombstoned") and not entry.get("revived"):
                    continue
                if version < int(mapping.get("version") or 0):
                    continue
            if self._has_pending_op(entry_id):
                continue

            summary = str(entry.get("summary") or "").strip()[:255] or "Item"
            description = entry.get("description") if self.backend.supports_description(entity_id) else None
            description = (str(description).strip()[:1000] or None) if description else None

            # Moved to this store from another linked store.
            if mapping is not None and mapping.get("entity_id") and mapping["entity_id"] != entity_id:
                old_entity, old_uid = mapping["entity_id"], mapping.get("uid")
                old_items = items_by_entity.get(old_entity)
                if old_uid and old_items is not None and any(i.uid == old_uid for i in old_items):
                    await self.backend.remove(old_entity, [old_uid])
                    self.state["known"].get(old_entity, {}).pop(old_uid, None)
                mapping["uid"] = None

            uid = mapping.get("uid") if mapping else None
            current = next((i for i in items if i.uid == uid), None) if uid else None
            if current is None:
                known_uids = set(self.state["known"].get(entity_id, {}))
                new_uid = await self.backend.add(entity_id, summary, description, known_uids)
                items.append(Item(new_uid, summary, NEEDS_ACTION, description))
                self._record_known(entity_id, new_uid, summary=summary, description=description, status=NEEDS_ACTION)
                uid = new_uid
            else:
                changes: dict[str, Any] = {}
                if current.summary != summary:
                    changes["summary"] = summary
                if (current.description or None) != description and self.backend.supports_description(entity_id):
                    changes["description"] = description
                if current.status != NEEDS_ACTION:
                    changes["status"] = NEEDS_ACTION
                if changes:
                    await self.backend.update(entity_id, uid, **changes)
                    for key, value in changes.items():
                        setattr(current, key, value)
                    self._record_known(entity_id, uid, **changes)
            self._set_mapping(entry_id, store_id=store_id, entity_id=entity_id, uid=uid,
                              version=max(version, int((mapping or {}).get("version") or 0)), tombstoned=False)

        # Duplicates found while applying results.
        for removal in self.state.pop("pending_removals", []):
            items = items_by_entity.get(removal["entity_id"])
            if items is not None and any(i.uid == removal["uid"] for i in items):
                await self.backend.remove(removal["entity_id"], [removal["uid"]])
            self.state["known"].get(removal["entity_id"], {}).pop(removal["uid"], None)

    # ------------------------------------------------------------------
    # Links

    def public_links(self) -> list[dict[str, Any]]:
        return [
            {
                "store_id": link["store_id"],
                "store_name": link.get("store_name"),
                "entity_id": link["entity_id"],
                "state": link["state"],
                "created_at": link.get("created_at"),
            }
            for link in self.state["links"].values()
        ]

    async def preview(self, entity_id: str) -> list[Item]:
        async with self.lock:
            existing = self._link_for_entity(entity_id)
            if existing is not None:
                raise SyncError("list_linked", "That list is already linked to another store.", 409)
            items = await self.backend.get_items(entity_id)
            if items is None:
                raise SyncError("list_missing", "That list doesn't exist.", 404)
            return items

    async def link(
        self, *, store_id: str, store_name: str, entity_id: str,
        matches: list[dict[str, Any]], imports: list[dict[str, Any]],
    ) -> dict[str, Any]:
        store_id = store_id.lower()
        async with self.lock:
            other = self._link_for_entity(entity_id)
            if other is not None and other["store_id"] != store_id:
                raise SyncError("list_linked", "That list is already linked to another store.", 409)
            current = self.state["links"].get(store_id)
            if current is not None and current["entity_id"] != entity_id:
                raise SyncError("store_linked", "Unlink this store from its current list first.", 409)
            items = await self.backend.get_items(entity_id)
            if items is None:
                raise SyncError("list_missing", "That list doesn't exist.", 404)
            uids = {item.uid for item in items}
            link = {
                "store_id": store_id,
                "store_name": store_name[:100],
                "entity_id": entity_id,
                "state": LINK_ACTIVE,
                "created_at": self.now(),
            }
            self.state["links"][store_id] = link
            known = self.state["known"].setdefault(entity_id, {})
            for item in items:
                known[item.uid] = item.snapshot()
            for pair in [*matches, *imports]:
                uid = str(pair.get("uid", ""))
                entry_id = str(pair.get("entry_id", "")).lower()
                if uid in uids and entry_id:
                    self._set_mapping(entry_id, store_id=store_id, entity_id=entity_id, uid=uid, version=0,
                                      tombstoned=False)
            await self._commit()
            return link

    async def unlink(self, store_id: str, *, remove_items: bool) -> bool:
        store_id = store_id.lower()
        async with self.lock:
            link = self.state["links"].get(store_id)
            if link is None:
                return False
            if remove_items:
                await self._remove_mapped_items(link)
            else:
                entity_id = link["entity_id"]
                for entry_id in [e for e, m in self.state["mappings"].items() if m.get("entity_id") == entity_id]:
                    self.state["mappings"].pop(entry_id)
                for op in self._pending_ops():
                    if op["entity_id"] == entity_id:
                        op["state"] = STATE_CANCELLED
                        op["finished_at"] = self.now()
            self.state["known"].pop(link["entity_id"], None)
            self.state["links"].pop(store_id)
            await self._commit()
            return True

    # ------------------------------------------------------------------
    # Moving between stores (simple_aisle.move_item)

    async def move_item(self, entity_id: str, item_ref: str, target_store_id: str) -> dict[str, Any]:
        target_store_id = target_store_id.lower()
        async with self.lock:
            source = self._link_for_entity(entity_id)
            target = self.state["links"].get(target_store_id)
            if source is None or source["state"] != LINK_ACTIVE:
                raise SyncError("not_linked", f"{entity_id} isn't linked to a Simple Aisle store.")
            if target is None or target["state"] != LINK_ACTIVE:
                raise SyncError("not_linked", "The destination isn't a linked Simple Aisle store.")
            if target["entity_id"] == entity_id:
                raise SyncError("same_list", "The item is already on that list.")
            await self._observe_locked(entity_id)
            await self._observe_locked(target["entity_id"])
            items = await self.backend.get_items(entity_id) or []
            item = next((i for i in items if i.uid == item_ref), None) or next(
                (i for i in items if i.status == NEEDS_ACTION and i.summary.casefold() == item_ref.strip().casefold()),
                None,
            )
            if item is None:
                raise SyncError("not_found", f"Couldn't find “{item_ref}” on that list.", 404)
            move_id = str(uuid.uuid4())
            entry_id = self._entry_for_uid(entity_id, item.uid)
            record = {
                "move_id": move_id,
                "source_entity": entity_id,
                "uid": item.uid,
                "target_entity": target["entity_id"],
                "target_store_id": target_store_id,
                "summary": item.summary,
                "description": item.description,
                "entry_id": entry_id,
                "target_uid": None,
            }
            self.state["journal"].append(record)
            await self._commit()
            result = await self._finish_move(record)
            await self._commit()
            return result

    async def recover(self) -> None:
        """At startup: finish journaled moves, return leases to the pool."""
        async with self.lock:
            self.reset_leases()
            for record in list(self.state["journal"]):
                try:
                    await self._finish_move(record)
                except Exception:  # noqa: BLE001 - keep the record for the next start
                    continue
            await self._commit()

    async def _finish_move(self, record: dict[str, Any]) -> dict[str, Any]:
        target_entity, source_entity = record["target_entity"], record["source_entity"]
        target_items = await self.backend.get_items(target_entity)
        if target_items is None:
            raise SyncError("list_missing", "The destination list is missing.", 404)
        known_target = self.state["known"].setdefault(target_entity, {})
        uid = record.get("target_uid")
        if not uid or not any(i.uid == uid for i in target_items):
            uid = await self.backend.add(target_entity, record["summary"], record["description"], set(known_target))
            record["target_uid"] = uid
            await self._save(self.state)
        self._record_known(target_entity, uid, summary=record["summary"], description=record["description"],
                           status=NEEDS_ACTION)

        source_items = await self.backend.get_items(source_entity) or []
        if any(i.uid == record["uid"] for i in source_items):
            await self.backend.remove(source_entity, [record["uid"]])
        self.state["known"].get(source_entity, {}).pop(record["uid"], None)

        entry_id = record.get("entry_id")
        target_store = record["target_store_id"]
        if entry_id:
            pending_add = next(
                (op for op in self._pending_ops() if op["type"] == OP_ADD and op["entry_id"] == entry_id
                 and not op.get("lease_device") and not op.get("delivered")),
                None,
            )
            if pending_add is not None:
                pending_add.update(store_id=target_store, entity_id=target_entity, uid=uid)
            elif not any(op["op_id"] == record["move_id"] for op in self.state["ops"]):
                self._enqueue(OP_MOVE, store_id=target_store, entity_id=target_entity, uid=uid, entry_id=entry_id,
                              data={"target_store_id": target_store}, op_id=record["move_id"])
            self._set_mapping(entry_id, store_id=target_store, entity_id=target_entity, uid=uid)
        else:
            # Not tracked by Simple Aisle yet: it arrives on the new store.
            op = self._enqueue(OP_ADD, store_id=target_store, entity_id=target_entity, uid=uid, entry_id=None,
                               data={"summary": record["summary"], "description": record["description"]})
            self._set_mapping(op["entry_id"], store_id=target_store, entity_id=target_entity, uid=uid, version=0,
                              tombstoned=False)
        self.state["journal"] = [r for r in self.state["journal"] if r["move_id"] != record["move_id"]]
        return {"entity_id": target_entity, "uid": uid, "entry_id": entry_id}

    # ------------------------------------------------------------------
    # Diagnostics (no secrets, no item text)

    def diagnostics(self) -> dict[str, Any]:
        ops = self.state["ops"]
        return {
            "household_bound": bool(self.state["household_id"]),
            "devices": len(self.state["devices"]),
            "links": [{"state": l["state"], "entity_id": l["entity_id"]} for l in self.state["links"].values()],
            "mappings": len(self.state["mappings"]),
            "ops": {s: sum(1 for o in ops if o["state"] == s)
                    for s in (STATE_PENDING, STATE_DONE, STATE_FAILED, STATE_CANCELLED)},
            "journal": len(self.state["journal"]),
            "stats": dict(self.state["stats"]),
        }
