# Copyright (c) 2025, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import json

from erpnext.tests.utils import ERPNextTestSuite


class TestWebsiteListForContact(ERPNextTestSuite):
	def test_get_list_context_currency_symbols(self):
		# get_list_context builds the enabled-currency symbol map via frappe.get_all (converted from
		# raw SQL). Exercises that query and asserts a known enabled currency is present.
		from erpnext.controllers.website_list_for_contact import get_list_context

		context = get_list_context()

		symbols = json.loads(context["currency_symbols"])
		self.assertIsInstance(symbols, dict)
		self.assertIn("USD", symbols)
