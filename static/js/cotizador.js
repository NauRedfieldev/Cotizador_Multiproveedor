/* P5: captura/presentación interna. El dinero se calcula SOLO en Python.
 * No mocks, autosave, localStorage financiero ni emisión documental en este flujo.
 */
(function () {
    'use strict';
    const fail = message => { throw new Error(message); };
    function decimal(value, digits, places, { positive = false, max } = {}) {
        if (typeof value !== 'string') fail('El dinero debe conservarse como texto decimal.');
        const text = value.trim();
        if (text.length > 80 || !/^\d+(\.\d+)?$/.test(text)) fail('Usa un decimal con punto, sin exponentes ni separadores.');
        const [integer, fraction = ''] = text.split('.');
        if ((integer.replace(/^0+/, '') || '0').length > digits - places || fraction.replace(/0+$/, '').length > places) fail(`Máximo ${places} decimales y ${digits - places} dígitos enteros.`);
        const scaled = BigInt(integer + fraction.slice(0, places).padEnd(places, '0'));
        if (positive && scaled === 0n) fail('El valor debe ser mayor que cero.');
        if (max !== undefined && scaled > BigInt(max)) fail('Valor fuera del rango permitido.');
        return text;
    }
    function integer(value, max = 99999999) {
        const text = String(value ?? '').trim();
        if (!/^\d+$/.test(text) || text.length > 10 || BigInt(text) < 1n || BigInt(text) > BigInt(max)) fail(`Se requiere un entero entre 1 y ${max}; no se truncan fracciones.`);
        return Number(text); // Solo cantidades acotadas, NUNCA precios/tasas.
    }
    function money(value) {
        if (value === null || value === undefined) return 'No disponible';
        const text = String(value);
        if (!/^-?\d+(\.\d+)?$/.test(text)) return '—';
        const negative = text.startsWith('-');
        const [whole, fraction = ''] = text.replace(/^-/, '').split('.');
        return (negative ? '-$' : '$') + whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + '.' + fraction.padEnd(2, '0');
    }
    function storedInteger(value) {
        const text = String(value);
        if (!/^\d+(?:\.0+)?$/.test(text)) fail('La cantidad comercial guardada no es entera.');
        return integer(text.split('.')[0]);
    }
    function price(value) {
        if (!['MXN', 'USD'].includes(value.currency)) fail('Selecciona MXN o USD.');
        const data = { amount: decimal(value.amount, 18, 6), currency: value.currency, vat_basis: value.vat_basis };
        if (data.vat_basis === 'INCLUDED') data.vat_rate = decimal(value.vat_rate, 5, 4, { max: '10000' });
        else if (data.vat_basis !== 'NET') fail('Indica si la entrada incluye IVA o es neta.');
        return data;
    }
    function item(line) {
        if (!line.units.confirmed) fail('Confirma unidades y factor de conversión.');
        if (!line.units.source_label.trim() || !line.units.sale_label.trim()) fail('Indica la unidad original y la vendida.');
        const data = { line_key: line.key, source: line.persisted ? { type: 'EXISTING' } : line.kind === 'API' ?
            { type: 'API', context_ref: line.offer.context_ref, offer_ref: line.offer.offer_ref } :
            { type: 'MANUAL', name: line.name.trim() }, quantity: integer(line.quantity),
            units: { source_label: line.units.source_label.trim(), sale_label: line.units.sale_label.trim(),
                conversion_factor: decimal(line.units.conversion_factor, 18, 6, { positive: true }), confirmed: true },
            description: line.description, pricing: { mode: line.mode } };
        if (line.kind === 'MANUAL') {
            if (!line.name.trim()) fail('El concepto manual requiere nombre.');
            data.cost = line.knownCost ? price(line.cost) : null;
        }
        if (line.mode === 'MARGIN') {
            if (line.kind === 'MANUAL' && !line.knownCost) fail('Sin costo conocido debes definir una venta manual.');
            data.pricing.margin_percent = decimal(line.margin, 4, 2, { max: '9999' });
        } else if (line.mode === 'MANUAL') data.pricing.sale = price(line.sale);
        else fail('Modalidad de precio inválida.');
        return data;
    }
    function editOutcome(detail, intent) {
        if (detail.schema_version !== 2 || detail.status !== 'DRAFT') return 'CONFLICT';
        if (detail.edit_version === intent.version + 1 && detail.state_fingerprint === intent.fingerprint) return 'SAVED';
        if (detail.edit_version === intent.version && detail.state_fingerprint === intent.baseFingerprint) return 'RETRY';
        return 'CONFLICT';
    }
    function retainIntent(error, previouslyUncertain = false) {
        return previouslyUncertain || error.uncertain || error.status === 401 || error.status === 0;
    }
    // M2: condiciones predeterminadas del servidor; solo texto inicial editable, una por línea.
    function defaultConditionsText(json) {
        let list;
        try { list = JSON.parse(json || '[]'); } catch { return ''; }
        if (!Array.isArray(list)) return '';
        return list.filter(s => typeof s === 'string').map(s => s.trim()).filter(s => s && !/[\r\n]/.test(s)).slice(0, 50).join('\n');
    }
    // Pista solo cuando la búsqueda falla por falta de tipo de cambio, según el permiso del usuario.
    function fxSearchHint(message, canChangeFx) {
        if (!/tipo de cambio/i.test(message || '')) return '';
        return canChangeFx ? ' Aplícalo en «Ajustes globales» y vuelve a buscar.'
            : ' No tienes permiso para capturar el tipo de cambio; pide a un administrador que te lo habilite.';
    }
    window.CCONORQuoteTools = Object.freeze({ decimal, integer, storedInteger, money, price, item, editOutcome, retainIntent, defaultConditionsText, fxSearchHint });
    if (typeof document === 'undefined') return;
    document.addEventListener('DOMContentLoaded', function () {
        const $ = id => document.getElementById(id);
        if (!$('commercialQuoteApp')) return;
        const api = window.apiClient.commercial;
        const initialIssueDate = $('issueDate').value;
        const defaultConditions = defaultConditionsText($('defaultConditions')?.textContent);
        const state = { caps: null, lines: [], products: [], saved: null, fx: null, dirty: false, busy: false,
            readonly: false, preview: null, epoch: 0, pending: null, fxPending: null, conflict: false };
        let previewTimer, previewAbort, searchTimer, searchAbort, searchVersion = 0, searchError = '';
        const has = code => !!state.caps?.[code];
        const canEdit = () => !!state.caps && !state.readonly;
        const editable = () => canEdit() && !state.busy && !state.pending;
        const text = (id, value) => { $(id).textContent = value; };
        function element(tag, value, className = '') {
            const node = document.createElement(tag);
            node.textContent = value;
            node.className = className;
            return node;
        }
        function error(message = '') {
            text('quoteError', message);
            $('quoteError').classList.toggle('d-none', !message);
        }
        function showError(err) {
            error(err.message + (err.status === 401 ? ' Abre el inicio de sesión en otra pestaña y conserva este formulario para reintentar.' : ''));
        }
        function locks() {
            const allowed = editable();
            if (!allowed) for (const control of $('quoteItemsBody').querySelectorAll('input, select, button')) control.disabled = true;
            if (state.busy || state.pending) for (const control of $('quoteWarnings').querySelectorAll('button')) control.disabled = true;
            for (const control of $('resultsList').querySelectorAll('button')) control.disabled = !allowed || control.dataset.verified !== 'true';
            $('refreshPermissionsBtn').disabled = state.busy;
            $('quoteHeader').disabled = !allowed || !!state.transition;
            $('quoteNotes').disabled = !allowed || !!state.saved || !!state.transition;
            $('viewSummaryBtn').disabled = state.busy || !!state.pending || !!state.fxPending || !state.saved || state.dirty || !state.caps;
            for (const id of ['saveQuoteBtn', 'clearTableBtn', 'addManualBtn', 'searchProductBtn', 'productSearchInput', 'newQuoteBtn', 'openQuoteBtn', 'applyFxBtn', 'clearFxBtn', 'newExchangeRate', 'taxRate', 'discountPercent']) {
                $(id).disabled = !allowed;
            }
            $('newQuoteBtn').disabled = state.busy || !!state.pending || !!state.fxPending;
            $('openQuoteBtn').disabled = !state.caps || state.busy || !!state.pending || !!state.fxPending;
            $('saveQuoteBtn').disabled = state.busy || !!state.fxPending || (!state.pending && (!canEdit() || !state.lines.length || state.conflict));
            text('saveQuoteBtn', state.busy ? 'Procesando…' : state.pending ? 'Resolver / reintentar el mismo guardado' : state.saved ? 'Actualizar borrador' : 'Guardar borrador');
            $('addManualBtn').disabled = !allowed || !has('set_commercial_sale');
            text('manualPermissionHint', has('set_commercial_sale') ? 'Captura manual autorizada. No se atribuye a una API.' : 'Requiere permiso de captura/venta manual.');
            $('taxRate').disabled = !allowed || !has('change_commercial_tax');
            $('discountPercent').disabled = !allowed || !has('apply_commercial_discount');
            text('taxPermissionHint', has('change_commercial_tax') ? '' : 'IVA conservado; sin permiso para modificarlo.');
            text('discountPermissionHint', has('apply_commercial_discount') ? '' : 'Descuento conservado; sin permiso para modificarlo.');
            for (const id of ['applyFxBtn', 'clearFxBtn', 'newExchangeRate']) $(id).disabled = !allowed || !has('change_commercial_fx');
            if (state.fxPending) { $('newExchangeRate').disabled = true; $('clearFxBtn').disabled = true; $('applyFxBtn').disabled = state.busy; }
            text('applyFxBtn', state.fxPending ? 'Resolver tipo de cambio' : 'Aplicar tipo de cambio');
            text('fxPermissionHint', has('change_commercial_fx') ? 'Cambiar el tipo de cambio es una acción explícita; una búsqueda no lo reemplaza.' : 'Sin permiso para cambiar el tipo de cambio. El ya guardado se conserva al editar.');
            text('quoteStatus', state.pending ? 'Resultado de guardado por resolver' : state.readonly ? 'Solo lectura' : state.conflict ? 'Conflicto: recarga explícita necesaria' :
                state.busy ? 'Procesando…' : state.dirty ? 'Cambios pendientes de guardar' : state.saved ? 'Guardado' : state.caps ? 'Nuevo · sin guardar' : 'Sin conexión autorizada');
        }
        function clearSummary(message = 'Pendiente de cálculo actualizado') {
            state.preview = null;
            for (const id of ['costBaseDisplay', 'saleSubtotalDisplay', 'discountDisplay', 'subtotalDisplay', 'ivaDisplay', 'totalFinalDisplay', 'marginDisplay']) text(id, '—');
            text('calculationStatus', message);
            text('costCompleteness', '');
            text('quoteWarnings', 'Sin cálculo vigente. Las aceptaciones anteriores no autorizan cambios nuevos.');
            for (const row of $('quoteItemsBody').querySelectorAll('[data-line-key]')) {
                row.querySelector('.sale-unit').textContent = '—';
                row.querySelector('.sale-amount').textContent = '—';
            }
        }
        function changed() {
            state.epoch += 1;
            state.dirty = true;
            previewAbort?.abort();
            clearTimeout(previewTimer);
            clearSummary();
            for (const row of $('quoteItemsBody').querySelectorAll('[data-line-key]')) {
                const line = state.lines.find(l => l.key === row.dataset.lineKey);
                if (line) {
                    row.children[1].textContent = `${line.name || 'Concepto manual sin nombre'} · ${line.provider || 'Manual'}`;
                    row.children[2].textContent = line.units.sale_label || 'Pendiente';
                    row.children[3].textContent = line.quantity;
                }
            }
            locks();
            if (state.lines.length) previewTimer = setTimeout(() => calculate().catch(() => {}), 350);
        }
        function header() {
            const name = $('clientName').value.trim();
            if (!name) fail('Captura la razón social para calcular o guardar.');
            if ($('clientEmail').value && !$('clientEmail').checkValidity()) fail('Correo del cliente inválido.');
            const rfc = $('clientRfc').value.trim().toUpperCase();
            if (rfc && !/^[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}$/.test(rfc)) fail('RFC inválido; usa el formato sin guiones.');
            const conditions = $('quoteConditions').value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
            if (conditions.length > 50 || conditions.some(s => s.length > 6000)) fail('Máximo 50 condiciones de hasta 6000 caracteres.');
            return { client_name: name, client_rfc: rfc, client_email: $('clientEmail').value.trim(),
                client_contact: $('clientContact').value.trim(), client_phone: $('clientPhone').value.trim(), client_project: $('clientProject').value.trim(),
                issue_date: $('issueDate').value, valid_days: integer($('validDays').value, 3650), conditions,
                tax_rate: decimal($('taxRate').value, 5, 4, { max: '10000' }),
                discount_percent: decimal($('discountPercent').value, 5, 2, { max: '10000' }), fx_context_id: state.fx?.id || null };
        }
        function payload() {
            if (!state.lines.length) fail('Agrega al menos una partida.');
            const inputs = state.transition ? { schema_version: 2, source_edit_version: state.transition.edit_version,
                tax_rate: decimal($('taxRate').value, 5, 4, { max: '10000' }),
                discount_percent: decimal($('discountPercent').value, 5, 2, { max: '10000' }), fx_context_id: state.fx?.id || null } : header();
            const body = { ...inputs, items: state.lines.map(line => {
                try { return item(line); } catch (err) { fail(`Partida ${state.lines.indexOf(line) + 1}: ${err.message}`); }
            }) };
            if (state.saved) body.edit_version = state.saved.edit_version;
            else body.schema_version = 2;
            return body;
        }
        function warnings(entries, persisted) {
            $('quoteWarnings').replaceChildren();
            if (!entries?.length) { text('quoteWarnings', 'Sin advertencias comerciales en este cálculo.'); return; }
            const labels = { MARGEN_CERO: 'Margen cero', COSTO_DESCONOCIDO: 'Costo desconocido: utilidad incompleta',
                VENTA_BAJO_COSTO: 'Venta bajo costo', VENTA_CERO: 'Venta de partida en cero', TOTAL_CERO: 'Total de cotización en cero',
                STOCK_CERO: 'El proveedor informa existencias en cero', STOCK_INSUFICIENTE: 'Existencias del proveedor menores que la cantidad (en unidad vendida)',
                STOCK_DESCONOCIDO: 'Existencias desconocidas: comprobar disponibilidad con el proveedor' };
            for (const warning of entries) {
                const lineIndex = state.lines.findIndex(l => l.key === warning.line_key);
                if (warning.requires_confirmation === false) {
                    const info = element('div', '', 'alert alert-info py-2 small');
                    info.append(element('div', `${warning.line_key ? 'Partida ' + (lineIndex + 1) : 'Global'}: ${labels[warning.code] || warning.code}`, 'fw-bold'),
                        element('div', 'Aviso informativo; no requiere aceptación ni bloquea la emisión.'));
                    $('quoteWarnings').append(info);
                    continue;
                }
                const box = element('div', '', 'alert alert-warning py-2 small');
                box.append(element('div', `${warning.line_key ? 'Partida ' + (lineIndex + 1) : 'Global'}: ${labels[warning.code] || warning.code}`, 'fw-bold'));
                box.append(element('div', warning.confirmed ? 'Aceptación vigente para esta versión.' : 'Pendiente de aceptación autorizada.'));
                const button = element('button', warning.confirmed ? 'Aceptada' : 'Aceptar esta advertencia', 'btn btn-sm btn-outline-dark mt-1');
                button.type = 'button';
                button.disabled = !persisted || state.dirty || state.busy || !has(warning.required_capability) || warning.confirmed;
                box.append(button, element('div', !persisted || state.dirty ? 'Guarda y recarga la versión antes de confirmar.' :
                    !has(warning.required_capability) ? `Requiere permiso: ${warning.required_capability}.` : '', 'small mt-1'));
                button.addEventListener('click', () => confirmWarning(warning));
                $('quoteWarnings').append(box);
            }
        }
        function summary(preview, persisted = false) {
            state.preview = preview;
            const result = preview.resultado;
            text('costBaseDisplay', money(result.costo_conocido) + ' MXN');
            text('saleSubtotalDisplay', money(result.subtotal_venta) + ' MXN');
            text('discountDisplay', '-' + money(result.descuento) + ' MXN');
            text('subtotalDisplay', money(result.subtotal_neto) + ' MXN');
            text('ivaDisplay', money(result.iva) + ' MXN');
            text('totalFinalDisplay', money(result.total) + ' MXN');
            text('marginDisplay', result.utilidad_bruta === null ? 'No disponible: costos incompletos' : money(result.utilidad_bruta) + ' MXN');
            text('costCompleteness', result.utilidad_bruta === null ? 'Suma parcial de costos conocidos; no representa el costo completo.' : 'Todos los costos son conocidos.');
            text('calculationStatus', persisted ? 'Cálculo de la versión guardada' : 'Preview del servidor · no guardado');
            const clientLines = new Map(preview.cliente.items.map((l, i) => [result.partidas[i].entrada.referencia, l]));
            for (const row of $('quoteItemsBody').querySelectorAll('[data-line-key]')) {
                const line = clientLines.get(row.dataset.lineKey);
                if (line) {
                    row.querySelector('.sale-unit').textContent = line.unit_price_label || 'Ver original';
                    row.querySelector('.sale-amount').textContent = money(line.amount);
                }
            }
            warnings(preview.warnings, persisted);
        }
        async function calculate() {
            clearTimeout(previewTimer);
            previewAbort?.abort();
            if (state.readonly || !state.caps || state.pending) return null;
            const controller = new AbortController();
            previewAbort = controller;
            const epoch = state.epoch;
            clearSummary('Calculando en el servidor…');
            try {
                const body = payload();
                const response = state.transition ? await api.previewTransition(state.transition.folio, state.transition.revision, body, controller.signal) :
                    state.saved ? await api.previewEdit(state.saved.folio, state.saved.revision, body, controller.signal) : await api.previewNew(body, controller.signal);
                if (epoch !== state.epoch || controller.signal.aborted) return null;
                error();
                summary(response.data, false);
                return response.data;
            } catch (err) {
                if (err.name === 'AbortError' || epoch !== state.epoch) return null;
                clearSummary('No hay cálculo actualizado: revisa los datos o la conexión.');
                if (err.status === 409 && state.saved) state.conflict = true;
                showError(err); locks();
                throw err;
            }
        }
        function inputField(parent, label, value, onChange, { disabled = false, options, checkbox = false } = {}) {
            const wrapper = element('label', '', 'd-block small mb-2');
            wrapper.append(element('span', label, 'd-block fw-semibold mb-1'));
            const input = document.createElement(options ? 'select' : 'input');
            input.className = options ? 'form-select form-select-sm' : checkbox ? 'form-check-input' : 'form-control form-control-sm';
            if (options) for (const [key, text] of options) { const o = element('option', text); o.value = key; input.append(o); }
            if (checkbox) { input.type = 'checkbox'; input.checked = !!value; }
            else { input.value = value ?? ''; if (!options) input.maxLength = 6000; }
            input.disabled = disabled || !editable();
            input.addEventListener(options || checkbox ? 'change' : 'input', () => onChange(checkbox ? input.checked : input.value));
            wrapper.append(input); parent.append(wrapper);
            return input;
        }
        function priceEditor(parent, label, data, enabled, update) {
            parent.append(element('h6', label));
            inputField(parent, 'Importe original', data.amount, value => { data.amount = value; update(); }, { disabled: !enabled });
            inputField(parent, 'Moneda', data.currency, value => { data.currency = value; update(); }, { disabled: !enabled, options: [['MXN', 'MXN'], ['USD', 'USD']] });
            inputField(parent, 'Base IVA de esta entrada', data.vat_basis, value => { data.vat_basis = value; update(true); }, {
                disabled: !enabled, options: [['', 'Selecciona base IVA'], ['NET', 'Sin IVA (neto)'], ['INCLUDED', 'Incluye IVA']] });
            if (data.vat_basis === 'INCLUDED') inputField(parent, 'Tasa incluida propia (0.16 = 16%)', data.vat_rate, value => { data.vat_rate = value; update(); }, { disabled: !enabled || !has('change_commercial_tax') });
        }
        function renderLines() {
            $('quoteItemsBody').replaceChildren();
            if (state.readonly && state.loadedDetail?.schema_version === 1) {
                for (const entry of state.loadedDetail.items) {
                    const row = document.createElement('tr'), cell = element('td', `${entry.name} · Cantidad histórica: ${entry.quantity} · Original: ${money(entry.source_unit_price)} ${entry.source_currency}`);
                    cell.colSpan = 7; row.append(cell); $('quoteItemsBody').append(row);
                }
                return;
            }
            if (!state.lines.length) {
                const row = document.createElement('tr'), cell = element('td', 'Tu cotización está vacía. Busca productos o añade un concepto manual.', 'text-center text-muted py-4');
                cell.colSpan = 7; row.append(cell); $('quoteItemsBody').append(row); return;
            }
            state.lines.forEach((line, index) => {
                const row = document.createElement('tr'); row.dataset.lineKey = line.key;
                row.append(element('td', String(index + 1)), element('td', `${line.name || 'Concepto manual sin nombre'} · ${line.provider || 'Manual'}`),
                    element('td', line.units.sale_label || 'Pendiente'), element('td', line.quantity),
                    element('td', '—', 'sale-unit'), element('td', '—', 'sale-amount'));
                const actions = document.createElement('td');
                const remove = element('button', 'Quitar', 'btn btn-sm btn-outline-danger'); remove.type = 'button'; remove.disabled = !editable();
                remove.addEventListener('click', () => { state.lines.splice(index, 1); renderLines(); changed(); });
                actions.append(remove); row.append(actions); $('quoteItemsBody').append(row);
                const detailRow = document.createElement('tr'), detailCell = document.createElement('td'); detailCell.colSpan = 7;
                const details = document.createElement('details'); details.open = !!line.expanded;
                details.addEventListener('toggle', () => { line.expanded = details.open; });
                details.append(element('summary', `Detalle de partida ${index + 1}: unidades, precio y captura`, 'small text-primary mb-2'));
                const grid = element('div', '', 'row g-3 p-2');
                const original = element('div', '', 'col-md-4'), unit = element('div', '', 'col-md-4'), pricing = element('div', '', 'col-md-4');
                const update = rerender => { changed(); if (rerender) renderLines(); };
                if (line.kind === 'API') {
                    original.append(element('h6', 'Original API · solo lectura'),
                        element('p', `${line.provider} · ${line.name}`, 'small'),
                        element('p', `${money(line.originalPrice)} ${line.originalCurrency} · ${line.originalVat === null ? 'neto' : 'incluye IVA ' + line.originalVat}`, 'small'),
                        element('p', `Marca: ${line.brand || '—'} · Modelo: ${line.model || '—'}`, 'small'));
                } else {
                    inputField(original, 'Nombre del concepto manual', line.name, value => { line.name = value; update(); }, { disabled: line.persisted || !has('set_commercial_sale') });
                    inputField(original, 'Costo conocido (desmarcado = desconocido, no cero)', line.knownCost, value => { line.knownCost = value; update(true); }, { checkbox: true, disabled: !has('set_commercial_sale') });
                    if (line.knownCost) priceEditor(original, 'Costo capturado', line.cost, has('set_commercial_sale'), update);
                }
                inputField(original, 'Descripción pública del concepto', line.description, value => { line.description = value; update(); });
                const unitsChanged = () => { line.units.confirmed = false; update(); };
                inputField(unit, 'Unidad original', line.units.source_label, value => { line.units.source_label = value; unitsChanged(); }, { disabled: line.persisted && line.kind === 'API' });
                inputField(unit, 'Unidad vendida', line.units.sale_label, value => { line.units.sale_label = value; unitsChanged(); });
                inputField(unit, 'Factor divisor original → venta', line.units.conversion_factor, value => { line.units.conversion_factor = value; unitsChanged(); });
                const confirmation = inputField(unit, 'Confirmo unidades y factor', line.units.confirmed, value => { line.units.confirmed = value; update(); }, { checkbox: true });
                unit.addEventListener('input', event => { if (event.target !== confirmation) confirmation.checked = line.units.confirmed; });
                inputField(unit, 'Cantidad entera', line.quantity, value => { line.quantity = value; update(); });
                inputField(pricing, 'Modalidad de precio', line.mode, value => { line.mode = value; update(true); }, {
                    disabled: !has('set_commercial_sale'), options: [['MARGIN', 'Costo + margen sobre venta'], ['MANUAL', 'Venta manual por unidad vendida']] });
                if (line.mode === 'MARGIN') inputField(pricing, 'Margen sobre venta (%)', line.margin, value => { line.margin = value; update(); }, { disabled: !has('change_commercial_margin') });
                else priceEditor(pricing, 'Venta manual por unidad vendida', line.sale, has('set_commercial_sale'), update);
                pricing.append(element('p', 'Los campos bloqueados requieren permiso; conservar un valor capturado no lo vuelve a calcular en JavaScript.', 'small text-muted'));
                grid.append(original, unit, pricing); details.append(grid); detailCell.append(details); detailRow.append(detailCell); $('quoteItemsBody').append(detailRow);
            });
            if (state.preview) summary(state.preview, !state.dirty && !!state.saved);
            if (state.readonly && state.loadedDetail?.schema_version === 2) sealedSummary(state.loadedDetail);
        }
        function blankLine() {
            return { key: crypto.randomUUID(), kind: 'MANUAL', persisted: false, name: '', provider: '', description: '', quantity: '1',
                units: { source_label: '', sale_label: '', conversion_factor: '1', confirmed: false }, mode: 'MANUAL', margin: '35',
                knownCost: false, cost: { amount: '', currency: 'MXN', vat_basis: '', vat_rate: '0.16' },
                sale: { amount: '', currency: 'MXN', vat_basis: '', vat_rate: '0.16' }, expanded: true };
        }
        function renderProducts() {
            $('resultsList').replaceChildren();
            for (const offer of state.products.filter(p => !$('providerFilter').value || p.provider === $('providerFilter').value)) {
                const box = element('div', '', 'list-group-item d-flex flex-wrap justify-content-between gap-2');
                const info = document.createElement('div');
                info.append(element('strong', offer.name), element('div', `${offer.provider_label} · ${offer.model || '—'} · ${money(offer.price_original)} ${offer.currency}`, 'small'),
                    element('div', offer.stock === null ? 'Disponibilidad desconocida' : offer.stock === 0 ? 'Sin existencias capturadas' : `Existencias capturadas: ${offer.stock}`, 'small'));
                const verified = offer.vat?.vat_basis && offer.vat.vat_basis !== 'UNKNOWN';
                if (!verified) info.append(element('div', 'Base IVA de origen no verificada: captura comercial bloqueada.', 'small text-danger'));
                const add = element('button', 'Agregar captura', 'btn btn-sm btn-outline-primary'); add.type = 'button'; add.disabled = !editable() || !verified;
                add.dataset.verified = String(!!verified);
                add.addEventListener('click', () => {
                    if (!editable()) return;
                    const line = blankLine();
                    Object.assign(line, { kind: 'API', name: offer.name, provider: offer.provider_label, offer,
                        originalPrice: offer.price_original, originalCurrency: offer.currency,
                        originalVat: offer.vat.vat_basis === 'INCLUDED' ? offer.vat.vat_rate : null,
                        brand: offer.brand, model: offer.model, mode: 'MARGIN' });
                    state.lines.push(line); renderLines(); changed();
                });
                box.append(info, add); $('resultsList').append(box);
            }
        }
        async function search() {
            clearTimeout(searchTimer); searchAbort?.abort();
            const version = ++searchVersion;
            if (!$('productSearchInput').value.trim() || !editable()) return;
            searchAbort = new AbortController();
            text('searchStatus', 'Buscando…'); $('resultsList').replaceChildren();
            try {
                const filters = { q: $('productSearchInput').value.trim() };
                if (state.fx && has('change_commercial_fx')) filters.tipo_cambio = state.fx.value;
                const response = await api.search(filters, searchAbort.signal);
                if (version !== searchVersion) return;
                state.products = response.data || [];
                const selected = $('providerFilter').value;
                $('providerFilter').replaceChildren();
                const all = element('option', 'Todos'); all.value = ''; $('providerFilter').append(all);
                for (const [code, label] of new Map(state.products.map(p => [p.provider, p.provider_label]))) {
                    const option = element('option', label); option.value = code; $('providerFilter').append(option);
                }
                $('providerFilter').value = [...$('providerFilter').options].some(o => o.value === selected) ? selected : '';
                text('searchStatus', state.products.length ? `${state.products.length} ofertas capturadas. No se reemplazaron las partidas anteriores.` : 'Sin coincidencias.');
                text('searchWarning', response.proveedores_caidos_nombres?.length ? 'No respondieron: ' + response.proveedores_caidos_nombres.join(', ') : '');
                $('searchWarning').classList.toggle('d-none', !response.proveedores_caidos_nombres?.length);
                // Solo retira el aviso de una búsqueda fallida anterior, nunca otro error vigente.
                if (searchError && $('quoteError').textContent === searchError) error();
                searchError = '';
                renderProducts();
            } catch (err) {
                if (err.name === 'AbortError' || version !== searchVersion) return;
                state.products = []; text('searchStatus', err.message + fxSearchHint(err.message, has('change_commercial_fx')));
                showError(err);
                searchError = $('quoteError').textContent;
            }
        }
        function fxDisplay() {
            $('exchangeRate').value = state.fx ? `1 USD = ${state.fx.value} MXN` : 'Sin tipo de cambio';
            text('fxEvidence', state.fx ? `${state.fx.source} · ${state.fx.date} · Contexto capturado` : 'El sistema no supone un tipo de cambio. Si todo es en pesos, puede quedar vacío.');
        }
        function loadLine(snapshot) {
            const line = blankLine();
            Object.assign(line, { key: snapshot.line_key, persisted: true, kind: snapshot.source_kind,
                name: snapshot.name, provider: snapshot.provider_name, brand: snapshot.brand, model: snapshot.model,
                description: snapshot.description, quantity: String(storedInteger(snapshot.quantity)),
                originalPrice: snapshot.source_unit_price, originalCurrency: snapshot.source_currency,
                originalVat: snapshot.cost_tax_rate_included, mode: snapshot.pricing_mode,
                margin: snapshot.margin_percent ?? '', expanded: false, knownCost: snapshot.source_unit_price !== null,
                units: { source_label: snapshot.source_unit_label, sale_label: snapshot.sale_unit_label,
                    conversion_factor: snapshot.conversion_factor, confirmed: true },
                cost: { amount: snapshot.source_unit_price ?? '', currency: snapshot.source_currency || 'MXN',
                    vat_basis: snapshot.source_unit_price === null ? '' : snapshot.cost_tax_rate_included === null ? 'NET' : 'INCLUDED', vat_rate: snapshot.cost_tax_rate_included ?? '0.16' },
                sale: { amount: snapshot.manual_unit_price ?? '', currency: snapshot.manual_currency || 'MXN',
                    vat_basis: snapshot.manual_unit_price === null ? '' : snapshot.manual_tax_rate_included === null ? 'NET' : 'INCLUDED', vat_rate: snapshot.manual_tax_rate_included ?? '0.16' } });
            return line;
        }
        function loadDetail(detail) {
            state.transition = null;
            $('transitionNotice').classList.add('d-none');
            state.loadedDetail = detail;
            state.caps = detail.capabilities;
            state.saved = { folio: detail.folio, revision: detail.revision, edit_version: detail.edit_version, state_fingerprint: detail.state_fingerprint };
            state.dirty = false; state.conflict = false; state.preview = null; state.epoch += 1;
            state.readonly = detail.schema_version !== 2 || detail.status !== 'DRAFT';
            const values = { clientName: detail.client.business_name, clientRfc: detail.client.rfc, clientEmail: detail.client.email,
                clientContact: detail.client.contact || '', clientPhone: detail.client.phone || '', clientProject: detail.client.project || '',
                issueDate: detail.issue_date, validDays: detail.valid_days, quoteConditions: detail.conditions.join('\n'), quoteNotes: '',
                quoteFolio: detail.folio, quoteRevision: `${detail.revision} / ${detail.edit_version}`,
                taxRate: detail.tax_rate, discountPercent: detail.discount_percent ?? '', openFolio: detail.folio, openRevision: detail.revision };
            for (const [id, value] of Object.entries(values)) $(id).value = value;
            state.fx = detail.tipo_cambio ? { id: detail.fx_context_id,
                value: detail.tipo_cambio.valor, source: detail.tipo_cambio.origen, date: detail.tipo_cambio.fecha } : null;
            fxDisplay();
            state.lines = detail.schema_version === 2 ? detail.items.map(loadLine) : [];
            renderLines(); clearSummary(); locks();
            if (detail.schema_version !== 2) {
                $('quoteItemsBody').replaceChildren();
                for (const entry of detail.items) {
                    const row = document.createElement('tr'), cell = element('td', `${entry.name} · Cantidad histórica: ${entry.quantity} · Original: ${money(entry.source_unit_price)} ${entry.source_currency}`);
                    cell.colSpan = 7; row.append(cell); $('quoteItemsBody').append(row);
                }
                error('Revisión histórica v1 en solo lectura. Requiere una transición comercial revisada; no se truncan cantidades ni se inventa margen.');
            } else if (detail.preview) { summary(detail.preview, true); error(); }
            else {
                sealedSummary(detail);
                error('Revisión emitida en solo lectura. No se sobrescribe ni se regenera su documento.');
            }
        }
        function sealedSummary(detail) {
            const seals = detail.sealed;
            text('costBaseDisplay', money(seals.known_cost_total) + ' MXN');
            text('saleSubtotalDisplay', money(seals.sale_subtotal) + ' MXN');
            text('discountDisplay', '-' + money(seals.discount_amount) + ' MXN');
            text('subtotalDisplay', money(detail.subtotal) + ' MXN'); text('ivaDisplay', money(detail.tax) + ' MXN'); text('totalFinalDisplay', money(detail.total) + ' MXN');
            text('marginDisplay', seals.costs_complete ? money(seals.gross_profit) + ' MXN' : 'No disponible: costos incompletos');
            text('costCompleteness', seals.costs_complete ? 'Todos los costos sellados son conocidos.' : 'Costos sellados incompletos.');
            text('calculationStatus', 'Importes sellados · no se recalcula la emisión');
            text('quoteWarnings', 'Solo lectura. Las confirmaciones históricas se conservan en el servidor.');
            for (const row of $('quoteItemsBody').querySelectorAll('[data-line-key]')) {
                const index = state.lines.findIndex(l => l.key === row.dataset.lineKey);
                row.querySelector('.sale-unit').textContent = detail.cliente.items[index]?.unit_price_label || 'Consultar original (P6)';
                const snapshot = detail.items.find(s => s.line_key === row.dataset.lineKey);
                row.querySelector('.sale-amount').textContent = money(snapshot.sale_amount);
            }
        }
        async function save() {
            if (state.busy) return;
            state.busy = true; locks();
            try {
                await sameActor(state.pending?.actor);
                if (!state.pending) {
                    const preview = await calculate();
                    if (!preview) return;
                    const body = payload();
                    if (!state.saved) {
                        body.request_id = crypto.randomUUID();
                        if (!state.transition) body.notes = $('quoteNotes').value.trim();
                    }
                    state.pending = { body: JSON.parse(JSON.stringify(body)), saved: state.saved ? { ...state.saved } : null,
                        transition: state.transition ? { ...state.transition } : null,
                        actor: state.actor, version: state.saved?.edit_version, baseFingerprint: state.saved?.state_fingerprint, fingerprint: preview.state_fingerprint };
                }
                const intent = state.pending;
                let saved;
                if (intent.saved) {
                    const detail = (await api.revision(intent.saved.folio, intent.saved.revision)).data;
                    const outcome = editOutcome(detail, intent);
                    if (outcome === 'SAVED') saved = detail;
                    else if (outcome === 'RETRY') saved = (await api.update(intent.saved.folio, intent.saved.revision, intent.body)).data;
                    else { state.conflict = true; state.pending = null; fail('La revisión cambió. Abre explícitamente la versión del servidor; no se sobrescribieron datos.'); }
                } else saved = (await (intent.transition ? api.clone(intent.transition.folio, intent.transition.revision, intent.body) : api.create(intent.body))).data;
                // El resultado de escritura ya es conocido, aunque falle la lectura posterior.
                state.saved = { folio: saved.folio, revision: saved.revision, edit_version: saved.edit_version, state_fingerprint: saved.state_fingerprint };
                state.pending = null;
                // La copia v2 ya existe: no volver a usar el v1 de origen aunque falle el GET.
                state.transition = null; $('transitionNotice').classList.add('d-none');
                $('quoteFolio').value = saved.folio; $('quoteRevision').value = `${saved.revision} / ${saved.edit_version}`;
                $('openFolio').value = saved.folio; $('openRevision').value = saved.revision;
                state.conflict = true; // Si falla GET, exigir recarga y nunca otra alta.
                const detail = (await api.revision(saved.folio, saved.revision)).data;
                loadDetail(detail);
                window.showToast('Borrador guardado. No se emitió ni se envió ningún documento.', 'success');
            } catch (err) {
                if (state.pending && retainIntent(err, state.pending.uncertain)) state.pending.uncertain = true;
                else if (state.pending) {
                    if (err.status === 409 && state.saved) state.conflict = true;
                    state.pending = null;
                }
                showError(err);
            } finally { state.busy = false; locks(); renderLines(); }
        }
        async function confirmWarning(warning) {
            if (!state.saved || state.dirty || state.busy || state.pending || !state.preview?.persistida) return;
            if (!window.confirm(`Aceptar ${warning.code} para esta versión guardada?`)) return;
            state.busy = true; locks();
            try {
                await sameActor();
                await api.confirm(state.saved.folio, state.saved.revision, { edit_version: state.saved.edit_version,
                    state_fingerprint: state.saved.state_fingerprint, warnings: [{ code: warning.code, line_key: warning.line_key }] });
                loadDetail((await api.revision(state.saved.folio, state.saved.revision)).data);
            } catch (err) { showError(err); }
            finally { state.busy = false; locks(); renderLines(); }
        }
        async function capabilities() {
            try { await sameActor(); error(); }
            catch (err) { state.caps = null; clearSummary('No hay conexión financiera autorizada.'); showError(err); }
            locks(); renderLines(); renderProducts();
        }
        async function sameActor(expected = state.actor) {
            const response = await api.capabilities();
            if (expected !== undefined && expected !== response.actor_id) {
                const err = new Error('La sesión pertenece a otro asesor. Vuelve a iniciar sesión con el asesor original; no se reenviaron los datos.');
                err.status = 401;
                throw err;
            }
            state.actor = response.actor_id;
            state.caps = response.data;
        }
        $('quoteHeader').addEventListener('input', changed);
        for (const id of ['taxRate', 'discountPercent']) $(id).addEventListener('input', changed);
        $('addManualBtn').addEventListener('click', () => { state.lines.push(blankLine()); renderLines(); changed(); });
        $('clearTableBtn').addEventListener('click', () => { if (window.confirm('Vaciar partidas de este borrador? No se borra historial.')) { state.lines = []; renderLines(); changed(); } });
        $('saveQuoteBtn').addEventListener('click', save);
        $('viewSummaryBtn').addEventListener('click', () => {
            if (!state.saved || state.dirty || state.pending || state.busy) return;
            window.location.assign(`/resumen/?${new URLSearchParams({ folio: state.saved.folio, revision: state.saved.revision })}`);
        });
        $('refreshPermissionsBtn').addEventListener('click', capabilities);
        $('searchProductBtn').addEventListener('click', search);
        $('providerFilter').addEventListener('change', renderProducts);
        $('productSearchInput').addEventListener('input', () => {
            clearTimeout(searchTimer); searchAbort?.abort(); searchVersion += 1;
            state.products = []; $('resultsList').replaceChildren(); text('searchStatus', '');
            $('searchWarning').classList.add('d-none');
            searchTimer = setTimeout(search, 350);
        });
        $('applyFxBtn').addEventListener('click', async () => {
            if (state.busy || state.pending) return;
            state.busy = true; locks();
            try {
                await sameActor(state.fxPending?.actor);
                if (!state.fxPending) state.fxPending = { actor: state.actor,
                    body: { request_id: crypto.randomUUID(), value: decimal($('newExchangeRate').value, 18, 8, { positive: true }) } };
                const result = (await api.createFX(state.fxPending.body)).data;
                state.fx = { id: result.fx_context_id, value: result.value, source: result.source, date: result.date };
                state.fxPending = null; fxDisplay(); changed();
            } catch (err) {
                if (state.fxPending && retainIntent(err, state.fxPending.uncertain)) state.fxPending.uncertain = true;
                else state.fxPending = null;
                showError(err);
            }
            finally { state.busy = false; locks(); renderLines(); }
        });
        $('clearFxBtn').addEventListener('click', () => { if (state.fxPending) return; state.fx = null; fxDisplay(); changed(); });
        $('openQuoteBtn').addEventListener('click', async () => {
            if (state.dirty && !window.confirm('Cargar esta revisión descartará los cambios NO guardados de la pantalla. Continuar?')) return;
            state.busy = true; locks(); previewAbort?.abort(); clearTimeout(previewTimer);
            try {
                await sameActor();
                const folio = $('openFolio').value.trim(); if (!folio) fail('Indica el folio.');
                loadDetail((await api.revision(folio, integer($('openRevision').value, 2147483647))).data);
            } catch (err) { showError(err); }
            finally { state.busy = false; locks(); renderLines(); }
        });
        $('newQuoteBtn').addEventListener('click', () => {
            if (state.dirty && !window.confirm('Descartar cambios no guardados y preparar otra cotización?')) return;
            state.saved = null; state.transition = null; state.loadedDetail = null; state.readonly = false; state.conflict = false; state.lines = []; state.fx = null;
            $('transitionNotice').classList.add('d-none');
            for (const id of ['clientName', 'clientRfc', 'clientEmail', 'clientContact', 'clientPhone', 'clientProject', 'quoteNotes', 'newExchangeRate']) $(id).value = '';
            $('quoteConditions').value = defaultConditions;
            $('quoteFolio').value = 'Se asigna al guardar'; $('quoteRevision').value = 'Sin guardar';
            $('taxRate').value = '0.16'; $('discountPercent').value = '0'; $('validDays').value = '15';
            $('issueDate').value = initialIssueDate;
            previewAbort?.abort(); clearTimeout(previewTimer); state.epoch += 1; state.dirty = false;
            clearSummary('Nueva cotización sin guardar'); fxDisplay(); error(); locks(); renderLines();
        });
        window.addEventListener('beforeunload', event => { if (state.dirty || state.pending || state.fxPending) { event.preventDefault(); event.returnValue = ''; } });
        const requestedModel = new URLSearchParams(window.location.search).get('sku');
        if (requestedModel) {
            $('productSearchInput').value = requestedModel;
            text('searchStatus', 'Modelo recibido del comparador: busca y elige la oferta exacta. No se importaron precios ni tipo de cambio desde el enlace.');
        }
        async function initialize() {
            await capabilities();
            const query = new URLSearchParams(window.location.search);
            const folio = query.get('transition_folio') || query.get('folio');
            if (!folio || !state.caps) return;
            state.busy = true; locks();
            try {
                const detail = (await api.revision(folio, integer(query.get('transition_revision') || query.get('revision'), 2147483647))).data;
                loadDetail(detail);
                if (query.has('transition_folio')) {
                    if (detail.schema_version !== 1) fail('Esta preparación es solo para un histórico v1.');
                    state.transition = { folio: detail.folio, revision: detail.revision, edit_version: detail.edit_version };
                    state.saved = null; state.loadedDetail = null; state.readonly = false; state.lines = []; state.fx = null;
                    $('quoteRevision').value = 'Nueva revisión: se asigna al guardar';
                    $('issueDate').value = initialIssueDate; $('taxRate').value = '0.16'; $('discountPercent').value = '0';
                    $('transitionNotice').classList.remove('d-none');
                    error(); fxDisplay(); clearSummary('Captura y revisa explícitamente las nuevas partidas. No se copiaron entradas financieras del histórico.');
                }
            } catch (err) { showError(err); }
            finally { state.busy = false; locks(); renderLines(); }
        }
        $('quoteConditions').value = defaultConditions;  // Una revisión abierta después trae las suyas.
        fxDisplay(); locks(); initialize();
    });
}());
