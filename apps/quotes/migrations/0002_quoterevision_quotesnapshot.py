import apps.quotes.storage
import django.core.validators
import django.db.models.deletion
import django.utils.timezone
import uuid
from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('quotes', '0001_initial')]

    operations = [
        migrations.CreateModel(
            name='QuoteRevision',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('number', models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
                ('request_id', models.UUIDField(default=uuid.uuid4, editable=False)),
                ('request_fingerprint', models.CharField(editable=False, max_length=64)),
                ('edit_version', models.PositiveIntegerField(default=1, editable=False)),
                ('status', models.CharField(choices=[('DRAFT', 'Borrador'), ('ISSUED', 'Emitida')], default='DRAFT', max_length=6)),
                ('schema_version', models.PositiveSmallIntegerField(default=1, editable=False)),
                ('header', models.JSONField()),
                ('currency', models.CharField(default='MXN', editable=False, max_length=3)),
                ('exchange_rate', models.DecimalField(blank=True, decimal_places=8, max_digits=18, null=True)),
                ('exchange_rate_source', models.CharField(blank=True, max_length=120)),
                ('exchange_rate_date', models.DateTimeField(blank=True, null=True)),
                ('tax_rate', models.DecimalField(decimal_places=4, max_digits=5, validators=[django.core.validators.MinValueValidator(Decimal('0')), django.core.validators.MaxValueValidator(Decimal('1'))])),
                ('issue_date', models.DateField()),
                ('valid_days', models.PositiveSmallIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
                ('subtotal', models.DecimalField(blank=True, decimal_places=2, max_digits=38, null=True)),
                ('tax', models.DecimalField(blank=True, decimal_places=2, max_digits=38, null=True)),
                ('total', models.DecimalField(blank=True, decimal_places=2, max_digits=38, null=True)),
                ('pdf', models.FileField(blank=True, max_length=200, storage=apps.quotes.storage.QuotePDFStorage(), upload_to=apps.quotes.storage.quote_pdf_path)),
                ('pdf_sha256', models.CharField(blank=True, max_length=64)),
                ('issued_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('based_on', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='derived_revisions', to='quotes.quoterevision')),
                ('quote', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='revisions', to='quotes.quote')),
            ],
            options={'ordering': ['quote_id', 'number']},
        ),
        migrations.CreateModel(
            name='QuoteSnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('position', models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
                ('code', models.TextField(blank=True)),
                ('name', models.TextField()),
                ('description', models.TextField(blank=True)),
                ('provider_code', models.CharField(blank=True, max_length=80)),
                ('provider_name', models.CharField(blank=True, max_length=255)),
                ('external_id', models.TextField(blank=True)),
                ('brand', models.TextField(blank=True)),
                ('model', models.TextField(blank=True)),
                ('mpn', models.TextField(blank=True)),
                ('source_unit_price', models.DecimalField(decimal_places=6, max_digits=18)),
                ('source_currency', models.CharField(choices=[('MXN', 'MXN'), ('USD', 'USD')], max_length=3)),
                ('quantity', models.DecimalField(decimal_places=2, max_digits=10)),
                ('stock', models.PositiveBigIntegerField(blank=True, null=True)),
                ('es_mejor_precio', models.BooleanField(default=False)),
                ('amount', models.DecimalField(blank=True, decimal_places=2, max_digits=38, null=True)),
                ('captured_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('revision', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='snapshots', to='quotes.quoterevision')),
            ],
            options={'ordering': ['position']},
        ),
        migrations.AddConstraint(model_name='quoterevision', constraint=models.UniqueConstraint(fields=('quote', 'number'), name='quote_revision_number_unique')),
        migrations.AddConstraint(model_name='quoterevision', constraint=models.UniqueConstraint(fields=('quote', 'request_id'), name='quote_revision_request_unique')),
        migrations.AddConstraint(model_name='quoterevision', constraint=models.CheckConstraint(condition=models.Q(number__gte=1), name='quote_revision_number_positive')),
        migrations.AddConstraint(model_name='quoterevision', constraint=models.CheckConstraint(condition=models.Q(currency='MXN'), name='quote_revision_currency_mxn')),
        migrations.AddConstraint(model_name='quoterevision', constraint=models.CheckConstraint(condition=models.Q(tax_rate__gte=0, tax_rate__lte=1), name='quote_revision_tax_range')),
        migrations.AddConstraint(
            model_name='quoterevision',
            constraint=models.CheckConstraint(condition=(
                models.Q(status='DRAFT', issued_at__isnull=True, pdf='', pdf_sha256='', subtotal__isnull=True, tax__isnull=True, total__isnull=True)
                | (models.Q(status='ISSUED', issued_at__isnull=False, subtotal__isnull=False, tax__isnull=False, total__isnull=False, subtotal__gte=0, tax__gte=0, total__gte=0)
                   & ~models.Q(pdf='') & ~models.Q(pdf_sha256=''))
            ), name='quote_revision_seal_consistent'),
        ),
        migrations.AddConstraint(model_name='quotesnapshot', constraint=models.UniqueConstraint(fields=('revision', 'position'), name='quote_snapshot_position_unique')),
        migrations.AddConstraint(model_name='quotesnapshot', constraint=models.CheckConstraint(condition=models.Q(position__gte=1, quantity__gt=0, source_unit_price__gte=0), name='quote_snapshot_values_positive')),
        migrations.AddConstraint(model_name='quotesnapshot', constraint=models.CheckConstraint(condition=models.Q(source_currency__in=['MXN', 'USD']), name='quote_snapshot_currency_supported')),
    ]
