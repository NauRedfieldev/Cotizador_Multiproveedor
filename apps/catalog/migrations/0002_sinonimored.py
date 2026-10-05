from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="SinonimoRed",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("abreviatura", models.CharField(max_length=40, unique=True, verbose_name="abreviatura")),
                ("expansion", models.CharField(max_length=120, verbose_name="expansión")),
            ],
            options={
                "verbose_name": "sinónimo de red",
                "verbose_name_plural": "sinónimos de red",
                "ordering": ["abreviatura"],
            },
        ),
    ]
