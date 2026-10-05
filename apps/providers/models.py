import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal

from django.contrib.postgres.indexes import GinIndex
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import F
from django.utils import timezone

UpsertStatus = Literal["created", "updated", "unchanged"]


def compute_content_hash(payload: Mapping[str, Any]) -> str:
    """sha256 del payload serializado de forma canónica (claves ordenadas, sin espacios).

    DjangoJSONEncoder permite Decimal (SDD M1 D4: JSON parseado con parse_float=Decimal).
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
    async def aupsert(
        self, provider: Provider, external_id: str, payload: Mapping[str, Any]
    ) -> tuple["RawProviderProduct", UpsertStatus]:
        content_hash = compute_content_hash(payload)
        now = timezone.now()

        # Mismo contenido: un solo UPDATE condicionado al hash, sin reescribir el payload.
        unchanged = await self.filter(
            provider=provider, external_id=external_id, content_hash=content_hash
        ).aupdate(last_seen_at=now, fetch_count=F("fetch_count") + 1)
        if unchanged:
            obj = await self.aget(provider=provider, external_id=external_id)
            return obj, "unchanged"

        obj, created = await self.aupdate_or_create(
            provider=provider,
            external_id=external_id,
            defaults={
                "payload": payload,
                "content_hash": content_hash,
                "last_seen_at": now,
                "fetch_count": F("fetch_count") + 1,
            },
            create_defaults={
                "payload": payload,
                "content_hash": content_hash,
                "first_seen_at": now,
                "last_seen_at": now,
                "fetch_count": 1,
            },
        )
        if not created:
            await obj.arefresh_from_db(fields=["fetch_count"])
        return obj, "created" if created else "updated"


class RawProviderProduct(models.Model):
    provider = models.ForeignKey(
        Provider, on_delete=models.CASCADE, related_name="raw_products"
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
        return f"Token de {self.provider_id} (expira {self.expires_at:%Y-%m-%d %H:%M})"
