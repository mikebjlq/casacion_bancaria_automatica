# © 2026 Tetra Consultores
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

DEFAULT_PROMPT = """Eres un contable experto en conciliación bancaria en España.

Recibes una lista de movimientos bancarios pendientes de conciliar. Cada movimiento trae
varios apuntes contables candidatos que tienen EXACTAMENTE el mismo importe pendiente.
Tu tarea es elegir, para cada movimiento bancario, el candidato que corresponde de verdad
a ese cobro o pago, o ninguno si no hay indicios suficientes.

Criterios, por orden de importancia:
1. Referencias: número de factura, número de asiento, referencia del proveedor o del
   cliente, número de pedido, remesa o mandato SEPA que aparezcan en el concepto o en el
   detalle del movimiento bancario. Ignora mayúsculas, espacios, guiones y ceros a la
   izquierda al comparar.
2. Empresa: el nombre del cliente o proveedor del candidato aparece (aunque sea abreviado,
   truncado o sin forma jurídica como S.L., S.A., SLU) en el concepto o en el detalle, o
   coincide con la empresa del movimiento bancario.
3. Fechas: el movimiento bancario suele producirse en la fecha de vencimiento del apunte o
   poco después. Prefiere el candidato con vencimiento (o fecha) más cercano y anterior a
   la fecha del banco. Un apunte posterior al movimiento bancario es poco probable, salvo
   anticipos.
4. Tipo de apunte: los cobros (importe positivo) suelen casar con facturas de cliente o
   cobros pendientes; los pagos (importe negativo) con facturas de proveedor, nóminas,
   Seguridad Social, impuestos o préstamos. Los "TRASPASO" entre cuentas propias casan con
   apuntes de cuentas de traspasos internos.
5. La "puntuacion_heuristica" es una estimación previa (0-100) calculada por Odoo con
   empresa, referencias y fechas. Úsala como apoyo, pero puedes contradecirla.

Reglas:
- Elige solo entre los candidatos que se te dan para cada movimiento; nunca inventes ids.
- No asignes el mismo apunte a dos movimientos bancarios distintos.
- Si dos candidatos son igual de probables y nada los distingue, elige el de fecha de
  vencimiento más cercana y pon una confianza baja (menos de 50).
- Si ningún candidato encaja, devuelve "apunte_id": null.
- El motivo debe ser breve (máximo 20 palabras) y en español.
"""

RESPONSE_FORMAT = """
FORMATO DE RESPUESTA (obligatorio): responde únicamente con un objeto JSON válido, sin texto
adicional, con esta estructura:
{"propuestas": [{"movimiento_bancario_id": <int>, "apunte_id": <int o null>,
"confianza": <int 0-100>, "motivo": "<texto breve>"}]}
Incluye una entrada por cada movimiento bancario recibido.
"""


class CasacionBancariaPrompt(models.Model):
    _name = "casacion.bancaria.prompt"
    _description = "Prompt de IA para la casación bancaria automática"
    _order = "company_id, version desc, id desc"

    name = fields.Char(string="Nombre", required=True, default="Prompt casación bancaria")
    version = fields.Integer(string="Versión", readonly=True, copy=False, default=1)
    is_current = fields.Boolean(
        string="En uso",
        readonly=True,
        copy=False,
        help="Versión que se envía a ChatGPT. Solo puede haber una en uso por empresa.",
    )
    prompt_text = fields.Text(string="Prompt", required=True, default=DEFAULT_PROMPT)
    notes = fields.Text(string="Notas de la versión")
    company_id = fields.Many2one(
        "res.company",
        string="Empresa",
        default=lambda self: self.env.company,
        help="Vacío: prompt general para todas las empresas.",
    )
    parent_id = fields.Many2one(
        "casacion.bancaria.prompt",
        string="Creada a partir de",
        readonly=True,
        copy=False,
        ondelete="set null",
    )
    response_format = fields.Text(
        string="Formato de respuesta (fijo)",
        compute="_compute_response_format",
        help="Se añade siempre detrás del prompt. No es editable porque el código depende de él.",
    )

    def _compute_response_format(self):
        for rec in self:
            rec.response_format = RESPONSE_FORMAT

    @api.constrains("is_current", "company_id")
    def _check_one_current(self):
        for rec in self.filtered("is_current"):
            others = self.search_count(
                [
                    ("id", "!=", rec.id),
                    ("is_current", "=", True),
                    ("company_id", "=", rec.company_id.id),
                ]
            )
            if others:
                raise ValidationError(
                    _("Solo puede haber una versión del prompt en uso por empresa.")
                )

    def write(self, vals):
        if "prompt_text" in vals and self.filtered("is_current"):
            raise UserError(
                _(
                    "La versión en uso no se puede modificar, para poder volver a ella. "
                    "Pulse «Nueva versión», edite la copia y actívela."
                )
            )
        return super().write(vals)

    def unlink(self):
        if self.filtered("is_current"):
            raise UserError(_("No se puede borrar la versión en uso."))
        return super().unlink()

    def _next_version(self, company):
        last = self.search([("company_id", "=", company.id)], order="version desc", limit=1)
        return (last.version or 0) + 1

    def action_new_version(self):
        self.ensure_one()
        new = self.copy(
            {
                "version": self._next_version(self.company_id),
                "parent_id": self.id,
                "notes": False,
            }
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": new.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_set_current(self):
        self.ensure_one()
        self.search(
            [("is_current", "=", True), ("company_id", "=", self.company_id.id)]
        ).write({"is_current": False})
        self.is_current = True

    def action_load_default(self):
        self.ensure_one()
        self.prompt_text = DEFAULT_PROMPT

    @api.model
    def _get_current(self, company):
        prompt = self.search(
            [("is_current", "=", True), ("company_id", "=", company.id)], limit=1
        )
        if not prompt:
            prompt = self.search(
                [("is_current", "=", True), ("company_id", "=", False)], limit=1
            )
        return prompt

    @api.model
    def _get_prompt_text(self, company):
        prompt = self._get_current(company)
        return (prompt.prompt_text or DEFAULT_PROMPT) + RESPONSE_FORMAT

    @api.model
    def _ensure_default_prompt(self):
        """Crea la versión 1 (general) si no hay ningún prompt."""
        if not self.with_context(active_test=False).search_count([]):
            self.create(
                {
                    "name": "Prompt inicial",
                    "version": 1,
                    "is_current": True,
                    "company_id": False,
                    "notes": "Versión inicial creada al instalar el módulo.",
                }
            )
