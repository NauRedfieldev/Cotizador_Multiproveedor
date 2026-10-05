from decimal import Decimal

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone

from apps.providers.models import (
    Provider,
    ProviderToken,
    RawProviderProduct,
    compute_content_hash,
)


class RawProviderProductUpsertTests(TestCase):
    def setUp(self):
        self.provider = Provider.objects.create(
            code="demo", name="Demo", base_url="https://demo.example.com"
        )

    async def test_first_upsert_creates_row(self):
        obj, status = await RawProviderProduct.objects.aupsert(
            self.provider, "SKU-1", {"name": "Laptop"}
        )
        self.assertEqual(status, "created")
        self.assertEqual(obj.fetch_count, 1)
        self.assertEqual(obj.first_seen_at, obj.last_seen_at)
        self.assertEqual(obj.content_hash, compute_content_hash({"name": "Laptop"}))

    async def test_same_payload_100_times_keeps_one_row(self):
        payload = {"name": "Laptop", "price": "19999.00"}
        for _ in range(100):
            await RawProviderProduct.objects.aupsert(self.provider, "SKU-1", payload)

        self.assertEqual(await RawProviderProduct.objects.acount(), 1)
        obj = await RawProviderProduct.objects.aget(provider=self.provider, external_id="SKU-1")
        self.assertEqual(obj.fetch_count, 100)
        self.assertEqual(obj.content_hash, compute_content_hash(payload))
        self.assertGreaterEqual(obj.last_seen_at, obj.first_seen_at)

    async def test_key_order_does_not_change_hash(self):
        a = {"a": 1, "b": {"x": 1, "y": 2}}
        b = {"b": {"y": 2, "x": 1}, "a": 1}
        self.assertEqual(compute_content_hash(a), compute_content_hash(b))

        await RawProviderProduct.objects.aupsert(self.provider, "SKU-1", a)
        obj, status = await RawProviderProduct.objects.aupsert(self.provider, "SKU-1", b)
        self.assertEqual(status, "unchanged")
        self.assertEqual(obj.fetch_count, 2)

    async def test_different_payload_updates_payload_and_hash(self):
        first, _ = await RawProviderProduct.objects.aupsert(
            self.provider, "SKU-1", {"price": "10.00"}
        )
        obj, status = await RawProviderProduct.objects.aupsert(
            self.provider, "SKU-1", {"price": "12.00"}
        )
        self.assertEqual(status, "updated")
        self.assertEqual(obj.payload, {"price": "12.00"})
        self.assertEqual(obj.content_hash, compute_content_hash({"price": "12.00"}))
        self.assertNotEqual(obj.content_hash, first.content_hash)
        self.assertEqual(obj.first_seen_at, first.first_seen_at)
        self.assertEqual(obj.fetch_count, 2)

        stored = await RawProviderProduct.objects.aget(pk=obj.pk)
        self.assertEqual(stored.payload, {"price": "12.00"})
        self.assertEqual(stored.fetch_count, 2)

    async def test_decimal_payload_is_hashable_and_stable(self):
        payload = {"price": Decimal("19.99")}
        self.assertEqual(compute_content_hash(payload), compute_content_hash(payload))
        await RawProviderProduct.objects.aupsert(self.provider, "SKU-1", payload)
        _, status = await RawProviderProduct.objects.aupsert(self.provider, "SKU-1", payload)
        self.assertEqual(status, "unchanged")

    async def test_same_external_id_in_two_providers(self):
        other = await Provider.objects.acreate(
            code="otro", name="Otro", base_url="https://otro.example.com"
        )
        await RawProviderProduct.objects.aupsert(self.provider, "SKU-1", {"a": 1})
        await RawProviderProduct.objects.aupsert(other, "SKU-1", {"a": 1})
        self.assertEqual(await RawProviderProduct.objects.acount(), 2)


class ConstraintTests(TestCase):
    def setUp(self):
        self.provider = Provider.objects.create(
            code="demo", name="Demo", base_url="https://demo.example.com"
        )

    def test_duplicate_raw_product_is_rejected(self):
        now = timezone.now()
        fields = dict(
            provider=self.provider, external_id="SKU-1", content_hash="x" * 64,
            payload={}, first_seen_at=now, last_seen_at=now,
        )
        RawProviderProduct.objects.create(**fields)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RawProviderProduct.objects.create(**fields)

    def test_one_token_per_provider(self):
        expires = timezone.now() + timezone.timedelta(hours=1)
        ProviderToken.objects.create(provider=self.provider, access_token="a", expires_at=expires)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProviderToken.objects.create(provider=self.provider, access_token="b", expires_at=expires)

    def test_indexes_exist(self):
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, "raw_provider_product")
        self.assertEqual(constraints["raw_pp_payload_gin"]["type"], "gin")
        self.assertIn("raw_pp_last_seen_idx", constraints)
        self.assertTrue(constraints["raw_pp_provider_external_uniq"]["unique"])
