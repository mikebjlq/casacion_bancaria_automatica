# © 2026 Tetra Consultores
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import json
import logging
import os
import re
import unicodedata
from collections import defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.casacion_prompt import RESPONSE_FORMAT

_logger = logging.getLogger(__name__)

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

AI_BATCH_SIZE = 15  # movimientos bancarios por llamada a la IA
AI_MAX_CANDIDATES = 15  # candidatos por movimiento que se envían a la IA
MIN_TOKEN_LEN = 4
STOP_WORDS = {
    "transferencia", "transferenci", "traspaso", "adeudo", "recibo", "cargo", "abono",
    "pago", "cobro", "factura", "fra", "sociedad", "limitada", "anonima", "espana",
    "cuenta", "tarjeta", "transaccion", "contactless", "liquidacion", "remesa",
    "cuota", "cuotas", "seguridad", "social", "nominas", "nomina", "compra",
}


def _norm(text):
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower()


def _tokens(text):
    return {
        t for t in re.split(r"[^a-z0-9]+", _norm(text))
        if len(t) >= MIN_TOKEN_LEN and t not in STOP_WORDS
    }


def _compact(text):
    """Texto sin separadores ni ceros a la izquierda de los números, para buscar referencias."""
    return re.sub(r"[^a-z0-9]", "", _norm(text))


