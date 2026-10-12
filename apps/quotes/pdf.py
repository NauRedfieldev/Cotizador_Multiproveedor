"""Generación del PDF oficial de cotización (reportlab).

ÚNICO renderer: borradores, emisiones y vía histórica. Descarga y correo de
emisiones reutilizan el archivo original, no llaman otra vez al renderer.

M8 adapta snapshots a este renderer; los PDF emitidos se descargan del archivo
original. get_quote_context es solo la vía histórica. Demo explícito, nunca
fallback por folio inexistente o fallo de BD.
"""

import io
import re
from copy import deepcopy
from decimal import Decimal
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (
    HRFlowable,
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .snapshot_values import amount

NAVY = colors.HexColor("#111638")
PURPLE = colors.HexColor("#7311F5")
MUTED = colors.HexColor("#5A5F7A")
LIGHT_BG = colors.HexColor("#F8F7FE")
LIGHT_BORDER = colors.HexColor("#EAE7F3")
DARK_RED = colors.HexColor("#C62828")

COMPANY = {
    "name": "CCONOR Soluciones Tecnológicas",
    "rfc": "CST-180422-9K2",
    "tagline": "Soluciones Integrales de Redes, Cómputo e Infraestructura TI",
    "phone": "+52 618-181-4359",
    "email": "ventas@cconor.com",
    "advisor": "Ing. Odalis Leal",
}

DEFAULT_CONDITIONS = [
    "Precios expresados en Pesos Mexicanos (MXN).",
    "Tiempo de entrega estimado: 2 a 4 días hábiles tras confirmación de pago.",
    "Ejemplo de garantía: confirmar con SYSCOM y Exel del Norte.",
    "Cuentas bancarias para depósito: BBVA Bancomer Clabe: 012028000123456789.",
]


def money(value):
    """Formato moneda estilo es-MX: $26,470.60 (sin el sufijo MXN)."""
    return f"${amount(Decimal(value)):,.2f}"


def unit_money(value):
    """Mostrar precisión completa de unitario convertido, al menos dos decimales."""
    text = f"{value:,.14f}".rstrip("0")
    integer, _, fraction = text.partition(".")
    return "$" + integer + "." + fraction.ljust(2, "0")


def _escaped(value):
    if isinstance(value, str):
        return escape(value)
    if isinstance(value, dict):
        return {key: _escaped(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_escaped(item) for item in value]
    return value


def safe_filename(folio):
    """Nombre de archivo seguro a partir del folio."""
    clean = re.sub(r"[^A-Za-z0-9\-_]", "_", folio or "cotizacion")
    return f"Cotizacion_{clean}.pdf"


# --------------------------------------------------------------------------- #
# Contextos separados: histórico real y demostración explícita
# --------------------------------------------------------------------------- #

def build_demo_quote(folio):
    """Datos demo con la misma forma y contenido de la pantalla de Resumen.

    El folio siempre es el solicitado; el resto replica la cotización
    COT-2026-0048 mostrada en la interfaz hasta conectar datos reales.
    """
    items = [
        {
            "code": "PRO-CAT6-EXT",
            "name": "Bobina Cable UTP Categoría 6 Exterior",
            "description": "100% Cobre sólido, chaqueta CMX con protección UV, carrete 305m color negro.",
            "provider": "SYSCOM",
            "qty": 4,
            "unit_price": Decimal("2659.60"),
            "amount": Decimal("10638.40"),
        },
        {
            "code": "DELL-P2422H",
            "name": 'Monitor Dell Profesional 23.8" Full HD',
            "description": "Panel IPS antirreflejo, resolución 1920x1080 @60Hz, HDMI, DP, VGA, 4x USB 3.2.",
            "provider": "Exel del Norte",
            "qty": 5,
            "unit_price": Decimal("4013.50"),
            "amount": Decimal("20067.50"),
        },
    ]
    subtotal = sum((i["amount"] for i in items), Decimal("0"))
    iva = amount(subtotal * Decimal("0.16"))
    return {
        "folio": folio,
        "document_label": "DEMO — SIN VALIDEZ COMERCIAL",
        "issue_date": "29/09/2026",
        "valid_until": "29/10/2026",
        "validity_label": "30 días",
        "company": dict(COMPANY),
        "client": {
            "business_name": "Constructora e Inmobiliaria del Norte S.A. de C.V.",
            "rfc": "CIN-140518-8A1",
            "contact": "Lic. Roberto Mendoza (Depto. de TI)",
            "email": "r.mendoza@inmobiliarianorte.com.mx",
            "phone": "(664) 123-4567",
            "project": "Cableado e Infraestructura Torre Alfa",
        },
        "items": items,
        "subtotal": subtotal,
        "iva": iva,
        "total": subtotal + iva,
        "iva_rate_label": "16%",
        "conditions": list(DEFAULT_CONDITIONS),
    }


def _load_legacy_quote(folio):
    """Datos guardados previos a M8. Los errores se propagan, jamás fabrican demo."""
    from .models import Quote

    quote = Quote.objects.select_related("advisor").prefetch_related("items__product__supplier").get(folio=folio)
    if quote.revisions.exists():
        raise ValueError("Usa obtener_documento para una cotización con revisiones M8.")

    advisor = getattr(quote, "advisor", None)
    advisor_name = (
        (getattr(advisor, "get_full_name", lambda: "")() or getattr(advisor, "username", ""))
        if advisor
        else COMPANY["advisor"]
    )
    items = []
    for item in quote.items.all():
        product = getattr(item, "product", None)
        supplier = getattr(product, "supplier", None) if product else None
        description = (item.description or "").strip()
        items.append(
            {
                "code": getattr(product, "sku", "") or "—",
                "name": getattr(product, "name", "") or description.split("\n")[0],
                "description": description,
                "provider": getattr(supplier, "name", "") or "—",
                "qty": item.quantity,
                "unit_price": item.unit_price,
                "amount": item.amount,
            }
        )
    notes = [line.strip() for line in (quote.notes or "").splitlines() if line.strip()]
    return {
        "folio": quote.folio,
        "document_label": "DOCUMENTO HISTÓRICO — SIN SNAPSHOT DE ORIGEN",
        "legacy_unknown_currency": True,
        "issue_date": quote.issue_date.strftime("%d/%m/%Y") if quote.issue_date else "",
        "valid_until": quote.expiration_date.strftime("%d/%m/%Y"),
        "validity_label": f"{quote.valid_days} días",
        "company": {**COMPANY, "advisor": advisor_name or COMPANY["advisor"]},
        "client": {
            "business_name": quote.client_name,
            "rfc": quote.client_rfc or "—",
            "contact": "—",
            "email": quote.client_email or "—",
            "phone": "—",
            "project": "—",
        },
        "items": items,
        "subtotal": quote.subtotal,
        "iva": quote.tax,
        "total": quote.total,
        "iva_rate_label": f"{(quote.tax_rate * 100):g}%",
        "conditions": notes + ["Datos históricos: moneda de origen y tipo de cambio no registrados. No se infiere conversión.",
                               "Membrete actual no validado como histórico; este documento no es un original archivado."],
    }


def get_quote_context(folio):
    """Vía legacy sin inferir moneda. Consultar revisiones mediante snapshots."""
    return _load_legacy_quote(folio)


# --------------------------------------------------------------------------- #
# Generación del PDF (reportlab) — única implementación
# --------------------------------------------------------------------------- #

def _styles():
    return {
        "normal": ParagraphStyle("normal", fontName="Helvetica", fontSize=9, leading=12, textColor=NAVY),
        "small": ParagraphStyle("small", fontName="Helvetica", fontSize=8, leading=10.5, textColor=MUTED),
        "small_dark": ParagraphStyle("small_dark", fontName="Helvetica", fontSize=8, leading=10.5, textColor=NAVY),
        "bold": ParagraphStyle("bold", fontName="Helvetica-Bold", fontSize=9, leading=12, textColor=NAVY),
        "folio": ParagraphStyle("folio", fontName="Courier-Bold", fontSize=13, leading=15, textColor=PURPLE),
        "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.5, leading=10.5, textColor=NAVY),
        "cell_bold": ParagraphStyle("cell_bold", fontName="Helvetica-Bold", fontSize=8.5, leading=10.5, textColor=NAVY),
        "cell_mono": ParagraphStyle("cell_mono", fontName="Courier", fontSize=8.5, leading=10.5, textColor=NAVY),
        "cell_right": ParagraphStyle("cell_right", fontName="Helvetica", fontSize=8.5, leading=10.5, textColor=NAVY, alignment=2),
        "cell_right_bold": ParagraphStyle("cell_right_bold", fontName="Helvetica-Bold", fontSize=8.5, leading=10.5, textColor=NAVY, alignment=2),
        "cell_center": ParagraphStyle("cell_center", fontName="Helvetica", fontSize=8.5, leading=10.5, textColor=NAVY, alignment=1),
        "cell_center_bold": ParagraphStyle("cell_center_bold", fontName="Helvetica-Bold", fontSize=8.5, leading=10.5, textColor=NAVY, alignment=1),
        "th": ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.white),
        "th_center": ParagraphStyle("th_center", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.white, alignment=1),
        "th_right": ParagraphStyle("th_right", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.white, alignment=2),
    }


def _logo_image():
    """Logo CCONOR preservando proporción; None si el archivo falta."""
    try:
        from django.conf import settings as dj_settings

        logo_path = dj_settings.BASE_DIR / "static" / "img" / "logo.png"
        if not logo_path.exists():
            return None
        try:
            from PIL import Image as PilImage

            with PilImage.open(logo_path) as im:
                ratio = im.height / im.width
        except Exception:
            ratio = 0.26
        width = 4.6 * cm
        return Image(str(logo_path), width=width, height=width * ratio)
    except Exception:
        return None


def _footer(canvas, doc, folio, company_name):
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawCentredString(
        letter[0] / 2, 1.0 * cm, f"{company_name}  |  {folio}  |  Página {doc.page}"
    )
    canvas.restoreState()


def generate_quote_pdf(quote):
    """Genera el PDF oficial de la cotización y devuelve sus bytes.

    `quote` es un contexto materializado de snapshots, histórico o demo.
    La emisión archiva este resultado; los borradores pueden regenerarse.
    """
    original_folio, original_company = quote["folio"], quote["company"]["name"]
    quote = _escaped(deepcopy(quote))
    st = _styles()
    c = quote["company"]
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=1.4 * cm,
        bottomMargin=1.8 * cm,
        title=f"Cotización {original_folio} - CCONOR",
        author=original_company,
    )
    story = []
    usable = letter[0] - 3.0 * cm
    if quote.get("document_label"):
        story.append(Paragraph(quote["document_label"], st["bold"]))
        story.append(Spacer(1, 0.15 * cm))
    if quote.get("revision"):
        story.append(Paragraph(f"Revisión {quote['revision']}", st["small"]))

    # ---- Encabezado membretado ----
    left_header = []
    logo = _logo_image()
    if logo:
        left_header.append(logo)
        left_header.append(Spacer(1, 0.15 * cm))
    left_header.append(
        Paragraph(
            f"<b>{c['name']}</b><br/>RFC: {c['rfc']}<br/>{c['tagline']}<br/>"
            f"Tel: {c['phone']} &nbsp;|&nbsp; {c['email']}",
            st["small"],
        )
    )
    folio_box = Table(
        [
            [Paragraph("Folio de Cotización:", st["small"])],
            [Paragraph(quote["folio"], st["folio"])],
            [
                Paragraph(
                    f"Fecha de Emisión: <b>{quote['issue_date']}</b><br/>"
                    f"Válido hasta: <b><font color=\"#C62828\">{quote['valid_until']} "
                    f"({quote['validity_label']})</font></b><br/>"
                    f"Asesor Técnico: <b>{c['advisor']}</b>",
                    st["small"],
                )
            ],
        ],
        colWidths=[6.2 * cm],
    )
    folio_box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
                ("BOX", (0, 0), (-1, -1), 0.5, LIGHT_BORDER),
                ("LINEBELOW", (0, 0), (-1, 0), 0, LIGHT_BG),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    header = Table([[left_header, folio_box]], colWidths=[usable - 6.6 * cm, 6.6 * cm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
    story.append(header)
    story.append(Spacer(1, 0.2 * cm))
    story.append(HRFlowable(width="100%", thickness=1.2, color=PURPLE))
    story.append(Spacer(1, 0.4 * cm))

    # ---- Datos del cliente ----
    cl = quote["client"]
    client_box = Table(
        [
            [
                Paragraph(
                    f"<b>{cl['business_name']}</b><br/>"
                    f"<b>RFC:</b> {cl['rfc']}<br/>"
                    f"<b>Atención:</b> {cl['contact']}",
                    st["cell"],
                ),
                Paragraph(
                    f"<b>Correo:</b> {cl['email']}<br/>"
                    f"<b>Teléfono:</b> {cl['phone']}<br/>"
                    f"<b>Proyecto:</b> {cl['project']}",
                    st["cell"],
                ),
            ]
        ],
        colWidths=[usable * 0.55, usable * 0.45],
    )
    client_box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
                ("BOX", (0, 0), (-1, -1), 0.5, LIGHT_BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(Paragraph("<b><font color=\"#FFFFFF\">&nbsp;DATOS DEL CLIENTE</font></b>", st["small"]))
    story.append(Spacer(1, 0.1 * cm))
    story.append(client_box)
    story.append(Spacer(1, 0.4 * cm))

    # ---- Tabla de partidas ----
    head = [
        Paragraph("#", st["th_center"]),
        Paragraph("Código / Clave", st["th"]),
        Paragraph("Descripción Detallada", st["th"]),
        Paragraph("Marca / Dist.", st["th_center"]),
        Paragraph("Cant.", st["th_center"]),
        Paragraph("P. Unitario", st["th_right"]),
        Paragraph("Importe histórico" if quote.get("legacy_unknown_currency") else "Importe MXN", st["th_right"]),
    ]
    rows = [head]
    for i, item in enumerate(quote["items"], start=1):
        source_label = ""
        unit_text = money(item["unit_price"]) if not quote.get("precision_units") else unit_money(item["unit_price"])
        if quote.get("precision_units"):
            stock = item["stock"]
            stock_label = "desconocido" if stock is None else "sin existencias" if stock == 0 else str(stock)
            source_label = (f"<br/>Origen: {unit_money(item['source_unit_price'])} {item['source_currency']}"
                            f"<br/>Stock capturado: {stock_label}")
            if stringWidth(unit_text, st["cell_right"].fontName, st["cell_right"].fontSize) > 2.1 * cm - 12:
                source_label += f"<br/>Unitario MXN: {unit_text}"
                unit_text = "Ver detalle"
        rows.append(
            [
                Paragraph(f"<b>{i}</b>", st["cell_center"]),
                Paragraph(item["code"], st["cell_mono"]),
                Paragraph(f"<b>{item['name']}</b><br/><font color=\"#5A5F7A\">{item['description']}{source_label}</font>", st["cell"]),
                Paragraph(item["provider"], st["cell_center"]),
                Paragraph(f"<b>{item['qty']}</b>", st["cell_center"]),
                Paragraph(unit_text, st["cell_right"]),
                Paragraph(f"<b>{money(item['amount'])}</b>", st["cell_right"]),
            ]
        )
    col_widths = [0.9 * cm, 2.4 * cm, usable - 11.9 * cm, 2.2 * cm, 1.2 * cm, 2.1 * cm, 2.1 * cm]
    items_table = Table(rows, colWidths=col_widths, repeatRows=1, splitInRow=1)
    items_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.5, LIGHT_BORDER),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_BG]),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(items_table)
    story.append(Spacer(1, 0.4 * cm))
    if quote.get("exchange_rate_label"):
        story.append(Paragraph(quote["exchange_rate_label"], st["small"]))
    if any(item.get("stock") in (0, None) for item in quote["items"]) and not quote.get("legacy_unknown_currency"):
        story.append(Paragraph("Hay partidas sin existencias o con disponibilidad desconocida. Confirmar entrega; el precio no garantiza stock.", st["small"]))

    # ---- Condiciones + totales ----
    conditions = [Paragraph("<b>Condiciones Comerciales y de Entrega:</b>", st["cell_bold"])]
    for cond in quote["conditions"]:
        conditions.append(Paragraph(f"• {cond}", st["small_dark"]))
    currency_label = "moneda no registrada" if quote.get("legacy_unknown_currency") else "MXN"
    totals = Table(
        [
            [Paragraph("Subtotal:", st["cell"]), Paragraph(f"<b>{money(quote['subtotal'])} {currency_label}</b>", st["cell_right"])],
            [Paragraph(f"I.V.A. ({quote['iva_rate_label']}):", st["cell"]), Paragraph(f"<b>{money(quote['iva'])} {currency_label}</b>", st["cell_right"])],
            [Paragraph("<b>Total estimado:</b>", st["cell_bold"]), Paragraph(f"<b><font color=\"#7311F5\">{money(quote['total'])} {currency_label}</font></b>", st["cell_right"])],
        ],
        colWidths=[3.0 * cm, 3.6 * cm],
    )
    totals.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
                ("BOX", (0, 0), (-1, -1), 0.5, LIGHT_BORDER),
                ("LINEABOVE", (0, 2), (-1, 2), 0.7, LIGHT_BORDER),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    bottom = Table([[conditions, totals]], colWidths=[usable - 7.0 * cm, 7.0 * cm], splitInRow=1)
    bottom.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
    story.append(bottom)

    # ---- Firma ----
    story.append(Spacer(1, 1.2 * cm))
    story.append(
        Paragraph(
            f"_________________________<br/><b>{c['name']}</b><br/>"
            "Área de Ingeniería y Cotizaciones Comerciales",
            ParagraphStyle("sign", parent=st["small"], alignment=1, textColor=NAVY),
        )
    )

    doc.build(
        story,
        onFirstPage=lambda cnv, d: _footer(cnv, d, original_folio, original_company),
        onLaterPages=lambda cnv, d: _footer(cnv, d, original_folio, original_company),
    )
    return buffer.getvalue()
