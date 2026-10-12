"""Límites M8; cuantización exacta de entradas y redondeo solo de importes."""

from decimal import MAX_EMAX, MIN_EMIN, ROUND_HALF_UP, Context, Decimal, DecimalException, localcontext

from django.core.exceptions import ValidationError

MONEY_CONTEXT = Context(prec=64, rounding=ROUND_HALF_UP, Emax=MAX_EMAX, Emin=MIN_EMIN, clamp=0)
MAX_ITEMS = 500


def decimal_limit(value, digits, places, *, minimum=Decimal("0"), maximum=None):
    if not isinstance(value, Decimal):
        raise ValidationError("Los importes deben ser Decimal, nunca float.")
    if not value.is_finite() or value < minimum or (maximum is not None and value > maximum):
        raise ValidationError("Valor decimal fuera del rango permitido.")
    # Comprobar magnitud antes de cuantizar: también evita exponentes desmesurados.
    if value and value.adjusted() >= digits - places:
        raise ValidationError("Demasiados dígitos enteros.")
    try:
        with localcontext(MONEY_CONTEXT):
            exact = value.quantize(Decimal((0, (1,), -places)))
    except DecimalException as exc:
        raise ValidationError("Decimal no representable.") from exc
    if exact != value:
        raise ValidationError(f"Máximo {places} decimales significativos; no se redondea al guardar.")
    return exact


def amount(value):
    with localcontext(MONEY_CONTEXT):
        rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return decimal_limit(rounded, 38, 2)
