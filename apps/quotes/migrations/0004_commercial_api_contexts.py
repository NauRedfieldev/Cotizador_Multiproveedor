"""Capturas y recibos P4 aditivos; sin backfill, roles o cambios a históricos."""

import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("quotes", "0003_commercial_revision_v2"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(name="QuoteSelectionContext", fields=[
            ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ("actor", models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.PROTECT,
                                        related_name="quote_selection_contexts")),
            ("kind", models.CharField(max_length=6, choices=[("SEARCH", "Búsqueda"), ("FX", "Tipo de cambio")])),
            ("payload", models.JSONField()),
            ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
        ], options={"constraints": [models.CheckConstraint(condition=Q(kind__in=["SEARCH", "FX"]), name="quote_context_kind")]}),
        migrations.CreateModel(name="QuoteAPIRequest", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("actor", models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.PROTECT,
                                        related_name="quote_api_requests")),
            ("request_id", models.UUIDField()),
            ("operation", models.CharField(max_length=8, choices=[("CREATE", "Alta"), ("CLONE", "Revisión"), ("FX", "FX")])),
            ("fingerprint", models.CharField(max_length=64)),
            ("revision", models.ForeignKey("quotes.quoterevision", null=True, blank=True,
                on_delete=django.db.models.deletion.PROTECT, related_name="api_requests")),
            ("context", models.ForeignKey("quotes.quoteselectioncontext", null=True, blank=True,
                on_delete=django.db.models.deletion.PROTECT, related_name="api_requests")),
            ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
        ], options={"constraints": [
            models.UniqueConstraint(fields=["actor", "request_id"], name="quote_api_request_unique"),
            models.CheckConstraint(condition=(Q(operation__in=["CREATE", "CLONE"], revision__isnull=False, context__isnull=True)
                | Q(operation="FX", revision__isnull=True, context__isnull=False)), name="quote_api_result_shape"),
            models.CheckConstraint(condition=Q(fingerprint__regex=r"^[0-9a-f]{64}$"), name="quote_api_fingerprint"),
        ]}),
    ]