class CasacionBancariaWizard(models.TransientModel):
    _name = "casacion.bancaria.wizard"
    _description = "Casación bancaria automática"

    state = fields.Selection(
        [("filtros", "Filtros"), ("propuestas", "Propuestas")],
        default="filtros",
        required=True,
    )
    company_id = fields.Many2one(
        "res.company", default=lambda self: self.env.company, required=True
    )
    journal_ids = fields.Many2many(
        "account.journal",
        string="Diarios",
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]",
        help="Vacío: todos los diarios de banco y caja.",
    )
    st_date_from = fields.Date(string="Movimientos bancarios desde")
    st_date_to = fields.Date(string="Movimientos bancarios hasta")
    aml_date_from = fields.Date(string="Apuntes a casar desde")
    aml_date_to = fields.Date(string="Apuntes a casar hasta")
    use_ai = fields.Boolean(
        string="Usar ChatGPT",
        default=True,
        help="Cuando el importe coincide con varios apuntes, pide a ChatGPT que elija "
        "el que más se parece. Sin marcar, se elige solo por la puntuación de Odoo.",
    )
    prompt_id = fields.Many2one(
        "casacion.bancaria.prompt",
        string="Prompt",
        default=lambda self: self.env["casacion.bancaria.prompt"]._get_current(
            self.env.company
        ),
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        help="Versión del prompt que se envía a ChatGPT. Por defecto, la que está en uso.",
    )
    min_score = fields.Integer(
        string="Marcar si la confianza es al menos",
        default=50,
        help="Las propuestas con menos confianza se muestran desmarcadas.",
    )
    line_ids = fields.One2many(
        "casacion.bancaria.wizard.line", "wizard_id", string="Propuestas"
    )
    summary = fields.Text(string="Resumen", readonly=True)

    # ------------------------------------------------------------------
    # Búsqueda
    # ------------------------------------------------------------------
    def _get_statement_lines(self):
        domain = [
            ("company_id", "=", self.company_id.id),
            ("is_reconciled", "=", False),
            ("state", "=", "posted"),
        ]
        if self.journal_ids:
            domain.append(("journal_id", "in", self.journal_ids.ids))
        else:
            domain.append(("journal_id.type", "in", ("bank", "cash")))
        if self.st_date_from:
            domain.append(("date", ">=", self.st_date_from))
        if self.st_date_to:
            domain.append(("date", "<=", self.st_date_to))
        return self.env["account.bank.statement.line"].search(domain, order="date, id")

    def _get_candidate_domain(self):
        journals = self.env["account.journal"].search(
            [("company_id", "=", self.company_id.id), ("type", "in", ("bank", "cash"))]
        )
        excluded_accounts = (
            journals.mapped("suspense_account_id") | journals.mapped("default_account_id")
        )
        domain = [
            ("company_id", "=", self.company_id.id),
            ("parent_state", "=", "posted"),
            ("account_id.reconcile", "=", True),
            ("account_id.account_type", "not in", ("asset_cash", "liability_credit_card")),
            ("account_id", "not in", excluded_accounts.ids),
            ("reconciled", "=", False),
            ("amount_residual", "!=", 0.0),
            ("display_type", "not in", ("line_section", "line_note")),
        ]
        if self.aml_date_from:
            domain.append(("date", ">=", self.aml_date_from))
        if self.aml_date_to:
            domain.append(("date", "<=", self.aml_date_to))
        return domain

    @api.model
    def _st_amount(self, st_line):
        """Importe del movimiento en la moneda de la empresa (línea de liquidez)."""
        liquidity, _suspense, _other = st_line._seek_for_lines()
        return sum(liquidity.mapped("balance"))

    @api.model
    def _st_text(self, st_line):
        return " ".join(
            filter(
                None,
                [
                    st_line.payment_ref,
                    st_line.partner_name,
                    st_line.partner_id.name,
                    st_line.narration and re.sub(r"<[^>]+>", " ", str(st_line.narration)),
                    st_line.transaction_details and str(st_line.transaction_details),
                ],
            )
        )

    @api.model
    def _score(self, st_line, st_text, aml, unique=False):
        """Puntuación 0-100 por empresa, referencias y fechas, con el motivo.

        Un candidato único parte de 30: el importe exacto ya es un indicio fuerte.
        """
        score = 30 if unique else 0
        reasons = [_("único apunte con ese importe")] if unique else []
        st_tokens = _tokens(st_text)
        st_compact = _compact(st_text)

        partner = aml.partner_id.commercial_partner_id
        if partner and st_line.partner_id.commercial_partner_id == partner:
            score += 40
            reasons.append(_("misma empresa"))
        elif partner and st_tokens & _tokens(partner.name):
            score += 30
            reasons.append(_("empresa en el concepto"))

        st_numbers = {n.lstrip("0") for n in re.findall(r"\d+", st_text)}
        refs = {
            aml.move_id.name, aml.move_id.ref, aml.move_id.payment_reference,
            aml.name, aml.ref,
        }
        for ref in filter(None, refs):
            if not re.search(r"\d", ref):
                continue  # «TRANSFERENCIA», «TRASPASO»… no identifican nada
            ref_c = _compact(ref)
            ref_digits = re.sub(r"\D", "", ref).lstrip("0")
            if (len(ref_c) >= 5 and ref_c in st_compact) or (
                len(ref_digits) >= 4 and ref_digits in st_numbers
            ):
                score += 35
                reasons.append(_("referencia %s", ref))
                break

        ref_date = aml.date_maturity or aml.date
        days = (st_line.date - ref_date).days
        if days >= 0:
            score += round(25 * max(0.0, 1 - days / 60.0))
            reasons.append(_("banco %s días después del vencimiento", days))
        else:
            score += round(10 * max(0.0, 1 + days / 30.0))
            reasons.append(_("banco %s días antes del vencimiento", -days))
        return min(score, 100), ", ".join(reasons)

    def action_search(self):
        self.ensure_one()
        self.line_ids.unlink()
        currency = self.company_id.currency_id
        st_lines = self._get_statement_lines()
        if not st_lines:
            raise UserError(_("No hay movimientos bancarios pendientes con esos filtros."))

        pool = defaultdict(lambda: self.env["account.move.line"])
        for aml in self.env["account.move.line"].search(self._get_candidate_domain()):
            pool[currency.round(aml.amount_residual)] |= aml

        # Para cada movimiento: candidatos con el mismo importe, puntuados.
        proposals = []
        without = 0
        for st in st_lines:
            amount = currency.round(self._st_amount(st))
            cands = pool.get(amount, self.env["account.move.line"]).filtered(
                lambda m, st=st: m.move_id != st.move_id
            )
            if not cands:
                without += 1
                continue
            st_text = self._st_text(st)
            scored = sorted(
                ((aml, *self._score(st, st_text, aml, unique=len(cands) == 1))
                 for aml in cands),
                key=lambda x: (-x[1], abs((st.date - (x[0].date_maturity or x[0].date)).days)),
            )
            proposals.append({"st": st, "text": st_text, "scored": scored, "ai": None})

        ai_error = False
        multi = [p for p in proposals if len(p["scored"]) > 1]
        if self.use_ai and multi:
            try:
                self._ai_rank(multi)
            except Exception as e:  # noqa: BLE001 - se informa en el resumen y se sigue
                _logger.exception("Casación bancaria: fallo en la llamada a la IA")
                ai_error = str(e)

        # Asignación sin repetir apuntes: primero los de un solo candidato,
        # después los más seguros.
        def priority(p):
            best = p["ai"]["confianza"] if p["ai"] else p["scored"][0][1]
            return (len(p["scored"]) > 1, -best)

        used = set()
        line_vals = []
        for p in sorted(proposals, key=priority):
            st = p["st"]
            chosen, score, reason, source = False, 0, "", "heuristica"
            ai_rejected = False
            if len(p["scored"]) == 1:
                source = "unico"
            ai = p["ai"]
            if ai and ai.get("apunte_id") and ai["apunte_id"] not in used:
                match = [x for x in p["scored"] if x[0].id == ai["apunte_id"]]
                if match:
                    chosen = match[0][0]
                    score = ai["confianza"]
                    reason = "IA: %s" % (ai.get("motivo") or "")
                    source = "ia"
            if not chosen and ai and not ai.get("apunte_id"):
                ai_rejected = True
                reason = "IA: %s" % (ai.get("motivo") or _("ningún candidato encaja"))
            if not chosen:
                for aml, sc, rs in p["scored"]:
                    if aml.id not in used:
                        chosen, score = aml, sc
                        reason = "; ".join(filter(None, [reason, rs]))
                        break
            if chosen:
                used.add(chosen.id)
            else:
                reason = _("Todos los candidatos ya están propuestos para otros movimientos.")
            line_vals.append(
                {
                    "wizard_id": self.id,
                    "statement_line_id": st.id,
                    "st_amount": self._st_amount(st),
                    "candidate_ids": [(6, 0, [x[0].id for x in p["scored"]])],
                    "move_line_id": chosen and chosen.id,
                    "selected": bool(chosen) and not ai_rejected
                    and score >= self.min_score,
                    "score": score,
                    "reason": reason,
                    "source": source,
                }
            )
        self.env["casacion.bancaria.wizard.line"].create(line_vals)

        n_multi = len(multi)
        summary = [
            _("Movimientos bancarios pendientes revisados: %s", len(st_lines)),
            _("Con propuesta: %s (con varios candidatos: %s)", len(proposals), n_multi),
            _("Sin ningún apunte del mismo importe: %s", without),
        ]
        if self.use_ai and n_multi:
            summary.append(
                _("ChatGPT no disponible, se ha usado la puntuación de Odoo: %s", ai_error)
                if ai_error
                else _("ChatGPT ha elegido entre candidatos (prompt: %s v%s).",
                       self.prompt_id.name or "-", self.prompt_id.version or "-")
            )
        self.write({"state": "propuestas", "summary": "\n".join(summary)})
        return self._reopen()

    # ------------------------------------------------------------------
    # IA
    # ------------------------------------------------------------------
    def _get_openai_credentials(self):
        icp = self.env["ir.config_parameter"].sudo()
        api_key = (
            icp.get_param("casacion_bancaria_automatica.openai_api_key")
            or icp.get_param("xtendoo_invoice_ai.openai_api_key")
            or os.environ.get("OPENAI_API_KEY")
        )
        if not api_key:
            raise UserError(
                _(
                    "No hay clave API de OpenAI. Configúrela en Ajustes → Facturación → "
                    "Casación bancaria automática."
                )
            )
        return {
            "api_key": api_key,
            "base_url": icp.get_param("casacion_bancaria_automatica.openai_base")
            or icp.get_param("xtendoo_invoice_ai.openai_base")
            or None,
            "model": icp.get_param("casacion_bancaria_automatica.openai_model")
            or "gpt-4o-mini",
        }

    def _ai_payload(self, proposals):
        data = []
        for p in proposals:
            st = p["st"]
            data.append(
                {
                    "id": st.id,
                    "fecha": str(st.date),
                    "importe": self._st_amount(st),
                    "diario": st.journal_id.name,
                    "concepto": st.payment_ref or "",
                    "detalle": (p["text"] or "")[:500],
                    "empresa": st.partner_id.name or st.partner_name or "",
                    "candidatos": [
                        {
                            "id": aml.id,
                            "fecha": str(aml.date),
                            "vencimiento": str(aml.date_maturity or aml.date),
                            "asiento": aml.move_id.name,
                            "tipo": aml.move_id.move_type,
                            "referencia": aml.move_id.ref or aml.move_id.payment_reference or "",
                            "etiqueta": aml.name or "",
                            "empresa": aml.partner_id.name or "",
                            "cuenta": "%s %s" % (aml.account_id.code, aml.account_id.name),
                            "pendiente": aml.amount_residual,
                            "puntuacion_heuristica": score,
                        }
                        for aml, score, _reason in p["scored"][:AI_MAX_CANDIDATES]
                    ],
                }
            )
        return {"movimientos_bancarios": data}

    def _ai_rank(self, proposals):
        if not OpenAI:
            raise UserError(_("La librería openai no está instalada."))
        creds = self._get_openai_credentials()
        client = OpenAI(api_key=creds["api_key"], base_url=creds["base_url"], timeout=90)
        if self.prompt_id:
            system_prompt = self.prompt_id.prompt_text + RESPONSE_FORMAT
        else:
            system_prompt = self.env["casacion.bancaria.prompt"]._get_prompt_text(
                self.company_id
            )
        by_id = {p["st"].id: p for p in proposals}
        for i in range(0, len(proposals), AI_BATCH_SIZE):
            batch = proposals[i : i + AI_BATCH_SIZE]
            payload = json.dumps(self._ai_payload(batch), ensure_ascii=False, default=str)
            response = client.chat.completions.create(
                model=creds["model"],
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": payload},
                ],
            )
            content = response.choices[0].message.content or "{}"
            _logger.info(
                "Casación bancaria IA: %s movimientos, tokens %s",
                len(batch),
                response.usage and response.usage.total_tokens,
            )
            result = json.loads(content)
            for item in result.get("propuestas", []):
                try:
                    st_id = int(item.get("movimiento_bancario_id"))
                except (TypeError, ValueError):
                    continue
                p = by_id.get(st_id)
                if not p:
                    continue
                aml_id = item.get("apunte_id")
                try:
                    aml_id = int(aml_id) if aml_id else None
                except (TypeError, ValueError):
                    aml_id = None
                if aml_id and aml_id not in {x[0].id for x in p["scored"]}:
                    aml_id = None  # la IA no puede inventar apuntes
                try:
                    confidence = max(0, min(100, int(item.get("confianza") or 0)))
                except (TypeError, ValueError):
                    confidence = 0
                p["ai"] = {
                    "apunte_id": aml_id,
                    "confianza": confidence,
                    "motivo": str(item.get("motivo") or "")[:250],
                }

    # ------------------------------------------------------------------
    # Botones
    # ------------------------------------------------------------------
    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_back(self):
        self.line_ids.unlink()
        self.write({"state": "filtros", "summary": False})
        return self._reopen()

    def action_select_all(self):
        self.line_ids.filtered(
            lambda line: line.move_line_id and line.state == "pendiente"
        ).write({"selected": True})

    def action_unselect_all(self):
        self.line_ids.write({"selected": False})

    def action_reconcile(self):
        self.ensure_one()
        lines = self.line_ids.filtered(lambda line: line.selected and line.state == "pendiente")
        if not lines:
            raise UserError(_("No hay ninguna propuesta marcada."))
        dup = defaultdict(list)
        for line in lines.filtered("move_line_id"):
            dup[line.move_line_id.id].append(line)
        repeated = [ls for ls in dup.values() if len(ls) > 1]
        if repeated:
            raise UserError(
                _(
                    "El apunte %s está elegido en más de un movimiento bancario. "
                    "Corríjalo antes de conciliar.",
                    repeated[0][0].move_line_id.display_name,
                )
            )
        ok = 0
        for line in lines:
            try:
                with self.env.cr.savepoint():
                    line._reconcile()
                line.write({"state": "conciliado", "selected": False, "error": False})
                ok += 1
            except Exception as e:  # noqa: BLE001 - se muestra en la línea
                _logger.warning("Casación bancaria: no se pudo conciliar %s: %s",
                                line.statement_line_id.id, e)
                line.write({"state": "error", "error": str(e)})
        errors = len(lines) - ok
        self.summary = "%s\n%s" % (
            self.summary or "",
            _("Conciliados: %s. Con error: %s.", ok, errors),
        )
        return self._reopen()


