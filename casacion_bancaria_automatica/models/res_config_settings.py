# © 2026 Tetra Consultores
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    casacion_openai_api_key = fields.Char(
        string="Clave API de OpenAI (casación)",
        config_parameter="casacion_bancaria_automatica.openai_api_key",
        help="Si se deja vacía se usa la del módulo de facturas con IA "
        "(xtendoo_invoice_ai.openai_api_key), el parámetro openai_api_key o la "
        "variable de entorno OPENAI_API_KEY.",
    )
    casacion_openai_model = fields.Char(
        string="Modelo de OpenAI (casación)",
        config_parameter="casacion_bancaria_automatica.openai_model",
        default="gpt-4o-mini",
    )
    casacion_openai_base = fields.Char(
        string="URL base de OpenAI (casación)",
        config_parameter="casacion_bancaria_automatica.openai_base",
    )
    casacion_tolerance_type = fields.Selection(
        related="company_id.casacion_tolerance_type", readonly=False
    )
    casacion_tolerance_amount = fields.Float(
        related="company_id.casacion_tolerance_amount", readonly=False
    )
    casacion_tolerance_percent = fields.Float(
        related="company_id.casacion_tolerance_percent", readonly=False
    )
    casacion_writeoff_expense_account_id = fields.Many2one(
        related="company_id.casacion_writeoff_expense_account_id", readonly=False
    )
    casacion_writeoff_income_account_id = fields.Many2one(
        related="company_id.casacion_writeoff_income_account_id", readonly=False
    )
    casacion_max_invoices = fields.Integer(
        related="company_id.casacion_max_invoices", readonly=False
    )
    casacion_allow_partial = fields.Boolean(
        related="company_id.casacion_allow_partial", readonly=False
    )
