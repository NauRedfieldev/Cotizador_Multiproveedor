"""Pruebas I1–I15 entre componentes: SYSCOM (M2 + M3) → catalog → quotes (docs/18).

Todavía no existe el motor de comparación que une la cadena, así que las pruebas recorren un
flujo de referencia escrito aquí (sin código de producción): buscar, emparejar por clave exacta,
elegir la oferta más barata, resolver el Supplier, crear el Product y cotizarlo.

Sin red y sin credenciales reales: ApiFalsa, sin_red() y credenciales_de_prueba().
"""
import copy
from datetime import timedelta
from decimal import Decimal

from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth import get_user_model
from django.core.exceptions import SynchronousOnlyOperation
from django.db import DataError, transaction
from django.test import TestCase, tag
from django.utils import timezone

from apps.catalog.models import Category, Product, Supplier
from apps.catalog.normalizer import normalizar_clave
from apps.providers.adapters.syscom import SyscomAdapter
from apps.providers.contracts import ProductQuery
from apps.providers.errors import ProviderRateLimitError, ProviderResponseError
from apps.providers.models import Provider, ProviderToken
from apps.providers.service import search_all
from apps.providers.tests.soporte import (
    CENTINELA_TOKEN,
    AdaptadorDePrueba,
    ApiFalsa,
    adaptadores_registrados,
    credenciales_de_prueba,
    exigir_bd_de_pruebas,
    producto,
    productos,
    respuesta_json,
    sin_red,
)
from apps.providers.tests.test_adaptador_syscom import (
    BASE_URL,
    HOST,
    PRODUCTOS,
    RUTA_PRODUCTOS,
    muestra,
    respuesta_muestra,
)
from apps.quotes.models import Quote, QuoteItem

CAMARA = "CAM-IP-2MP-A1"  # 900001, 85.50 en la muestra
DOMO = "DOMO-IP-4MP-B2"  # 900002, 150.25 en la muestra
HOST_PRUEBA = "prueba.example.com"


# --- Flujo de referencia (lo que tendrá que hacer el motor de comparación) -----------------


def clave(oferta):
    """Clave exacta de emparejamiento: MPN o, si falta, el modelo, sin separadores."""
    return normalizar_clave(oferta.mpn or oferta.model or "")


async def emparejar(oferta):
    """Product del catálogo con la misma clave exacta, o None. Nunca fuzzy."""
    objetivo = clave(oferta)
    if not objetivo:
        return None
    coincidencias = [p async for p in Product.objects.all() if normalizar_clave(p.sku) == objetivo]
    return coincidencias[0] if len(coincidencias) == 1 else None


def mas_barata(resultados, clave_buscada, moneda="USD"):
    """Oferta de menor precio con esa clave y esa moneda, entre los proveedores que respondieron."""
    candidatas = [
        oferta
        for resultado in resultados
        if resultado.ok
        for oferta in resultado.offers
        if clave(oferta) == clave_buscada and oferta.currency == moneda
    ]
    return min(candidatas, key=lambda oferta: oferta.price, default=None)


async def producto_desde_oferta(oferta, categoria):
    """Crea o actualiza el Product de la oferta, con el Supplier enlazado a su proveedor."""
    supplier = await Supplier.objects.aget(provider__code=oferta.provider_code)
    producto_catalogo, _ = await Product.objects.aupdate_or_create(
        sku=oferta.mpn or oferta.model,
        defaults={
            "name": oferta.name,
            "category": categoria,
            "supplier": supplier,
            "unit_price": oferta.price,
        },
    )
    return producto_catalogo


async def totales(quote):
    """subtotal, tax y total son consultas síncronas: desde async van por sync_to_async."""
    return await sync_to_async(lambda: (quote.subtotal, quote.tax, quote.total))()


def respuesta_error(status, cabeceras=None):
    return respuesta_json({}, status=status, cabeceras=cabeceras)


def recuentos():
    return (Product.objects.count(), Quote.objects.count(), QuoteItem.objects.count())


