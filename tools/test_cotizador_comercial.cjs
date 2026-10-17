// P5: node --test tools/test_cotizador_comercial.cjs (sin paquetes/build/red).
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
    for (const name of ['api.js', 'cotizador.js']) vm.runInContext(fs.readFileSync(path.join(root, 'static/js', name), 'utf8'), context);
    return { tools: context.window.CCONORQuoteTools, api: context.window.apiClient.commercial };
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
