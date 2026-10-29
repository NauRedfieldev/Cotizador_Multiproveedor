from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('quotes', '0006_default_commercial_condition'),
    ]

    operations = [
        migrations.AddField(
            model_name='quotesnapshot',
            name='edited_name',
            field=models.TextField(blank=True, default=''),
        ),
    ]
