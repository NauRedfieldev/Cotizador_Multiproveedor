/* P6: Mis cotizaciones (diseño 12A): lista, revisión exacta, originales y correo. Sin mocks ni cálculo financiero. */
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
    // Solo un aviso marcado explícitamente como informativo deja de bloquear la emisión.
    function pendingConfirmation(warnings) {
        return (warnings || []).some(w => w.requires_confirmation !== false && !w.confirmed);
    }
    window.CCONORDocumentTools = Object.freeze({ money, positiveInteger, identity, documentURL, emissionOutcome, fetchPDF, pendingConfirmation });
    // Textos para el asesor; el servidor decide qué avisos bloquean la emisión.
    const WARNING_LABELS = { MARGEN_CERO: 'Margen cero', COSTO_DESCONOCIDO: 'Costo desconocido: utilidad incompleta',
        VENTA_BAJO_COSTO: 'Venta bajo costo', VENTA_CERO: 'Venta de partida en cero', TOTAL_CERO: 'Total de cotización en cero',
        STOCK_CERO: 'El proveedor informa existencias en cero', STOCK_INSUFICIENTE: 'Existencias del proveedor menores que la cantidad',
        STOCK_DESCONOCIDO: 'Existencias desconocidas: comprobar disponibilidad con el proveedor' };
    // 24C: relativa en pantalla; la exacta al pasar el cursor (helpers de api.js).
    const F = window.cconorFechas;
    function dateLabel(iso) { return F ? F.hace(iso) : String(iso || ''); }
    function exactDate(iso) { return F ? F.exacta(iso) : String(iso || ''); }
    document.addEventListener('DOMContentLoaded', function () {
        const $ = id => document.getElementById(id);
        if (!$('commercialSummaryApp')) return;
        const api = window.apiClient.commercial;
        const state = { detail: null, actor: undefined, authorized: false, busy: false, pending: null, page: 1, pages: 1, objectURL: null, filter: '', checkedAt: 0,
            company: '', companies: [] };
        // 5A: pestañas por empresa (las da el listado). '' = todas. Solo con dos empresas configuradas.
        function companyTabs(companies, counts = {}) {
            state.companies = companies;
            const box = $('historyCompanyFilter');
            show('historyCompanyFilter', companies.length > 1);
            if (companies.length < 2) { box.replaceChildren(); return; }
            // Lienzo fila 10: una sola línea con el nombre corto (el completo al pasar el cursor); la activa lleva
            // línea y conteo del color de su empresa («Todas», azul marino).
            const tabs = [...companies.map(c => [c.key, c.short_name || c.name, c.name, c.color]), ['', 'Todas', 'Todas las empresas', '']];
            box.replaceChildren(...tabs.map(([key, label, full, color]) => {
                const tab = node('button', '', 'mis-empresa' + (state.company === key ? ' is-active' : ''));
                tab.type = 'button'; tab.dataset.company = key; tab.title = full; tab.setAttribute('aria-pressed', String(state.company === key));
                tab.setAttribute('aria-label', `${full}: ${counts[key] ?? 0}`);
                if (color) { tab.style.setProperty('--tab-color', color); const dot = node('span', '', 'mis-empresa-punto'); dot.style.background = color; dot.setAttribute('aria-hidden', 'true'); tab.append(dot); }
                tab.append(node('span', label), node('span', String(counts[key] ?? 0), 'mis-empresa-conteo'));
                tab.disabled = state.busy || !!state.pending || state.actor === undefined;
                tab.addEventListener('click', () => { state.company = key; list(1); });
                return tab;
            }));
        }
        let readEpoch = 0, listEpoch = 0, listAbort, listTimer;
        function text(id, value) { $(id).textContent = value; }
        function node(tag, value, classes = '') { const el = document.createElement(tag); el.textContent = value; el.className = classes; return el; }
        function show(id, visible) { $(id).classList.toggle('d-none', !visible); }
        function kindOf(d) { return d.schema_version === 1 ? ['v1', 'Histórico'] : d.status === 'ISSUED' ? ['issued', 'Emitida'] : ['draft', 'Borrador']; }
        function pill(d) { const [kind, label] = kindOf(d); return node('span', label, `mis-pill mis-pill-${kind}`); }
        function editorURL(d) { return `/cotizador/?${new URLSearchParams({ folio: d.folio, revision: d.revision })}`; }
        function error(err = '') {
            text('documentErrorText', err ? err.message || err : ''); show('documentError', !!err);
            show('documentErrorLogin', !!err && err.status === 401);
        }
        function locks() {
            const d = state.detail, blocked = state.busy || !!state.pending || !state.authorized;
            const draft = d?.schema_version === 2 && d.status === 'DRAFT', issued = d?.schema_version === 2 && d.status === 'ISSUED', legacy = d?.schema_version === 1;
            $('historySearchInput').disabled = blocked || state.actor === undefined;
            for (const button of $('historyStatusFilter').querySelectorAll('button')) button.disabled = blocked || state.actor === undefined;
            for (const button of $('historyCompanyFilter').querySelectorAll('button')) button.disabled = blocked || state.actor === undefined;
            for (const button of $('historyList').querySelectorAll('button')) {
                button.disabled = blocked;
                button.classList.toggle('is-selected', !!d && button.dataset.folio === d.folio);
            }
            for (const button of $('revisionSelector').querySelectorAll('button')) button.disabled = blocked;
            // Solo los botones que aplican al estado de la revisión abierta.
            show('editDocumentBtn', draft); show('emitDocumentBtn', draft); show('openEmailBtn', issued); show('cloneDocumentBtn', issued || legacy);
            $('editDocumentBtn').disabled = blocked || !draft;
            $('emitDocumentBtn').disabled = blocked || !draft || !d.emission_enabled || pendingConfirmation(d.warnings);
            $('openEmailBtn').disabled = blocked || !issued;
            $('cloneDocumentBtn').disabled = blocked || !(issued || legacy);
            text('cloneDocumentBtn', legacy ? 'Preparar revisión comercial' : 'Nueva revisión');
            for (const id of ['downloadPdfBtn', 'printQuoteBtn']) $(id).disabled = blocked || !d;
            text('downloadPdfLabel', draft ? 'PDF borrador' : 'Descargar PDF');
            $('emailFields').disabled = blocked || !issued; $('confirmSendEmailBtn').disabled = blocked || !issued;
            show('resolveDocumentBtn', !!state.pending);
            $('resolveDocumentBtn').disabled = state.busy;
            text('resolveDocumentBtn', state.pending?.kind === 'email' ? 'Ya verifiqué el resultado del correo' : 'Resolver / reintentar la misma operación');
            $('historyPreviousBtn').disabled = blocked || state.page <= 1;
            $('historyNextBtn').disabled = blocked || state.page >= state.pages;
            text('documentActivity', state.pending ? 'Operación pendiente de resolver' : state.busy ? 'Procesando…' : !state.authorized ? 'Sin conexión autorizada' : '');
        }
        async function session() {
            try {
                const caps = await api.capabilities();
                if (state.actor !== undefined && state.actor !== caps.actor_id) {
                    const err = Error('La sesión cambió de asesor. Vuelve a la sesión original; no se reenviaron datos.');
                    err.status = 401; throw err;
                }
                state.actor = caps.actor_id; state.authorized = true; state.checkedAt = Date.now();
            } catch (err) {
                state.authorized = false; clearDocument();
                listAbort?.abort(); listEpoch += 1; $('historyList').replaceChildren(); text('historyStatus', ''); show('historyPager', false);
                error(err); throw err;
            }
        }
        function clearDocument() {
            readEpoch += 1;
            state.detail = null;
            if (state.objectURL) URL.revokeObjectURL(state.objectURL);
            state.objectURL = null;
            $('documentFrame').removeAttribute('src'); show('documentFrame', false);
            show('legacyDocumentNotice', false);
            show('documentView', false); show('documentEmpty', true);
            $('documentWarnings').replaceChildren(); $('revisionSelector').replaceChildren();
            for (const id of ['emailRecipientInput', 'emailSubjectInput', 'emailMessageInput']) $(id).value = '';
            if ($('emailDialog').open) $('emailDialog').close();
            for (const id of ['emailStatus', 'documentNotice', 'documentTitle', 'documentIdentity', 'documentMeta', 'documentTotalLabel', 'documentTotalValue']) text(id, '');
        }
        function warnings(d) {
            const box = $('documentWarnings');
            if (!d.emission_enabled) box.append(node('div', 'La emisión está deshabilitada en este entorno.', 'alert alert-info small py-2'));
            // 23C: precios de proveedor de otro día; recuerda revisarlos en el cotizador. No bloquea «Emitir».
            const captured = d.prices_captured_at ? new Date(d.prices_captured_at) : null;
            if (captured && !Number.isNaN(captured.getTime()) && captured.toDateString() !== new Date().toDateString()) {
                const panel = node('div', '', 'mis-precios-viejos');
                const age = node('strong', `Los precios son de ${dateLabel(d.prices_captured_at)}.`); age.title = exactDate(d.prices_captured_at);
                panel.append(age, node('span', 'Revísalos antes de emitir.'));
                const link = node('a', 'Revisar en el cotizador →', 'btn btn-sm btn-outline-primary ms-auto');
                link.href = `${editorURL(d)}&revisar=precios`;
                panel.append(link); box.append(panel);
            }
            const pending = d.warnings.filter(w => w.requires_confirmation !== false && !w.confirmed);
            if (pending.length) {
                const panel = node('div', '', 'mis-avisos');
                panel.append(node('strong', `${pending.length} aviso${pending.length === 1 ? '' : 's'} por revisar antes de emitir`, 'd-block mb-1'));
                for (const w of pending) panel.append(node('div', `${WARNING_LABELS[w.code] || w.code}. Se acepta en el cotizador.`, 'small'));
                const link = node('a', 'Revisar en el cotizador', 'btn btn-sm btn-outline-primary mt-2'); link.href = editorURL(d);
                panel.append(link); box.append(panel);
            }
            for (const w of d.warnings.filter(w => w.requires_confirmation === false)) box.append(node('div', `${WARNING_LABELS[w.code] || w.code}: aviso informativo, no requiere aceptación.`, 'alert alert-info small py-2'));
            for (const w of d.warnings.filter(w => w.requires_confirmation !== false && w.confirmed)) box.append(node('div', `${WARNING_LABELS[w.code] || w.code}: aceptado.`, 'small text-success mb-2'));
        }
        async function displayPDF(detail, epoch) {
            try {
                const data = await fetchPDF(detail);
                if (epoch !== readEpoch) return;
                if (state.objectURL) URL.revokeObjectURL(state.objectURL);
                state.objectURL = URL.createObjectURL(data.blob);
                $('documentFrame').src = state.objectURL;
                show('documentFrame', true);
            } catch (err) { if (epoch === readEpoch) error(err); }
        }
        async function open(folio, revision) {
            if (state.busy || state.pending) return;
            clearDocument(); const epoch = readEpoch; state.busy = true; locks(); error();
            try {
                const number = positiveInteger(revision);
                if (typeof folio !== 'string' || !folio || folio.length > 20) throw Error('Indica un folio válido.');
                await session();
                const d = (await api.summary(folio, number)).data;
                if (epoch !== readEpoch) return;
                if (d.actor_id !== state.actor) throw Error('La sesión cambió durante la lectura. Recarga con el asesor original.');
                identity(d); state.detail = d;
                history.replaceState(null, '', `${location.pathname}?${new URLSearchParams({ folio: d.folio, revision: d.revision })}`);
                show('documentEmpty', false); show('documentView', true);
                const [kind, label] = kindOf(d), client = d.cliente;
                text('documentTitle', `${d.folio} · Rev. ${d.revision}`);
                $('documentStatus').className = `mis-pill mis-pill-${kind}`; text('documentStatus', label);
                text('documentIdentity', [d.client_name, client?.client?.email].filter(Boolean).join(' · '));
                if (client) {
                    text('documentMeta', `${d.status === 'ISSUED' ? 'Emitida' : 'Fecha:'} ${dateLabel(client.issue_date)} · ${F ? F.vence(client.valid_until) : `vigente hasta ${client.valid_until}`} · ${client.items.length} partida${client.items.length === 1 ? '' : 's'}`);
                    $('documentMeta').title = `Fecha: ${exactDate(client.issue_date)} · Vigente hasta: ${exactDate(client.valid_until)}`;
                    text('documentTotalLabel', d.status === 'ISSUED' ? 'Total con IVA' : 'Total estimado (sin emitir)');
                    text('documentTotalValue', money(d.status === 'ISSUED' ? d.total : client.total));
                } else {
                    text('documentMeta', 'Histórico de uso interno');
                    text('documentTotalLabel', d.total === null ? '' : 'Total con IVA'); text('documentTotalValue', d.total === null ? '' : money(d.total));
                }
                for (const r of d.revisiones) {
                    const tab = node('button', `Rev. ${r.revision} · ${kindOf(r)[1]}`, 'mis-revision'), active = r.revision === d.revision;
                    tab.type = 'button'; tab.setAttribute('role', 'tab'); tab.setAttribute('aria-selected', String(active));
                    tab.classList.toggle('is-active', active);
                    tab.addEventListener('click', () => { if (!active) open(d.folio, r.revision); });
                    $('revisionSelector').append(tab);
                }
                if (d.schema_version === 1) {
                    show('legacyDocumentNotice', true);
                    text('documentNotice', 'Original histórico de consulta interna.');
                } else {
                    $('emailRecipientInput').value = client.client.email || '';
                    $('emailSubjectInput').value = `Cotización ${d.folio} · Revisión ${d.revision} - ${client.company.name}`;
                    text('emailDialogInfo', `${d.folio} · Rev. ${d.revision} · se adjunta el PDF original emitido.`);
                    text('emailAttachment', `Cotizacion_${d.folio}_r${d.revision}.pdf · original emitido`);
                    text('documentNotice', d.status === 'ISSUED' ? 'Se muestra el PDF original archivado; no se recalcula ni se regenera.' :
                        'Borrador guardado: su PDF lleva la marca BORRADOR y el correo se habilita al emitir.');
                    // Vista previa = el PDF real (borrador con su marca u original archivado), igual al que se descarga.
                    if (d.status === 'DRAFT') warnings(d);
                    displayPDF(d, epoch);
                }
            } catch (err) { if (epoch === readEpoch) { clearDocument(); error(err); } }
            finally { state.busy = false; locks(); }
        }
        async function list(page = 1) {
            if (state.pending) return;
            clearTimeout(listTimer); listAbort?.abort(); const epoch = ++listEpoch;
            listAbort = new AbortController(); $('historyList').replaceChildren(); text('historyStatus', 'Consultando tus cotizaciones…');
            try {
                const filters = { q: $('historySearchInput').value.trim(), status: state.filter, page: String(page) };
                if (state.company) filters.empresa = state.company;
                const response = await api.list(filters, listAbort.signal);
                if (epoch !== listEpoch) return;
                if (response.actor_id !== state.actor) throw Error('La sesión cambió. Recarga la página antes de consultar.');
                companyTabs(response.companies || [], response.company_counts || {});
                state.page = response.pagination.page; state.pages = response.pagination.pages;
                const count = response.pagination.count;
                text('historyStatus', response.data.length ? `${count} cotizaci${count === 1 ? 'ón' : 'ones'}` : 'No hay cotizaciones con estos filtros.');
                text('historyPage', `Página ${state.page} de ${state.pages}`); show('historyPager', state.pages > 1);
                for (const d of response.data) {
                    const item = node('button', '', 'mis-item'); item.type = 'button'; item.dataset.folio = d.folio;
                    const top = node('span', '', 'mis-item-fila'), middle = node('span', '', 'mis-item-fila');
                    top.append(node('strong', `${d.folio} · Rev. ${d.revision}`), pill(d));
                    middle.append(node('span', d.client_name), node('strong', d.status === 'ISSUED' ? money(d.total) : ''));
                    const when = node('span', dateLabel(d.issue_date), 'small text-secondary'); when.title = exactDate(d.issue_date);
                    item.append(top, middle, when);
                    // 5A: barra del color de su empresa y, en «Todas», la etiqueta con su nombre.
                    const company = state.companies.length > 1 ? state.companies.find(c => c.key === d.company_key) : null;
                    if (company) {
                        item.classList.add('mis-item-con-empresa');
                        item.style.setProperty('--empresa-color', company.color);
                        if (!state.company) {
                            const tag = node('span', company.short_name || company.name, 'mis-item-empresa');
                            tag.title = company.name;
                            tag.style.background = company.tint; tag.style.color = company.text;
                            item.append(tag);
                        }
                    }
                    item.addEventListener('click', () => open(d.folio, d.revision));
                    $('historyList').append(item);
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
                    if (outcome === 'CONFLICT') { state.pending = null; clearDocument(); throw Error('La revisión cambió. Abre de nuevo la versión del servidor antes de emitir.'); }
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
                if (!state.detail) error(`La operación se confirmó para ${target[0]}, revisión ${target[1]}. Falta recargar su lectura; no vuelvas a crearla ni emitir otra revisión por este fallo.`);
                await list(state.page);
            }
        }
        async function email() {
            if (state.busy || state.pending || state.detail?.schema_version !== 2 || state.detail.status !== 'ISSUED') return;
            const recipient = $('emailRecipientInput');
            if (!recipient.value.trim() || !recipient.checkValidity()) { text('emailStatus', 'Indica un correo destinatario válido.'); recipient.focus(); return; }
            const d = state.detail, to = recipient.value.trim();
            if (!window.confirm(`Enviar a ${to} el PDF original de ${d.folio}, revisión ${d.revision}?`)) return;
            state.busy = true; locks();
            try {
                await session();
                await api.email(d.folio, { ...identity(d), email: to, subject: $('emailSubjectInput').value.trim(), message: $('emailMessageInput').value, attach_pdf: true });
                $('emailDialog').close();
                text('documentNotice', `El servidor de correo aceptó ${d.folio}, revisión ${d.revision}, para ${to}. Esto no certifica la entrega al destinatario.`); error();
            } catch (err) {
                if (err.uncertain) {
                    state.pending = { kind: 'email', folio: d.folio, revision: d.revision };
                    text('emailStatus', 'Resultado desconocido: el correo puede haberse enviado. Verifica la recepción antes de habilitar otro envío; no se reintentará automáticamente.');
                    text('documentNotice', 'Correo con resultado desconocido: verifica la recepción antes de volver a enviar.');
                } else text('emailStatus', err.message || String(err));
                error(err);
            } finally { state.busy = false; locks(); }
        }
        async function pdf(print = false) {
            if (state.busy || state.pending || !state.detail) return;
            const d = state.detail, epoch = readEpoch;
            if (d.schema_version === 1 && !window.confirm('Este original histórico puede contener costos/proveedores. Es solo para consulta interna. Continuar?')) return;
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
        $('editDocumentBtn').addEventListener('click', () => { if (state.detail) location.assign(editorURL(state.detail)); });
        $('emitDocumentBtn').addEventListener('click', () => mutate('emit'));
        $('cloneDocumentBtn').addEventListener('click', () => {
            if (state.detail?.schema_version === 1) {
                if (window.confirm('Preparar una revisión comercial nueva sin copiar costos, márgenes o unidades automáticamente del histórico?')) location.assign(`/cotizador/?${new URLSearchParams({ transition_folio: state.detail.folio, transition_revision: state.detail.revision })}`);
            } else mutate('clone');
        });
        $('resolveDocumentBtn').addEventListener('click', () => {
            if (state.pending?.kind === 'email') {
                if (window.confirm('Ya verificaste recepción/registros? Habilitar otro envío no demuestra si el anterior llegó y podría duplicarlo.')) { state.pending = null; text('emailStatus', ''); locks(); }
            } else if (state.pending) mutate(state.pending.kind);
        });
        $('openEmailBtn').addEventListener('click', () => {
            if ($('openEmailBtn').disabled) return;
            text('emailStatus', ''); $('emailDialog').showModal(); $('emailRecipientInput').focus();
        });
        for (const id of ['emailCancelBtn', 'emailCloseBtn']) $(id).addEventListener('click', () => $('emailDialog').close());
        $('confirmSendEmailBtn').addEventListener('click', email);
        $('downloadPdfBtn').addEventListener('click', () => pdf(false)); $('printQuoteBtn').addEventListener('click', () => pdf(true));
        $('documentErrorClose').addEventListener('click', () => error());
        for (const button of $('historyStatusFilter').querySelectorAll('button')) button.addEventListener('click', () => {
            state.filter = button.dataset.status;
            for (const other of $('historyStatusFilter').querySelectorAll('button')) {
                other.classList.toggle('is-active', other === button); other.setAttribute('aria-pressed', String(other === button));
            }
            list(1);
        });
        $('historySearchInput').addEventListener('input', () => { clearTimeout(listTimer); listAbort?.abort(); listEpoch += 1; $('historyList').replaceChildren(); listTimer = setTimeout(() => list(1), 350); });
        $('historyPreviousBtn').addEventListener('click', () => list(state.page - 1)); $('historyNextBtn').addEventListener('click', () => list(state.page + 1));
        // La sesión se revisa sola al volver a la pestaña (como mucho cada 15 s), igual que en el cotizador.
        function recheck() {
            if (document.visibilityState !== 'visible' || state.busy || state.pending || Date.now() - state.checkedAt < 15000) return;
            const wasAuthorized = state.authorized;
            session().then(() => { error(); locks(); if (!wasAuthorized) list(1); }, () => locks());
        }
        document.addEventListener('visibilitychange', recheck); window.addEventListener('focus', recheck);
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