class BaseIntegracion(TestCase):
    """SYSCOM activo, con un token guardado (no se pide ninguno), una categoría y un asesor."""

    con_supplier = True

    def setUp(self):
        exigir_bd_de_pruebas()
        self.enterContext(sin_red())
        self.enterContext(
            credenciales_de_prueba("syscom", CLIENT_ID="id-de-prueba", CLIENT_SECRET="secreto-de-prueba")
        )
        self.api = ApiFalsa()
        self.syscom = Provider.objects.create(code="syscom", name="SYSCOM", base_url=BASE_URL)
        ahora = timezone.now()
        ProviderToken.objects.create(
            provider=self.syscom,
            access_token=CENTINELA_TOKEN,
            expires_at=ahora + timedelta(days=300),
            obtained_at=ahora,
        )
        self.supplier_syscom = (
            Supplier.objects.create(name="SYSCOM", provider=self.syscom) if self.con_supplier else None
        )
        self.categoria = Category.objects.create(name="Videovigilancia")
        self.asesor = get_user_model().objects.create_user(username="asesor")

    def productos_responden(self, *respuestas):
        self.api.responder(HOST, RUTA_PRODUCTOS, *(respuestas or (respuesta_muestra("syscom_productos"),)))

    async def buscar(self, consulta=ProductQuery(term="camara ip")):
        return await search_all(consulta, transport=self.api.transport)

    async def cotizacion(self):
        return await Quote.objects.acreate(client_name="Cliente de prueba", advisor=self.asesor)

    async def cotizar(self, partidas):
        """partidas: [(oferta, cantidad)]. Las partidas van sin precio para que hagan el snapshot."""
        quote = await self.cotizacion()
        for oferta, cantidad in partidas:
            producto_catalogo = await producto_desde_oferta(oferta, self.categoria)
            await QuoteItem.objects.acreate(quote=quote, product=producto_catalogo, quantity=Decimal(cantidad))
        return quote


def por_modelo(resultado, modelo):
    return next(oferta for oferta in resultado.offers if oferta.model == modelo)


# --- A. De M2/M3 a catalog (Supplier) --------------------------------------------------------


@tag("django_db")
class SupplierDeLasOfertasTests(BaseIntegracion):
    async def test_cada_oferta_de_syscom_resuelve_su_supplier(self):
        """I1 Cada oferta encuentra el Supplier "SYSCOM" por provider_code, sin pedir token."""
        # Arrange
        self.productos_responden()

        # Act
        [resultado] = await self.buscar()
        suppliers = [await Supplier.objects.aget(provider__code=o.provider_code) for o in resultado.offers]

        # Assert
        self.assertEqual(
            (resultado.ok, len(resultado.offers), suppliers, self.api.rutas_pedidas()),
            (True, 3, [self.supplier_syscom] * 3, [PRODUCTOS]),
        )

    async def test_un_proveedor_inactivo_no_se_consulta_aunque_tenga_supplier(self):
        """I3 Con el Provider inactivo no hay resultados ni peticiones, y el Supplier sigue enlazado."""
        # Arrange
        self.syscom.active = False
        await self.syscom.asave()
        self.productos_responden()

        # Act
        resultados = await self.buscar()

        # Assert
        enlazado = await Supplier.objects.aget(pk=self.supplier_syscom.pk)
        self.assertEqual(
            (resultados, self.api.peticiones, enlazado.provider_id), ([], [], self.syscom.pk)
        )

    async def test_un_error_de_syscom_no_escribe_en_catalog_ni_quotes(self):
        """I4 Un 503 y un 429 llegan como error del resultado, sin ofertas y sin escrituras."""
        # Arrange
        self.productos_responden(
            respuesta_error(503), respuesta_error(429, cabeceras={"Retry-After": "30"})
        )

        # Act
        [con_503] = await self.buscar()
        [con_429] = await self.buscar()

        # Assert
        self.assertIsInstance(con_503.error, ProviderResponseError)
        self.assertIsInstance(con_429.error, ProviderRateLimitError)
        self.assertEqual(
            (con_503.offers, con_429.offers, await sync_to_async(recuentos)()), ((), (), (0, 0, 0))
        )


