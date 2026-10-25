# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class GroupBooking(Document):
	def before_insert(self):
		# a block always has a release date: unpicked rooms go back on sale
		# at the cutoff (kamra.group_blocks), 7 days before arrival unless
		# the group negotiated otherwise
		if not self.cutoff_date and self.check_in_date:
			from kamra.group_blocks import default_cutoff
			self.cutoff_date = default_cutoff(self.check_in_date)
