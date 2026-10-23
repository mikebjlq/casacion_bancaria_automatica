# © 2026 Tetra Consultores
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    casacion_openai_api_key = fields.Char(
        string="Clave API de OpenAI (casación)",
        config_parameter="casacion_bancaria_automatica.openai_api_key",
        help="Si se deja vacía se usa la del módulo de facturas con IA "
        "(xtendoo_invoice_ai.openai_api_key) o la variable de entorno OPENAI_API_KEY.",
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
