# © 2026 Tetra Consultores
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import json
import logging
import os
import re
import unicodedata
from collections import defaultdict

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import formatLang

from ..models.casacion_prompt import RESPONSE_FORMAT

_logger = logging.getLogger(__name__)

AI_BATCH_SIZE = 15  # movimientos bancarios por llamada a la IA
AI_MAX_CANDIDATES = 15  # candidatos por movimiento que se envían a la IA
# Confianza máxima de la IA cuando Odoo no ve en el concepto ni la empresa ni la referencia
# del apunte elegido: así esa propuesta nunca se marca sola para conciliar.
AI_MAX_SCORE_WITHOUT_EVIDENCE = 40
MIN_TOKEN_LEN = 4
PARTIAL_WORDS = {"anticipo", "parcial", "senal", "provision", "adelanto", "plazo"}
PARTIAL_PHRASES = ("a cuenta", "pago cuenta", "entrega cuenta")
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


def _partial_hint(text):
    """¿El concepto habla de anticipo, pago a cuenta, parcial…?"""
    norm = _norm(text)
    words = set(re.split(r"[^a-z0-9]+", norm))
    return bool(words & PARTIAL_WORDS) or any(ph in norm for ph in PARTIAL_PHRASES)


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
    used_aml_ids = fields.Many2many(
        "account.move.line",
        compute="_compute_used_aml_ids",
        string="Apuntes ya elegidos",
    )

    def _compute_display_name(self):
        # En la ruta de navegación, en vez de «casacion.bancaria.wizard,9».
        for wizard in self:
            wizard.display_name = _("Casación bancaria automática")

    @api.depends("line_ids.move_line_ids", "line_ids.state")
    def _compute_used_aml_ids(self):
        for wizard in self:
            wizard.used_aml_ids = wizard.line_ids.filtered(
                lambda line: line.state == "pendiente"
            ).move_line_ids

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
    def _partner_match(self, st_line, st_text, aml):
        """«misma» si el movimiento tiene la empresa del apunte, «concepto» si su nombre
        aparece en el texto del banco, o False."""
        partner = aml.partner_id.commercial_partner_id
        if not partner:
            return False
        if st_line.partner_id.commercial_partner_id == partner:
            return "misma"
        if _tokens(st_text) & _tokens(partner.name):
            return "concepto"
        return False

    @api.model
    def _ref_match(self, st_text, aml):
        """Referencia del apunte (número de factura, ref. del proveedor…) que aparece en
        el texto del banco, o False."""
        st_compact = _compact(st_text)
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
                return ref
        return False

    @api.model
    def _has_evidence(self, st_line, st_text, aml):
        """¿Hay algo en el movimiento, aparte del importe, que señale a este apunte?"""
        return bool(
            self._partner_match(st_line, st_text, aml) or self._ref_match(st_text, aml)
        )

    @api.model
    def _score(self, st_line, st_text, aml, unique=False):
        """Puntuación 0-100 por empresa, referencias y fechas, con el motivo.

        Un candidato único parte de 30: el importe exacto ya es un indicio fuerte.
        """
        score = 30 if unique else 0
        reasons = [_("único apunte con ese importe")] if unique else []

        partner_match = self._partner_match(st_line, st_text, aml)
        if partner_match == "misma":
            score += 40
            reasons.append(_("misma empresa"))
        elif partner_match == "concepto":
            score += 30
            reasons.append(_("empresa en el concepto"))

        ref = self._ref_match(st_text, aml)
        if ref:
            score += 35
            reasons.append(_("referencia %s", ref))

        ref_date = aml.date_maturity or aml.date
        days = (st_line.date - ref_date).days
        if days >= 0:
            score += round(25 * max(0.0, 1 - days / 60.0))
            reasons.append(_("banco %s días después del vencimiento", days))
        else:
            score += round(10 * max(0.0, 1 + days / 30.0))
            reasons.append(_("banco %s días antes del vencimiento", -days))
        return min(score, 100), ", ".join(reasons)

    # ------------------------------------------------------------------
    # Opciones de casación
    # ------------------------------------------------------------------
    @api.model
    def _partners_in_text(self, st_line, st_text, partners):
        """Empresas (comerciales) del movimiento: la del propio movimiento y las que
        aparecen por su nombre en el concepto."""
        found = {}
        st_partner = st_line.partner_id.commercial_partner_id
        if st_partner:
            found[st_partner.id] = "misma"
        st_tokens = _tokens(st_text)
        for partner in partners:
            if partner.id not in found and st_tokens & _tokens(partner.name):
                found[partner.id] = "concepto"
        return found

    def _option(self, st_line, st_text, amls, kind, amount, unique=False):
        """Opción de casación ``amls`` para el movimiento, con puntuación y motivo."""
        currency = self.company_id.currency_id
        residual = sum(amls.mapped("amount_residual"))
        diff = currency.round(amount - residual)
        if len(amls) == 1:
            score, reason = self._score(st_line, st_text, amls, unique=unique)
        else:
            # Varias facturas: empresa + proporción de referencias que aparecen + fechas.
            score, reasons = 15, [_("%s facturas que suman el importe", len(amls))]
            partner_match = self._partner_match(st_line, st_text, amls[0])
            if partner_match == "misma":
                score += 40
                reasons.append(_("misma empresa"))
            elif partner_match == "concepto":
                score += 30
                reasons.append(_("empresa en el concepto"))
            refs = [r for r in (self._ref_match(st_text, aml) for aml in amls) if r]
            if refs:
                score += round(35 * len(refs) / len(amls))
                reasons.append(_("referencias %s", ", ".join(refs)))
            days = [(st_line.date - (a.date_maturity or a.date)).days for a in amls]
            if all(d >= -5 for d in days):
                score += 10
            score -= 3 * max(0, len(amls) - 2)
            reason = ", ".join(reasons)
        if kind == "tolerancia":
            score -= 10
            reason = _("diferencia %(diff)s dentro del margen; %(reason)s",
                       diff=formatLang(self.env, diff, currency_obj=currency), reason=reason)
        elif kind == "varias" and not currency.is_zero(diff):
            score -= 5
            reason = _("%(reason)s; diferencia %(diff)s dentro del margen",
                       reason=reason, diff=formatLang(self.env, diff, currency_obj=currency))
        return {
            "amls": amls,
            "kind": kind,
            "score": max(0, min(100, score)),
            "reason": reason,
            "diff": 0.0 if kind == "parcial" else diff,
        }

    @api.model
    def _subset_sums(self, amls, amount, tolerance, max_items, limit=20):
        """Combinaciones de 2..max_items apuntes cuya suma pendiente esté a ± tolerancia
        del importe. Se trabaja en céntimos y con poda por cota."""
        items = sorted(
            ((round(a.amount_residual * 100), a) for a in amls),
            key=lambda x: -abs(x[0]),
        )
        target, tol = round(amount * 100), round(tolerance * 100)
        values = [v for v, _a in items]
        # suffix[i] = suma de los valores desde i (todos tienen el mismo signo)
        suffix = [0] * (len(values) + 1)
        for i in range(len(values) - 1, -1, -1):
            suffix[i] = suffix[i + 1] + values[i]
        sign = 1 if target > 0 else -1
        results, nodes = [], [0]

        def dfs(start, total, chosen):
            nodes[0] += 1
            if len(results) >= limit or nodes[0] > 200000:
                return
            if len(chosen) >= 2 and abs(total - target) <= tol:
                results.append([items[i][1] for i in chosen])
            if len(chosen) >= max_items:
                return
            for i in range(start, len(values)):
                new_total = total + values[i]
                if sign * new_total > sign * target + tol:
                    continue  # se pasa: probar valores más pequeños
                if sign * (total + suffix[i]) < sign * target - tol:
                    return  # ni sumando todos los que quedan se llega
                dfs(i + 1, new_total, chosen + [i])

        dfs(0, 0, [])
        return [self.env["account.move.line"].union(*r) for r in results]

    def _assign_partial_pairs(self, proposals, chosen_by_st, used, by_partner, partners):
        """Anticipo + resto: dos movimientos sin propuesta de la misma empresa que juntos
        suman el pendiente de una factura. Se exige que alguno traiga la referencia de
        la factura o que el primero hable de anticipo/pago a cuenta, salvo que solo haya
        una factura posible."""
        company = self.company_id
        currency = company.currency_id
        AML = self.env["account.move.line"]
        pending = [p for p in sorted(proposals, key=lambda x: (x["st"].date, x["st"].id))
                   if not (chosen_by_st.get(p["st"].id) or (None,))[0]]
        found = {p["st"].id: self._partners_in_text(p["st"], p["text"], partners)
                 for p in pending}
        paired = set()
        for i, p1 in enumerate(pending):
            if p1["st"].id in paired or not found[p1["st"].id]:
                continue
            best = None
            for p2 in pending[i + 1:]:
                if p2["st"].id in paired:
                    continue
                common = set(found[p1["st"].id]) & set(found[p2["st"].id])
                total = currency.round(p1["amount"] + p2["amount"])
                tol = company._casacion_tolerance(total)
                cands = []
                for partner_id in common:
                    for aml in by_partner.get(partner_id, AML):
                        if (aml.id in used or aml.amount_residual * total <= 0
                                or abs(aml.amount_residual - total) > tol + 0.005
                                or abs(p1["amount"]) >= abs(aml.amount_residual)):
                            continue
                        has_ref = bool(self._ref_match(p1["text"], aml)
                                       or self._ref_match(p2["text"], aml))
                        cands.append((2 if has_ref else (1 if _partial_hint(p1["text"]) else 0),
                                      aml))
                if not cands:
                    continue
                cands.sort(key=lambda c: -c[0])
                strength, aml = cands[0]
                if strength == 0 and len(cands) > 1:
                    continue  # varias facturas posibles y nada que las distinga
                if len(cands) > 1 and cands[1][0] == strength:
                    continue  # empate
                if not best or strength > best[0]:
                    best = (strength, aml, p2)
            if not best:
                continue
            strength, aml, p2 = best
            bonus = 10 * strength
            o1 = self._option(p1["st"], p1["text"], aml, "parcial", p1["amount"])
            o1["score"] = max(0, min(100, o1["score"] - 5 + bonus))
            o1["reason"] = _("anticipo: con el movimiento del %(date)s suma la factura; %(r)s",
                             date=p2["st"].date, r=o1["reason"])
            o2 = self._option(p2["st"], p2["text"], aml, "resto", p2["amount"])
            o2["diff"] = currency.round(p1["amount"] + p2["amount"] - aml.amount_residual)
            o2["score"] = max(0, min(100, o2["score"] + bonus))
            o2["reason"] = _("resto de la factura tras el anticipo del %(date)s; %(r)s",
                             date=p1["st"].date, r=o2["reason"])
            for p, o in ((p1, o1), (p2, o2)):
                p["options"].append(o)
                chosen_by_st[p["st"].id] = (o, o["score"], o["reason"], "heuristica", False)
                paired.add(p["st"].id)
            used.add(aml.id)

    def action_search(self):
        self.ensure_one()
        self.line_ids.unlink()
        company = self.company_id
        currency = company.currency_id
        AML = self.env["account.move.line"]
        st_lines = self._get_statement_lines()
        if not st_lines:
            raise UserError(_("No hay movimientos bancarios pendientes con esos filtros."))

        all_amls = AML.search(self._get_candidate_domain())
        pool = defaultdict(lambda: AML)
        by_partner = defaultdict(lambda: AML)
        for aml in all_amls:
            pool[currency.round(aml.amount_residual)] |= aml
            if aml.partner_id:
                by_partner[aml.partner_id.commercial_partner_id.id] |= aml
        partners = self.env["res.partner"].browse(list(by_partner))
        max_items = max(1, company.casacion_max_invoices or 1)

        # 1) Opciones por movimiento: exactas, con margen y varias facturas.
        proposals = []
        for st in st_lines:
            amount = currency.round(self._st_amount(st))
            tol = company._casacion_tolerance(amount)
            st_text = self._st_text(st)
            p = {"st": st, "text": st_text, "amount": amount, "tol": tol,
                 "options": [], "scored": [], "ai": None}
            exact = pool.get(amount, AML).filtered(lambda m, st=st: m.move_id != st.move_id)
            unique = len(exact) == 1
            for aml in exact:
                p["options"].append(self._option(st, st_text, aml, "exacto", amount, unique))
            if not exact and tol:
                # Con margen solo si el concepto señala al apunte (empresa o referencia):
                # sin eso, un importe parecido no es prueba de nada.
                near = all_amls.filtered(
                    lambda m, a=amount, t=tol, st=st, tx=st_text: m.move_id != st.move_id
                    and m.amount_residual * a > 0
                    and abs(m.amount_residual - a) <= t + 0.005
                    and self._has_evidence(st, tx, m)
                )
                for aml in near:
                    p["options"].append(
                        self._option(st, st_text, aml, "tolerancia", amount, len(near) == 1))
            if not p["options"] and max_items > 1:
                for partner_id in self._partners_in_text(st, st_text, partners):
                    amls = by_partner.get(partner_id, AML).filtered(
                        lambda m, a=amount, t=tol: m.amount_residual * a > 0
                        and abs(m.amount_residual) < abs(a) + t
                    ).sorted(lambda m: m.date_maturity or m.date, reverse=True)[:25]
                    for combo in self._subset_sums(amls, amount, tol, max_items):
                        p["options"].append(
                            self._option(st, st_text, combo, "varias", amount))
            p["options"].sort(key=lambda o: (-o["score"], sum(
                abs((st.date - (a.date_maturity or a.date)).days) for a in o["amls"])))
            p["scored"] = [(o["amls"], o["score"], o["reason"])
                           for o in p["options"] if o["kind"] == "exacto"]
            proposals.append(p)

        ai_error = False
        multi = [p for p in proposals if len(p["scored"]) > 1]
        if self.use_ai and multi:
            try:
                self._ai_rank(multi)
            except Exception as e:  # noqa: BLE001 - se informa en el resumen y se sigue
                _logger.exception("Casación bancaria: fallo en la llamada a la IA")
                ai_error = str(e)

        # 2) Asignación sin repetir apuntes: primero los de una sola opción, después
        #    los más seguros.
        def priority(p):
            if not p["options"]:
                return (2, 0)
            best = p["ai"]["confianza"] if p["ai"] else p["options"][0]["score"]
            return (len(p["options"]) > 1, -best)

        used = set()
        chosen_by_st = {}
        for p in sorted(proposals, key=priority):
            st = p["st"]
            chosen, score, reason, source, ai_rejected = None, 0, "", "heuristica", False
            if len(p["options"]) == 1:
                source = "unico"
            ai = p["ai"]
            if ai and ai.get("apunte_id") and ai["apunte_id"] not in used:
                match = [o for o in p["options"]
                         if o["kind"] == "exacto" and o["amls"].id == ai["apunte_id"]]
                if match:
                    chosen = match[0]
                    score = ai["confianza"]
                    reason = "IA: %s" % (ai.get("motivo") or "")
                    source = "ia"
                    if score > AI_MAX_SCORE_WITHOUT_EVIDENCE and not self._has_evidence(
                        st, p["text"], chosen["amls"]
                    ):
                        score = AI_MAX_SCORE_WITHOUT_EVIDENCE
                        reason += _(" [Confianza limitada: el concepto no menciona ni la "
                                    "empresa ni la referencia; revisar a mano]")
            if not chosen and ai and not ai.get("apunte_id"):
                ai_rejected = True
                reason = "IA: %s" % (ai.get("motivo") or _("ningún candidato encaja"))
            if not chosen:
                for opt in p["options"]:
                    if not set(opt["amls"].ids) & used:
                        chosen, score = opt, opt["score"]
                        reason = "; ".join(filter(None, [reason, opt["reason"]]))
                        break
            if chosen:
                used.update(chosen["amls"].ids)
                chosen_by_st[st.id] = (chosen, score, reason, source, ai_rejected)
            elif p["options"]:
                chosen_by_st[st.id] = (None, 0, _(
                    "Todos los candidatos ya están propuestos para otros movimientos."),
                    source, False)

        # 3) Pagos parciales (anticipo y resto), por orden de fecha.
        partial_left = {}  # aml.id -> pendiente que queda tras los parciales propuestos
        if company.casacion_allow_partial:
            self._assign_partial_pairs(proposals, chosen_by_st, used, by_partner, partners)
            for p in sorted(proposals, key=lambda x: (x["st"].date, x["st"].id)):
                st, amount, tol = p["st"], p["amount"], p["tol"]
                if chosen_by_st.get(p["st"].id, (None,))[0]:
                    continue
                found = self._partners_in_text(st, p["text"], partners)
                if not found:
                    continue
                opt = None
                # Resto de una factura con un parcial propuesto antes en esta búsqueda.
                for aml_id, left in partial_left.items():
                    aml = AML.browse(aml_id)
                    if (aml.partner_id.commercial_partner_id.id in found
                            and abs(left - amount) <= tol + 0.005):
                        opt = self._option(st, p["text"], aml, "resto", amount)
                        opt["diff"] = currency.round(amount - left)
                        opt["reason"] = _("resto de la factura tras el anticipo; %s",
                                          opt["reason"])
                        partial_left.pop(aml_id)
                        break
                if not opt:
                    cands = AML
                    for partner_id in found:
                        cands |= by_partner.get(partner_id, AML).filtered(
                            lambda m, a=amount, t=tol: m.id not in used
                            and m.id not in partial_left
                            and m.amount_residual * a > 0
                            and abs(m.amount_residual) > abs(a) + t
                        )
                    keyword = _partial_hint(p["text"])
                    scored = []
                    for aml in cands:
                        o = self._option(st, p["text"], aml, "parcial", amount)
                        has_ref = bool(self._ref_match(p["text"], aml))
                        if not (has_ref or keyword or len(cands) == 1):
                            continue
                        o["score"] = max(0, min(100, o["score"] - 15 + (10 if keyword else 0)))
                        o["reason"] = _("pago parcial (quedan %(left)s); %(reason)s",
                                        left=formatLang(self.env, aml.amount_residual - amount,
                                                        currency_obj=currency),
                                        reason=o["reason"])
                        scored.append(o)
                    scored.sort(key=lambda o: -o["score"])
                    if scored:
                        opt = scored[0]
                        aml = opt["amls"]
                        partial_left[aml.id] = currency.round(aml.amount_residual - amount)
                        p["options"] += scored
                if opt:
                    chosen_by_st[st.id] = (opt, opt["score"], opt["reason"], "heuristica", False)
                    if opt["kind"] == "resto":
                        p["options"].append(opt)

        # 4) Líneas del asistente.
        line_vals = []
        without = 0
        counts = defaultdict(int)
        for p in proposals:
            st = p["st"]
            if st.id not in chosen_by_st:
                without += 1
                continue
            chosen, score, reason, source, ai_rejected = chosen_by_st[st.id]
            cand = AML.union(*[o["amls"] for o in p["options"]]) if p["options"] else AML
            if chosen:
                counts[chosen["kind"]] += 1
            line_vals.append({
                "wizard_id": self.id,
                "statement_line_id": st.id,
                "st_amount": self._st_amount(st),
                "candidate_ids": [(6, 0, cand.ids)],
                "move_line_id": chosen and chosen["amls"][:1].id,
                "move_line_ids": [(6, 0, chosen["amls"].ids if chosen else [])],
                "match_type": chosen and chosen["kind"],
                "difference": chosen and chosen["diff"] or 0.0,
                "selected": bool(chosen) and not ai_rejected and score >= self.min_score,
                "score": score,
                "reason": reason,
                "source": source,
            })
        self.env["casacion.bancaria.wizard.line"].create(line_vals)

        n_multi = len(multi)
        summary = [
            _("Movimientos bancarios pendientes revisados: %s", len(st_lines)),
            _("Con propuesta: %(n)s (exactas %(e)s, con margen %(t)s, varias facturas "
              "%(v)s, parciales %(p)s, restos %(r)s)",
              n=sum(counts.values()), e=counts["exacto"], t=counts["tolerancia"],
              v=counts["varias"], p=counts["parcial"], r=counts["resto"]),
            _("Sin ninguna propuesta: %s", without),
            _("Margen de error: %s", company._casacion_tolerance_label()),
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
            or icp.get_param("openai_api_key")
            or os.environ.get("OPENAI_API_KEY")
        )
        if not api_key:
            raise UserError(
                _(
                    "No hay clave API de OpenAI. Configúrela en Ajustes → Contabilidad → "
                    "Casación bancaria automática."
                )
            )
        return {
            "api_key": api_key,
            "base_url": icp.get_param("casacion_bancaria_automatica.openai_base")
            or icp.get_param("xtendoo_invoice_ai.openai_base")
            or "https://api.openai.com/v1",
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
                            "pruebas_odoo": _reason,
                            "dias_banco_tras_vencimiento": (
                                st.date - (aml.date_maturity or aml.date)
                            ).days,
                        }
                        for aml, score, _reason in p["scored"][:AI_MAX_CANDIDATES]
                    ],
                }
            )
        return {"movimientos_bancarios": data}

    @api.model
    def _ai_chat(self, creds, system_prompt, payload):
        """Llamada a chat/completions con requests (sin depender de la librería openai)."""
        response = requests.post(
            creds["base_url"].rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer %s" % creds["api_key"]},
            json={
                "model": creds["model"],
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": payload},
                ],
            },
            timeout=90,
        )
        if response.status_code != 200:
            raise UserError(
                _("OpenAI respondió %(code)s: %(text)s",
                  code=response.status_code, text=response.text[:300])
            )
        return response.json()

    def _ai_rank(self, proposals):
        creds = self._get_openai_credentials()
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
            response = self._ai_chat(creds, system_prompt, payload)
            content = response["choices"][0]["message"].get("content") or "{}"
            _logger.info(
                "Casación bancaria IA: %s movimientos, tokens %s",
                len(batch),
                (response.get("usage") or {}).get("total_tokens"),
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

    def _classify(self, st_amount, amls):
        """Tipo de casación y diferencia (banco - pendiente) al elegir ``amls`` a mano."""
        self.ensure_one()
        company = self.company_id
        currency = company.currency_id
        residual = sum(amls.mapped("amount_residual"))
        diff = currency.round(st_amount - residual)
        if currency.is_zero(diff):
            return "exacto" if len(amls) == 1 else "varias", 0.0
        if abs(diff) <= company._casacion_tolerance(st_amount) + 0.005:
            return "tolerancia" if len(amls) == 1 else "varias", diff
        if (company.casacion_allow_partial and len(amls) == 1
                and residual * st_amount > 0 and abs(st_amount) < abs(residual)):
            return "parcial", 0.0
        return "nocasa", diff

    def action_reconcile(self):
        self.ensure_one()
        lines = self.line_ids.filtered(lambda line: line.selected and line.state == "pendiente")
        if not lines:
            raise UserError(_("No hay ninguna propuesta marcada."))
        # Un apunte no puede ir en dos movimientos, salvo anticipo + resto.
        dup = defaultdict(list)
        for line in lines.filtered(lambda ln: ln.match_type not in ("parcial", "resto")):
            for aml in line.move_line_ids:
                dup[aml.id].append(line)
        repeated = [(aml_id, ls) for aml_id, ls in dup.items() if len(ls) > 1]
        if repeated:
            raise UserError(
                _(
                    "El apunte %s está elegido en más de un movimiento bancario. "
                    "Corríjalo antes de conciliar.",
                    self.env["account.move.line"].browse(repeated[0][0]).display_name,
                )
            )
        ok = 0
        # Por fecha: el anticipo antes que el resto.
        for line in lines.sorted(lambda ln: (ln.st_date, ln.statement_line_id.id)):
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
    move_line_ids = fields.Many2many(
        "account.move.line",
        "casacion_bancaria_line_chosen_rel",
        "line_id",
        "move_line_id",
        string="Apuntes a casar",
        help="Todas las facturas o apuntes que se concilian con el movimiento.",
    )
    match_type = fields.Selection(
        [
            ("exacto", "Exacto"),
            ("tolerancia", "Con margen"),
            ("varias", "Varias facturas"),
            ("parcial", "Parcial"),
            ("resto", "Resto"),
            ("nocasa", "No cuadra"),
        ],
        string="Tipo",
    )
    difference = fields.Monetary(
        string="Diferencia", currency_field="currency_id",
        help="Banco menos pendiente de los apuntes. Se lleva a la cuenta de diferencias.",
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
            if self.move_line_id not in self.move_line_ids or len(self.move_line_ids) != 1:
                self.move_line_ids = self.move_line_id
                self.match_type, self.difference = self.wizard_id._classify(
                    self.st_amount, self.move_line_id)
            self.selected = self.match_type != "nocasa"
            if self.source and self.source != "unico":
                self.reason = _("Elegido a mano")
                self.source = False
        else:
            self.move_line_ids = False
            self.match_type = False
            self.difference = 0.0
            self.selected = False

    def action_show_candidates(self):
        """Abre los candidatos puntuados para comparar y elegir uno con «Elegir»."""
        self.ensure_one()
        wizard = self.wizard_id
        st = self.statement_line_id
        st_text = wizard._st_text(st)
        unique = len(self.candidate_ids) == 1
        Candidate = self.env["casacion.bancaria.wizard.candidate"]
        Candidate.search([("line_id", "=", self.id)]).unlink()
        vals = []
        for aml in self.candidate_ids:
            score, reason = wizard._score(st, st_text, aml, unique=unique)
            if aml == self.move_line_id and self.source == "ia":
                reason = self.reason or reason
            vals.append({
                "line_id": self.id,
                "move_line_id": aml.id,
                "score": score,
                "reason": reason,
            })
        Candidate.create(vals)
        return {
            "type": "ir.actions.act_window",
            "name": _("Candidatos de %(ref)s (%(amount)s)",
                      ref=st.payment_ref or st.date,
                      amount=formatLang(self.env, self.st_amount,
                                        currency_obj=self.currency_id)),
            "res_model": "casacion.bancaria.wizard.candidate",
            "view_mode": "tree",
            "views": [(self.env.ref(
                "casacion_bancaria_automatica.view_casacion_bancaria_wizard_candidate_list"
            ).id, "tree")],
            "domain": [("line_id", "=", self.id)],
            "target": "new",
            "context": {"create": False, "edit": False, "delete": False},
        }

    def _set_move_line(self, aml):
        """Elige ``aml`` para esta línea.

        Si otra línea pendiente lo tenía elegido, se le da el apunte que tenía esta
        (si también es candidato suyo) o se queda sin apunte; en ambos casos se
        desmarca para que se revise.
        """
        self.ensure_one()
        if self.state != "pendiente":
            raise UserError(_("Esta propuesta ya no está pendiente."))
        if aml not in self.candidate_ids:
            raise UserError(_("El apunte no es candidato de este movimiento."))
        previous = self.move_line_id
        others = self.wizard_id.line_ids.filtered(
            lambda line: line != self and line.state == "pendiente"
            and aml in line.move_line_ids
        )
        match_type, difference = self.wizard_id._classify(self.st_amount, aml)
        self.write({
            "move_line_id": aml.id,
            "move_line_ids": [(6, 0, aml.ids)],
            "match_type": match_type,
            "difference": difference,
            "selected": match_type != "nocasa",
            "source": False,
            "reason": _("Elegido a mano"),
        })
        for other in others:
            if previous and previous != aml and previous in other.candidate_ids:
                p_type, p_diff = other.wizard_id._classify(other.st_amount, previous)
                other.write({
                    "move_line_id": previous.id,
                    "move_line_ids": [(6, 0, previous.ids)],
                    "match_type": p_type,
                    "difference": p_diff,
                    "selected": False,
                    "source": False,
                    "reason": _("Intercambiado con el movimiento %(ref)s del %(date)s; revisar",
                                ref=self.st_ref or "", date=self.st_date),
                })
            else:
                other.write({
                    "move_line_id": False,
                    "move_line_ids": [(5,)],
                    "match_type": False,
                    "difference": 0.0,
                    "selected": False,
                    "source": False,
                    "reason": _("Su apunte se ha elegido para el movimiento %(ref)s del %(date)s",
                                ref=self.st_ref or "", date=self.st_date),
                })

    def _reconcile(self):
        """Concilia el movimiento con los apuntes elegidos usando account_reconcile_oca.

        - exacto / varias / tolerancia / resto: los apuntes se cierran enteros y la
          diferencia (dentro del margen) va a la cuenta de diferencias de la empresa.
        - parcial: el último apunte se concilia solo por lo que queda del movimiento y
          la factura sigue abierta por el resto.
        """
        self.ensure_one()
        st = self.statement_line_id
        wizard = self.wizard_id
        company = wizard.company_id
        currency = company.currency_id
        amls = self.move_line_ids.sorted(lambda m: (m.date_maturity or m.date, m.id))
        if not amls:
            raise UserError(_("No hay apunte elegido."))
        if st.is_reconciled:
            raise UserError(_("El movimiento bancario ya está conciliado."))
        if any(a.reconciled or a.company_currency_id.is_zero(a.amount_residual) for a in amls):
            raise UserError(_("Alguno de los apuntes ya está conciliado."))
        _liquidity, _suspense, other = st._seek_for_lines()
        if other:
            raise UserError(
                _("El movimiento bancario ya tiene contrapartidas; concílielo a mano.")
            )
        st_amount = wizard._st_amount(st)
        kind, diff = wizard._classify(st_amount, amls)
        if self.match_type == "resto" and kind == "parcial":
            raise UserError(_("Concilie antes el anticipo de esta factura."))
        if kind == "nocasa":
            raise UserError(_("El importe pendiente de los apuntes ya no cuadra con el banco "
                              "(diferencia %s).",
                              formatLang(self.env, diff, currency_obj=currency)))
        partners = amls.partner_id.commercial_partner_id
        if not st.partner_id and len(partners) == 1:
            st.partner_id = partners

        info = st._default_reconcile_data(from_unreconcile=True)
        data = [line for line in info["data"] if line["kind"] != "suspense"]
        aux = info["reconcile_auxiliary_id"]
        for aml in amls:
            max_amount = False
            if kind == "parcial":
                pending = sum(line["amount"] for line in data)
                max_amount = currency.round(pending)
            aux, new_lines = st._get_reconcile_line(
                aml, "other", is_counterpart=True, max_amount=max_amount,
                reconcile_auxiliary_id=aux,
            )
            data += new_lines
        total = currency.round(sum(line["amount"] for line in data))
        if not currency.is_zero(total):
            if kind == "parcial":
                raise UserError(_("El pago parcial no ha quedado cuadrado."))
            amount = -total  # línea de diferencia: debe > 0 = gasto, haber > 0 = ingreso
            account = (company.casacion_writeoff_expense_account_id if amount > 0
                       else company.casacion_writeoff_income_account_id)
            if not account:
                raise UserError(_(
                    "Falta la cuenta de diferencias %s en Ajustes → Contabilidad → "
                    "Casación bancaria automática.",
                    _("en contra") if amount > 0 else _("a favor")))
            partner = st.partner_id
            data.append({
                "reference": "reconcile_auxiliary;%s" % aux,
                "id": False,
                "account_id": [account.id, account.display_name],
                "partner_id": partner and [partner.id, partner.display_name] or False,
                "date": fields.Date.to_string(st.date),
                "name": _("Diferencia de casación: %s", st.payment_ref or st.name or ""),
                "amount": amount,
                "net_amount": amount,
                "debit": amount if amount > 0 else 0.0,
                "credit": -amount if amount < 0 else 0.0,
                "kind": "other",
                "currency_id": currency.id,
                "line_currency_id": currency.id,
                "currency_amount": amount,
            })
            aux += 1
        st.reconcile_data_info = st._recompute_suspense_line(data, aux, st.manual_reference)
        if not st.reconcile_data_info.get("can_reconcile"):
            raise UserError(_("Odoo no permite conciliar esta combinación."))
        st.reconcile_bank_line()
        st.invalidate_recordset(["is_reconciled"])
        if not st.is_reconciled:
            raise UserError(_("La conciliación no ha quedado completa."))


class CasacionBancariaWizardCandidate(models.TransientModel):
    _name = "casacion.bancaria.wizard.candidate"
    _description = "Candidato de casación bancaria"
    _order = "is_current desc, score desc, aml_maturity, id"

    line_id = fields.Many2one(
        "casacion.bancaria.wizard.line", required=True, ondelete="cascade"
    )
    move_line_id = fields.Many2one(
        "account.move.line", string="Apunte", required=True, ondelete="cascade"
    )
    currency_id = fields.Many2one(related="move_line_id.company_currency_id")
    aml_date = fields.Date(related="move_line_id.date", string="Fecha")
    aml_maturity = fields.Date(related="move_line_id.date_maturity", string="Vencimiento",
                               store=True)
    aml_move_id = fields.Many2one(related="move_line_id.move_id", string="Asiento")
    aml_ref = fields.Char(related="move_line_id.move_id.ref", string="Referencia")
    aml_name = fields.Char(related="move_line_id.name", string="Etiqueta")
    aml_partner_id = fields.Many2one(related="move_line_id.partner_id", string="Empresa")
    aml_account_id = fields.Many2one(related="move_line_id.account_id", string="Cuenta")
    aml_residual = fields.Monetary(related="move_line_id.amount_residual",
                                   string="Pendiente", currency_field="currency_id")
    score = fields.Integer(string="Puntuación")
    reason = fields.Char(string="Motivo")
    is_current = fields.Boolean(string="Elegido", compute="_compute_is_current", store=True)
    used_line_id = fields.Many2one(
        "casacion.bancaria.wizard.line", string="Elegido en otro movimiento",
        compute="_compute_usage",
    )
    used_info = fields.Char(string="Ya propuesto para", compute="_compute_usage")

    @api.depends("line_id.move_line_id")
    def _compute_is_current(self):
        for cand in self:
            cand.is_current = cand.line_id.move_line_id == cand.move_line_id

    @api.depends("line_id.wizard_id.line_ids.move_line_id")
    def _compute_usage(self):
        for cand in self:
            line = cand.line_id
            other = line.wizard_id.line_ids.filtered(
                lambda ln, cand=cand: ln != line and ln.state == "pendiente"
                and ln.move_line_id == cand.move_line_id
            )[:1]
            cand.used_line_id = other
            cand.used_info = other and "%s · %s" % (other.st_date, other.st_ref or "") or False

    def action_choose(self):
        self.ensure_one()
        self.line_id._set_move_line(self.move_line_id)
        return {"type": "ir.actions.act_window_close"}
