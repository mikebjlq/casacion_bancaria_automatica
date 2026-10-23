# © 2026 Tetra Consultores
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

{
    "name": "Casación bancaria automática",
    "version": "12.0.1.0.0",
    "category": "Accounting",
    "author": "Tetra Consultores",
    "license": "AGPL-3",
    "summary": "Propone y concilia movimientos bancarios por importe, con ayuda de ChatGPT (Odoo 12)",
    "depends": [
        "account",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/casacion_prompt_data.xml",
        "views/casacion_prompt_views.xml",
        "views/res_config_settings_views.xml",
        "wizard/casacion_wizard_views.xml",
        "views/menus.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
