"""M10 · Punto de extensión de impuestos: única fuente de la regla fiscal vigente.

Regla vigente (C10, acordada con CCONOR):
- Una sola tasa de IVA por cotización; 16% inicial, editable con permiso.
- El IVA se calcula sobre el neto (subtotal menos descuento global) y se redondea
  una vez a centavos, HALF_UP.
- Un precio capturado «con IVA» declara su propia tasa; se le quita con esa tasa,
  no con la de la cotización.

Las tasas son fracciones (0.16 = 16%), de 0 a 1 con hasta 4 decimales. Todo es
Decimal: nunca float.

Fuera de alcance: exenciones, tasas por partida, retenciones y CFDI. Requieren
acuerdo con CCONOR y su área contable antes de programarse.

Cómo extender (detalle en docs/31-m10-impuestos.md):
1. Una regla nueva no cambia lo que ya calcula esta: se agrega aparte y se
   identifica con un `QuoteRevision.calculation_version` nuevo.
2. Las cotizaciones emitidas nunca se recalculan; cambiar su cálculo exige una
   revisión nueva.
3. Si se necesitan datos nuevos (p. ej. tasa por partida), van con migración y
   pruebas, sin alterar los originales guardados.

Este módulo es puro: sin ORM, HTTP ni permisos. Solo depende de snapshot_values.
"""

from decimal import Decimal, localcontext

from .snapshot_values import MONEY_CONTEXT, amount, decimal_limit

TASA_IVA_INICIAL = Decimal("0.16")
_UNO = Decimal("1")


def validar_tasa(valor):
    """Devuelve la tasa exacta (0 a 1, máximo 4 decimales) o lanza ValidationError."""
    return decimal_limit(valor, 5, 4, maximum=_UNO)


def calcular_iva(neto, tasa):
    """IVA de la cotización: neto × tasa, redondeado una vez a centavos (HALF_UP).

    `neto` ya viene en centavos; con tasa de 4 decimales el producto es exacto en
    MONEY_CONTEXT, así que solo hay un redondeo.
    """
    with localcontext(MONEY_CONTEXT):
        return amount(neto * validar_tasa(tasa))


def divisor_iva_incluido(tasa_incluida):
    """Divisor para obtener el precio sin IVA: 1 si no incluye IVA; si no, 1 + tasa."""
    if tasa_incluida is None:
        return _UNO
    return _UNO + validar_tasa(tasa_incluida)
