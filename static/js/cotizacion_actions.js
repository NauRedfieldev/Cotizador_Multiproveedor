/* P6: resumen, originales y correo por revisión exacta. Sin mocks ni cálculo financiero. */
(function () {
    'use strict';
    function money(value) {
        if (value === null || value === undefined) return 'Sin total sellado';
        if (!/^-?\d+(\.\d+)?$/.test(String(value))) return '—';
        const [whole, fraction = ''] = String(value).replace(/^-/, '').split('.');
        return (String(value).startsWith('-') ? '-$' : '$') + whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + '.' + fraction.padEnd(2, '0');
    }
    function positiveInteger(value) {
        if (!/^[1-9][0-9]{0,9}$/.test(String(value)) || Number(value) > 2147483647) throw Error('Número de revisión/versión inválido.');
        return Number(value);
    }
    function identity(detail) {
        if (typeof detail.folio !== 'string' || !detail.folio || detail.folio.length > 20 || !['DRAFT', 'ISSUED'].includes(detail.status)) throw Error('Identidad de documento inválida.');
        const data = { exact: true, revision: positiveInteger(detail.revision), edit_version: positiveInteger(detail.edit_version), expected_status: detail.status };
        if (detail.schema_version === 2) {
            if (!/^[0-9a-f]{64}$/.test(detail.state_fingerprint || '')) throw Error('Falta huella de documento guardado.');
            data.state_fingerprint = detail.state_fingerprint;
        } else if (detail.schema_version !== 1) throw Error('Formato no soportado.');
        return data;
    }
    function documentURL(detail) {
        const data = identity(detail);
        return `/cotizaciones/${encodeURIComponent(detail.folio)}/pdf/?${new URLSearchParams({ ...data, exact: '1' })}`;
    }
    function emissionOutcome(detail, intent) {
        if (detail.schema_version !== 2 || detail.edit_version !== intent.body.edit_version || detail.state_fingerprint !== intent.body.state_fingerprint) return 'CONFLICT';
        return detail.status === 'ISSUED' ? 'DONE' : detail.status === 'DRAFT' ? 'RETRY' : 'CONFLICT';
    }
    async function fetchPDF(detail) {
        const response = await fetch(documentURL(detail), { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/pdf' } });
        if (!response.ok || !(response.headers.get('Content-Type') || '').toLowerCase().includes('application/pdf')) {
            let message = `No se recibió el PDF de la revisión (HTTP ${response.status}).`;
            if ((response.headers.get('Content-Type') || '').includes('application/json')) {
                try { message = (await response.json()).error || message; } catch (_) { /* No HTML como mensaje. */ }
            }
            const error = Error(message); error.status = response.status; throw error;
        }
        const blob = await response.blob();
        if (!blob.size) throw Error('El documento PDF está vacío.');
        return { blob, filename: `Cotizacion_${detail.folio}_r${detail.revision}${detail.status === 'DRAFT' ? '_BORRADOR' : ''}.pdf` };
    }
    window.CCONORDocumentTools = Object.freeze({ money, positiveInteger, identity, documentURL, emissionOutcome, fetchPDF });
    document.addEventListener('DOMContentLoaded', function () {
        const $ = id => document.getElementById(id);
        if (!$('commercialSummaryApp')) return;
        const api = window.apiClient.commercial;
        const state = { detail: null, actor: undefined, authorized: false, busy: false, pending: null, page: 1, pages: 1, objectURL: null };
        let readEpoch = 0, listEpoch = 0, listAbort, listTimer;
        function text(id, value) { $(id).textContent = value; }
        function node(tag, value, classes = '') { const el = document.createElement(tag); el.textContent = value; el.className = classes; return el; }
        function error(err = '') { text('documentError', err.message || err); $('documentError').classList.toggle('d-none', !err); }
        function locks() {
            const d = state.detail, blocked = state.busy || !!state.pending || !state.authorized;
            for (const id of ['openDocumentBtn', 'documentFolio', 'documentRevision', 'historySearchInput', 'historyStatusFilter', 'refreshHistoryBtn']) $(id).disabled = blocked || state.actor === undefined;
            $('documentConnectionBtn').disabled = state.busy;
            $('revisionSelector').disabled = blocked || !d;
            for (const id of ['editDocumentBtn', 'cloneDocumentBtn', 'downloadPdfBtn', 'printQuoteBtn', 'emitDocumentBtn', 'confirmSendEmailBtn']) $(id).disabled = blocked || !d;
            $('editDocumentBtn').disabled = blocked || !d || d.schema_version !== 2 || d.status !== 'DRAFT';
            $('cloneDocumentBtn').disabled = blocked || !d || (d.schema_version === 2 && d.status !== 'ISSUED');
            text('cloneDocumentBtn', d?.schema_version === 1 ? 'Preparar revisión comercial v2' : 'Nueva revisión');
            $('emitDocumentBtn').disabled = blocked || !d?.emission_enabled || d.status !== 'DRAFT' || d.warnings.some(w => !w.confirmed);
            const canEmail = !blocked && d?.schema_version === 2 && d.status === 'ISSUED';
            $('emailFields').disabled = !canEmail; $('confirmSendEmailBtn').disabled = !canEmail;
            $('resolveDocumentBtn').classList.toggle('d-none', !state.pending);
            $('resolveDocumentBtn').disabled = state.busy;
            text('resolveDocumentBtn', state.pending?.kind === 'email' ? 'Ya verifiqué el resultado del correo' : 'Resolver / reintentar la misma operación');
            for (const button of $('historyTableBody').querySelectorAll('button')) button.disabled = blocked;
            $('historyPreviousBtn').disabled = blocked || state.page <= 1;
            $('historyNextBtn').disabled = blocked || state.page >= state.pages;
            text('documentStatus', state.pending ? 'Operación pendiente de resolver' : state.busy ? 'Procesando…' : !state.authorized ? 'Sin conexión autorizada' : d ? `${d.status === 'ISSUED' ? 'Emitida' : 'Borrador'} · Formato v${d.schema_version}` : 'Selecciona una revisión');
        }
        async function session() {
            try {
                const caps = await api.capabilities();
                if (state.actor !== undefined && state.actor !== caps.actor_id) {
                    const err = Error('La sesión cambió de asesor. Vuelve a la sesión original; no se reenviaron datos.');
                    err.status = 401; throw err;
                }
                state.actor = caps.actor_id; state.authorized = true;
            } catch (err) {
                state.authorized = false; clearDocument();
                listAbort?.abort(); listEpoch += 1; $('historyTableBody').replaceChildren();
                error(err); throw err;
            }
        }
        function clearDocument() {
            readEpoch += 1;
            state.detail = null;
            if (state.objectURL) URL.revokeObjectURL(state.objectURL);
            state.objectURL = null;
            $('documentFrame').removeAttribute('src'); $('documentFrame').classList.add('d-none');
            $('printableQuote').classList.add('d-none'); $('legacyDocumentNotice').classList.add('d-none');
            $('documentEmpty').classList.remove('d-none'); $('documentItems').replaceChildren();
            $('documentWarnings').replaceChildren(); $('revisionSelector').replaceChildren();
            for (const id of ['emailRecipientInput', 'emailSubjectInput', 'emailMessageInput']) $(id).value = '';
            text('emailStatus', ''); text('documentIdentity', 'Sin revisión cargada.');
            text('documentNotice', 'Sin documento vigente en pantalla. Abre o recarga una revisión exacta; no se sustituye por otra automáticamente.');
        }
        function customerPreview(client) {
            $('printableQuote').classList.remove('d-none');
            text('documentCompanyName', client.company.name);
            text('documentCompanyDetails', [client.company.rfc, client.company.tagline, client.company.phone, client.company.email, client.company.advisor].filter(Boolean).join(' · '));
            text('documentClientName', client.client.business_name);
            text('documentClientDetails', [client.client.rfc, client.client.contact, client.client.email, client.client.phone, client.client.project].filter(Boolean).join(' · '));
            text('documentLabel', client.document_label);
            text('documentDates', `${client.folio} · Revisión ${client.revision} · Fecha ${client.issue_date} · Vigencia hasta ${client.valid_until} (${client.validity_label})`);
            $('documentItems').replaceChildren();
            client.items.forEach((item, index) => {
                const row = document.createElement('tr');
                for (const value of [index + 1, item.code, [item.name, item.description, item.brand, item.model].filter(Boolean).join(' · '), item.unit_label, item.qty, item.unit_price_label || 'Consultar original', money(item.amount)]) row.append(node('td', value));
                $('documentItems').append(row);
            });
            text('documentPrecisionNote', 'Los unitarios abreviados se identifican como aproximados. Prevalece el importe definitivo de cada partida; el descuento es global antes de IVA.');
            $('documentConditions').replaceChildren(...client.conditions.map(c => node('li', c)));
            for (const [id, key] of [['documentSaleSubtotal', 'sale_subtotal'], ['documentDiscount', 'discount_amount'], ['documentSubtotal', 'subtotal'], ['documentTax', 'iva'], ['documentTotal', 'total']]) text(id, money(client[key]));
            text('documentDiscountLabel', `Descuento (${client.discount_percent_label}) antes de IVA`);
            text('documentTaxLabel', `IVA (${client.iva_rate_label})`);
        }
        async function displayPDF(detail, epoch) {
            try {
                const data = await fetchPDF(detail);
                if (epoch !== readEpoch) return;
                if (state.objectURL) URL.revokeObjectURL(state.objectURL);
                state.objectURL = URL.createObjectURL(data.blob);
                $('documentFrame').src = state.objectURL;
                $('documentFrame').classList.remove('d-none');
            } catch (err) { if (epoch === readEpoch) error(err); }
        }
        async function open(folio, revision) {
            if (state.busy || state.pending) return;
            clearDocument(); const epoch = readEpoch; state.busy = true; locks(); error();
            try {
                const number = positiveInteger(revision);
                if (typeof folio !== 'string' || !folio || folio.length > 20) throw Error('Indica un folio válido.');
                $('documentFolio').value = folio; $('documentRevision').value = number;
                await session();
                const d = (await api.summary(folio, number)).data;
                if (epoch !== readEpoch) return;
                if (d.actor_id !== state.actor) throw Error('La sesión cambió durante la lectura. Recarga con el asesor original.');
                identity(d); state.detail = d;
                $('documentFolio').value = d.folio; $('documentRevision').value = d.revision;
                const query = new URLSearchParams({ folio: d.folio, revision: d.revision });
                history.replaceState(null, '', `${location.pathname}?${query}`);
                text('documentIdentity', `${d.folio} · Revisión ${d.revision} · Versión ${d.edit_version} · ${d.client_name}`);
                $('documentEmpty').classList.add('d-none');
                for (const r of d.revisiones) { const option = node('option', `Revisión ${r.revision} · ${r.status === 'ISSUED' ? 'Emitida' : 'Borrador'} · v${r.schema_version}`); option.value = r.revision; $('revisionSelector').append(option); }
                $('revisionSelector').value = d.revision;
                if (d.schema_version === 1) {
                    $('legacyDocumentNotice').classList.remove('d-none');
                    text('documentNotice', 'Original histórico interno. Para el cliente prepara una revisión comercial v2 revisada.');
                } else {
                    $('emailRecipientInput').value = d.cliente.client.email || '';
                    $('emailSubjectInput').value = `Cotización ${d.folio} · Revisión ${d.revision} - ${d.cliente.company.name}`;
                    text('documentNotice', d.status === 'ISSUED' ? 'Se muestra el PDF original archivado. No se recalcula ni regenera esta emisión.' :
                        'Borrador guardado. El PDF llevará BORRADOR; no se puede enviar por correo hasta emitir.');
                    if (d.status === 'DRAFT') {
                        customerPreview(d.cliente);
                        if (!d.emission_enabled) $('documentWarnings').append(node('div', 'La emisión HTTP está deshabilitada en este entorno.', 'alert alert-info small'));
                        for (const w of d.warnings) $('documentWarnings').append(node('div', `${w.code}: ${w.confirmed ? 'aceptación vigente' : 'requiere confirmación autorizada en el cotizador'}`, 'alert alert-warning small py-2'));
                    } else displayPDF(d, epoch);
                }
            } catch (err) { if (epoch === readEpoch) { clearDocument(); error(err); } }
            finally { state.busy = false; locks(); }
        }
        async function list(page = 1) {
            if (state.pending) return;
            clearTimeout(listTimer); listAbort?.abort(); const epoch = ++listEpoch;
            listAbort = new AbortController(); $('historyTableBody').replaceChildren(); text('historyStatus', 'Consultando tus cotizaciones…');
            try {
                const response = await api.list({ q: $('historySearchInput').value.trim(), status: $('historyStatusFilter').value, page: String(page) }, listAbort.signal);
                if (epoch !== listEpoch) return;
                if (response.actor_id !== state.actor) throw Error('La sesión cambió. Revisa la conexión antes de consultar.');
                state.page = response.pagination.page; state.pages = response.pagination.pages;
                text('historyStatus', response.data.length ? `${response.pagination.count} cotizaciones propias con revisiones capturadas.` : 'No se encontraron cotizaciones propias con estos filtros.');
                text('historyPage', `Página ${state.page} de ${state.pages}`);
                for (const d of response.data) {
                    const row = document.createElement('tr');
                    for (const value of [d.folio, d.client_name, d.issue_date, d.revision, `${d.status === 'ISSUED' ? 'Emitida' : 'Borrador'} / v${d.schema_version}`, money(d.total)]) row.append(node('td', value));
                    const cell = document.createElement('td'), button = node('button', 'Abrir', 'btn btn-outline-primary btn-sm'); button.type = 'button';
                    button.addEventListener('click', () => open(d.folio, d.revision)); cell.append(button); row.append(cell); $('historyTableBody').append(row);
                }
            } catch (err) { if (err.name !== 'AbortError' && epoch === listEpoch) { text('historyStatus', err.message); state.pages = 1; state.page = 1; text('historyPage', ''); } }
            finally { if (epoch === listEpoch) locks(); }
        }
        async function mutate(kind) {
            if (state.busy) return;
            if (!state.pending) {
                const d = state.detail; if (!d) return;
                if (!window.confirm(kind === 'emit' ? `Emitir ${d.folio}, revisión ${d.revision}, versión ${d.edit_version}? No enviará correo ni permitirá sobrescribir el original.` : `Crear un borrador nuevo desde ${d.folio}, revisión ${d.revision}? El original no se modifica.`)) return;
                state.pending = { kind, actor: state.actor, folio: d.folio, revision: d.revision,
                    body: kind === 'emit' ? { edit_version: d.edit_version, state_fingerprint: d.state_fingerprint } : { schema_version: 2, request_id: crypto.randomUUID() } };
            }
            state.busy = true; locks();
            let target;
            try {
                await session(); const intent = state.pending;
                if (intent.actor !== state.actor) throw Error('No reenviar una operación de otro asesor.');
                let result;
                if (intent.kind === 'emit') {
                    const current = (await api.summary(intent.folio, intent.revision)).data;
                    if (current.actor_id !== intent.actor) { const err = Error('La sesión cambió durante la lectura; no se emitió.'); err.status = 401; throw err; }
                    const outcome = emissionOutcome(current, intent);
                    if (outcome === 'CONFLICT') { state.pending = null; clearDocument(); throw Error('La revisión cambió. Abre explícitamente la versión del servidor antes de emitir.'); }
                    result = outcome === 'DONE' ? current : (await api.emit(intent.folio, intent.revision, intent.body)).data;
                } else result = (await api.clone(intent.folio, intent.revision, intent.body)).data;
                target = [result.folio, result.revision]; state.pending = null;
                error();
            } catch (err) {
                if (state.pending) {
                    if (state.pending.uncertain || err.uncertain || !err.status || err.status === 401) state.pending.uncertain = true;
                    else { state.pending = null; if (err.status === 409) clearDocument(); }
                }
                error(err);
            } finally { state.busy = false; locks(); }
            if (target) {
                await open(...target);
                if (!state.detail) text('documentNotice', `La operación se confirmó para ${target[0]}, revisión ${target[1]}. Falta recargar su lectura; no vuelvas a crearla ni emitir otra revisión por este fallo.`);
                await list(state.page);
            }
        }
        async function email() {
            if (state.busy || state.pending || state.detail?.schema_version !== 2 || state.detail.status !== 'ISSUED') return;
            const recipient = $('emailRecipientInput');
            if (!recipient.value.trim() || !recipient.checkValidity()) { error('Indica un correo destinatario válido.'); return; }
            const d = state.detail, to = recipient.value.trim();
            if (!window.confirm(`Enviar a ${to} el PDF original de ${d.folio}, revisión ${d.revision}?`)) return;
            state.busy = true; locks();
            try {
                await session();
                await api.email(d.folio, { ...identity(d), email: to, subject: $('emailSubjectInput').value.trim(), message: $('emailMessageInput').value, attach_pdf: true });
                text('emailStatus', `Servidor de correo aceptó ${d.folio}, revisión ${d.revision}, para ${to}. Esto no certifica entrega al destinatario.`); error();
            } catch (err) {
                if (err.uncertain) {
                    state.pending = { kind: 'email', folio: d.folio, revision: d.revision };
                    text('emailStatus', 'Resultado desconocido: el correo puede haberse enviado. Verifica recepción o registros antes de habilitar otro envío; no se reintentará automáticamente.');
                }
                error(err);
            } finally { state.busy = false; locks(); }
        }
        async function pdf(print = false) {
            if (state.busy || state.pending || !state.detail) return;
            const d = state.detail, epoch = readEpoch;
            if (d.schema_version === 1 && !window.confirm('Este original v1 puede contener costos/proveedores. Es solo para consulta interna. Continuar?')) return;
            const tab = print ? window.open('about:blank', '_blank') : null;
            if (tab) tab.opener = null;
            state.busy = true; locks();
            try {
                await session(); const data = await fetchPDF(d);
                if (epoch !== readEpoch) { tab?.close(); return; }
                const url = URL.createObjectURL(data.blob);
                if (print && tab) tab.location.replace(url);
                else { const a = document.createElement('a'); a.href = url; a.download = data.filename; document.body.append(a); a.click(); a.remove(); }
                setTimeout(() => URL.revokeObjectURL(url), 60000); error();
                if (print && !tab) error('El navegador bloqueó la pestaña; se descargó el PDF para que puedas abrirlo e imprimirlo.');
            } catch (err) { tab?.close(); error(err); if (err.status === 409) clearDocument(); }
            finally { state.busy = false; locks(); }
        }
        $('openDocumentBtn').addEventListener('click', () => open($('documentFolio').value.trim(), $('documentRevision').value));
        $('revisionSelector').addEventListener('change', () => open(state.detail.folio, $('revisionSelector').value));
        $('editDocumentBtn').addEventListener('click', () => { if (state.detail) location.assign(`/cotizador/?${new URLSearchParams({ folio: state.detail.folio, revision: state.detail.revision })}`); });
        $('emitDocumentBtn').addEventListener('click', () => mutate('emit'));
        $('cloneDocumentBtn').addEventListener('click', () => {
            if (state.detail?.schema_version === 1) {
                if (window.confirm('Preparar una revisión comercial nueva sin copiar costos, márgenes o unidades automáticamente del histórico?')) location.assign(`/cotizador/?${new URLSearchParams({ transition_folio: state.detail.folio, transition_revision: state.detail.revision })}`);
            } else mutate('clone');
        });
        $('resolveDocumentBtn').addEventListener('click', () => {
            if (state.pending?.kind === 'email') {
                if (window.confirm('Ya verificaste recepción/registros? Habilitar otro envío no demuestra si el anterior llegó y podría duplicarlo.')) { state.pending = null; locks(); }
            } else if (state.pending) mutate(state.pending.kind);
        });
        $('confirmSendEmailBtn').addEventListener('click', email);
        $('downloadPdfBtn').addEventListener('click', () => pdf(false)); $('printQuoteBtn').addEventListener('click', () => pdf(true));
        $('refreshHistoryBtn').addEventListener('click', () => list(1));
        $('historyStatusFilter').addEventListener('change', () => list(1));
        $('historySearchInput').addEventListener('input', () => { clearTimeout(listTimer); listAbort?.abort(); listEpoch += 1; $('historyTableBody').replaceChildren(); listTimer = setTimeout(() => list(1), 350); });
        $('historyPreviousBtn').addEventListener('click', () => list(state.page - 1)); $('historyNextBtn').addEventListener('click', () => list(state.page + 1));
        $('documentConnectionBtn').addEventListener('click', () => initialize());
        window.addEventListener('beforeunload', event => { if (state.pending || state.busy) { event.preventDefault(); event.returnValue = ''; } });
        async function initialize() {
            try {
                await session(); error(); locks(); await list(1);
                if (!state.detail && !state.pending) {
                    const query = new URLSearchParams(location.search);
                    if (query.has('folio')) await open(query.get('folio'), query.get('revision'));
                }
            } catch (err) { error(err); locks(); }
        }
        locks(); initialize();
    });
}());
