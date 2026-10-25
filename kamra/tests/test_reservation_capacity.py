"""Room-type capacity checks serialize before they count inventory."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate


class ReservationCapacityTestCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		self.property = frappe.get_doc({"doctype": "Property",
			"property_name": "TEST Capacity " + frappe.generate_hash(length=8)}).insert(ignore_permissions=True).name
		self.room_type = frappe.get_doc({"doctype": "Room Type", "property": self.property,
			"room_type_code": "STD", "room_type_name": "Standard",
			"base_price": 1000}).insert(ignore_permissions=True).name
		frappe.get_doc({"doctype": "Room", "property": self.property,
			"room_type": self.room_type, "room_number": "101"}).insert(ignore_permissions=True)
		self.guest = frappe.get_doc({"doctype": "Guest", "first_name": "Capacity Guest"}).insert(ignore_permissions=True).name

	def reservation(self, status="Confirmed"):
		return frappe.get_doc({"doctype": "Reservation", "property": self.property,
			"room_type": self.room_type, "guest": self.guest, "status": status,
			"check_in_date": nowdate(), "check_out_date": add_days(nowdate(), 1), "adults": 2})

	def test_room_type_row_is_locked_before_inventory_counts(self):
		doc = self.reservation()
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as queries:
			doc.validate_type_capacity()
		sql = [str(call.args[0]).upper() for call in queries.call_args_list]
		locks = [i for i, q in enumerate(sql) if "TABROOM TYPE" in q and "FOR UPDATE" in q]
		counts = [i for i, q in enumerate(sql) if "COUNT(" in q]
		self.assertTrue(locks, "Room Type was not locked before checking capacity")
		self.assertTrue(counts)
		self.assertLess(locks[0], min(counts))

	def test_requested_stay_does_not_lock_or_consume_capacity(self):
		doc = self.reservation(status="Requested")
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as queries:
			doc.validate_type_capacity()
		self.assertEqual(queries.call_count, 0)

	def test_sold_out_type_still_refuses_an_unassigned_stay(self):
		self.reservation().insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			self.reservation().validate_type_capacity()
