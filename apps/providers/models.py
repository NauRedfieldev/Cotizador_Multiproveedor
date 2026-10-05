import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal

from django.contrib.postgres.indexes import GinIndex
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone

UpsertStatus = Literal["created", "updated", "unchanged"]

INTENTOS_AUPSERT = 3


def compute_content_hash(payload: Mapping[str, Any]) -> str:
    """sha256 del payload serializado de forma canónica (claves ordenadas, sin espacios).

    El hash corresponde al JSON tal como se guarda en RawProviderProduct.payload, porque usa
    el mismo DjangoJSONEncoder que el JSONField (SDD M1 D4: JSON parseado con
    parse_float=Decimal). Por eso Decimal("1.1") y "1.1" comparten hash (los dos se guardan
    como texto), Decimal("1.10") conserva sus ceros, los datetime se guardan en ISO 8601
    truncados a milisegundos y los UUID como texto. Un tipo que JSON no admite (p. ej. un
    set) lanza TypeError.
    """
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), cls=DjangoJSONEncoder)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Provider(models.Model):
    code = models.SlugField(max_length=32, unique=True)
    name = models.CharField(max_length=120)
    base_url = models.URLField()
    active = models.BooleanField(default=True)
    timeout_ms = models.PositiveIntegerField(default=8000)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class RawProviderProductManager(models.Manager):
    COLUMNAS = (
        "id, provider_id, external_id, content_hash, payload, "
        "first_seen_at, last_seen_at, fetch_count"
    )

    async def aupsert(
        self, provider: Provider, external_id: str | int, payload: Mapping[str, Any]
    ) -> tuple["RawProviderProduct", UpsertStatus]:
        if provider.pk is None:
            raise ValueError("El proveedor debe estar guardado antes de llamar a aupsert.")
        external_id = str(external_id)
        if not external_id.strip():
            raise ValueError("external_id no puede estar vacío.")

        parametros = {
            "proveedor": provider.pk,
            "external_id": external_id,
            "hash": compute_content_hash(payload),
            "payload": json.dumps(payload, cls=DjangoJSONEncoder),
            "ahora": timezone.now(),
        }
        tabla = self.model._meta.db_table
        # Mismo contenido: un solo UPDATE condicionado al hash, sin reescribir el payload.
        sin_cambios = f"""
            UPDATE {tabla}
               SET last_seen_at = %(ahora)s, fetch_count = fetch_count + 1
             WHERE provider_id = %(proveedor)s AND external_id = %(external_id)s
               AND content_hash = %(hash)s
            RETURNING {self.COLUMNAS}
        """
        # Alta o cambio de contenido. Si choca con una fila que ya tiene este mismo hash
        # (otro proceso la acaba de guardar), el WHERE impide reescribirla y no devuelve nada.
        alta_o_cambio = f"""
            INSERT INTO {tabla} (provider_id, external_id, content_hash, payload,
                                 first_seen_at, last_seen_at, fetch_count)
            VALUES (%(proveedor)s, %(external_id)s, %(hash)s, %(payload)s::jsonb,
                    %(ahora)s, %(ahora)s, 1)
            ON CONFLICT (provider_id, external_id) DO UPDATE
               SET payload = EXCLUDED.payload,
                   content_hash = EXCLUDED.content_hash,
                   last_seen_at = EXCLUDED.last_seen_at,
                   fetch_count = {tabla}.fetch_count + 1
             WHERE {tabla}.content_hash <> EXCLUDED.content_hash
            RETURNING {self.COLUMNAS}
        """
        for _ in range(INTENTOS_AUPSERT):
            if obj := await self._afila(sin_cambios, parametros):
                return obj, "unchanged"
            if obj := await self._afila(alta_o_cambio, parametros):
                return obj, "created" if obj.fetch_count == 1 else "updated"
        raise RuntimeError(
            f"aupsert no pudo registrar {external_id!r} tras {INTENTOS_AUPSERT} intentos: "
            "la fila cambió en cada intento."
        )

    async def _afila(self, sql: str, parametros: dict[str, Any]) -> "RawProviderProduct | None":
        """Ejecuta una sentencia con RETURNING y devuelve la fila como instancia del modelo."""
        async for obj in self.raw(sql, parametros):
            return obj
        return None


class RawProviderProduct(models.Model):
    provider = models.ForeignKey(
        Provider, on_delete=models.PROTECT, related_name="raw_products"
    )
    external_id = models.CharField(max_length=128)
    content_hash = models.CharField(max_length=64)
    payload = models.JSONField(encoder=DjangoJSONEncoder)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    fetch_count = models.PositiveIntegerField(default=1)

    objects = RawProviderProductManager()

    class Meta:
        db_table = "raw_provider_product"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "external_id"], name="raw_pp_provider_external_uniq"
            ),
        ]
        indexes = [
            models.Index(fields=["last_seen_at"], name="raw_pp_last_seen_idx"),
            GinIndex(fields=["payload"], name="raw_pp_payload_gin"),
        ]

    def __str__(self) -> str:
        return f"{self.provider_id}:{self.external_id}"


class ProviderToken(models.Model):
    provider = models.OneToOneField(
        Provider, on_delete=models.CASCADE, related_name="token"
    )
    access_token = models.TextField()
    expires_at = models.DateTimeField()
    obtained_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        if self.expires_at is None:
            return f"Token de {self.provider_id} (expira: sin fecha)"
        return f"Token de {self.provider_id} (expira {self.expires_at:%Y-%m-%d %H:%M})"