@tag("django_db")
class SinSupplierEnlazadoTests(BaseIntegracion):
    con_supplier = False

    async def test_sin_supplier_enlazado_las_ofertas_llegan_sin_distribuidor(self):
        """I2 Estado actual (D10): M2 no depende de catalog, pero la oferta no tiene Supplier."""
        # Arrange
        self.productos_responden()

        # Act
        [resultado] = await self.buscar()

        # Assert
        self.assertEqual((resultado.ok, len(resultado.offers)), (True, 3))
        with self.assertRaises(Supplier.DoesNotExist):
            await Supplier.objects.aget(provider__code=resultado.offers[0].provider_code)


# --- B. Emparejamiento por clave exacta -------------------------------------------------------


@tag("django_db")
class EmparejamientoTests(BaseIntegracion):
    async def test_la_oferta_se_empareja_con_el_producto_por_clave_exacta(self):
        """I5 "CAM IP 2MP A1" del catálogo solo empareja con la oferta 900001."""
        # Arrange
        en_catalogo = await Product.objects.acreate(
            sku="CAM IP 2MP A1", name="Cámara del catálogo", category=self.categoria,
            supplier=self.supplier_syscom, unit_price=Decimal("90.00"),
        )
        self.productos_responden()

        # Act
        [resultado] = await self.buscar()
        emparejados = {oferta.external_id: await emparejar(oferta) for oferta in resultado.offers}

        # Assert
        self.assertEqual(emparejados, {"900001": en_catalogo, "900002": None, "900003": None})

    async def test_la_busqueda_por_sku_empareja_y_la_no_disponible_no_crea_nada(self):
        """I6 ?modelo= da una oferta que empareja; el 404 product_not_available, ninguna."""
        # Arrange
        en_catalogo = await Product.objects.acreate(
            sku=CAMARA, name="Cámara del catálogo", category=self.categoria,
            supplier=self.supplier_syscom, unit_price=Decimal("90.00"),
        )
        self.productos_responden(
            respuesta_muestra("syscom_modelo"), respuesta_muestra("syscom_modelo_no_disponible", 404)
        )

        # Act
        [por_sku] = await self.buscar(ProductQuery(sku=CAMARA))
        [no_disponible] = await self.buscar(ProductQuery(sku="NO-EXISTE-123"))

        # Assert
        self.assertEqual(
            (
                len(por_sku.offers),
                await emparejar(por_sku.offers[0]),
                no_disponible.ok,
                no_disponible.offers,
                await sync_to_async(recuentos)(),
            ),
            (1, en_catalogo, True, (), (1, 0, 0)),
        )


# --- C. De catalog a quotes ----------------------------------------------------------------


