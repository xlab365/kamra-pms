# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

"""Group-block holds: rooms a confirmed Group Booking has blocked but not yet
picked up are kept off public / OTA / desk sale until the block's cutoff
date, then flow back automatically.

- A block row (Group Room Block.rooms_blocked) holds `blocked - picked` rooms
  of its type on the group's nights. Picked-up rooms are real reservations
  (counted by the normal availability math), never also counted as a hold,
  so nothing is double-counted.
- Only Confirmed groups hold. An Open group is a proposal (create_group_block
  drafts one) and holds nothing until someone confirms it.
- The hold ends at the group's cutoff_date. New groups get a default cutoff
  of 7 days before arrival (the day before arrival for a short-lead block).
  A legacy group with no cutoff holds until its arrival date.
- release_expired_blocks (every 15 minutes) writes the release record once
  the cutoff passes: rooms_released per block row, released_on on the group,
  and an Agent Action Log entry; then refreshes the OTA push.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, getdate, now_datetime, nowdate

from kamra.reservation_state import LIVE_STATUSES

DEFAULT_RELEASE_DAYS = 7


def default_cutoff(check_in_date) -> str | None:
	"""Release date for a new block: 7 days before arrival, or - when the
	block is made inside that window - the day before arrival (never in the
	past, never after arrival)."""
	if not check_in_date:
		return None
	arrival = getdate(check_in_date)
	cutoff = getdate(add_days(arrival, -DEFAULT_RELEASE_DAYS))
	today = getdate(nowdate())
	if cutoff < today:
		cutoff = max(today, getdate(add_days(arrival, -1)))
		cutoff = min(cutoff, arrival)
	return str(cutoff)


def effective_cutoff(cutoff_date, check_in_date):
	"""The last date a block holds rooms: its cutoff, else arrival."""
	return getdate(cutoff_date or check_in_date)


def _holding_groups(property: str, check_in_date, check_out_date,
                    for_group: str | None = None) -> list[dict]:
	"""Confirmed, unreleased groups overlapping the window whose cutoff has
	not passed. `for_group` (a pickup booking against its own block) is
	left out: the group's own rooms are not a shortage for it."""
	fields = ["name", "cutoff_date", "check_in_date", "check_out_date"]
	has_released = frappe.db.has_column("Group Booking", "released_on")
	if has_released:
		fields.append("released_on")
	today = getdate(nowdate())
	out = []
	for gb in frappe.get_all(
		"Group Booking",
		filters={"property": property, "status": "Confirmed",
		         "check_in_date": ("<", check_out_date),
		         "check_out_date": (">", check_in_date)},
		fields=fields,
	):
		if for_group and gb.name == for_group:
			continue
		if has_released and gb.get("released_on"):
			continue
		if gb.cutoff_date and getdate(gb.cutoff_date) < today:
			continue
		out.append(gb)
	return out


def _unpicked(group: str, room_type: str) -> int:
	blocked = frappe.db.get_value(
		"Group Room Block", {"parent": group, "room_type": room_type},
		"rooms_blocked") or 0
	if not blocked:
		return 0
	picked = frappe.db.count("Reservation", {
		"group_booking": group, "room_type": room_type,
		"status": ("in", list(LIVE_STATUSES))})
	return max(0, int(blocked) - int(picked))


def block_hold(property: str, room_type: str, check_in_date, check_out_date,
               for_group: str | None = None) -> int:
	"""Rooms of `room_type` held by group blocks overlapping the window."""
	return sum(_unpicked(gb.name, room_type) for gb in _holding_groups(
		property, check_in_date, check_out_date, for_group))


def block_hold_by_night(property: str, room_type: str, nights: list,
                        for_group: str | None = None) -> dict[str, int]:
	"""Per-night hold for `room_type`: {date: rooms held}. One read of the
	groups for the whole range, then each group counts on its own nights."""
	if not nights:
		return {}
	dates = [getdate(d) for d in nights]
	groups = _holding_groups(property, min(dates),
	                         add_days(max(dates), 1), for_group)
	out = {str(d): 0 for d in dates}
	for gb in groups:
		held = _unpicked(gb.name, room_type)
		if not held:
			continue
		ci, co = getdate(gb.check_in_date), getdate(gb.check_out_date)
		for d in dates:
			if ci <= d < co:
				out[str(d)] += held
	return out


def release_expired_blocks() -> list[dict]:
	"""Scheduler (every 15 min): a Confirmed group whose cutoff has passed
	gives its unpicked rooms back to sale. Writes the release record once
	per group - rooms_released per block row, released_on, an action-log
	entry - and refreshes the OTA push. Idempotent."""
	if not frappe.db.has_column("Group Booking", "released_on"):
		return []
	from kamra.savings import log_action

	today = getdate(nowdate())
	released = []
	for gb in frappe.get_all(
		"Group Booking",
		filters={"status": "Confirmed", "released_on": ("is", "not set")},
		fields=["name", "property", "group_name", "cutoff_date",
		        "check_in_date"],
	):
		if effective_cutoff(gb.cutoff_date, gb.check_in_date) >= today:
			continue
		sp = f"gbr_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(sp)
		try:
			doc = frappe.get_doc("Group Booking", gb.name)
			parts, total = [], 0
			for row in doc.blocks:
				n = _unpicked(doc.name, row.room_type)
				# the release record: written straight to the rows - a
				# system stamp, not an edit of the negotiated block
				frappe.db.set_value("Group Room Block", row.name,
				                    "rooms_released", n, update_modified=False)
				total += n
				if n:
					rt_name = frappe.db.get_value(
						"Room Type", row.room_type, "room_type_name") or row.room_type
					parts.append(f"{rt_name} {n}")
			frappe.db.set_value("Group Booking", doc.name, "released_on",
			                    now_datetime(), update_modified=False)
			cutoff = effective_cutoff(doc.cutoff_date, doc.check_in_date)
			log_action(
				"group_block_release", "Group Booking", doc.name, doc.property,
				rationale=(f"Cutoff {cutoff} passed for {doc.group_name}: "
				           f"released {total} unpicked room"
				           f"{'' if total == 1 else 's'} back to sale"
				           + (f" ({', '.join(parts)})" if parts else "")),
				channel=None,  # the scheduler, not a desk or an agent
			)
			released.append({"group_booking": doc.name, "rooms_released": total})
			if total:
				from kamra.channel_manager import enqueue_property_push
				enqueue_property_push(doc.property)
		except Exception:
			frappe.db.rollback(save_point=sp)
			frappe.log_error(title=f"Group block release failed: {gb.name}")
	return released
