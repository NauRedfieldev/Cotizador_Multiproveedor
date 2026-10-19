// P5/P6: node --test tools/test_cotizador_comercial.cjs (sin paquetes/build/red).
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
function runtime(fetch = async () => { throw Error('No red'); }) {
    const context = vm.createContext({ window: {}, console, URLSearchParams, AbortController, setTimeout, clearTimeout,
        document: { cookie: 'csrftoken=synthetic-csrf', addEventListener() {}, getElementById: id => id === 'commercialQuoteApp' ? {} : null },
        localStorage: { getItem() { throw Error('No storage en cotizador'); }, setItem() { throw Error('No storage en cotizador'); } }, fetch });
    for (const name of ['api.js', 'cotizador.js', 'cotizacion_actions.js']) vm.runInContext(fs.readFileSync(path.join(root, 'static/js', name), 'utf8'), context);
    return { tools: context.window.CCONORQuoteTools, documents: context.window.CCONORDocumentTools, api: context.window.apiClient.commercial };
}
const plain = value => JSON.parse(JSON.stringify(value));
function line(changes = {}) {
    return { key: 'uuid-estable', persisted: false, kind: 'MANUAL', name: 'QA', description: '', quantity: '2',
        units: { source_label: 'Servicio', sale_label: 'Servicio', conversion_factor: '1', confirmed: true },
        mode: 'MANUAL', margin: '35', knownCost: false,
        cost: { amount: '', currency: 'MXN', vat_basis: 'NET' },
        sale: { amount: '123456789012.123456', currency: 'MXN', vat_basis: 'NET' }, ...changes };
}
function response(status, body, type = 'application/json') {
    return { status, ok: status >= 200 && status < 300, headers: { get: () => type }, json: async () => body };
}
test('cantidades enteras sin truncamiento, bool ni exponentes', () => {
    const { tools } = runtime();
    assert.equal(tools.integer('99999999'), 99999999);
    for (const bad of ['1.5', '1e2', '0', '-1', true, '100000000']) assert.throws(() => tools.integer(bad));
    assert.equal(tools.storedInteger('2.00'), 2);
    assert.throws(() => tools.storedInteger('2.50'));
});
test('precios y porcentajes se conservan como texto exacto', () => {
    const { tools } = runtime();
    assert.equal(tools.decimal('999999999999.123456', 18, 6), '999999999999.123456');
    assert.equal(tools.decimal('35.0000', 4, 2, { max: '9999' }), '35.0000');
    for (const bad of [1.1, '1e2', 'NaN', '-1', '1,5', '.16', '1.1234567']) assert.throws(() => tools.decimal(bad, 18, 6));
    assert.throws(() => tools.decimal('100', 4, 2, { max: '9999' }));
    assert.throws(() => tools.decimal('0', 18, 6, { positive: true }));
});
test('presentación monetaria no pierde enteros grandes ni sustituye costo ausente por cero', () => {
    const { tools } = runtime();
    assert.equal(tools.money('123456789012345678901234.56'), '$123,456,789,012,345,678,901,234.56');
    assert.equal(tools.money('-100.00'), '-$100.00');
    assert.equal(tools.money(null), 'No disponible');
    assert.equal(tools.money('0.00'), '$0.00');
});
test('manual desconocido y costo cero son entradas distintas', () => {
    const { tools } = runtime();
    assert.equal(tools.item(line()).cost, null);
    const known = tools.item(line({ knownCost: true, cost: { amount: '0', currency: 'MXN', vat_basis: 'NET' } }));
    assert.equal(known.cost.amount, '0');
    assert.equal(known.pricing.sale.amount, '123456789012.123456');
    assert.throws(() => tools.item(line({ mode: 'MARGIN' })));
});
test('precio manual incluido conserva su tasa propia', () => {
    const { tools } = runtime();
    const payload = tools.item(line({ sale: { amount: '116', currency: 'MXN', vat_basis: 'INCLUDED', vat_rate: '0.16' } }));
    assert.equal(payload.pricing.sale.vat_rate, '0.16');
    assert.throws(() => tools.price({ amount: '100', currency: 'MXN', vat_basis: '' }));
});
test('API envía referencias, no costos ni moneda originales; EXISTING no recaptura', () => {
    const { tools } = runtime();
    const api = line({ kind: 'API', mode: 'MARGIN', offer: { context_ref: 'captura', offer_ref: 'oferta' } });
    const data = tools.item(api);
    assert.deepEqual(plain(data.source), { type: 'API', context_ref: 'captura', offer_ref: 'oferta' });
    assert.equal(data.cost, undefined);
    assert.equal(data.currency, undefined);
    assert.equal(data.pricing.margin_percent, '35');
    assert.deepEqual(plain(tools.item({ ...api, persisted: true }).source), { type: 'EXISTING' });
    assert.throws(() => tools.item({ ...api, units: { ...api.units, confirmed: false } }));
});
test('resolución de PUT perdido comprueba versión y huella antes de repetir', () => {
    const { tools } = runtime();
    const intent = { version: 3, baseFingerprint: 'antes', fingerprint: 'propuesta' };
    const draft = { schema_version: 2, status: 'DRAFT', edit_version: 3, state_fingerprint: 'antes' };
    assert.equal(tools.editOutcome(draft, intent), 'RETRY');
    assert.equal(tools.editOutcome({ ...draft, edit_version: 4, state_fingerprint: 'propuesta' }, intent), 'SAVED');
    assert.equal(tools.editOutcome({ ...draft, edit_version: 4, state_fingerprint: 'ajena' }, intent), 'CONFLICT');
    assert.equal(tools.editOutcome({ ...draft, status: 'ISSUED' }, intent), 'CONFLICT');
});
test('un guardado incierto no pierde la intención tras CSRF o permiso denegado posteriores', () => {
    const { tools } = runtime();
    assert.equal(tools.retainIntent({ status: 500, uncertain: true }), true);
    assert.equal(tools.retainIntent({ status: 401 }), true);
    assert.equal(tools.retainIntent({ status: 403, uncertain: false }, true), true);
    assert.equal(tools.retainIntent({ status: 409, uncertain: false }, true), true);
    assert.equal(tools.retainIntent({ status: 403, uncertain: false }), false);
});
test('cliente comercial siempre fetch, schema2, q y credenciales; no depende del mock global', async () => {
    let call;
    const { api } = runtime(async (...args) => { call = args; return response(200, { success: true, data: [] }); });
    await api.search({ q: 'equipo & modelo' });
    const url = new URL(call[0], 'http://qa.invalid');
    assert.equal(url.searchParams.get('q'), 'equipo & modelo');
    assert.equal(url.searchParams.get('schema_version'), '2');
    assert.equal(call[1].credentials, 'same-origin');
    assert.equal(call[1].cache, 'no-store');
});
test('POST conserva UUID/cuerpo al reintentar y usa CSRF', async () => {
    const calls = [];
    const { api } = runtime(async (...args) => { calls.push(args); return response(201, { success: true, data: { folio: 'QA' } }); });
    const body = { request_id: 'uuid-intencion', tax_rate: '0.16', items: [] };
    await api.create(body); await api.create(body);
    assert.equal(calls[0][1].body, calls[1][1].body);
    assert.equal(calls[0][1].headers['X-CSRFToken'], 'synthetic-csrf');
});
test('HTML CSRF/401/409 no son éxito ni HTML mostrado al usuario', async () => {
    for (const status of [401, 403, 409]) {
        const { api } = runtime(async () => response(status, {}, 'text/html'));
        await assert.rejects(api.create({}), e => e.status === status && e.uncertain === false && !e.message.includes('<html>'));
    }
});
test('respuesta perdida, 500 o 2xx no JSON conservan la incertidumbre de una mutación', async () => {
    for (const fetch of [async () => { throw Error('corte'); }, async () => response(500, {}), async () => response(200, {}, 'text/html')]) {
        const { api } = runtime(fetch);
        await assert.rejects(api.create({}), e => e.uncertain === true);
        await assert.rejects(api.previewNew({}), e => e.uncertain === false);
    }
});
test('cancelar preview no se transforma en error financiero ni guardado incierto', async () => {
    const { api } = runtime(async () => { const e = Error('abort'); e.name = 'AbortError'; throw e; });
    await assert.rejects(api.previewNew({}), e => e.name === 'AbortError');
});
test('rutas de revisión exacta escapan folio y no usan rutas de listado/borrado', async () => {
    let url;
    const { api } = runtime(async value => { url = value; return response(200, { success: true, data: {} }); });
    await api.revision('QA/A?', 2);
    assert.equal(url, '/api/v1/quotes/QA%2FA%3F/revisiones/2/');
});
test('controlador conectado sin innerHTML, parseInt, parseFloat ni storage financiero', () => {
    const source = fs.readFileSync(path.join(root, 'static/js/cotizador.js'), 'utf8');
    assert.doesNotMatch(source, /\.innerHTML\s*=|parseInt\(|parseFloat\(|localStorage\./);
});
test('transición v1: tras escribir la copia v2 se olvida el origen antes de releer', () => {
    const source = fs.readFileSync(path.join(root, 'static/js/cotizador.js'), 'utf8');
    const save = source.slice(source.indexOf('async function save()'), source.indexOf('async function confirmWarning('));
    const written = save.indexOf('state.pending = null;\n');
    const cleared = save.indexOf('state.transition = null;', written);
    const reread = save.indexOf('await api.revision(saved.folio, saved.revision)');
    assert.ok(written > 0 && cleared > written && reread > cleared);
});

const documentDetail = (changes = {}) => ({ folio: 'QA-001', revision: 2, edit_version: 3, status: 'DRAFT', schema_version: 2, state_fingerprint: 'a'.repeat(64), ...changes });
test('P6: identidad documental fija revisión/versión/estado/huella, sin datos internos', () => {
    const { documents } = runtime();
    assert.deepEqual(plain(documents.identity(documentDetail())), { exact: true, revision: 2, edit_version: 3, expected_status: 'DRAFT', state_fingerprint: 'a'.repeat(64) });
    assert.throws(() => documents.identity(documentDetail({ state_fingerprint: null })));
    assert.throws(() => documents.identity(documentDetail({ revision: '2.5' })));
    assert.equal(documents.identity(documentDetail({ schema_version: 1, state_fingerprint: null })).state_fingerprint, undefined);
});
test('C16: avisos informativos de stock no bloquean emitir; confirmables y desconocidos sí', () => {
    const { documents } = runtime();
    assert.equal(documents.pendingConfirmation([{ code: 'STOCK_CERO', requires_confirmation: false, confirmed: false },
        { code: 'STOCK_INSUFICIENTE', requires_confirmation: false, confirmed: false }]), false);
    assert.equal(documents.pendingConfirmation([{ code: 'STOCK_DESCONOCIDO', requires_confirmation: true, confirmed: false }]), true);
    assert.equal(documents.pendingConfirmation([{ code: 'STOCK_DESCONOCIDO', requires_confirmation: true, confirmed: true }]), false);
    assert.equal(documents.pendingConfirmation([{ code: 'NUEVO', confirmed: false }]), true); // Sin bandera: bloquea.
    assert.equal(documents.pendingConfirmation([]), false);
});
test('P6: URL PDF no elige implícitamente última emitida ni importa parámetros financieros', () => {
    const { documents } = runtime();
    const url = new URL(documents.documentURL(documentDetail({ folio: 'QA/A?' })), 'http://qa.invalid');
    assert.equal(url.pathname, '/cotizaciones/QA%2FA%3F/pdf/');
    assert.equal(url.searchParams.get('revision'), '2');
    assert.equal(url.searchParams.get('exact'), '1');
    assert.equal(url.searchParams.get('edit_version'), '3');
    assert.equal(url.searchParams.get('cost'), null);
});
test('P6: recuperar emisión perdida exige la misma versión y huella', () => {
    const { documents } = runtime();
    const intent = { body: { edit_version: 3, state_fingerprint: 'a'.repeat(64) } };
    assert.equal(documents.emissionOutcome(documentDetail(), intent), 'RETRY');
    assert.equal(documents.emissionOutcome(documentDetail({ status: 'ISSUED' }), intent), 'DONE');
    assert.equal(documents.emissionOutcome(documentDetail({ edit_version: 4 }), intent), 'CONFLICT');
});
test('P6: descarga rechaza HTML/JSON en lugar de presentarlo como PDF', async () => {
    for (const [status, type] of [[200, 'text/html'], [403, 'text/html'], [409, 'application/json']]) {
        const { documents } = runtime(async () => response(status, { error: 'QA conflicto' }, type));
        await assert.rejects(documents.fetchPDF(documentDetail()), error => error.status === status);
    }
});
test('P6: descarga PDF usa credenciales/no-store y conserva el blob original', async () => {
    let call; const blob = new Blob(['%PDF-QA']);
    const { documents } = runtime(async (...args) => { call = args; return { ...response(200, {}, 'application/pdf'), blob: async () => blob }; });
    const pdf = await documents.fetchPDF(documentDetail());
    assert.equal(pdf.blob, blob);
    assert.equal(call[1].credentials, 'same-origin');
    assert.equal(call[1].cache, 'no-store');
    assert.match(pdf.filename, /r2_BORRADOR\.pdf$/);
});
test('P6: correo lleva revisión exacta fuera de /api/v1, sin reintentos automáticos', async () => {
    const calls = [];
    const { api } = runtime(async (...args) => { calls.push(args); return response(200, { success: true }); });
    await api.email('QA/A', { revision: 2, edit_version: 3, email: 'qa@example.invalid' });
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], '/api/cotizaciones/QA%2FA/enviar-correo/');
    assert.equal(JSON.parse(calls[0][1].body).revision, 2);
});
test('P6: resumen/documentos no usan DOM HTML externo, storage ni window.print sobre pantalla interna', () => {
    const source = fs.readFileSync(path.join(root, 'static/js/cotizacion_actions.js'), 'utf8');
    assert.doesNotMatch(source, /\.innerHTML\s*=|parseFloat\(|localStorage\.|window\.print\(/);
});
