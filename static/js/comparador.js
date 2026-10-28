/* Carga diferida del comparador (docs/34).
 *
 * La página llega con los proveedores rápidos (SYSCOM). Los de CCONOR_PROVEEDORES_DIFERIDOS (Exel) se
 * piden aquí, una sola vez, y sus tarjetas se intercalan en el orden de la vista: nivel de coincidencia,
 * precio en MXN, proveedor e id (apps/quotes/views.py, buscar). Sin #comparadorPendiente no hace nada. */
(function () {
    'use strict';
    const pendiente = document.getElementById('comparadorPendiente');
    if (!pendiente) return;
    const lista = document.getElementById('comparadorOfertas');
    const nombres = pendiente.dataset.pendientes;
    const TODOS_CAIDOS = 'Ningún proveedor pudo responder en este momento. Inténtalo de nuevo en unos minutos.';

    function clave(tarjeta) {
        const d = tarjeta.dataset;
        return [Number(d.nivel), Number(d.precio), d.proveedor, d.id];
    }
    function vaAntes(a, b) {
        const x = clave(a), y = clave(b);
        for (let i = 0; i < x.length; i++) {
            if (x[i] < y[i]) return true;
            if (x[i] > y[i]) return false;
        }
        return false;
    }
    function insertar(html) {
        const plantilla = document.createElement('template');
        plantilla.innerHTML = html;  // HTML del servidor, escapado por las plantillas de Django
        const existentes = [...lista.querySelectorAll('.comparador-oferta')];
        for (const nueva of plantilla.content.querySelectorAll('.comparador-oferta')) {
            const siguiente = existentes.find(t => vaAntes(nueva, t));
            lista.insertBefore(nueva, siguiente || pendiente);
            existentes.splice(siguiente ? existentes.indexOf(siguiente) : existentes.length, 0, nueva);
        }
        // «Otros resultados relacionados» va justo antes de la primera sin coincidencia.
        lista.querySelectorAll('.comparador-separador').forEach(s => s.remove());
        const primera = lista.querySelector('.comparador-oferta[data-sin-coincidencia="1"]');
        const separador = document.getElementById('comparadorSeparadorPlantilla');
        if (primera && separador) lista.insertBefore(separador.content.cloneNode(true), primera);
        document.dispatchEvent(new CustomEvent('comparador:ofertas'));
    }
    function caidos(nuevos) {
        const aviso = document.getElementById('comparadorAvisoCaidos');
        const texto = document.getElementById('comparadorCaidos');
        if (!aviso || !texto) return [];  // el asesor cerró el aviso
        const actuales = texto.textContent.split(', ').filter(Boolean);
        const todos = actuales.concat(nuevos.filter(n => !actuales.includes(n)));
        texto.textContent = todos.join(', ');
        aviso.hidden = !todos.length;
        return todos;
    }
    function terminar(totalCaidos) {
        pendiente.remove();
        const vacio = document.getElementById('comparadorSinResultados');
        if (lista.querySelector('.comparador-oferta') || !vacio) return;
        if (totalCaidos >= (Number(lista.dataset.proveedores) || Infinity)) vacio.querySelector('p').textContent = TODOS_CAIDOS;
        vacio.hidden = false;
    }
    function fallo(mensaje) {
        pendiente.setAttribute('role', 'alert');
        pendiente.classList.replace('text-secondary', 'text-danger');
        const icono = document.createElement('i');
        icono.className = 'bi bi-cloud-slash-fill';
        icono.setAttribute('aria-hidden', 'true');
        const texto = document.createElement('span');
        texto.textContent = mensaje;
        pendiente.replaceChildren(icono, texto);
    }

    fetch(pendiente.dataset.pendienteUrl, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
        .then(async respuesta => {
            let datos = null;
            try { datos = await respuesta.json(); } catch { /* p. ej. la página de acceso si caducó la sesión */ }
            if (!respuesta.ok || !datos) throw new Error(datos?.error || '');
            return datos;
        })
        .then(datos => {
            insertar(datos.html || '');
            const total = caidos(datos.caidos || []);
            if (datos.error) fallo(`No se pudo consultar ${nombres}: ${datos.error}`);
            else terminar(total.length);
        })
        .catch(error => fallo(error.message || `No se pudo consultar ${nombres}. Vuelve a buscar.`));
})();
