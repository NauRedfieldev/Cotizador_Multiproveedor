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
    const cuando = iso => window.cconorFechas.hace(iso);   // 24C: «hace 2 horas»; la exacta va en title
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
        $('fxMenuOrigin').title = s ? window.cconorFechas.exacta(s.date) : '';
        // 21A: «Sigue vigente» en el comparador guarda confirmed; el aviso cuenta desde ahí.
        $('fxMenuOtroDia').textContent = s && !esHoy(s.confirmed || s.date) ? 'Capturado otro día: confirma que siga vigente.' : '';
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

/* 19A: bandeja del comparador. «Agregar» pone la oferta exacta en la bandeja de la sesión; el cotizador
 * la trae volviendo a consultar al proveedor. Aquí solo se muestran precios de referencia. */
(function () {
    'use strict';
    const caja = document.getElementById('bandeja');
    const api = window.apiClient?.commercial;
    if (!caja || !api) return;
    const $ = id => document.getElementById(id);
    let items = [];
    let abierta = false;

    function nodo(tag, texto, clase) {
        const n = document.createElement(tag);
        if (texto !== undefined) n.textContent = texto;
        if (clase) n.className = clase;
        return n;
    }
    function boton(texto, etiqueta, clase, accion) {
        const b = nodo('button', texto, clase);
        b.type = 'button';
        b.setAttribute('aria-label', etiqueta);
        b.addEventListener('click', accion);
        return b;
    }
    async function cambiar(peticion) {
        $('bandejaError').textContent = '';
        try {
            pintar((await peticion).data);
            return true;
        } catch (err) {
            abrir(true);
            $('bandejaError').textContent = err.message;
            return false;
        }
    }
    // 8C: con la bandeja cerrada, «Agregar» no la abre: un punto «vuela» del botón a la pastilla y esta
    // se estira unos segundos con el producto agregado. La bandeja solo se abre si el asesor la pide.
    let avisoTemporizador = null;
    function avisar(item) {
        if (!item) return;
        const pastilla = $('bandejaPastilla');
        const total = `${items.length} ${items.length === 1 ? 'producto' : 'productos'}`;
        $('bandejaAvisoNombre').textContent = item.name;
        $('bandejaAvisoDetalle').textContent = `agregado · ahora hay ${total}`;
        $('bandejaPastillaConteo').textContent = String(items.length);
        $('bandejaAnuncio').textContent = `${item.name} agregado a la bandeja. Ahora hay ${total}.`;
        pastilla.classList.remove('is-aviso');
        void pastilla.offsetWidth;  // reinicia la animación si se agregan varios seguidos
        pastilla.classList.add('is-aviso');
        clearTimeout(avisoTemporizador);
        avisoTemporizador = setTimeout(() => pastilla.classList.remove('is-aviso'), 3500);
    }
    function volar(origen) {
        if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches || !document.body.animate) return;
        const desde = origen.getBoundingClientRect();
        const pastilla = caja.hidden ? null : $('bandejaPastilla').getBoundingClientRect();
        const x0 = desde.left + desde.width / 2, y0 = desde.top + desde.height / 2;
        const dx = (pastilla ? pastilla.left + pastilla.width / 2 : window.innerWidth - 90) - x0;
        const dy = (pastilla ? pastilla.top + pastilla.height / 2 : window.innerHeight - 48) - y0;
        const punto = nodo('span', undefined, 'bandeja-vuelo');
        Object.assign(punto.style, { left: `${x0}px`, top: `${y0}px` });
        document.body.append(punto);
        const vuelo = punto.animate([
            { transform: 'translate(-50%, -50%) scale(1)', opacity: 1 },
            { transform: `translate(calc(-50% + ${dx * 0.55}px), calc(-50% + ${dy * 0.35 - 40}px)) scale(0.9)`, opacity: 1, offset: 0.5 },
            { transform: `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px)) scale(0.45)`, opacity: 0.25 }
        ], { duration: 650, easing: 'cubic-bezier(.4, 0, .2, 1)' });
        vuelo.onfinish = vuelo.oncancel = () => punto.remove();
    }
    function abrir(valor) {
        if (valor) { clearTimeout(avisoTemporizador); $('bandejaPastilla').classList.remove('is-aviso'); }
        abierta = valor;
        $('bandejaPanel').hidden = !abierta;
        $('bandejaPastilla').hidden = abierta;
        $('bandejaPastilla').setAttribute('aria-expanded', String(abierta));
    }
    function pintar(lista) {
        items = lista;
        const piezas = items.reduce((total, i) => total + i.quantity, 0);
        caja.hidden = !items.length && !$('bandejaError').textContent;
        $('bandejaPastillaTexto').textContent = `Bandeja · ${items.length} ${items.length === 1 ? 'producto' : 'productos'}`;
        $('bandejaPiezas').textContent = `${piezas} ${piezas === 1 ? 'pieza' : 'piezas'}`;
        $('bandejaLista').replaceChildren(...items.map(item => {
            // 20A: el nombre ocupa hasta dos líneas; debajo, proveedor y precio con el contador a la derecha.
            const fila = nodo('li');
            const nombre = nodo('strong', item.name, 'bandeja-nombre');
            nombre.title = item.name;
            const detalle = nodo('div', undefined, 'bandeja-detalle');
            const cantidad = nodo('span', undefined, 'bandeja-cantidad');
            cantidad.append(
                boton('−', `Quitar una pieza de ${item.name}`, '', () => cambiar(api.traySet(item.key, item.quantity - 1))),
                nodo('strong', String(item.quantity)),
                boton('+', `Agregar una pieza de ${item.name}`, '', () => cambiar(api.trayAdd(item.key))));
            detalle.append(nodo('span', `${item.provider_name} · $${item.price} ${item.currency}`, 'text-muted flex-grow-1'),
                cantidad, boton('×', `Quitar ${item.name} de la bandeja`, 'bandeja-quitar', () => cambiar(api.traySet(item.key, 0))));
            fila.append(nombre, detalle);
            return fila;
        }));
        // Cada oferta de la página indica si ya está en la bandeja y cuántas piezas lleva.
        for (const b of document.querySelectorAll('[data-bandeja-clave]')) {
            const enBandeja = items.find(i => i.key === b.dataset.bandejaClave);
            b.classList.toggle('bandeja-en', !!enBandeja);
            b.querySelector('span').textContent = enBandeja ? `En la bandeja · ${enBandeja.quantity}` : 'Agregar';
            b.querySelector('i').className = enBandeja ? 'bi bi-check-lg' : 'bi bi-plus-lg';
        }
        if (!items.length) abrir(false);
    }
    document.addEventListener('click', async event => {
        const b = event.target.closest('[data-bandeja-clave]');
        if (!b) return;
        const clave = b.dataset.bandejaClave;
        if (!abierta) volar(b);
        if (await cambiar(api.trayAdd(clave)) && !abierta) avisar(items.find(i => i.key === clave));
    });
    // 6A: destino de la bandeja. «Borrador» lista los borradores propios (v2) y lleva al cotizador con
    // ?folio=…&revision=…&bandeja=traer: allí se vuelve a consultar al proveedor y se agrega; no se guarda solo.
    const ir = $('bandejaIr');
    const hace = iso => window.cconorFechas?.hace(iso) ?? String(iso ?? '');
    let destino = 'nueva', borradores = [], elegido = null, turno = 0, temporizador = null, cargados = false;
    function pintarIr() {
        const base = ir.dataset.cotizadorUrl;
        const listo = destino === 'nueva' || !!elegido;
        ir.classList.toggle('disabled', !listo);
        ir.setAttribute('aria-disabled', String(!listo));
        if (destino === 'nueva') { ir.textContent = 'Abrir el cotizador →'; ir.href = base; }
        else if (elegido) {
            ir.textContent = `Agregar a ${elegido.folio} →`;
            ir.href = `${base}?${new URLSearchParams({ folio: elegido.folio, revision: String(elegido.revision), bandeja: 'traer' })}`;
        } else { ir.textContent = 'Elige un borrador'; ir.href = base; }
    }
    function pintarBorradores(hayMas) {
        $('bandejaBorradoresLista').replaceChildren(...borradores.map(borrador => {
            const fila = nodo('label', undefined, 'bandeja-borrador');
            const radio = nodo('input');
            Object.assign(radio, { type: 'radio', name: 'bandejaBorrador', value: borrador.folio, checked: elegido?.folio === borrador.folio });
            radio.addEventListener('change', () => { elegido = borrador; pintarIr(); });
            const info = nodo('span', undefined, 'bandeja-borrador-info');
            info.append(nodo('strong', borrador.folio), nodo('span', borrador.client_name || 'Sin cliente', 'bandeja-borrador-cliente'));
            const partidas = borrador.items_count === 1 ? '1 partida' : `${borrador.items_count} partidas`;
            const fecha = nodo('span', `${hace(borrador.issue_date)} · ${partidas}`, 'bandeja-borrador-fecha');
            fecha.title = window.cconorFechas?.exacta(borrador.issue_date) ?? '';
            fila.append(radio, info, fecha);
            return fila;
        }));
        $('bandejaBorradoresEstado').textContent = !borradores.length ? 'No hay borradores con esa búsqueda.'
            : hayMas ? 'Se muestran los más recientes; busca por folio o cliente.' : '';
    }
    async function cargarBorradores() {
        const propio = ++turno;
        const q = $('bandejaBuscar').value.trim();
        $('bandejaBorradoresEstado').textContent = 'Buscando…';
        try {
            const respuesta = await api.list({ status: 'DRAFT', page: '1', ...(q ? { q } : {}) });
            if (propio !== turno) return;
            borradores = respuesta.data.filter(b => b.schema_version === 2);  // los históricos v1 no se editan
            if (!borradores.some(b => b.folio === elegido?.folio)) elegido = borradores[0] ?? null;
            pintarBorradores((respuesta.pagination?.pages ?? 1) > 1);
        } catch (err) {
            if (propio !== turno) return;
            borradores = []; elegido = null;
            $('bandejaBorradoresLista').replaceChildren();
            $('bandejaBorradoresEstado').textContent = err.message;
        }
        pintarIr();
    }
    for (const radio of document.querySelectorAll('input[name="bandejaDestino"]')) {
        radio.addEventListener('change', () => {
            destino = radio.value;
            $('bandejaBorradores').hidden = destino !== 'borrador';
            if (destino === 'borrador' && !cargados) { cargados = true; cargarBorradores(); }
            pintarIr();
        });
    }
    $('bandejaBuscar').addEventListener('input', () => { clearTimeout(temporizador); temporizador = setTimeout(cargarBorradores, 300); });
    ir.addEventListener('click', event => { if (ir.getAttribute('aria-disabled') === 'true') event.preventDefault(); });
    pintarIr();
    $('bandejaPastilla').addEventListener('click', () => abrir(true));
    $('bandejaMinimizar').addEventListener('click', () => abrir(false));
    $('bandejaVaciar').addEventListener('click', () => {
        if (window.confirm('¿Vaciar la bandeja? No cambia ninguna cotización.')) cambiar(api.trayRemove());
    });
    abrir(false);
    api.tray().then(r => pintar(r.data)).catch(() => { /* sin bandeja si no se pudo leer */ });
})();
