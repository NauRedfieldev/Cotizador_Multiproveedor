/**
 * CCONOR Soluciones Tecnológicas
 * Script principal del sistema
 */

document.addEventListener('DOMContentLoaded', function () {
    console.log('CCONOR - Sistema de Cotización Multi-Proveedor cargado correctamente.');

    // Inicializar tooltips de Bootstrap si están presentes
    const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl);
    });

    // Avisos emergentes: un botón con data-enfocar="id" cierra su aviso y lleva al campo.
    document.addEventListener('click', function (event) {
        const boton = event.target.closest('[data-enfocar]');
        if (!boton) return;
        const aviso = boton.closest('.alert');
        if (aviso) bootstrap.Alert.getOrCreateInstance(aviso).close();
        const campo = document.getElementById(boton.dataset.enfocar);
        if (campo) campo.focus();
    });
});

/* 15A: tipo de cambio de la sesión. Uno solo para el comparador, el cotizador y el chip del menú.
 * window.cconorTipoCambio.cargar() / .guardar(cuerpo) devuelven {session, syscom, syscom_failed}
 * y avisan con el evento «cconor:tipo-cambio». Nada se aplica solo: siempre lo decide el asesor. */
(function () {
    'use strict';
    const item = document.getElementById('fxChipItem');
    let ultimo = null;
    let carga = null;

    function csrf() {
        const par = document.cookie.split(';').map(c => c.trim()).find(c => c.startsWith('csrftoken='));
        return par ? decodeURIComponent(par.slice('csrftoken='.length)) : '';
    }
    async function pedir(opciones) {
        if (!item) throw new Error('Sin sesión.');
        const respuesta = await fetch(item.dataset.url, { credentials: 'same-origin', cache: 'no-store', ...opciones,
            headers: { 'Accept': 'application/json', ...(opciones?.body ? { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() } : {}) } });
        let cuerpo = null;
        try { cuerpo = await respuesta.json(); } catch (_) { /* respuesta no JSON: error abajo */ }
        if (!respuesta.ok || !cuerpo || cuerpo.success !== true) throw new Error(cuerpo?.error || 'No se pudo guardar el tipo de cambio.');
        ultimo = cuerpo.data;
        document.dispatchEvent(new CustomEvent('cconor:tipo-cambio', { detail: ultimo }));
        return ultimo;
    }
    const sinCeros = v => String(v).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '');
    // Para mostrar: sin ceros de más, pero con al menos dos decimales (18.70, 17.12345678).
    const mostrar = v => { const [entero, fraccion = ''] = sinCeros(v).split('.'); return `${entero}.${fraccion.padEnd(2, '0')}`; };
    function cuando(iso) {
        const fecha = new Date(iso);
        if (Number.isNaN(fecha.getTime())) return '';
        const hora = fecha.toLocaleTimeString('es-MX', { hour: '2-digit', minute: '2-digit' });
        return esHoy(iso) ? `hoy ${hora}` : `${fecha.toLocaleDateString('es-MX', { dateStyle: 'short' })} ${hora}`;
    }
    const esHoy = iso => new Date(iso).toDateString() === new Date().toDateString();
    const origen = s => (s.source === 'syscom' ? 'SYSCOM' : 'manual');

    window.cconorTipoCambio = Object.freeze({
        cargar: () => (carga ??= pedir({ method: 'GET' })),
        guardar: cuerpo => pedir({ method: 'PUT', body: JSON.stringify(cuerpo) }),
        ultimo: () => ultimo,
        sinCeros, mostrar, cuando, esHoy, origen,
    });
    if (!item) return;

    const $ = id => document.getElementById(id);
    function pintar(datos) {
        const s = datos.session, sys = datos.syscom;
        const chip = $('fxChip');
        chip.classList.toggle('cconor-fx-vacio', !s);
        $('fxChipIcon').hidden = !s;
        const texto = $('fxChipText');
        texto.replaceChildren();
        if (s) {
            const extra = document.createElement('span');
            extra.className = 'cconor-fx-origen';
            extra.textContent = `· ${origen(s)}`;
            texto.append(`USD $${mostrar(s.value)} `, extra);
        } else {
            texto.textContent = 'USD sin capturar'; // el de SYSCOM se ve al abrirlo
        }
        chip.setAttribute('aria-label', s ? `Tipo de cambio de tu sesión: 1 USD = ${mostrar(s.value)} MXN, ${origen(s)}` : 'Tipo de cambio sin capturar');
        $('fxMenuValue').textContent = s ? `1 USD = $${mostrar(s.value)} MXN` : 'Sin capturar';
        $('fxMenuOrigin').textContent = s ? `${s.source === 'syscom' ? 'SYSCOM' : 'Capturado a mano'}, ${cuando(s.date)}` : '';
        $('fxMenuOtroDia').textContent = s && !esHoy(s.date) ? 'Capturado otro día: confirma que siga vigente.' : '';
        // SYSCOM: «Usar» si aún no hay uno; «Actualizar» si el de la sesión vino de SYSCOM y cambió.
        const ofrecer = sys && (!s || (s.source === 'syscom' && sinCeros(s.value) !== sinCeros(sys.value)));
        $('fxMenuSyscom').hidden = !ofrecer;
        if (ofrecer) {
            $('fxMenuSyscomText').textContent = `${s ? 'SYSCOM ahora' : 'SYSCOM hoy'}: $${mostrar(sys.value)}${sys.label ? ` (${sys.label})` : ''}`;
            $('fxMenuUseSyscom').textContent = s ? 'Actualizar' : 'Usar';
        }
        item.hidden = false;
    }
    // En el comparador la página se vuelve a buscar con el valor nuevo, para no mostrar precios con el anterior.
    function recargarComparador(datos) {
        if (!document.querySelector('[data-tipo-cambio-recargar]')) return;
        const parametros = new URLSearchParams(window.location.search);
        parametros.delete('usar_tipo_cambio');
        if (parametros.has('tipo_cambio')) parametros.set('tipo_cambio', datos.session.value);
        window.location.assign(window.location.pathname + (parametros.toString() ? `?${parametros}` : ''));
    }
    async function aplicar(cuerpo) {
        $('fxMenuError').textContent = '';
        try {
            const datos = await window.cconorTipoCambio.guardar(cuerpo);
            $('fxMenuInput').value = '';
            bootstrap.Dropdown.getOrCreateInstance($('fxChip')).hide();
            recargarComparador(datos);
        } catch (err) {
            $('fxMenuError').textContent = err.message;
        }
    }
    document.addEventListener('cconor:tipo-cambio', event => pintar(event.detail));
    $('fxMenuUseSyscom').addEventListener('click', () => aplicar({ use_syscom: true }));
    $('fxMenuForm').addEventListener('submit', event => {
        event.preventDefault();
        const valor = $('fxMenuInput').value.trim();
        if (!/^\d+(\.\d+)?$/.test(valor)) { $('fxMenuError').textContent = 'Usa un decimal positivo con punto, sin separadores de miles.'; return; }
        aplicar({ value: valor });
    });
    window.cconorTipoCambio.cargar().catch(() => { /* sin chip si no se pudo leer */ });
})();
