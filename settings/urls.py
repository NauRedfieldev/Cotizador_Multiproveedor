"""
URL configuration for multiprovider project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView

urlpatterns = [
    path('admin/', admin.site.urls),
    # Vistas de presentación (templates/): solo renderizan HTML estático.
    # El backend conectará modelos y datos reales más adelante.
    path('', TemplateView.as_view(template_name='inicio.html'), name='inicio'),
    path('clientes/', TemplateView.as_view(template_name='clientes.html'), name='clientes'),
    path('cotizador/', TemplateView.as_view(template_name='cotizador.html'), name='cotizador'),
    path('comparador/', TemplateView.as_view(template_name='comparador.html'), name='comparador'),
    # ensure_csrf_cookie: el modal de envío por correo hace POST por fetch
    # y el JS lee el token CSRF desde la cookie.
    path('resumen/', ensure_csrf_cookie(TemplateView.as_view(template_name='resumen.html')), name='resumen'),
    # App quotes: descarga de PDF oficial y envío de cotización por correo.
    path('', include('apps.quotes.urls')),
]
