"""Generación del PDF oficial de cotización (reportlab).

ÚNICO punto de generación de los bytes del PDF: lo usan tanto la descarga
manual (`descargar_pdf_cotizacion`) como el envío por correo
(`enviar_cotizacion_correo`). NO duplicar esta lógica en otro módulo.

Fuente de datos: `get_quote_context(folio)` intenta primero la BD (modelo
`quotes.Quote`, cuando exista y esté migrado) y, si no hay datos reales,
usa datos demo con la misma forma que la pantalla de Resumen. El folio del
documento siempre es el solicitado en la URL.
"""

import io
import re
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable,
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

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
    "Garantía: Directa de fábrica con los mayoristas autorizados (SYSCOM y CT Online).",
    "Cuentas bancarias para depósito: BBVA Bancomer Clabe: 012028000123456789.",
]


def money(value):
    """Formato moneda estilo es-MX: $26,470.60 (sin el sufijo MXN)."""
    return f"${Decimal(value):,.2f}"


def safe_filename(folio):
    """Nombre de archivo seguro a partir del folio."""
    clean = re.sub(r"[^A-Za-z0-9\-_]", "_", folio or "cotizacion")
    return f"Cotizacion_{clean}.pdf"


# --------------------------------------------------------------------------- #
# Contexto de datos (real si hay BD, demo si no)
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
            "provider": "CT Online",
            "qty": 5,
            "unit_price": Decimal("4013.50"),
            "amount": Decimal("20067.50"),
        },
    ]
    return {
        "folio": folio,
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
        "subtotal": Decimal("26470.60"),
        "iva": Decimal("4235.30"),
        "total": Decimal("30705.90"),
        "iva_rate_label": "16%",
        "conditions": list(DEFAULT_CONDITIONS),
    }


def _try_load_real_quote(folio):
    """Intenta armar el contexto desde el modelo quotes.Quote.

    Devuelve None si el modelo no existe, el folio no está en BD o las
    tablas aún no están migradas. Así el PDF funciona en demo y empieza a
    usar datos reales sin cambiar código cuando el backend esté listo.
    """
    try:
        from django.apps import apps as django_apps

        quote_model = django_apps.get_model("quotes", "Quote")
    except LookupError:
        return None
    try:
        quote = quote_model.objects.prefetch_related("items").get(folio=folio)
    except Exception:
        return None

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
        "conditions": notes or list(DEFAULT_CONDITIONS),
    }


def get_quote_context(folio):
    """Contexto del PDF para cualquier folio: real si hay BD, demo si no."""
    return _try_load_real_quote(folio) or build_demo_quote(folio)


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


def _footer(canvas, doc, folio):
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawCentredString(
        letter[0] / 2, 1.0 * cm, f"{COMPANY['name']}  |  {folio}  |  Página {doc.page}"
    )
    canvas.restoreState()


def generate_quote_pdf(quote):
    """Genera el PDF oficial de la cotización y devuelve sus bytes.

    `quote` es el dict de `get_quote_context()`. Misma función para la
    descarga manual y para adjuntarlo al correo.
    """
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
        title=f"Cotización {quote['folio']} - CCONOR",
        author=c["name"],
    )
    story = []
    usable = letter[0] - 3.0 * cm

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
        Paragraph("Importe MXN", st["th_right"]),
    ]
    rows = [head]
    for i, item in enumerate(quote["items"], start=1):
        rows.append(
            [
                Paragraph(f"<b>{i}</b>", st["cell_center"]),
                Paragraph(item["code"], st["cell_mono"]),
                Paragraph(f"<b>{item['name']}</b><br/><font color=\"#5A5F7A\">{item['description']}</font>", st["cell"]),
                Paragraph(item["provider"], st["cell_center"]),
                Paragraph(f"<b>{item['qty']}</b>", st["cell_center"]),
                Paragraph(money(item["unit_price"]), st["cell_right"]),
                Paragraph(f"<b>{money(item['amount'])}</b>", st["cell_right"]),
            ]
        )
    col_widths = [0.9 * cm, 2.4 * cm, usable - 11.9 * cm, 2.2 * cm, 1.2 * cm, 2.1 * cm, 2.1 * cm]
    items_table = Table(rows, colWidths=col_widths, repeatRows=1)
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

    # ---- Condiciones + totales ----
    conditions = [Paragraph("<b>Condiciones Comerciales y de Entrega:</b>", st["cell_bold"])]
    for cond in quote["conditions"]:
        conditions.append(Paragraph(f"• {cond}", st["small_dark"]))
    totals = Table(
        [
            [Paragraph("Subtotal:", st["cell"]), Paragraph(f"<b>{money(quote['subtotal'])} MXN</b>", st["cell_right"])],
            [Paragraph(f"I.V.A. ({quote['iva_rate_label']}):", st["cell"]), Paragraph(f"<b>{money(quote['iva'])} MXN</b>", st["cell_right"])],
            [Paragraph("<b>Total a Pagar:</b>", st["cell_bold"]), Paragraph(f"<b><font color=\"#7311F5\">{money(quote['total'])} MXN</font></b>", st["cell_right"])],
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
    bottom = Table([[conditions, totals]], colWidths=[usable - 7.0 * cm, 7.0 * cm])
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
        onFirstPage=lambda cnv, d: _footer(cnv, d, quote["folio"]),
        onLaterPages=lambda cnv, d: _footer(cnv, d, quote["folio"]),
    )
    return buffer.getvalue()
