# Casación bancaria automática para Odoo

Asistente que propone y concilia movimientos bancarios pendientes con apuntes contables del mismo importe. Si hay varios candidatos, propone el que más se parece (empresa, referencias, fechas) y, opcionalmente, deja elegir a ChatGPT con un prompt versionado y editable desde Odoo.

Una rama por versión de Odoo: `12.0`, `16.0`, `17.0`, `18.0`.

| Rama | Conciliación | Dependencias |
|---|---|---|
| 18.0 / 17.0 / 16.0 | `account_reconcile_oca` (OCA/account-reconcile) | `account_reconcile_oca`, librería Python `openai` |
| 12.0 | Widget estándar de Odoo 12 (`process_reconciliation`) | solo `account` (la API se llama con `requests`) |

## Instalación

```bash
cd /ruta/a/addons_extra
git clone -b <rama> <url-del-repo> casacion_bancaria_automatica_repo
# añadir /ruta/a/addons_extra/casacion_bancaria_automatica_repo al addons_path
pip install openai          # ramas 16.0, 17.0 y 18.0
odoo-bin -c <conf> -d <bd> -i casacion_bancaria_automatica --stop-after-init
```

Probar primero en una BD desechable y borrarla al terminar.

## Uso

- Contabilidad → Contabilidad → **Casación bancaria automática**: diarios, fechas de movimientos bancarios y de apuntes (vacías = todas), umbral de confianza para marcar, usar ChatGPT y versión del prompt.
- Ajustes → Facturación → **Casación bancaria automática (ChatGPT)**: clave API, modelo (por defecto `gpt-4o-mini`) y URL base.
- Facturación → Configuración → **Prompts IA de casación bancaria**: versiones del prompt. La versión en uso no se edita: «Nueva versión», editar la copia y «Usar esta versión». Para volver atrás se activa una versión anterior.
