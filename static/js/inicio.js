/* Inicio (panel 7D): borradores y emitidas propias. Solo lectura; los importes son texto del servidor. */
(function () {
    'use strict';
    const MONTH_NAMES = ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre'];
    const SHOWN = 3;

    function money(value) {
        if (value === null || value === undefined) return 'Sin total sellado';
        if (!/^-?\d+(\.\d+)?$/.test(String(value))) return '—';
        const [whole, fraction = ''] = String(value).replace(/^-/, '').split('.');
        return (String(value).startsWith('-') ? '-$' : '$') + whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + '.' + fraction.padEnd(2, '0');
    }
    // 24C: «hace 2 días» (helpers de api.js); la fecha exacta va en el title.
    function shortDate(iso, now = new Date()) {
        return /^\d{4}-\d{2}-\d{2}$/.test(String(iso)) && window.cconorFechas ? window.cconorFechas.hace(iso, now) : '';
    }
    function inMonth(iso, now) {
        const match = /^(\d{4})-(\d{2})-\d{2}$/.exec(String(iso));
        return Boolean(match) && Number(match[1]) === now.getFullYear() && Number(match[2]) === now.getMonth() + 1;
    }
    function monthLabel(now) { return `${MONTH_NAMES[now.getMonth()]} ${now.getFullYear()}`; }
    function items(count) { return `${count} partida${count === 1 ? '' : 's'}`; }
    // Una página trae como mucho 20 filas: si todas son del mes y hay más páginas, el conteo es «N+».
    function monthCount(rows, pages, now) {
        const count = rows.filter(row => inMonth(row.issue_date, now)).length;
        return pages > 1 && count === rows.length ? `${count}+` : String(count);
    }
    function revisionQuery(row) { return new URLSearchParams({ folio: row.folio, revision: String(row.revision) }).toString(); }
    function pdfHref(row) { return `/cotizaciones/${encodeURIComponent(row.folio)}/pdf/?${new URLSearchParams({ revision: String(row.revision) })}`; }
    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }
    function link(label, href, className) {
        const node = el('a', className, label);
        node.href = href;
        return node;
    }
    function pill(kind, label) {
        const node = el('span', `panel-pill panel-pill-${kind}`);
        node.append(el('span', 'panel-dot'), label);
        return node;
    }
    function draftCard(row, urls) {
        const card = el('article', 'panel-card');
        const top = el('div', 'panel-card-top');
        top.append(el('strong', '', `${row.folio} · Rev. ${row.revision}`), pill('draft', 'Borrador'));
        const body = el('div', 'panel-card-body');
        const when = el('span', 'panel-muted', `${items(row.items_count)} · ${shortDate(row.issue_date)}`);
        if (window.cconorFechas) when.title = window.cconorFechas.exacta(row.issue_date);
        body.append(el('span', '', row.client_name), when);
        const foot = el('div', 'panel-card-foot');
        foot.append(el('span', 'panel-muted', 'Sin total hasta emitir'),
            link('Continuar editando', `${urls.cotizador}?${revisionQuery(row)}`, 'btn btn-cconor-primary btn-sm'));
        card.append(top, body, foot);
        return card;
    }
    function issuedCard(row, urls) {
        const legacy = row.schema_version === 1;
        const card = el('article', 'panel-card');
        const top = el('div', 'panel-card-top');
        top.append(el('strong', '', `${row.folio} · Rev. ${row.revision}`), legacy ? pill('legacy', 'Histórico v1') : pill('issued', 'Emitida'));
        const body = el('div', 'panel-card-body panel-card-split');
        const who = el('div', 'panel-card-body');
        const when = el('span', 'panel-muted', `Emitida ${shortDate(row.issue_date)}`);
        if (window.cconorFechas) when.title = window.cconorFechas.exacta(row.issue_date);
        who.append(el('span', '', row.client_name), when);
        body.append(who, el('strong', 'panel-total', money(row.total)));
        const foot = el('div', 'panel-card-foot panel-card-actions');
        foot.append(link('Ver resumen', `${urls.resumen}?${revisionQuery(row)}`, 'btn btn-outline-primary btn-sm'));
        // Un histórico v1 puede traer costos: su PDF solo se abre desde el resumen, con su advertencia.
        if (!legacy) foot.append(link('PDF', pdfHref(row), 'btn btn-outline-primary btn-sm'));
        card.append(top, body, foot);
        return card;
    }
    function fill(box, rows, build, empty) {
        box.replaceChildren();
        if (!rows.length) { box.append(el('p', 'panel-empty', empty)); return; }
        for (const row of rows.slice(0, SHOWN)) box.append(build(row));
    }

    window.CCONORHomeTools = Object.freeze({ money, shortDate, inMonth, monthLabel, monthCount, items, revisionQuery, pdfHref, draftCard, issuedCard });

    const panel = document.getElementById('homePanel');
    if (!panel) return;
    const $ = id => document.getElementById(id);
    const urls = { cotizador: panel.dataset.cotizadorUrl, resumen: panel.dataset.resumenUrl };

    async function load() {
        const now = new Date();
        $('kpiMonthLabel').textContent = monthLabel(now);
        try {
            const [drafts, issued] = await Promise.all([
                apiClient.commercial.list({ status: 'DRAFT', page: '1' }),
                apiClient.commercial.list({ status: 'ISSUED', page: '1' })
            ]);
            if (drafts.actor_id !== issued.actor_id) throw Error('La sesión cambió durante la consulta. Recarga la página.');
            const draftTotal = drafts.pagination.count, issuedTotal = issued.pagination.count;
            $('kpiDrafts').textContent = String(draftTotal); $('draftsCount').textContent = String(draftTotal);
            $('kpiIssued').textContent = String(issuedTotal); $('issuedCount').textContent = String(issuedTotal);
            $('kpiMonth').textContent = monthCount(issued.data, issued.pagination.pages, now);
            fill($('draftsList'), drafts.data, row => draftCard(row, urls), 'No tienes borradores por terminar.');
            fill($('issuedList'), issued.data, row => issuedCard(row, urls), 'Aún no has emitido cotizaciones.');
            const total = draftTotal + issuedTotal;
            $('panelMoreText').textContent = draftTotal > SHOWN || issuedTotal > SHOWN ? `Se muestran las ${SHOWN} más recientes de cada columna.` : '';
            if (total > 0) $('panelMoreLink').textContent = `Ver las ${total} en Mis cotizaciones →`;
        } catch (err) {
            const status = $('panelStatus');
            status.textContent = err.message;
            status.classList.remove('d-none');
            fill($('draftsList'), [], draftCard, 'No se pudieron cargar tus borradores.');
            fill($('issuedList'), [], issuedCard, 'No se pudieron cargar tus cotizaciones emitidas.');
        }
    }
    load();
}());
