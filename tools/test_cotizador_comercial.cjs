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
        units: { source_label: 'Pieza', sale_label: 'Pieza', conversion_factor: '1', confirmed: true },
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
test('servicio propio: costo $0 sin IVA y precio fijo, aunque antes tuviera margen o costo', () => {
    const { tools } = runtime();
    const service = changes => line({ units: { source_label: 'servicio', sale_label: 'servicio', conversion_factor: '1', confirmed: true }, ...changes });
    for (const changes of [{}, { mode: 'MARGIN' }, { knownCost: true, cost: { amount: '1000', currency: 'USD', vat_basis: 'NET' } }]) {
        const data = tools.item(service(changes));
        assert.deepEqual(plain(data.cost), { amount: '0', currency: 'MXN', vat_basis: 'NET' });
        assert.equal(data.pricing.mode, 'MANUAL');
        assert.equal(data.pricing.sale.amount, '123456789012.123456');
        assert.equal(data.pricing.margin_percent, undefined);
    }
    assert.equal(tools.isService(service({ units: { source_label: 'Servicios', sale_label: 'Servicios', conversion_factor: '1', confirmed: true } })), true);
    // No es servicio: proveedor (API), forma de venta «por partes» u otra unidad.
    assert.equal(tools.isService(service({ kind: 'API' })), false);
    assert.equal(tools.isService(service({ unitMode: 'SPLIT' })), false);
    assert.equal(tools.isService(line()), false);
    assert.equal(tools.item(line()).cost, null);
    // Sin precio sigue faltando el precio, no el costo.
    assert.throws(() => tools.item(service({ sale: { amount: '', currency: 'MXN', vat_basis: 'NET' } })), err => err.field === 'sale');
});
test('unidades del menú: comunes sin importar mayúsculas; las demás se escriben', () => {
    const { tools } = runtime();
    assert.deepEqual([...tools.COMMON_UNITS], ['pieza', 'metro', 'unidad', 'caja', 'rollo', 'juego', 'kit', 'servicio', 'hora']);
    assert.equal(tools.commonUnit(' Metro '), 'metro');
    assert.equal(tools.commonUnit('SERVICIO'), 'servicio');
    for (const other of ['piezas', 'tramo', '', null, undefined]) assert.equal(tools.commonUnit(other), null);
});
test('flechas de cantidad: enteros de 1 a 99,999,999; lo inválido cuenta como 0', () => {
    const { tools } = runtime();
    assert.equal(tools.stepQuantity('5', 1), '6');
    assert.equal(tools.stepQuantity('5', -1), '4');
    assert.equal(tools.stepQuantity('1', -1), '1');
    assert.equal(tools.stepQuantity('99999999', 1), '99999999');
    for (const bad of ['', 'abc', '1.5', '-3', null]) {
        assert.equal(tools.stepQuantity(bad, 1), '1');
        assert.equal(tools.stepQuantity(bad, -1), '1');
    }
});
test('7A reordenar: posición al soltar y mover sin tocar la lista original', () => {
    const { tools } = runtime();
    const mids = [100, 200, 300, 400];                  // centro de 4 filas
    assert.equal(tools.dropTarget(mids, 50, 3), 0);     // la 4.ª arriba de todo → posición 1
    assert.equal(tools.dropTarget(mids, 150, 3), 1);    // entre la 1.ª y la 2.ª
    assert.equal(tools.dropTarget(mids, 450, 0), 3);    // la 1.ª al final
    assert.equal(tools.dropTarget(mids, 250, 1), 1);    // cerca de su lugar: no se mueve
    assert.equal(tools.dropTarget(mids, 180, 1), 1);
    assert.equal(tools.dropTarget(mids, 350, 1), 2);    // la 2.ª pasa después de la 3.ª
    const list = ['a', 'b', 'c', 'd'];
    assert.deepEqual([...tools.moveItem(list, 3, 1)], ['a', 'd', 'b', 'c']);
    assert.deepEqual([...tools.moveItem(list, 0, 3)], ['b', 'c', 'd', 'a']);
    assert.deepEqual([...tools.moveItem(list, 2, 2)], list);
    assert.deepEqual(list, ['a', 'b', 'c', 'd']);
});
test('bandeja: lo traído sigue en la bandeja hasta guardar y no se ofrece dos veces', () => {
    const { tools } = runtime();
    const items = [{ key: 'syscom:1' }, { key: 'syscom:2' }, { key: 'syscom:3' }];
    const lines = [{ trayKey: 'syscom:2' }, { trayKey: undefined }, {}];
    assert.deepEqual(tools.trayPending(items, lines).map(i => i.key), ['syscom:1', 'syscom:3']);
    assert.deepEqual(tools.trayPending(items, []).map(i => i.key), ['syscom:1', 'syscom:2', 'syscom:3']);
    // «Traer» ya no quita de la bandeja; solo el guardado confirmado (settleTray) lo hace.
    const source = fs.readFileSync(path.join(root, 'static/js/cotizador.js'), 'utf8');
    const bring = source.slice(source.indexOf('async function bringTray()'), source.indexOf("$('trayBringBtn').addEventListener"));
    assert.doesNotMatch(bring, /trayRemove/);
    const save = source.slice(source.indexOf('async function save()'), source.indexOf('async function confirmWarning('));
    const written = save.indexOf('state.pending = null;\n');
    assert.ok(written > 0 && save.indexOf('settleTray(intent.trayKeys)') > written);
});
test('estado de la partida: falta algo, editando o lista', () => {
    const { tools } = runtime();
    assert.deepEqual(plain(tools.lineStatus(line({ expanded: false }))), { kind: 'ready', label: 'Lista' });
    assert.deepEqual(plain(tools.lineStatus(line({ expanded: true }))), { kind: 'editing', label: 'Editando' });
    assert.deepEqual(plain(tools.lineStatus(line({ sale: { amount: '', currency: 'MXN', vat_basis: 'NET' } }))), { kind: 'missing', label: 'Falta el precio' });
    assert.deepEqual(plain(tools.lineStatus(line({ name: ' ' }))), { kind: 'missing', label: 'Falta el nombre' });
    assert.deepEqual(plain(tools.lineStatus(line({ units: { source_label: 'Pieza', sale_label: 'Pieza', conversion_factor: '1', confirmed: false } }))),
        { kind: 'missing', label: 'Falta confirmar la unidad' });
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
test('concepto editado viaja solo en partidas API, recortado', () => {
    const { tools } = runtime();
    const api = line({ kind: 'API', mode: 'MARGIN', offer: { context_ref: 'captura', offer_ref: 'oferta' } });
    assert.equal(tools.item(api).edited_name, '');
    assert.equal(tools.item({ ...api, editedName: '  Mi concepto ' }).edited_name, 'Mi concepto');
    assert.equal(tools.item({ ...api, persisted: true, editedName: 'Guardado' }).edited_name, 'Guardado');
    assert.equal(tools.item(line({ editedName: 'Ignorado' })).edited_name, undefined);
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
test('M2: condiciones predeterminadas como texto inicial, una por línea', () => {
    const { tools } = runtime();
    assert.equal(tools.defaultConditionsText('["Primera", "  Segunda  "]'), 'Primera\nSegunda');
    assert.equal(tools.defaultConditionsText('[]'), '');
    for (const bad of [undefined, '', 'no json', '{"a":1}', 'null']) assert.equal(tools.defaultConditionsText(bad), '');
    assert.equal(tools.defaultConditionsText('["ok", 3, "", "dos\\nlíneas", null]'), 'ok');
    assert.equal(tools.defaultConditionsText(JSON.stringify(Array.from({ length: 60 }, (_, i) => `C${i}`))).split('\n').length, 50);
});
test('M2: nueva cotización y carga inicial usan las predeterminadas, no un cuadro vacío', () => {
    const source = fs.readFileSync(path.join(root, 'static/js/cotizador.js'), 'utf8');
    const reset = source.slice(source.indexOf("$('newQuoteBtn').addEventListener"), source.indexOf("window.addEventListener('beforeunload'"));
    assert.match(reset, /\$\('quoteConditions'\)\.value = defaultConditions;/);
    assert.doesNotMatch(reset, /'quoteConditions', 'newExchangeRate'\]\) \$\(id\)\.value = ''/);
    const start = source.slice(source.indexOf('async function initialize()'));
    const preload = start.search(/^\s*\$\('quoteConditions'\)\.value = defaultConditions;/m);
    assert.ok(preload > 0 && preload < start.search(/^\s*fxDisplay\(\); locks\(\); initialize\(\);/m));
});
test('búsqueda: la pista de tipo de cambio solo acompaña a ese error y depende del permiso', () => {
    const { tools } = runtime();
    const fx = 'Captura el tipo de cambio (pesos por dólar) para comparar los precios en USD.';
    assert.match(tools.fxSearchHint(fx, true), /paso 2/);
    assert.equal(tools.fxSearchHint(fx, false), '');   // lienzo fila 13: sin permiso, el de SYSCOM es automático
    for (const other of ['Inicia sesión para continuar.', 'No fue posible consultar a los proveedores.', '', undefined]) {
        assert.equal(tools.fxSearchHint(other, true), '');
        assert.equal(tools.fxSearchHint(other, false), '');
    }
    const source = fs.readFileSync(path.join(root, 'static/js/cotizador.js'), 'utf8');
    assert.match(source, /text\('searchStatus', err\.message \+ fxSearchHint\(err\.message, has\('change_commercial_fx'\)\)\)/);
});
test('IVA en porcentaje en pantalla y fracción exacta hacia el servidor, sin float', () => {
    const { tools } = runtime();
    for (const [pct, fraction] of [['16', '0.1600'], ['8.5', '0.0850'], ['0', '0.0000'], ['100', '1.0000'], ['16.00', '0.1600'], ['0.01', '0.0001']]) {
        assert.equal(tools.percentToFraction(pct), fraction);
    }
    for (const [fraction, pct] of [['0.1600', '16'], ['0.085', '8.5'], ['0.16', '16'], ['1.0000', '100'], ['0', '0'], ['0.0001', '0.01']]) {
        assert.equal(tools.fractionToPercent(fraction), pct);
    }
    for (const bad of ['', '16%', '-1', '100.01', '16.555', '1e1', '16,5']) assert.throws(() => tools.percentToFraction(bad));
    assert.throws(() => tools.fractionToPercent('1.0001'));
});
test('redondeo solo de presentación, HALF_UP sobre texto e indica si es aproximado', () => {
    const { tools } = runtime();
    assert.deepEqual(plain(tools.roundText('8.343', 2)), { text: '8.34', exact: false });
    assert.deepEqual(plain(tools.roundText('12.835', 2)), { text: '12.84', exact: false });
    assert.deepEqual(plain(tools.roundText('99.995', 2)), { text: '100.00', exact: false });
    assert.deepEqual(plain(tools.roundText('185.4', 2)), { text: '185.40', exact: true });
    assert.deepEqual(plain(tools.roundText('200.000000', 2)), { text: '200.00', exact: true });
    assert.deepEqual(plain(tools.roundText('-20.005', 2)), { text: '-20.01', exact: false });
    assert.equal(tools.roundText('no', 2), null);
});
test('descuento: «-» solo cuando resta algo; en cero sin signo, como el PDF', () => {
    const { tools } = runtime();
    assert.equal(tools.discountText('0.00'), '$0.00 MXN');
    assert.equal(tools.discountText('0'), '$0.00 MXN');
    assert.equal(tools.discountText('10.00'), '-$10.00 MXN');
    assert.equal(tools.discountText('0.01'), '-$0.01 MXN');
    assert.equal(tools.discountText('1500.5'), '-$1,500.50 MXN');
});
test('venta por partes: otra unidad o factor distinto de 1', () => {
    const { tools } = runtime();
    assert.equal(tools.splitUnits({ source_label: 'Pieza', sale_label: 'Pieza', conversion_factor: '1.000000' }), false);
    assert.equal(tools.splitUnits({ source_label: '', sale_label: '', conversion_factor: '1' }), false);
    assert.equal(tools.splitUnits({ source_label: 'Bobina', sale_label: 'Metro', conversion_factor: '100' }), true);
    assert.equal(tools.splitUnits({ source_label: 'Caja', sale_label: 'Caja', conversion_factor: '12' }), true);
    assert.equal(tools.splitUnits({ source_label: 'Rollo', sale_label: 'Metro', conversion_factor: '1' }), true);
    assert.equal(tools.trimZeros('100.000000'), '100');
    assert.equal(tools.trimZeros('0.500000'), '0.5');
    assert.equal(tools.trimZeros('305'), '305');
    assert.equal(tools.trimZeros('abc'), 'abc');
});
test('cada error de partida indica su campo para «Ir al campo»', () => {
    const { tools } = runtime();
    const field = changes => { try { tools.item(line(changes)); } catch (err) { return err.field; } return null; };
    assert.equal(field({ units: { source_label: '', sale_label: 'Pieza', conversion_factor: '1', confirmed: true } }), 'units');
    assert.equal(field({ units: { source_label: 'Bobina', sale_label: 'Metro', conversion_factor: '0', confirmed: true } }), 'factor');
    assert.equal(field({ quantity: '1.5' }), 'quantity');
    assert.equal(field({ units: { source_label: 'Pieza', sale_label: 'Pieza', conversion_factor: '1', confirmed: false } }), 'confirm');
    assert.equal(field({ name: '  ' }), 'name');
    assert.equal(field({ mode: 'MARGIN' }), 'mode');
    assert.equal(field({ sale: { amount: '', currency: 'MXN', vat_basis: 'NET' } }), 'sale');
    assert.equal(field({ knownCost: true, cost: { amount: '10', currency: 'MXN', vat_basis: '' } }), 'cost');
    assert.equal(field({ mode: 'MARGIN', knownCost: true, cost: { amount: '10', currency: 'MXN', vat_basis: 'NET' }, margin: '100' }), 'margin');
    assert.equal(field({}), null);
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

// Inicio (panel 7D): static/js/inicio.js con un DOM mínimo, sin red.
function fakeNode(tag) {
    return { tag, className: '', textContent: '', href: '', children: [], dataset: {}, classList: { remove() {}, add() {} },
        append(...nodes) { this.children.push(...nodes); }, replaceChildren() { this.children = []; } };
}
function homeContext(panel = null, fetch = async () => { throw Error('No red'); }) {
    const nodes = new Map();
    const context = vm.createContext({ window: {}, console, URLSearchParams, fetch,
        // api.js siembra sus mocks históricos en las páginas que no son cotizador/resumen; el panel no los lee.
        localStorage: { getItem: () => null, setItem() {} },
        document: { cookie: 'csrftoken=synthetic-csrf', createElement: fakeNode,
            getElementById: id => id === 'homePanel' ? panel : panel ? (nodes.get(id) || nodes.set(id, fakeNode('div')).get(id)) : null } });
    for (const name of ['api.js', 'inicio.js']) vm.runInContext(fs.readFileSync(path.join(root, 'static/js', name), 'utf8'), context);
    return { tools: context.window.CCONORHomeTools, nodes, fechas: context.window.cconorFechas };
}
function anchorsOf(node, found = []) {
    if (node.tag === 'a') found.push(node);
    for (const child of node.children || []) if (typeof child === 'object') anchorsOf(child, found);
    return found;
}
test('24C: fechas relativas en pantalla y la exacta para el cursor', () => {
    const { fechas } = homeContext();
    const now = new Date(2026, 9, 20, 19, 30);
    const at = (...a) => new Date(...a).toISOString();
    assert.equal(fechas.hace(at(2026, 9, 20, 19, 29, 40), now), 'hace un momento');
    assert.equal(fechas.hace(at(2026, 9, 20, 19, 5), now), 'hace 25 min');
    assert.equal(fechas.hace(at(2026, 9, 20, 18, 0), now), 'hace 1 hora');
    assert.equal(fechas.hace(at(2026, 9, 20, 16, 30), now), 'hace 3 horas');
    assert.equal(fechas.hace(at(2026, 9, 19, 23, 0), now), 'hace 1 día');          // otro día aunque sean 20 h
    assert.equal(fechas.hace('2026-10-20', now), 'hoy');
    assert.equal(fechas.hace('2026-10-17', now), 'hace 3 días');
    assert.equal(fechas.hace('2026-07-01', now), 'hace 3 meses');
    assert.equal(fechas.hace('2024-10-01', now), 'hace 2 años');
    assert.equal(fechas.vence('2026-11-04', now), 'vence en 15 días');
    assert.equal(fechas.vence('2026-10-20', now), 'vence hoy');
    assert.equal(fechas.vence('2026-10-21', now), 'vence mañana');
    assert.equal(fechas.vence('2026-10-18', now), 'venció hace 2 días');
    assert.equal(fechas.exacta('2026-10-20'), '20 oct 2026');
    // El contexto del PDF manda día/mes/año: 04/11/2026 es 4 de noviembre, nunca 11 de abril.
    assert.equal(fechas.vence('04/11/2026', now), 'vence en 15 días');
    assert.equal(fechas.exacta('04/11/2026'), '4 nov 2026');
    assert.equal(fechas.hace('20/10/2026', now), 'hoy');
    assert.equal(fechas.hace('4/11/2026', now), '');
    assert.equal(fechas.exacta(at(2026, 9, 20, 16, 5)), '20 oct 2026, 16:05');
    for (const malo of ['mal', '', null, undefined]) assert.equal(fechas.hace(malo, now), '');
});
test('inicio: importes, fechas y conteo del mes sin float', () => {
    const { tools } = homeContext();
    assert.equal(tools.money('48230.5'), '$48,230.50');
    assert.equal(tools.money('1234567.891'), '$1,234,567.891');
    assert.equal(tools.money(null), 'Sin total sellado');
    assert.equal(tools.money('1e2'), '—');
    // 24C: fecha relativa; la exacta va en el title.
    assert.equal(tools.shortDate('2026-10-05', new Date(2026, 9, 20)), 'hace 15 días');
    assert.equal(tools.shortDate('2026-10-20', new Date(2026, 9, 20)), 'hoy');
    assert.equal(tools.shortDate('mal'), '');
    const now = new Date(2026, 9, 20);
    assert.equal(tools.monthLabel(now), 'Octubre 2026');
    const rows = [{ issue_date: '2026-10-18' }, { issue_date: '2026-09-30' }, { issue_date: '2026-10-02' }, { issue_date: '2025-10-18' }];
    assert.equal(tools.monthCount(rows, 1, now), '2');
    assert.equal(tools.monthCount([{ issue_date: '2026-10-18' }], 2, now), '1+');
    assert.equal(tools.monthCount(rows, 2, now), '2');
    assert.equal(tools.items(1), '1 partida');
    assert.equal(tools.items(5), '5 partidas');
});
test('inicio: enlaces a borrador, resumen y PDF con la revisión exacta', () => {
    const { tools } = homeContext();
    assert.equal(tools.revisionQuery({ folio: 'COT-2026-0007', revision: 2 }), 'folio=COT-2026-0007&revision=2');
    assert.equal(tools.pdfHref({ folio: 'COT-2026-0007', revision: 2 }), '/cotizaciones/COT-2026-0007/pdf/?revision=2');
    assert.equal(tools.pdfHref({ folio: 'A/B', revision: 1 }), '/cotizaciones/A%2FB/pdf/?revision=1');
    const urls = { cotizador: '/cotizador/', resumen: '/resumen/' };
    const draft = { folio: 'COT-2026-0007', revision: 2, client_name: 'Cliente', items_count: 5, issue_date: '2026-10-20', schema_version: 2 };
    assert.deepEqual(anchorsOf(tools.draftCard(draft, urls)).map(a => a.href), ['/cotizador/?folio=COT-2026-0007&revision=2']);
    const issued = { ...draft, total: '48230.50' };
    assert.deepEqual(anchorsOf(tools.issuedCard(issued, urls)).map(a => a.href),
        ['/resumen/?folio=COT-2026-0007&revision=2', '/cotizaciones/COT-2026-0007/pdf/?revision=2']);
    // Un histórico v1 no ofrece PDF desde el panel: solo el resumen, que advierte sobre costos.
    assert.deepEqual(anchorsOf(tools.issuedCard({ ...issued, schema_version: 1 }, urls)).map(a => a.href),
        ['/resumen/?folio=COT-2026-0007&revision=2']);
});
test('inicio: el panel consulta borradores y emitidas y llena tarjetas y conteos', async () => {
    const panel = fakeNode('section'); panel.dataset = { cotizadorUrl: '/cotizador/', resumenUrl: '/resumen/' };
    const calls = [];
    const row = (n, status, total) => ({ folio: `COT-2026-000${n}`, revision: 1, status, total, schema_version: 2, client_name: `Cliente ${n}`, issue_date: '2026-10-18', items_count: n });
    const fetch = async url => {
        calls.push(url);
        const issued = url.includes('status=ISSUED');
        const data = issued ? [row(1, 'ISSUED', '100.00'), row(2, 'ISSUED', '200.00'), row(3, 'ISSUED', '300.00'), row(4, 'ISSUED', '400.00')] : [row(5, 'DRAFT', null)];
        return response(200, { success: true, actor_id: 7, data, pagination: { page: 1, pages: 1, count: data.length } });
    };
    const { nodes } = homeContext(panel, fetch);
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.deepEqual(calls.sort(), ['/api/v1/quotes/?status=DRAFT&page=1', '/api/v1/quotes/?status=ISSUED&page=1']);
    assert.equal(nodes.get('kpiDrafts').textContent, '1');
    assert.equal(nodes.get('kpiIssued').textContent, '4');
    assert.equal(nodes.get('draftsList').children.length, 1);
    assert.equal(nodes.get('issuedList').children.length, 3);          // solo las 3 más recientes
    assert.equal(nodes.get('panelMoreLink').textContent, 'Ver las 5 en Mis cotizaciones →');
    assert.match(nodes.get('panelMoreText').textContent, /3 más recientes/);
});
test('inicio: sin permiso o con error muestra el aviso y deja los conteos vacíos', async () => {
    const panel = fakeNode('section'); panel.dataset = { cotizadorUrl: '/cotizador/', resumenUrl: '/resumen/' };
    const fetch = async () => response(403, { success: false, error: 'Tu cuenta no tiene acceso comercial.' });
    const { nodes } = homeContext(panel, fetch);
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(nodes.get('panelStatus').textContent, 'Tu cuenta no tiene acceso comercial.');
    assert.equal(nodes.get('kpiDrafts')?.textContent ?? '', '');
    assert.equal(nodes.get('draftsList').children.length, 1);         // texto de lista vacía, nunca datos inventados
});
