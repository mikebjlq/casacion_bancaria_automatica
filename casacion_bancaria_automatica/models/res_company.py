# © 2026 Tetra Consultores
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    casacion_tolerance_type = fields.Selection(
        [("none", "Sin margen"), ("fixed", "Importe fijo"), ("percent", "Porcentaje")],
        string="Margen de error de la casación",
        default="none",
        required=True,
    )
    casacion_tolerance_amount = fields.Float(
        string="Margen (importe)", digits=(16, 2), default=1.0,
        help="Se casan facturas cuyo pendiente difiera del banco en ± este importe.",
    )
    casacion_tolerance_percent = fields.Float(
        string="Margen (%)", digits=(16, 2), default=1.0,
        help="Se casan facturas cuyo pendiente difiera del banco en ± este porcentaje "
        "del importe del banco.",
    )
    casacion_writeoff_expense_account_id = fields.Many2one(
        "account.account",
        string="Cuenta de diferencias en contra",
        help="Cuando se cobra de menos o se paga de más dentro del margen (p. ej. 669).",
    )
    casacion_writeoff_income_account_id = fields.Many2one(
        "account.account",
        string="Cuenta de diferencias a favor",
        help="Cuando se cobra de más o se paga de menos dentro del margen (p. ej. 769).",
    )
    casacion_max_invoices = fields.Integer(
        string="Máximo de facturas por movimiento",
        default=5,
        help="Combinaciones de hasta este número de facturas de la misma empresa "
        "cuya suma coincida con el movimiento bancario. 1 = no combinar.",
    )
    casacion_allow_partial = fields.Boolean(
        string="Proponer pagos parciales",
        default=True,
        help="Si un movimiento es menor que una factura de la empresa del concepto "
        "(anticipo, pago a cuenta…), se propone como pago parcial y la factura queda "
        "abierta por el resto.",
    )

    def _casacion_tolerance(self, amount):
        """Margen absoluto (en moneda de la empresa) para un importe del banco."""
        self.ensure_one()
        if self.casacion_tolerance_type == "fixed":
            return abs(self.casacion_tolerance_amount or 0.0)
        if self.casacion_tolerance_type == "percent":
            return abs(amount) * abs(self.casacion_tolerance_percent or 0.0) / 100.0
        return 0.0

    def _casacion_tolerance_label(self):
        self.ensure_one()
        if self.casacion_tolerance_type == "fixed":
            return "± %.2f %s" % (self.casacion_tolerance_amount, self.currency_id.symbol or "")
        if self.casacion_tolerance_type == "percent":
            return "± %.2f %%" % self.casacion_tolerance_percent
        return "sin margen"