class CasacionBancariaWizardLine(models.TransientModel):
    _name = "casacion.bancaria.wizard.line"
    _description = "Propuesta de casación bancaria"
    _order = "candidate_count, st_date, id"

    wizard_id = fields.Many2one("casacion.bancaria.wizard", required=True, ondelete="cascade")
    selected = fields.Boolean(string="Conciliar")
    state = fields.Selection(
        [("pendiente", "Pendiente"), ("conciliado", "Conciliado"), ("error", "Error")],
        default="pendiente",
        required=True,
        string="Estado",
    )
    error = fields.Char(string="Error")
    statement_line_id = fields.Many2one(
        "account.bank.statement.line", string="Movimiento bancario", required=True,
        ondelete="cascade",
    )
    st_date = fields.Date(related="statement_line_id.date", string="Fecha banco", store=True)
    st_journal_id = fields.Many2one(related="statement_line_id.journal_id", string="Diario")
    st_ref = fields.Char(related="statement_line_id.payment_ref", string="Concepto")
    st_partner_id = fields.Many2one(related="statement_line_id.partner_id", string="Empresa banco")
    currency_id = fields.Many2one(related="wizard_id.company_id.currency_id")
    st_amount = fields.Monetary(string="Importe", currency_field="currency_id")
    candidate_ids = fields.Many2many(
        "account.move.line",
        "casacion_bancaria_line_candidate_rel",
        "line_id",
        "move_line_id",
        string="Candidatos",
    )
    candidate_count = fields.Integer(
        string="Nº candidatos", compute="_compute_candidate_count", store=True
    )
    move_line_id = fields.Many2one(
        "account.move.line",
        string="Apunte a casar",
        domain="[('id', 'in', candidate_ids)]",
    )
    aml_date = fields.Date(related="move_line_id.date", string="Fecha apunte")
    aml_maturity = fields.Date(related="move_line_id.date_maturity", string="Vencimiento")
    aml_move_id = fields.Many2one(related="move_line_id.move_id", string="Asiento")
    aml_partner_id = fields.Many2one(related="move_line_id.partner_id", string="Empresa apunte")
    aml_account_id = fields.Many2one(related="move_line_id.account_id", string="Cuenta")
    score = fields.Integer(string="Confianza")
    reason = fields.Char(string="Motivo")
    source = fields.Selection(
        [("unico", "Único"), ("heuristica", "Odoo"), ("ia", "ChatGPT")],
        string="Origen",
    )

    @api.depends("candidate_ids")
    def _compute_candidate_count(self):
        for line in self:
            line.candidate_count = len(line.candidate_ids)

    @api.onchange("move_line_id")
    def _onchange_move_line_id(self):
        if self.move_line_id:
            self.selected = True
            if self.source and self.source != "unico":
                self.reason = _("Elegido a mano")
                self.source = False
        else:
            self.selected = False

    def action_show_candidates(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Candidatos de %s", self.statement_line_id.payment_ref or ""),
            "res_model": "account.move.line",
            "view_mode": "list",
            "domain": [("id", "in", self.candidate_ids.ids)],
            "target": "new",
            "context": {"create": False, "edit": False, "delete": False},
        }

    def _reconcile(self):
        """Concilia el movimiento bancario con el apunte usando account_reconcile_oca."""
        self.ensure_one()
        st = self.statement_line_id
        aml = self.move_line_id
        if not aml:
            raise UserError(_("No hay apunte elegido."))
        if st.is_reconciled:
            raise UserError(_("El movimiento bancario ya está conciliado."))
        if aml.reconciled or aml.company_currency_id.is_zero(aml.amount_residual):
            raise UserError(_("El apunte ya está conciliado."))
        _liquidity, _suspense, other = st._seek_for_lines()
        if other:
            raise UserError(
                _("El movimiento bancario ya tiene contrapartidas; concílielo a mano.")
            )
        if aml.company_currency_id.compare_amounts(
            aml.amount_residual, self.wizard_id._st_amount(st)
        ):
            raise UserError(_("El importe pendiente del apunte ya no coincide."))
        if not st.partner_id and aml.partner_id:
            st.partner_id = aml.partner_id
        st.reconcile_data_info = st._default_reconcile_data(from_unreconcile=True)
        st._add_account_move_line(aml)
        if not st.can_reconcile:
            raise UserError(_("Odoo no permite conciliar esta combinación."))
        st.reconcile_bank_line()
        st.invalidate_recordset(["is_reconciled"])
        if not st.is_reconciled:
            raise UserError(_("La conciliación no ha quedado completa."))
