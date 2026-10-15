"""PDF privados locales: sin URL pública y sin datos personales en las rutas."""

import os
from uuid import uuid4

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible


@deconstructible
class QuotePDFStorage(FileSystemStorage):
    def __init__(self):
        super().__init__(file_permissions_mode=0o600, directory_permissions_mode=0o700)

    @property
    def base_location(self):
        return settings.QUOTE_PDF_ROOT

    @property
    def location(self):
        # No cachear: override_settings y despliegues eligen su propio volumen.
        return os.path.abspath(self.base_location)

    @property
    def base_url(self):
        return None


def quote_pdf_path(instance, filename):
    return f"quotes/{instance.quote_id}/r{instance.number}/{uuid4().hex}.pdf"
