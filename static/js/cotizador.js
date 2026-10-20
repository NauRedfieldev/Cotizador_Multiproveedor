/* P5: captura/presentación interna. El dinero se calcula SOLO en Python.
 * No mocks, autosave, localStorage financiero ni emisión documental en este flujo.
 */
(function () {
    'use strict';
    // field: dato de la partida que falló, para marcarlo e «Ir al campo».
    const fail = (message, field) => { const err = new Error(message); if (field) err.field = field; throw err; };
    const within = (field, run) => { try { return run(); } catch (err) { err.field ??= field; throw err; } };
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
        else if (data.vat_basis !== 'NET') fail('Indica si el importe incluye IVA o es sin IVA.');
        return data;
    }
    function item(line) {
        if (line.kind === 'MANUAL' && !line.name.trim()) fail('Escribe el nombre del concepto.', 'name');
        if (!line.units.source_label.trim() || !line.units.sale_label.trim()) fail('Indica la unidad del proveedor y la unidad en que lo vendes.', 'units');
        const conversion = within('factor', () => decimal(line.units.conversion_factor, 18, 6, { positive: true }));
        const quantity = within('quantity', () => integer(line.quantity));
        if (!line.units.confirmed) fail('Marca la casilla «Revisé…» para confirmar la unidad y la cantidad.', 'confirm');
        const data = { line_key: line.key, source: line.persisted ? { type: 'EXISTING' } : line.kind === 'API' ?
            { type: 'API', context_ref: line.offer.context_ref, offer_ref: line.offer.offer_ref } :
            { type: 'MANUAL', name: line.name.trim() }, quantity,
            units: { source_label: line.units.source_label.trim(), sale_label: line.units.sale_label.trim(),
                conversion_factor: conversion, confirmed: true },
            description: line.description, pricing: { mode: line.mode } };
        if (line.kind === 'MANUAL') data.cost = line.knownCost ? within('cost', () => price(line.cost)) : null;
        if (line.mode === 'MARGIN') {
            if (line.kind === 'MANUAL' && !line.knownCost) fail('Sin costo conocido, elige «Precio fijo» y captura el precio.', 'mode');
            data.pricing.margin_percent = within('margin', () => decimal(line.margin, 4, 2, { max: '9999' }));
        } else if (line.mode === 'MANUAL') data.pricing.sale = within('sale', () => price(line.sale));
        else fail('Elige cómo calcular el precio.', 'mode');
        return data;
    }
    // IVA en pantalla como porcentaje (16); el servidor recibe la fracción exacta (0.1600).
    function percentToFraction(value) {
        const text = decimal(String(value ?? ''), 5, 2, { max: '10000' });
        const [whole, fraction = ''] = text.split('.');
        const scaled = BigInt(whole + fraction.padEnd(2, '0'));
        return `${scaled / 10000n}.${String(scaled % 10000n).padStart(4, '0')}`;
    }
    function fractionToPercent(value) {
        const text = decimal(String(value ?? ''), 5, 4, { max: '10000' });
        const [whole, fraction = ''] = text.split('.');
        const scaled = BigInt(whole + fraction.padEnd(4, '0'));
        const rest = String(scaled % 100n).padStart(2, '0').replace(/0+$/, '');
        return rest ? `${scaled / 100n}.${rest}` : String(scaled / 100n);
    }
    // Solo para mostrar: redondeo HALF_UP sobre el texto decimal, sin float.
    function roundText(value, places) {
        const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(String(value ?? ''));
        if (!match) return null;
        const [, sign, whole, fraction = ''] = match;
        if (fraction.length <= places) return { text: sign + whole + (places ? '.' + fraction.padEnd(places, '0') : ''), exact: true };
        let scaled = BigInt(whole + fraction.slice(0, places));
        if (fraction[places] >= '5') scaled += 1n;
        const digits = scaled.toString().padStart(places + 1, '0');
        return { text: sign + digits.slice(0, digits.length - places) + (places ? '.' + digits.slice(-places) : ''),
            exact: /^0*$/.test(fraction.slice(places)) };
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
        return canChangeFx ? ' Aplícalo en el paso 2 «Tipo de cambio del día» y vuelve a buscar.'
            : ' No tienes permiso para capturar el tipo de cambio; pide a un administrador que te lo habilite.';
    }
    // «100.000000» → «100»: mismo valor decimal, más legible al recargar.
    function trimZeros(value) {
        const text = String(value ?? '');
        return /^\d+\.\d+$/.test(text) ? text.replace(/0+$/, '').replace(/\.$/, '') : text;
    }
    // Descuento: con signo menos si resta algo; en cero, «$0.00» sin signo (igual que el PDF).
    function discountText(value) {
        const zero = /^0*(\.0*)?$/.test(String(value ?? '').trim());
        return (zero ? '' : '-') + money(value) + ' MXN';
    }
    // Una unidad por partes = otra etiqueta de venta o factor distinto de 1.
    function splitUnits(units) {
        return units.source_label.trim() !== units.sale_label.trim() || !/^0*1(\.0*)?$/.test(String(units.conversion_factor).trim());
    }
    window.CCONORQuoteTools = Object.freeze({ decimal, integer, storedInteger, money, price, item, editOutcome, retainIntent,
        defaultConditionsText, fxSearchHint, percentToFraction, fractionToPercent, roundText, splitUnits, trimZeros, discountText });
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
        // 1A: aviso emergente persistente (se cierra a mano o al corregir); action = {label, run}.
        let currentError = '', sessionError = '', fieldSeq = 0, lastSessionCheck = 0;
        function error(message = '', { title = 'No se pudo completar', action = null } = {}) {
            currentError = message;
            text('quoteErrorTitle', title);
            text('quoteErrorText', message);
            $('quoteErrorAction').classList.toggle('d-none', !action);
            text('quoteErrorAction', action?.label || '');
            $('quoteErrorAction').onclick = action ? () => { error(); action.run(); } : null;
            $('quoteError').classList.toggle('d-none', !message);
        }
        function showError(err, action = null) {
            if (err.status === 401) {
                state.sessionExpired = true;
                error(err.message + ' Este formulario se conserva para reintentar.', { title: 'Inicia sesión de nuevo',
                    action: { label: 'Iniciar sesión en otra pestaña', run: () => window.open($('commercialQuoteApp').dataset.loginUrl, '_blank', 'noopener') } });
                sessionError = currentError;
                locks();
                return;
            }
            error(err.message, { action });
        }
        // Mensajes de contexto (solo lectura, histórico): informativos, no errores.
        function notice(message = '') {
            text('quoteNotice', message);
            $('quoteNotice').classList.toggle('d-none', !message);
        }
        // Pendientes antes de guardar: todos a la vez, cada uno con su campo o partida.
        function headerIssues(transition) {
            const issues = [], add = (field, message) => issues.push({ field, message, place: transition ? 'Ajustes del total' : 'Datos del cliente' });
            if (!transition) {
                if (!$('clientName').value.trim()) add('clientName', 'Falta la razón social del cliente.');
                if ($('clientEmail').value && !$('clientEmail').checkValidity()) add('clientEmail', 'El correo del cliente no es válido.');
                const rfc = $('clientRfc').value.trim().toUpperCase();
                if (rfc && !/^[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}$/.test(rfc)) add('clientRfc', 'RFC inválido; escríbelo sin guiones ni espacios.');
                try { integer($('validDays').value, 3650); } catch { add('validDays', 'La vigencia debe ser un número entero de días (1 a 3650).'); }
                const conditions = $('quoteConditions').value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
                if (conditions.length > 50 || conditions.some(s => s.length > 6000)) add('quoteConditions', 'Máximo 50 condiciones de hasta 6000 caracteres.');
            }
            const totals = (field, message) => issues.push({ field, message, place: 'Ajustes del total' });
            try { percentToFraction($('taxRate').value); } catch { totals('taxRate', 'El IVA debe ser un porcentaje de 0 a 100, con hasta 2 decimales.'); }
            try { decimal($('discountPercent').value, 5, 2, { max: '10000' }); } catch { totals('discountPercent', 'El descuento debe ser un porcentaje de 0 a 100, con hasta 2 decimales.'); }
            return issues;
        }
        const usesUsd = line => line.originalCurrency === 'USD' || (line.kind === 'MANUAL' &&
            ((line.knownCost && line.cost.currency === 'USD') || (line.mode === 'MANUAL' && line.sale.currency === 'USD')));
        function quoteIssues() {
            const issues = headerIssues(!!state.transition);
            if (!state.fx && state.lines.some(usesUsd)) issues.push({ field: 'newExchangeRate', message: 'Aplica el tipo de cambio del día: hay precios en dólares.', place: 'Tipo de cambio' });
            if (!state.lines.length) issues.push({ field: 'productSearchInput', message: 'Agrega al menos un producto o concepto.', place: 'Productos y conceptos' });
            state.lines.forEach((line, index) => {
                try { item(line); } catch (err) {
                    issues.push({ lineKey: line.key, lineField: err.field, message: err.message, place: `Partida ${index + 1} · ${line.name || 'Concepto manual'}` });
                }
            });
            return issues;
        }
        function clearFieldError(input) {
            input.classList.remove('is-invalid');
            if (input.nextElementSibling?.classList.contains('quote-feedback')) input.nextElementSibling.remove();
        }
        function clearIssues() {
            for (const input of document.querySelectorAll('#commercialQuoteApp .is-invalid')) clearFieldError(input);
            for (const line of state.lines) line.issue = null;
        }
        function markIssues(issues) {
            clearIssues();
            for (const issue of issues) {
                if (issue.field) {
                    const input = $(issue.field);
                    input.classList.add('is-invalid');
                    input.after(element('div', issue.message, 'invalid-feedback quote-feedback'));
                } else {
                    const line = state.lines.find(l => l.key === issue.lineKey);
                    if (line) { line.issue = issue; line.expanded = true; }
                }
            }
            renderLines(); locks();
        }
        function goTo(issue) {
            let target = issue.field ? $(issue.field) : null;
            if (issue.lineKey) {
                const line = state.lines.find(l => l.key === issue.lineKey);
                if (!line) return;
                line.expanded = true; renderLines(); locks();
                const box = [...$('quoteItemsBody').querySelectorAll('[data-line-detail]')].find(d => d.dataset.lineDetail === issue.lineKey);
                target = box?.querySelector(`[data-field="${issue.lineField}"]:not([disabled])`) || box?.querySelector('input:not([disabled]), select:not([disabled])') || box;
            }
            target?.scrollIntoView({ behavior: 'smooth', block: 'center' });
            target?.focus?.({ preventScroll: true });
        }
        // 1A con un pendiente; 1B (ventana con la lista) con varios.
        function presentIssues(issues) {
            markIssues(issues);
            if (issues.length === 1) {
                const [issue] = issues;
                error(issue.message, { title: 'No se pudo guardar', action: { label: issue.lineKey ? 'Ir a la partida' : 'Ir al campo', run: () => goTo(issue) } });
                return;
            }
            error();
            text('quoteIssuesTitle', `Revisa ${issues.length} puntos antes de guardar`);
            $('quoteIssuesList').replaceChildren();
            for (const issue of issues) {
                const entry = element('li', '', 'list-group-item d-flex align-items-center gap-3');
                const info = element('div', '', 'flex-grow-1');
                info.append(element('div', issue.message, 'fw-semibold'), element('div', issue.place, 'small text-muted'));
                const go = element('button', issue.lineKey ? 'Ir a la partida' : 'Ir al campo', 'btn btn-sm btn-outline-primary');
                go.type = 'button';
                go.addEventListener('click', () => { closeIssues(); goTo(issue); });
                entry.append(element('span', '', 'issue-dot'), info, go);
                $('quoteIssuesList').append(entry);
            }
            const dialog = $('quoteIssuesDialog');
            if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
        }
        function closeIssues() {
            const dialog = $('quoteIssuesDialog');
            if (typeof dialog.close === 'function' && dialog.open) dialog.close(); else dialog.removeAttribute('open');
        }
        function locks() {
            const allowed = editable();
            if (!allowed) for (const control of $('quoteItemsBody').querySelectorAll('input, select, button')) control.disabled = true;
            if (state.busy || state.pending) for (const control of $('quoteWarnings').querySelectorAll('button')) control.disabled = true;
            for (const control of $('resultsList').querySelectorAll('button')) control.disabled = !allowed || control.dataset.verified !== 'true';
            $('quoteHeader').disabled = !allowed || !!state.transition;
            $('quoteNotes').disabled = !allowed || !!state.saved || !!state.transition;
            $('viewSummaryBtn').disabled = state.busy || !!state.pending || !!state.fxPending || !state.saved || state.dirty || !state.caps;
            for (const id of ['saveQuoteBtn', 'clearTableBtn', 'addManualBtn', 'searchProductBtn', 'productSearchInput', 'newQuoteBtn', 'applyFxBtn', 'clearFxBtn', 'newExchangeRate', 'taxRate', 'discountPercent']) {
                $(id).disabled = !allowed;
            }
            $('newQuoteBtn').disabled = state.busy || !!state.pending || !!state.fxPending;
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
            text('applyFxBtn', state.fxPending ? 'Resolver tipo de cambio' : 'Aplicar');
            text('fxPermissionHint', has('change_commercial_fx') ? '' : 'Sin permiso para capturar el tipo de cambio; pide a un administrador que te lo habilite. El ya guardado se conserva al editar.');
            const [statusText, statusKind] = state.sessionExpired ? ['Sesión vencida', 'danger'] :
                state.pending ? ['Resultado de guardado por confirmar', 'dirty'] : state.readonly ? ['Solo lectura', 'readonly'] :
                state.conflict ? ['Conflicto: recarga la revisión', 'danger'] : state.busy ? ['Procesando…', 'neutral'] :
                state.dirty ? ['Cambios sin guardar', 'dirty'] : state.saved ? ['Guardado', 'saved'] :
                state.caps ? ['Nueva · sin guardar', 'neutral'] : ['Sin conexión autorizada', 'danger'];
            const identity = state.transition ? `Nueva revisión desde ${state.transition.folio}` :
                state.saved ? `${state.saved.folio} · Revisión ${state.saved.revision}` : 'Cotización nueva';
            for (const [box, label] of [['quoteStatus', 'quoteStatusText'], ['stickyStatus', 'stickyStatusText']]) {
                $(box).className = `quote-status quote-status--${statusKind}`;
                text(label, statusText);
            }
            text('quoteIdentity', identity); text('stickyIdentity', identity);
            $('stickyNewBtn').disabled = $('newQuoteBtn').disabled;
            $('stickySaveBtn').disabled = $('saveQuoteBtn').disabled;
            text('stickySaveBtn', $('saveQuoteBtn').textContent);
            syncStickyTotal();
        }
        // Avisos: el recuadro solo se muestra si el cálculo trae alguno.
        function showWarnings(count) {
            $('quoteWarningsCard').classList.toggle('d-none', !count);
            text('quoteWarningsTitle', `Avisos de esta cotización (${count})`);
            if (!count) $('quoteWarnings').replaceChildren();
        }
        // Barra fija: refleja el total del resumen; los botones delegan en los principales.
        function syncStickyTotal() { text('stickyTotal', $('totalFinalDisplay').textContent); }
        function clearSummary(message = 'Pendiente de cálculo actualizado') {
            state.preview = null;
            for (const id of ['costBaseDisplay', 'saleSubtotalDisplay', 'discountDisplay', 'subtotalDisplay', 'ivaDisplay', 'totalFinalDisplay', 'marginDisplay']) text(id, '—');
            text('calculationStatus', message);
            text('costCompleteness', '');
            showWarnings(0);
            syncStickyTotal();
            for (const row of $('quoteItemsBody').querySelectorAll('[data-line-key]')) {
                row.querySelector('.sale-unit').textContent = '—';
                row.querySelector('.sale-amount').textContent = '—';
            }
            for (const box of $('quoteItemsBody').querySelectorAll('[data-result-for]')) {
                box.replaceChildren(element('div', 'Se calcula al completar los datos.', 'text-muted'));
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
            const [first] = headerIssues(false);
            if (first) fail(first.message, first.field);
            const conditions = $('quoteConditions').value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
            return { client_name: $('clientName').value.trim(), client_rfc: $('clientRfc').value.trim().toUpperCase(), client_email: $('clientEmail').value.trim(),
                client_contact: $('clientContact').value.trim(), client_phone: $('clientPhone').value.trim(), client_project: $('clientProject').value.trim(),
                issue_date: $('issueDate').value, valid_days: integer($('validDays').value, 3650), conditions,
                tax_rate: percentToFraction($('taxRate').value),
                discount_percent: decimal($('discountPercent').value, 5, 2, { max: '10000' }), fx_context_id: state.fx?.id || null };
        }
        function payload() {
            if (!state.lines.length) fail('Agrega al menos un producto o concepto.', 'productSearchInput');
            const inputs = state.transition ? { schema_version: 2, source_edit_version: state.transition.edit_version,
                tax_rate: percentToFraction($('taxRate').value),
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
            showWarnings(entries?.length || 0);
            if (!entries?.length) return;
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
            text('discountDisplay', discountText(result.descuento));
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
            result.partidas.forEach((partida, index) => lineResult(partida, preview.cliente.items[index]));
            syncStickyTotal();
            warnings(preview.warnings, persisted);
        }
        // Resumen por partida dentro de su detalle; los importes vienen del servidor.
        function lineResult(partida, client) {
            const key = partida.entrada.referencia;
            const box = [...$('quoteItemsBody').querySelectorAll('[data-result-for]')].find(b => b.dataset.resultFor === key);
            const line = state.lines.find(l => l.key === key);
            if (!box || !line || !client) return;
            const unit = line.units.sale_label.trim() || 'unidad';
            const shown = value => { const r = roundText(value, 2); return r ? (r.exact ? '' : '≈ ') + money(r.text) + ' MXN' : '—'; };
            const row = (label, value, strong = false) => {
                const node = element('div', '', 'd-flex justify-content-between gap-2 mb-1');
                node.append(element('span', label, 'text-muted'), element(strong ? 'strong' : 'span', value, 'text-end'));
                return node;
            };
            box.replaceChildren(
                row(`Costo por ${unit}`, partida.costo_unitario_mxn === null ? 'Desconocido' : shown(partida.costo_unitario_mxn)),
                row(`Precio por ${unit}`, client.unit_price_label || '—', true),
                element('hr', '', 'my-2'),
                row(`${line.quantity} × ${unit}`, money(client.amount) + ' MXN', true),
                row('Utilidad estimada', partida.utilidad_antes_descuento === null ? 'No calculable' : shown(partida.utilidad_antes_descuento)),
                element('div', 'Sin IVA y antes del descuento global.', 'small text-muted mt-1'));
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
                if (err.status === undefined) {
                    // Dato incompleto mientras se captura: se indica en el resumen, sin aviso emergente.
                    clearSummary('Falta completar datos para calcular: ' + err.message);
                    locks();
                    throw err;
                }
                clearSummary('No hay cálculo actualizado: revisa los datos o la conexión.');
                if (err.status === 409 && state.saved) state.conflict = true;
                showError(err); locks();
                throw err;
            }
        }
        // Controles del detalle de partida: etiqueta visible, data-field para «Ir al campo».
        function fieldBox(parent, label, help = '') {
            const id = `qf-${++fieldSeq}`, wrapper = element('div', '', 'mb-3');
            const caption = element('label', label, 'form-label small fw-semibold mb-1'); caption.htmlFor = id;
            wrapper.append(caption); parent.append(wrapper);
            return { id, wrapper, caption, help: () => { if (help) wrapper.append(element('div', help, 'form-text')); } };
        }
        function textField(parent, label, value, onChange, { disabled = false, name = '', suffix = '', help = '', placeholder = '' } = {}) {
            const box = fieldBox(parent, label, help);
            const input = document.createElement('input');
            Object.assign(input, { id: box.id, className: 'form-control form-control-sm', value: value ?? '', maxLength: 6000, placeholder });
            if (name) input.dataset.field = name;
            input.disabled = disabled || !editable();
            input.addEventListener('input', () => onChange(input.value));
            if (suffix) {
                const group = element('div', '', 'input-group input-group-sm');
                group.append(input, element('span', suffix, 'input-group-text'));
                box.wrapper.append(group);
            } else box.wrapper.append(input);
            box.help();
            return { input, caption: box.caption, suffix: box.wrapper.querySelector('.input-group-text') };
        }
        function choice(parent, legend, value, options, onChange, { disabled = false, name = '' } = {}) {
            const set = element('fieldset', '', 'mb-3'), group = element('div', '', 'btn-group w-100'), groupName = `qc-${++fieldSeq}`;
            set.append(element('legend', legend, 'form-label small fw-semibold mb-1'));
            group.setAttribute('role', 'group');
            for (const [key, label] of options) {
                const input = document.createElement('input');
                Object.assign(input, { type: 'radio', className: 'btn-check', name: groupName, id: `${groupName}-${key}`, value: key, checked: value === key, autocomplete: 'off' });
                if (name) input.dataset.field = name;
                input.disabled = disabled || !editable();
                input.addEventListener('change', () => { if (input.checked) onChange(key); });
                const caption = element('label', label, 'btn btn-cconor-choice btn-sm'); caption.htmlFor = input.id;
                group.append(input, caption);
            }
            set.append(group); parent.append(set);
        }
        function checkField(parent, label, checked, onChange, { name = '' } = {}) {
            const wrapper = element('div', '', 'form-check mb-3'), input = document.createElement('input');
            Object.assign(input, { type: 'checkbox', className: 'form-check-input', id: `qk-${++fieldSeq}`, checked: !!checked });
            if (name) input.dataset.field = name;
            input.disabled = !editable();
            input.addEventListener('change', () => onChange(input.checked));
            const caption = element('label', label, 'form-check-label small fw-semibold'); caption.htmlFor = input.id;
            wrapper.append(input, caption); parent.append(wrapper);
            return { input, caption };
        }
        const safePercent = value => { try { return fractionToPercent(value); } catch { return String(value ?? ''); } };
        function priceEditor(parent, label, data, enabled, update, name) {
            const box = fieldBox(parent, label);
            const group = element('div', '', 'input-group input-group-sm');
            const amount = document.createElement('input');
            Object.assign(amount, { id: box.id, className: 'form-control', value: data.amount ?? '', inputMode: 'decimal', maxLength: 80 });
            amount.dataset.field = name;
            amount.disabled = !enabled || !editable();
            amount.addEventListener('input', () => { data.amount = amount.value; update(); });
            const currency = document.createElement('select');
            currency.className = 'form-select'; currency.style.maxWidth = '6rem'; currency.setAttribute('aria-label', `Moneda: ${label}`);
            for (const code of ['MXN', 'USD']) { const option = element('option', code); option.value = code; currency.append(option); }
            currency.value = data.currency; currency.disabled = !enabled || !editable();
            currency.addEventListener('change', () => { data.currency = currency.value; update(); });
            group.append(amount, currency); box.wrapper.append(group);
            choice(parent, '¿Ese importe incluye IVA?', data.vat_basis, [['NET', 'No, es sin IVA'], ['INCLUDED', 'Sí, incluye IVA']],
                value => { data.vat_basis = value; update(true); }, { disabled: !enabled, name });
            if (data.vat_basis === 'INCLUDED') {
                textField(parent, 'IVA que incluye', data.vatRateInput ?? safePercent(data.vat_rate), value => {
                    data.vatRateInput = value;
                    try { data.vat_rate = percentToFraction(value); } catch { data.vat_rate = ''; }
                    update();
                }, { disabled: !enabled || !has('change_commercial_tax'), name, suffix: '%' });
            }
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
                if (line.issue) row.classList.add('line-has-issue');
                row.append(element('td', String(index + 1)), element('td', `${line.name || 'Concepto manual sin nombre'} · ${line.provider || 'Manual'}`),
                    element('td', line.units.sale_label || 'Pendiente'), element('td', line.quantity),
                    element('td', '—', 'sale-unit'), element('td', '—', 'sale-amount'));
                const actions = document.createElement('td');
                const remove = element('button', 'Quitar', 'btn btn-sm btn-outline-danger'); remove.type = 'button'; remove.disabled = !editable();
                remove.addEventListener('click', () => { state.lines.splice(index, 1); renderLines(); changed(); });
                actions.append(remove); row.append(actions); $('quoteItemsBody').append(row);
                const detailRow = document.createElement('tr'), detailCell = document.createElement('td'); detailCell.colSpan = 7;
                const details = element('details', '', 'line-detail'); details.open = !!line.expanded; details.dataset.lineDetail = line.key;
                details.addEventListener('toggle', () => { line.expanded = details.open; });
                details.append(element('summary', `Detalle de la partida ${index + 1}`, 'small text-primary fw-semibold mb-2'));
                const issueAlert = line.issue ? element('div', line.issue.message, 'alert alert-danger py-2 small mb-3') : null;
                if (issueAlert) details.append(issueAlert);
                const grid = element('div', '', 'row g-4 px-1 pb-2');
                const source = element('div', '', 'col-lg-4'), units = element('div', '', 'col-lg-4'), pricing = element('div', '', 'col-lg-4');
                const update = rerender => {
                    if (line.issue) { line.issue = null; row.classList.remove('line-has-issue'); issueAlert?.remove(); }
                    changed(); if (rerender) renderLines();
                };
                const canSell = has('set_commercial_sale');

                if (line.kind === 'API') {
                    source.append(element('h6', 'Del proveedor', 'fw-bold'));
                    const box = element('div', '', 'line-source-box small mb-3');
                    const fact = (label, value) => { const r = element('div', '', 'd-flex justify-content-between gap-2 mb-1'); r.append(element('span', label, 'text-muted'), element('strong', value, 'text-end')); box.append(r); };
                    fact('Proveedor', line.provider || '—');
                    fact('Marca · Modelo', `${line.brand || '—'} · ${line.model || '—'}`);
                    fact('Precio', `${money(trimZeros(line.originalPrice))} ${line.originalCurrency} · ${line.originalVat === null ? 'sin IVA' : 'incluye IVA ' + safePercent(line.originalVat) + '%'}`);
                    box.append(element('div', 'Estos datos vienen del proveedor y no se pueden editar.', 'text-muted mt-2'));
                    source.append(box);
                } else {
                    source.append(element('h6', 'Concepto y costo', 'fw-bold'));
                    textField(source, 'Nombre del concepto', line.name, value => { line.name = value; update(); }, { disabled: line.persisted || !canSell, name: 'name' });
                    choice(source, '¿Conoces el costo?', line.knownCost ? 'YES' : 'NO', [['YES', 'Sí, lo conozco'], ['NO', 'No lo sé']],
                        value => { line.knownCost = value === 'YES'; update(true); }, { disabled: !canSell, name: 'knownCost' });
                    if (line.knownCost) priceEditor(source, 'Costo por unidad del proveedor', line.cost, canSell, update, 'cost');
                    else source.append(element('div', '«No lo sé» no es lo mismo que costo $0: la utilidad no se podrá calcular.', 'form-text mb-3'));
                }
                textField(source, 'Descripción adicional para el cliente (opcional)', line.description, value => { line.description = value; update(); }, { name: 'description' });

                units.append(element('h6', '¿Cómo lo vendes?', 'fw-bold'));
                const split = line.unitMode ? line.unitMode === 'SPLIT' : splitUnits(line.units);
                const lockSource = line.persisted && line.kind === 'API';
                choice(units, 'Forma de venta', split ? 'SPLIT' : 'SAME', [['SAME', 'Igual que el proveedor'], ['SPLIT', 'Por partes (metros, etc.)']], value => {
                    line.unitMode = value;
                    if (value === 'SAME') { line.units.sale_label = line.units.source_label; line.units.conversion_factor = '1'; }
                    line.units.confirmed = false; update(true);
                }, { name: 'units' });
                const refs = {};
                const refresh = () => {
                    const from = line.units.source_label.trim() || 'unidad del proveedor', to = line.units.sale_label.trim() || 'tu unidad';
                    if (refs.factor) refs.factor.caption.textContent = `¿Cuántas «${to}» trae 1 «${from}»?`;
                    if (refs.chip) refs.chip.textContent = `1 ${from} = ${line.units.conversion_factor || '…'} ${to}`;
                    if (refs.quantity?.suffix) refs.quantity.suffix.textContent = line.units.sale_label.trim() || 'unidades';
                    refs.confirm.caption.textContent = split ? `Revisé la equivalencia: 1 ${from} = ${line.units.conversion_factor || '…'} ${to}` : 'Revisé la unidad y la cantidad';
                };
                const unitsChanged = () => { line.units.confirmed = false; refs.confirm.input.checked = false; refresh(); update(); };
                if (!split) {
                    textField(units, 'Unidad (pieza, servicio, metro…)', line.units.source_label, value => {
                        line.units.source_label = value; line.units.sale_label = value; line.units.conversion_factor = '1'; unitsChanged();
                    }, { disabled: lockSource, name: 'units' });
                } else {
                    textField(units, 'El proveedor lo vende por', line.units.source_label, value => { line.units.source_label = value; unitsChanged(); },
                        { disabled: lockSource, name: 'units', placeholder: 'Ej. Bobina' });
                    textField(units, 'Tú lo vendes por', line.units.sale_label, value => { line.units.sale_label = value; unitsChanged(); },
                        { name: 'units', placeholder: 'Ej. Metro' });
                    refs.factor = textField(units, '¿Cuántas trae?', line.units.conversion_factor, value => { line.units.conversion_factor = value; unitsChanged(); },
                        { name: 'factor', help: 'Ejemplo: una bobina de 100 m → 100.' });
                    refs.chip = element('div', '', 'equivalence-chip mb-3');
                    units.append(refs.chip);
                }
                refs.quantity = textField(units, 'Cantidad', line.quantity, value => { line.quantity = value; update(); },
                    { name: 'quantity', suffix: line.units.sale_label.trim() || 'unidades', help: 'Solo números enteros (1, 3, 10…).' });
                refs.confirm = checkField(units, '', line.units.confirmed, value => { line.units.confirmed = value; update(); }, { name: 'confirm' });
                refresh();

                pricing.append(element('h6', 'Precio al cliente', 'fw-bold'));
                choice(pricing, 'Cómo calcular el precio', line.mode, [['MARGIN', 'Con margen'], ['MANUAL', 'Precio fijo']],
                    value => { line.mode = value; update(true); }, { disabled: !canSell, name: 'mode' });
                if (line.mode === 'MARGIN') {
                    textField(pricing, 'Margen sobre la venta', line.margin, value => { line.margin = value; update(); },
                        { disabled: !has('change_commercial_margin'), name: 'margin', suffix: '%' });
                } else priceEditor(pricing, `Precio por ${line.units.sale_label.trim() || 'unidad'}`, line.sale, canSell, update, 'sale');
                const result = element('div', '', 'line-result-box small'); result.dataset.resultFor = line.key;
                result.append(element('div', 'Se calcula al completar los datos.', 'text-muted'));
                pricing.append(result);
                if (!canSell || !has('change_commercial_margin')) pricing.append(element('p', 'Los campos bloqueados requieren permiso.', 'small text-muted mt-2 mb-0'));

                grid.append(source, units, pricing); details.append(grid); detailCell.append(details); detailRow.append(detailCell); $('quoteItemsBody').append(detailRow);
                if (line.issue?.lineField) for (const input of details.querySelectorAll(`[data-field="${line.issue.lineField}"]`)) input.classList.add('is-invalid');
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
                // Solo el enlace a la ficha del proveedor (sin foto en el cotizador); pestaña nueva.
                if (typeof offer.product_url === 'string' && /^https:\/\/[^\s"'<>]+$/.test(offer.product_url)) {
                    const ficha = element('a', `Ver en ${offer.provider_label} ↗`, 'small fw-semibold');
                    Object.assign(ficha, { href: offer.product_url, target: '_blank', rel: 'noopener noreferrer' });
                    info.append(element('div', '', 'small'));
                    info.lastChild.append(ficha);
                }
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
                if (searchError && currentError === searchError) error();
                searchError = '';
                renderProducts();
            } catch (err) {
                if (err.name === 'AbortError' || version !== searchVersion) return;
                state.products = []; text('searchStatus', err.message + fxSearchHint(err.message, has('change_commercial_fx')));
                const needsFx = /tipo de cambio/i.test(err.message);
                showError(err, needsFx && has('change_commercial_fx') ? { label: 'Ir al tipo de cambio', run: () => goTo({ field: 'newExchangeRate' }) } : null);
                if (needsFx && !has('change_commercial_fx')) text('quoteErrorText', err.message + fxSearchHint(err.message, false));
                searchError = currentError;
            }
        }
        function fxDisplay() {
            const value = state.fx ? String(state.fx.value).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '') : '';
            const when = state.fx ? new Date(state.fx.date) : null;
            const date = when && !Number.isNaN(when.getTime()) ? when.toLocaleString('es-MX', { dateStyle: 'short', timeStyle: 'short' }) : state.fx?.date;
            text('exchangeRate', state.fx ? `Aplicado: 1 USD = ${value} MXN` : 'Sin tipo de cambio');
            $('exchangeRate').classList.toggle('fx-status-ok', !!state.fx);
            text('fxEvidence', state.fx ? `Capturado el ${date} · ${state.fx.source === 'manual' ? 'captura manual' : state.fx.source}` : 'El sistema no supone un tipo de cambio. Si todo es en pesos, puede quedar vacío.');
            if (state.fx) clearFieldError($('newExchangeRate'));
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
                    conversion_factor: trimZeros(snapshot.conversion_factor), confirmed: true },
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
            clearIssues(); notice('');
            state.caps = detail.capabilities;
            state.saved = { folio: detail.folio, revision: detail.revision, edit_version: detail.edit_version, state_fingerprint: detail.state_fingerprint };
            state.dirty = false; state.conflict = false; state.preview = null; state.epoch += 1;
            state.readonly = detail.schema_version !== 2 || detail.status !== 'DRAFT';
            const values = { clientName: detail.client.business_name, clientRfc: detail.client.rfc, clientEmail: detail.client.email,
                clientContact: detail.client.contact || '', clientPhone: detail.client.phone || '', clientProject: detail.client.project || '',
                issueDate: detail.issue_date, validDays: detail.valid_days, quoteConditions: detail.conditions.join('\n'), quoteNotes: '',
                quoteFolio: detail.folio, quoteRevision: `${detail.revision} / ${detail.edit_version}`,
                taxRate: safePercent(detail.tax_rate), discountPercent: detail.discount_percent ?? '' };
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
                error(); notice('Revisión histórica v1 en solo lectura. Requiere una transición comercial revisada; no se truncan cantidades ni se inventa margen.');
            } else if (detail.preview) { summary(detail.preview, true); error(); }
            else {
                sealedSummary(detail);
                error(); notice('Revisión emitida en solo lectura. No se sobrescribe ni se regenera su documento.');
            }
        }
        function sealedSummary(detail) {
            const seals = detail.sealed;
            text('costBaseDisplay', money(seals.known_cost_total) + ' MXN');
            text('saleSubtotalDisplay', money(seals.sale_subtotal) + ' MXN');
            text('discountDisplay', discountText(seals.discount_amount));
            text('subtotalDisplay', money(detail.subtotal) + ' MXN'); text('ivaDisplay', money(detail.tax) + ' MXN'); text('totalFinalDisplay', money(detail.total) + ' MXN');
            text('marginDisplay', seals.costs_complete ? money(seals.gross_profit) + ' MXN' : 'No disponible: costos incompletos');
            text('costCompleteness', seals.costs_complete ? 'Todos los costos sellados son conocidos.' : 'Costos sellados incompletos.');
            text('calculationStatus', 'Importes sellados · no se recalcula la emisión');
            syncStickyTotal();
            showWarnings(0);
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
                    const issues = quoteIssues();
                    if (issues.length) { presentIssues(issues); return; }
                    clearIssues(); error();
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
            if (state.sessionExpired) {
                state.sessionExpired = false;
                if (sessionError && currentError === sessionError) error();
                sessionError = '';
            }
        }
        // Sin botón: al volver a la pestaña se revisan sesión y permisos (como mucho cada 15 s).
        async function refreshSession() {
            if (document.visibilityState !== 'visible' || state.busy || state.pending || Date.now() - lastSessionCheck < 15000) return;
            lastSessionCheck = Date.now();
            try { await sameActor(); }
            catch (err) { if (err.status === 401 && !state.sessionExpired) showError(err); }
            locks(); renderLines(); renderProducts();
        }
        $('quoteHeader').addEventListener('input', changed);
        for (const id of ['quoteHeader', 'taxRate', 'discountPercent', 'newExchangeRate']) $(id).addEventListener('input', event => clearFieldError(event.target));
        $('quoteItemsBody').addEventListener('input', event => event.target.classList?.remove('is-invalid'));
        $('quoteErrorClose').addEventListener('click', () => error());
        for (const id of ['quoteIssuesOk', 'quoteIssuesClose']) $(id).addEventListener('click', closeIssues);
        for (const id of ['taxRate', 'discountPercent']) $(id).addEventListener('input', changed);
        $('addManualBtn').addEventListener('click', () => { state.lines.push(blankLine()); renderLines(); changed(); });
        $('clearTableBtn').addEventListener('click', () => { if (window.confirm('Vaciar partidas de este borrador? No se borra historial.')) { state.lines = []; renderLines(); changed(); } });
        $('saveQuoteBtn').addEventListener('click', save);
        $('viewSummaryBtn').addEventListener('click', () => {
            if (!state.saved || state.dirty || state.pending || state.busy) return;
            window.location.assign(`/resumen/?${new URLSearchParams({ folio: state.saved.folio, revision: state.saved.revision })}`);
        });
        document.addEventListener('visibilitychange', refreshSession);
        window.addEventListener('focus', refreshSession);
        $('stickyNewBtn').addEventListener('click', () => $('newQuoteBtn').click());
        $('stickySaveBtn').addEventListener('click', () => $('saveQuoteBtn').click());
        // La barra aparece cuando el encabezado sale de la pantalla por arriba.
        if ('IntersectionObserver' in window) {
            new IntersectionObserver(([entry]) => {
                const visible = !entry.isIntersecting && entry.boundingClientRect.top < 0;
                $('quoteStickyBar').classList.toggle('is-visible', visible);
                $('quoteStickyBar').setAttribute('aria-hidden', String(!visible));
                for (const id of ['stickyNewBtn', 'stickySaveBtn']) $(id).tabIndex = visible ? 0 : -1;
                document.body.classList.toggle('quote-scrolled', visible);
            }).observe($('quoteTopHeader'));
        }
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
        $('newQuoteBtn').addEventListener('click', () => {
            if (state.dirty && !window.confirm('Descartar cambios no guardados y preparar otra cotización?')) return;
            state.saved = null; state.transition = null; state.loadedDetail = null; state.readonly = false; state.conflict = false; state.lines = []; state.fx = null;
            $('transitionNotice').classList.add('d-none');
            for (const id of ['clientName', 'clientRfc', 'clientEmail', 'clientContact', 'clientPhone', 'clientProject', 'quoteNotes', 'newExchangeRate']) $(id).value = '';
            $('quoteConditions').value = defaultConditions;
            $('quoteFolio').value = 'Se asigna al guardar'; $('quoteRevision').value = 'Sin guardar';
            $('taxRate').value = '16'; $('discountPercent').value = '0'; $('validDays').value = '15';
            $('issueDate').value = initialIssueDate;
            previewAbort?.abort(); clearTimeout(previewTimer); state.epoch += 1; state.dirty = false;
            clearIssues(); notice('');
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
                    $('issueDate').value = initialIssueDate; $('taxRate').value = '16'; $('discountPercent').value = '0';
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