@tag("django_db")
class CotizacionTests(BaseIntegracion):
    async def test_cotizacion_completa_desde_ofertas_de_syscom(self):
        """I7 3 × 85.50 + 2 × 150.25 = 557.00; IVA 89.12; total 646.12. Todo en Decimal."""
        # Arrange
        self.productos_responden()
        [resultado] = await self.buscar()
        camara, domo = por_modelo(resultado, CAMARA), por_modelo(resultado, DOMO)

        # Act
        quote = await self.cotizar([(camara, "3"), (domo, "2")])

        # Assert
        partidas = [(i.description, i.unit_price, i.amount) async for i in quote.items.all()]
        importes = await totales(quote)
        self.assertEqual(
            (partidas, importes),
            (
                [
                    (camara.name, Decimal("85.50"), Decimal("256.50")),
                    (domo.name, Decimal("150.25"), Decimal("300.50")),
                ],
                (Decimal("557.00"), Decimal("89.12"), Decimal("646.12")),
            ),
        )
        self.assertTrue(all(type(valor) is Decimal for valor in importes))

    async def test_el_snapshot_no_cambia_con_un_precio_nuevo_de_syscom(self):
        """I8 Un precio nuevo actualiza el Product, pero no la partida ya cotizada."""
        # Arrange
        datos = muestra("syscom_productos")
        datos["productos"][0]["precios"]["precio_descuento"] = "99.99"
        self.productos_responden(respuesta_muestra("syscom_productos"), respuesta_json(datos))
        [resultado] = await self.buscar()
        quote = await self.cotizar([(por_modelo(resultado, CAMARA), "1")])
        antes = await totales(quote)

        # Act
        [nuevo] = await self.buscar()
        actualizado = await producto_desde_oferta(por_modelo(nuevo, CAMARA), self.categoria)

        # Assert
        partida = await quote.items.aget()
        await actualizado.arefresh_from_db()
        self.assertEqual(
            (actualizado.unit_price, partida.unit_price, await totales(quote)),
            (Decimal("99.99"), Decimal("85.50"), antes),
        )

    async def test_la_oferta_mas_barata_entre_proveedores_llega_a_la_cotizacion(self):
        """I9 SYSCOM 85.50 frente a "prueba" 80.00 con el mismo modelo: gana "prueba"."""
        # Arrange
        prueba = await Provider.objects.acreate(
            code="prueba", name="Prueba", base_url=f"https://{HOST_PRUEBA}"
        )
        supplier_prueba = await Supplier.objects.acreate(name="Prueba", provider=prueba)
        self.productos_responden()
        self.api.responder(
            HOST_PRUEBA, "/productos",
            productos(producto("P-1", precio="80.00", nombre="Cámara bala IP (prueba)", modelo=CAMARA)),
        )

        # Act
        with adaptadores_registrados(SyscomAdapter, AdaptadorDePrueba):
            resultados = await self.buscar()
        elegida = mas_barata(resultados, normalizar_clave(CAMARA))
        quote = await self.cotizar([(elegida, "1")])

        # Assert
        partida = await quote.items.select_related("product__supplier").aget()
        self.assertEqual(
            (
                [(r.provider_code, r.ok) for r in resultados],
                elegida.provider_code,
                partida.product.supplier,
                partida.unit_price,
            ),
            ([("prueba", True), ("syscom", True)], "prueba", supplier_prueba, Decimal("80.00")),
        )

    async def test_un_proveedor_caido_no_impide_cotizar_con_el_otro(self):
        """I10 "prueba" responde 503: la partida se cotiza con SYSCOM a 85.50."""
        # Arrange
        prueba = await Provider.objects.acreate(
            code="prueba", name="Prueba", base_url=f"https://{HOST_PRUEBA}"
        )
        await Supplier.objects.acreate(name="Prueba", provider=prueba)
        self.productos_responden()
        self.api.responder(HOST_PRUEBA, "/productos", respuesta_error(503))

        # Act
        with adaptadores_registrados(SyscomAdapter, AdaptadorDePrueba):
            caido, syscom = await self.buscar()
        elegida = mas_barata([caido, syscom], normalizar_clave(CAMARA))
        quote = await self.cotizar([(elegida, "1")])

        # Assert
        partida = await quote.items.select_related("product__supplier").aget()
        self.assertIsInstance(caido.error, ProviderResponseError)
        self.assertEqual(
            (elegida.provider_code, partida.product.supplier, partida.unit_price),
            ("syscom", self.supplier_syscom, Decimal("85.50")),
        )


# --- D. Caracterización de las brechas actuales ----------------------------------------------


