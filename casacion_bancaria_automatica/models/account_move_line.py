# © 2026 Tetra Consultores
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import models
from odoo.tools.misc import formatLang


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    def name_get(self):
        if not self.env.context.get("casacion_display"):
            return super().name_get()
        result = []
        for line in self:
            parts = [
                line.date_maturity and str(line.date_maturity) or str(line.date),
                line.move_id.name,
                line.partner_id.name,
                line.move_id.ref or line.name,
                formatLang(self.env, line.amount_residual, currency_obj=line.company_currency_id),
            ]
            result.append((line.id, " · ".join(p for p in parts if p)))
        return result