@tag("django_db")
class CaracterizacionTests(BaseIntegracion):
    async def test_caracterizacion_el_precio_en_usd_entra_sin_convertir(self):
        """I11 Hoy el precio en USD se cotiza como si fueran pesos (propuesta en docs/19)."""
        # Arrange
        self.productos_responden()
        [resultado] = await self.buscar()
        camara = por_modelo(resultado, CAMARA)

        # Act
        quote = await self.cotizar([(camara, "1")])

        # Assert
        campos = {campo.name for modelo in (Quote, QuoteItem, Product) for campo in modelo._meta.get_fields()}
        self.assertEqual(
            (camara.currency, await totales(quote), "currency" in campos, "exchange_rate" in campos),
            ("USD", (Decimal("85.50"), Decimal("13.68"), Decimal("99.18")), False, False),
        )

    async def test_caracterizacion_los_totales_no_se_leen_desde_async(self):
        """I12 subtotal consulta la BD de forma síncrona: en una vista async falla."""
        # Arrange
        self.productos_responden()
        [resultado] = await self.buscar()
        quote = await self.cotizar([(por_modelo(resultado, CAMARA), "1")])

        # Act / Assert
        with self.assertRaises(SynchronousOnlyOperation):
            quote.subtotal
        self.assertEqual(await totales(quote), (Decimal("85.50"), Decimal("13.68"), Decimal("99.18")))

    def test_caracterizacion_un_titulo_de_mas_de_200_caracteres_no_cabe_en_product(self):
        """I13 M3 acepta un título de 250 caracteres, pero Product.name es varchar(200)."""
        # Arrange
        datos = muestra("syscom_productos")
        datos["productos"][0]["titulo"] = "C" * 250
        self.productos_responden(respuesta_json(datos))
        [resultado] = async_to_sync(self.buscar)()
        camara = por_modelo(resultado, CAMARA)

        # Act / Assert
        self.assertEqual(len(camara.name), 250)
        with self.assertRaises(DataError), transaction.atomic():
            async_to_sync(producto_desde_oferta)(camara, self.categoria)
        self.assertEqual(Product.objects.count(), 0)

    async def test_caracterizacion_un_precio_con_mas_de_dos_decimales_se_redondea_al_guardar(self):
        """I14 85.505 queda así en memoria (Product y snapshot) y como 85.51 en la BD (numeric(12,2))."""
        # Arrange
        datos = muestra("syscom_productos")
        datos["productos"][0]["precios"]["precio_descuento"] = "85.505"
        self.productos_responden(respuesta_json(datos))
        [resultado] = await self.buscar()
        camara = por_modelo(resultado, CAMARA)

        # Act
        guardado = await producto_desde_oferta(camara, self.categoria)
        quote = await self.cotizacion()
        partida = await QuoteItem.objects.acreate(quote=quote, product=guardado, quantity=Decimal("1"))
        en_memoria = (guardado.unit_price, partida.unit_price, partida.amount)
        await guardado.arefresh_from_db()
        await partida.arefresh_from_db()

        # Assert: amount (ROUND_HALF_UP) coincide con lo que redondea PostgreSQL.
        self.assertEqual(
            (camara.price, en_memoria, guardado.unit_price, partida.unit_price, (await totales(quote))[0]),
            (
                Decimal("85.505"),
                (Decimal("85.505"), Decimal("85.505"), Decimal("85.51")),
                Decimal("85.51"),
                Decimal("85.51"),
                Decimal("85.51"),
            ),
        )


# --- E. Secretos de punta a punta -------------------------------------------------------------


@tag("django_db")
class SecretosTests(BaseIntegracion):
    async def test_el_token_no_aparece_en_la_cotizacion_ni_en_los_logs(self):
        """I15 Se usa el token guardado y no queda en el catálogo, la cotización ni los logs."""
        # Arrange: un producto con precio 0 obliga a registrar un descarte.
        datos = muestra("syscom_productos")
        con_precio_cero = copy.deepcopy(datos["productos"][1])
        con_precio_cero["producto_id"] = "900009"
        con_precio_cero["precios"]["precio_descuento"] = "0.00"
        datos["productos"].append(con_precio_cero)
        self.productos_responden(respuesta_json(datos))

        # Act
        with self.assertLogs(level="DEBUG") as registro:
            [resultado] = await self.buscar()
            quote = await self.cotizar([(oferta, "1") for oferta in resultado.offers])

        # Assert
        guardados = [
            str(valor)
            async for fila in Product.objects.values_list("sku", "name", "description")
            for valor in fila
        ] + [descripcion async for descripcion in quote.items.values_list("description", flat=True)]
        texto = "\n".join(registro.output + guardados)
        self.assertEqual(
            (
                resultado.discarded,
                self.api.rutas_pedidas(),
                self.api.peticiones[0].headers["Authorization"],
                CENTINELA_TOKEN in texto,
            ),
            (1, [PRODUCTOS], f"Bearer {CENTINELA_TOKEN}", False),
        )
