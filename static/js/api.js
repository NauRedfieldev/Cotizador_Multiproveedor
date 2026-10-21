/**
 * ==============================================================================
 * CCONOR SOLUCIONES TECNOLÓGICAS - CAPA DE SERVICIOS API & CLIENTE HTTP
 * ==============================================================================
 * 
 * Arquitectura desacoplada:
 * - Centraliza todas las peticiones a endpoints de backend.
 * - Modo Mock (`USE_MOCKS = true`): Retorna contratos simulados idénticos a producción
 *   con latencia de red realista (300-600ms) y persistencia en localStorage.
 * - Modo Producción (`USE_MOCKS = false`): Envía peticiones `fetch()` con cabeceras CSRF
 *   hacia los endpoints REST de Django/Rubí.
 * 
 * CONTRATOS DE DATOS: Ver sección de comentarios sobre cada servicio.
 */

// Interruptor global: Cambiar a `false` al conectar el backend real
const USE_MOCKS = true;

// URL base de la API REST cuando USE_MOCKS = false
const API_BASE_URL = '/api/v1';

// Claves de persistencia para mocks en localStorage
const STORAGE_KEYS = {
    QUOTES: 'cconor_mock_quotes_v1'
};

// ==============================================================================
// NOMBRE VISIBLE DE CADA PROVEEDOR — FUENTE ÚNICA DEL SISTEMA
// ==============================================================================
// Toda etiqueta de proveedor mostrada en búsqueda, comparaciones, partidas del
// cotizador, resumen o PDF se deriva de este mapa (nunca se escribe a mano en
// templates ni en los datasets). Cambiar el nombre aquí lo actualiza en todo
// el sistema y en los datos guardados con nombres anteriores.
const PROVIDER_LABELS = {
    syscom: 'SYSCOM',
    ct: 'Exel del Norte'
};

// Copia del producto con la etiqueta vigente del proveedor (y la de su
// comparación). Se aplica en la salida del servicio, de modo que aunque un
// objeto guarde un nombre de proveedor obsoleto, nunca llegue a la interfaz.
function withCurrentLabels(product) {
    const p = { ...product };
    p.provider_label = PROVIDER_LABELS[p.provider] || p.provider_label;
    return p;
}

// Datos derivados del esquema de fracciones: precio_base (alias numérico de
// precio_mxn, un solo origen de verdad) y precio_fraccion =
// precio_base / factor_conversion (2 decimales). Ej.: bobina 2180 / 305 m = 7.15.
function withDerivedData(product) {
    const p = withCurrentLabels(product);
    p.precio_base = parseFloat(p.precio_mxn);
    if (p.es_divisible && p.factor_conversion > 0) {
        p.precio_fraccion = Math.round((p.precio_base / p.factor_conversion) * 100) / 100;
    }
    return p;
}

// ==============================================================================
// 1. DATASETS INICIALES NORMALIZADOS (CONTRATOS DE DATOS MOCKS)
// ==============================================================================

/**
 * Contrato oficial de búsqueda JSON (Product Schema, M7b):
 * {
 *   provider: string,           // 'syscom' | 'ct'
 *   provider_label: string,     // Derivado de PROVIDER_LABELS (fuente única)
 *   external_id: string,        // Identificador del producto en el proveedor
 *   name: string,
 *   brand: string | null,
 *   model: string | null,
 *   mpn: string | null,
 *   price_original: string,     // Dinero como string: sin pérdida de precisión
 *   currency: string,           // 'MXN' | 'USD'
 *   precio_mxn: string,         // Precio comparable en pesos (string)
 *   es_mejor_precio: boolean,
 *   stock: number | null,       // null = el proveedor no informa existencias
 *   match: {
 *     estado: 'AUTO_MATCH' | 'REVISION_MANUAL' | 'SIN_MATCH',
 *     estado_label: string,
 *     canonico: { descripcion: string } | null,  // Solo en AUTO_MATCH
 *     score: number | null,
 *     motivo: string
 *   },
 *   // Extensión local de venta fraccionable (no es parte del contrato M7):
 *   es_divisible: boolean,
 *   unidad_base: string,          // Solo si es divisible (ej. 'Caja')
 *   unidad_base_etiqueta: string, // Solo si es divisible (ej. 'Bobina / Caja')
 *   factor_conversion: number,    // Solo si es divisible (ej. 305 m)
 *   unidad_fraccion: string,      // Solo si es divisible (ej. 'Metro')
 *   simbolo_fraccion: string,     // Solo si es divisible (ej. 'm')
 *   precio_fraccion: number       // Derivado: precio_base / factor_conversion
 * }
 */
const MOCK_PRODUCTS = [
    {
        provider: 'syscom',
        provider_label: PROVIDER_LABELS.syscom,
        external_id: 'RG-ES224GC-P',
        name: 'Switch Ruijie Reyee 24 puertos Gigabit PoE+ 370W Cloud Managed',
        brand: 'Ruijie',
        model: 'RG-ES224GC-P',
        mpn: null,
        price_original: '8890.0000',
        currency: 'MXN',
        precio_mxn: '8890.0000',
        es_mejor_precio: true,
        stock: 28,
        match: {
            estado: 'AUTO_MATCH',
            estado_label: 'Coincidencia automática',
            canonico: { descripcion: 'Switch Ruijie Reyee 24 puertos Gigabit PoE+ 370W Cloud Managed' },
            score: 100,
            motivo: 'COINCIDENCIA_UNICA_VERIFICADA'
        },
        es_divisible: false
    },
    {
        provider: 'syscom',
        provider_label: PROVIDER_LABELS.syscom,
        external_id: 'PRO-CAT6-EXT',
        name: 'Bobina de Cable UTP Cat6 Exterior CMX 100% Cobre 305m Negro',
        brand: null,
        model: 'PRO-CAT6-EXT',
        mpn: null,
        price_original: '2180.0000',
        currency: 'MXN',
        precio_mxn: '2180.0000',
        es_mejor_precio: true,
        stock: 54,
        match: {
            estado: 'REVISION_MANUAL',
            estado_label: 'Revisión manual',
            canonico: null,
            score: null,
            motivo: 'MARCA_AUSENTE'
        },
        es_divisible: true,
        unidad_base: 'Caja',
        unidad_base_etiqueta: 'Bobina / Caja',
        factor_conversion: 305,
        unidad_fraccion: 'Metro',
        simbolo_fraccion: 'm'
    },
    {
        provider: 'syscom',
        provider_label: PROVIDER_LABELS.syscom,
        external_id: 'DS-2CD2043G2-I',
        name: 'Cámara IP Tubular Hikvision AcuSense 4 Megapíxel Lente 2.8mm PoE',
        brand: 'Hikvision',
        model: 'DS-2CD2043G2-I',
        mpn: null,
        price_original: '1890.0000',
        currency: 'MXN',
        precio_mxn: '1890.0000',
        es_mejor_precio: true,
        stock: 35,
        match: {
            estado: 'AUTO_MATCH',
            estado_label: 'Coincidencia automática',
            canonico: { descripcion: 'Cámara IP Tubular Hikvision AcuSense 4 Megapíxel Lente 2.8mm PoE' },
            score: 100,
            motivo: 'COINCIDENCIA_UNICA_VERIFICADA'
        },
        es_divisible: false
    },
    {
        provider: 'ct',
        provider_label: PROVIDER_LABELS.ct,
        external_id: 'TL-SG3428MP',
        name: 'Switch TP-Link Omada 24 puertos Gigabit PoE+ 384W L2+ Managed',
        brand: 'TP-Link',
        model: 'TL-SG3428MP',
        mpn: null,
        price_original: '9450.0000',
        currency: 'MXN',
        precio_mxn: '9450.0000',
        es_mejor_precio: false,
        stock: 16,
        match: {
            estado: 'AUTO_MATCH',
            estado_label: 'Coincidencia automática',
            canonico: { descripcion: 'Switch TP-Link Omada 24 puertos Gigabit PoE+ 384W L2+ Managed' },
            score: 100,
            motivo: 'COINCIDENCIA_UNICA_VERIFICADA'
        },
        es_divisible: false
    },
    {
        provider: 'ct',
        provider_label: PROVIDER_LABELS.ct,
        external_id: 'DELL-P2422H',
        name: 'Monitor Dell Profesional 23.8" Full HD IPS HDMI/DP/VGA',
        brand: 'Dell',
        model: 'DELL-P2422H',
        mpn: null,
        price_original: '3490.0000',
        currency: 'MXN',
        precio_mxn: '3490.0000',
        es_mejor_precio: true,
        stock: 22,
        match: {
            estado: 'AUTO_MATCH',
            estado_label: 'Coincidencia automática',
            canonico: { descripcion: 'Monitor Dell Profesional 23.8" Full HD IPS HDMI/DP/VGA' },
            score: 100,
            motivo: 'COINCIDENCIA_UNICA_VERIFICADA'
        },
        es_divisible: false
    },
    {
        provider: 'ct',
        provider_label: PROVIDER_LABELS.ct,
        external_id: 'HPE-DL380-G10',
        name: 'Servidor HPE ProLiant DL380 Gen10 Xeon Silver 4208 16GB 8SFF',
        brand: 'HPE',
        model: 'HPE-DL380-G10',
        mpn: null,
        price_original: '52400.0000',
        currency: 'MXN',
        precio_mxn: '52400.0000',
        es_mejor_precio: true,
        stock: 5,
        match: {
            estado: 'AUTO_MATCH',
            estado_label: 'Coincidencia automática',
            canonico: { descripcion: 'Servidor HPE ProLiant DL380 Gen10 Xeon Silver 4208 16GB 8SFF' },
            score: 100,
            motivo: 'COINCIDENCIA_UNICA_VERIFICADA'
        },
        es_divisible: false
    }
];

/**
 * Contrato de Cotización Histórica (Quote Schema):
 * {
 *   folio: string,              // "COT-2026-0048"
 *   client_id: string,
 *   client_name: string,
 *   created_at: string,         // ISO
 *   validity_days: number,
 *   status: 'Aprobada' | 'Pendiente' | 'Enviada' | 'Vencida',
 *   subtotal_base: number,
 *   margin_total: number,
 *   subtotal_client: number,
 *   tax_iva: number,
 *   total: number,
 *   items_count: number,
 *   items: Array<QuoteItem>
 * }
 */
const INITIAL_MOCK_QUOTES = [
    {
        folio: 'COT-2026-0048',
        client_id: 'cli-001',
        client_name: 'Constructora e Inmobiliaria del Norte S.A. de C.V.',
        created_at: '2026-03-28T10:00:00Z',
        validity_days: 30,
        status: 'Pendiente',
        subtotal_base: 54530.00,
        margin_total: 9805.90,
        subtotal_client: 55461.55,
        tax_iva: 8874.35,
        total: 64335.90,
        items_count: 2,
        items: [
            { sku: 'PRO-CAT6-EXT', name: 'Bobina de Cable UTP Cat6 Exterior CMX', provider: 'syscom', price: 2180.00, margin: 22, qty: 4, total: 10638.40 },
            { sku: 'DELL-P2422H', name: 'Monitor Dell Pro 23.8" Full HD IPS', provider: 'ct', price: 3490.00, margin: 15, qty: 5, total: 20067.50 }
        ]
    },
    {
        folio: 'COT-2026-0047',
        client_id: 'cli-002',
        client_name: 'Tecnologías y Enlaces Fronterizos S. de R.L.',
        created_at: '2026-03-22T14:30:00Z',
        validity_days: 15,
        status: 'Aprobada',
        subtotal_base: 35560.00,
        margin_total: 7112.00,
        subtotal_client: 42672.00,
        tax_iva: 6827.52,
        total: 49499.52,
        items_count: 4,
        items: [
            { sku: 'RG-ES224GC-P', name: 'Switch Ruijie Reyee 24 puertos PoE+', provider: 'syscom', price: 8890.00, margin: 20, qty: 4, total: 42672.00 }
        ]
    },
    {
        folio: 'COT-2026-0046',
        client_id: 'cli-003',
        client_name: 'Maquiladora Médica Internacional S.A. de C.V.',
        created_at: '2026-03-10T16:15:00Z',
        validity_days: 30,
        status: 'Enviada',
        subtotal_base: 104800.00,
        margin_total: 12576.00,
        subtotal_client: 117376.00,
        tax_iva: 18780.16,
        total: 136156.16,
        items_count: 2,
        items: [
            { sku: 'HPE-DL380-G10', name: 'Servidor HPE ProLiant DL380 Gen10', provider: 'ct', price: 52400.00, margin: 12, qty: 2, total: 117376.00 }
        ]
    },
    {
        folio: 'COT-2026-0045',
        client_id: 'cli-001',
        client_name: 'Constructora e Inmobiliaria del Norte S.A. de C.V.',
        created_at: '2026-02-15T11:00:00Z',
        validity_days: 15,
        status: 'Vencida',
        subtotal_base: 18900.00,
        margin_total: 4725.00,
        subtotal_client: 23625.00,
        tax_iva: 3780.00,
        total: 27405.00,
        items_count: 10,
        items: [
            { sku: 'DS-2CD2043G2-I', name: 'Cámara IP Tubular Hikvision AcuSense', provider: 'syscom', price: 1890.00, margin: 25, qty: 10, total: 23625.00 }
        ]
    }
];

// Inicializar almacenamiento local si no existe
function initMockStorage() {
    if (!USE_MOCKS || document.getElementById('commercialQuoteApp') || document.getElementById('commercialSummaryApp')) return;
    if (!localStorage.getItem(STORAGE_KEYS.QUOTES)) {
        localStorage.setItem(STORAGE_KEYS.QUOTES, JSON.stringify(INITIAL_MOCK_QUOTES));
    }
}
initMockStorage();

// Simular latencia de red para reflejar estados de carga realistas
function delay(ms = 350) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// Obtener cookie CSRF de Django para llamadas fetch reales
function getCsrfToken() {
    const name = 'csrftoken';
    let cookieValue = null;
    if (document.cookie && document.cookie !== '') {
        const cookies = document.cookie.split(';');
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

// ==============================================================================
// 2. SERVICIOS DE API DESACOPLADOS (PRODUCTS, QUOTES)
// ==============================================================================

const apiClient = {
    /**
     * MÓDULO DE PRODUCTOS (CATÁLOGO & COMPARADOR EN VIVO):
     * Contratos para búsqueda normalizada multi-proveedor (SYSCOM / Exel del Norte).
     */
    products: {
        /**
         * GET /api/v1/products/search/?q=&tipo_cambio=
         * Respuesta M7b: { success, data: [...], proveedores_caidos,
         * proveedores_caidos_nombres, tipo_cambio: { valor, origen, fecha } }.
         * El dinero viaja como string para no perder precisión.
         */
        async search(filters = {}) {
            if (USE_MOCKS) {
                await delay(450); // Simular latencia de búsqueda en catálogo mayorista
                // Copias con la etiqueta vigente del proveedor y datos derivados.
                let list = MOCK_PRODUCTS.map(withDerivedData);

                if (filters.query) {
                    const q = filters.query.toLowerCase().trim();
                    list = list.filter(p =>
                        p.name.toLowerCase().includes(q) ||
                        p.external_id.toLowerCase().includes(q) ||
                        (p.model || '').toLowerCase().includes(q)
                    );
                }

                // El filtro por proveedor es local en la interfaz (atributo
                // provider de cada resultado), no un parámetro de búsqueda.
                // Todos los precios mock son MXN: el tipo de cambio solo se
                // devuelve como contexto de la búsqueda.
                return {
                    success: true,
                    count: list.length,
                    data: list,
                    proveedores_caidos: [],
                    proveedores_caidos_nombres: [],
                    tipo_cambio: filters.tipo_cambio
                        ? {
                            valor: String(filters.tipo_cambio),
                            origen: 'manual',
                            fecha: new Date().toISOString()
                        }
                        : null
                };
            }

            const params = new URLSearchParams(filters);
            const res = await fetch(`${API_BASE_URL}/products/search/?${params.toString()}`);
            if (!res.ok) {
                // En 400/429/503 el backend responde { success: false, error }.
                if (res.status === 400 || res.status === 429 || res.status === 503) {
                    const body = await res.json().catch(() => null);
                    throw new Error(body && body.error ? body.error : `Error HTTP: ${res.status}`);
                }
                throw new Error(`Error HTTP: ${res.status}`);
            }
            const data = await res.json();
            if (Array.isArray(data.data)) data.data = data.data.map(withDerivedData);
            return data;
        }
    },

    /**
     * MÓDULO DE COTIZACIONES (HISTORIAL & PERSISTENCIA):
     * Contratos para guardar, consultar y gestionar cotizaciones B2B.
     */
    quotes: {
        /**
         * GET /api/v1/quotes?search=&status=&date_from=&date_to=
         * Retorna historial de cotizaciones con filtros avanzados.
         */
        async getAll(filters = {}) {
            if (USE_MOCKS) {
                await delay(350);
                const raw = localStorage.getItem(STORAGE_KEYS.QUOTES);
                let list = raw ? JSON.parse(raw) : [...INITIAL_MOCK_QUOTES];

                if (filters.search) {
                    const q = filters.search.toLowerCase().trim();
                    list = list.filter(quote =>
                        quote.folio.toLowerCase().includes(q) ||
                        quote.client_name.toLowerCase().includes(q)
                    );
                }

                if (filters.status && filters.status !== 'todos') {
                    list = list.filter(quote => quote.status.toLowerCase() === filters.status.toLowerCase());
                }

                return { success: true, count: list.length, data: list };
            }

            const params = new URLSearchParams(filters);
            const res = await fetch(`${API_BASE_URL}/quotes/?${params.toString()}`);
            if (!res.ok) throw new Error(`Error HTTP: ${res.status}`);
            return await res.json();
        },

        /**
         * GET /api/v1/quotes/{folio}/
         * Retorna el detalle completo de una cotización para vista previa o exportación PDF.
         */
        async getById(folio) {
            if (USE_MOCKS) {
                await delay(250);
                const raw = localStorage.getItem(STORAGE_KEYS.QUOTES);
                const list = raw ? JSON.parse(raw) : INITIAL_MOCK_QUOTES;
                const quote = list.find(q => q.folio === folio);
                if (!quote) throw new Error(`Cotización ${folio} no encontrada`);
                return { success: true, data: quote };
            }

            const res = await fetch(`${API_BASE_URL}/quotes/${folio}/`);
            if (!res.ok) throw new Error(`Error HTTP: ${res.status}`);
            return await res.json();
        },

        /**
         * POST /api/v1/quotes/
         * Payload M7b exacto del modelo Quote: client_name, client_rfc,
         * client_email, valid_days, notes, tipo_cambio (string) e items con
         * provider/external_id/description/model/quantity/costo_mxn/margen
         * (números como strings). Sin folio, client_id, subtotal ni IVA:
         * el folio lo asigna el servidor (el mock lo genera para su registro).
         */
        async save(quoteData) {
            if (USE_MOCKS) {
                await delay(500);
                const raw = localStorage.getItem(STORAGE_KEYS.QUOTES);
                const list = raw ? JSON.parse(raw) : [...INITIAL_MOCK_QUOTES];

                // Folio del registro interno del mock (NO viene en el payload).
                const folio = `COT-2026-${String(list.length + 49).padStart(4, '0')}`;
                const newQuote = {
                    ...quoteData,
                    folio,
                    created_at: new Date().toISOString(),
                    status: 'Pendiente'
                };

                list.unshift(newQuote);
                localStorage.setItem(STORAGE_KEYS.QUOTES, JSON.stringify(list));
                return { success: true, message: `Cotización ${folio} guardada con éxito`, data: newQuote };
            }

            const res = await fetch(`${API_BASE_URL}/quotes/`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify(quoteData)
            });
            if (!res.ok) {
                // En 400/429/503 el backend responde { success: false, error }.
                if (res.status === 400 || res.status === 429 || res.status === 503) {
                    const body = await res.json().catch(() => null);
                    throw new Error(body && body.error ? body.error : `Error HTTP: ${res.status}`);
                }
                throw new Error(`Error HTTP: ${res.status}`);
            }
            return await res.json();
        },

        /**
         * DELETE /api/v1/quotes/{folio}/
         */
        async delete(folio) {
            if (USE_MOCKS) {
                await delay(300);
                const raw = localStorage.getItem(STORAGE_KEYS.QUOTES);
                let list = raw ? JSON.parse(raw) : [...INITIAL_MOCK_QUOTES];

                list = list.filter(q => q.folio !== folio);
                localStorage.setItem(STORAGE_KEYS.QUOTES, JSON.stringify(list));
                return { success: true, message: `Cotización ${folio} eliminada` };
            }

            const res = await fetch(`${API_BASE_URL}/quotes/${folio}/`, {
                method: 'DELETE',
                headers: {
                    'X-CSRFToken': getCsrfToken()
                }
            });
            if (!res.ok) throw new Error(`Error HTTP: ${res.status}`);
            return await res.json();
        }
    }
};

// P5/P6: flujo comercial siempre conectado. No cambiar USE_MOCKS globalmente:
// los mocks restantes no se usan en una propuesta comercial v2 ni en documentos.
class CommercialHTTPError extends Error {
    constructor(message, status = 0, code = '', uncertain = false) {
        super(message);
        this.name = 'CommercialHTTPError';
        this.status = status;
        this.code = code;
        this.uncertain = uncertain;
    }
}

async function commercialRequest(path, { method = 'GET', data, signal, mutation = false, root = API_BASE_URL } = {}) {
    let response;
    try {
        response = await fetch(`${root}${path}`, {
            method, signal, credentials: 'same-origin', cache: 'no-store',
            headers: { 'Accept': 'application/json', ...(data === undefined ? {} : {
                'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()
            }) },
            ...(data === undefined ? {} : { body: JSON.stringify(data) })
        });
    } catch (error) {
        if (error.name === 'AbortError') throw error;
        throw new CommercialHTTPError('No se recibió respuesta. Comprueba la conexión antes de reintentar.', 0, 'NETWORK', mutation);
    }
    let body = null;
    if ((response.headers.get('Content-Type') || '').includes('application/json')) {
        try { body = await response.json(); } catch (_) { /* No considerar un HTML/JSON roto como éxito. */ }
    }
    if (!response.ok || !body || body.success !== true) {
        const message = body?.error || (response.status === 401 ? 'La sesión venció. Inicia sesión y reintenta.' :
            response.status === 403 ? 'Solicitud no autorizada. Revisa sesión, permisos y protección CSRF.' :
                `Respuesta no válida del servidor (HTTP ${response.status}).`);
        throw new CommercialHTTPError(message, response.status, body?.code || 'INVALID_RESPONSE',
            mutation && (response.status >= 500 || response.ok));
    }
    return body;
}

const revisionPath = (folio, number) => `/quotes/${encodeURIComponent(folio)}/revisiones/${number}/`;
apiClient.commercial = Object.freeze({
    capabilities: () => commercialRequest('/commercial/capabilities/'),
    search: (filters, signal) => commercialRequest(`/products/search/?${new URLSearchParams({ ...filters, schema_version: '2' })}`, { signal }),
    previewNew: (data, signal) => commercialRequest('/quotes/preview/', { method: 'POST', data, signal }),
    create: data => commercialRequest('/quotes/', { method: 'POST', data, mutation: true }),
    revision: (folio, number) => commercialRequest(revisionPath(folio, number)),
    list: (filters, signal) => commercialRequest(`/quotes/?${new URLSearchParams(filters)}`, { signal }),
    clients: (filters, signal) => commercialRequest(`/clients/?${new URLSearchParams(filters)}`, { signal }),
    // 19A: bandeja del comparador (sesión).
    tray: () => commercialRequest('/tray/'),
    trayAdd: key => commercialRequest('/tray/', { method: 'POST', data: { key } }),
    traySet: (key, quantity) => commercialRequest('/tray/', { method: 'PATCH', data: { key, quantity } }),
    trayRemove: keys => commercialRequest('/tray/', { method: 'DELETE', ...(keys ? { data: { keys } } : {}) }),
    summary: (folio, number, signal) => commercialRequest(`${revisionPath(folio, number)}resumen/`, { signal }),
    emit: (folio, number, data) => commercialRequest(`${revisionPath(folio, number)}emitir/`, { method: 'POST', data, mutation: true }),
    clone: (folio, number, data) => commercialRequest(`${revisionPath(folio, number)}nueva/`, { method: 'POST', data, mutation: true }),
    previewTransition: (folio, number, data, signal) => commercialRequest(`${revisionPath(folio, number)}nueva/preview/`, { method: 'POST', data, signal }),
    email: (folio, data) => commercialRequest(`/api/cotizaciones/${encodeURIComponent(folio)}/enviar-correo/`, { root: '', method: 'POST', data, mutation: true }),
    previewEdit: (folio, number, data, signal) => commercialRequest(`${revisionPath(folio, number)}preview/`, { method: 'POST', data, signal }),
    update: (folio, number, data) => commercialRequest(revisionPath(folio, number), { method: 'PUT', data, mutation: true }),
    confirm: (folio, number, data) => commercialRequest(`${revisionPath(folio, number)}confirmaciones/`, { method: 'POST', data, mutation: true }),
    createFX: data => commercialRequest('/quotes/fx-contexts/', { method: 'POST', data, mutation: true })
});

// ==============================================================================
// 3. SISTEMA DE TOASTS Y FEEDBACK VISUAL NO INVASIVO (UI KIT GLOBAL)
// ==============================================================================

/**
 * Muestra una notificación Toast animada en la esquina superior derecha.
 * @param {string} message - Mensaje a mostrar
 * @param {'success'|'danger'|'warning'|'info'} type - Tipo de notificación
 * @param {string} title - Título opcional
 * @param {number} duration - Duración en milisegundos (default: 3500ms)
 * @param {'top'|'bottom'} position - Esquina derecha donde aparece (default: arriba)
 */
function showToast(message, type = 'success', title = '', duration = 3500, position = 'top') {
    const bottom = position === 'bottom';
    const containerId = bottom ? 'cconorToastContainerBottom' : 'cconorToastContainer';
    let container = document.getElementById(containerId);
    if (!container) {
        container = document.createElement('div');
        container.id = containerId;
        container.className = 'cconor-toast-container' + (bottom ? ' cconor-toast-container--bottom' : '');
        document.body.appendChild(container);
    }

    const icons = {
        success: 'bi-check-circle-fill text-success',
        danger: 'bi-exclamation-triangle-fill text-danger',
        warning: 'bi-exclamation-circle-fill text-warning',
        info: 'bi-info-circle-fill text-primary'
    };

    const titles = {
        success: 'Operación Exitosa',
        danger: 'Atención / Error',
        warning: 'Advertencia',
        info: 'Información del Sistema'
    };

    const toastId = 'toast-' + Date.now();
    const toast = document.createElement('div');
    toast.className = `alert alert-${type} shadow-lg cconor-toast d-flex align-items-start gap-3 p-3 mb-2 border-0`;
    toast.id = toastId;
    toast.style.background = '#FFFFFF';
    toast.style.borderLeft = `5px solid var(--bs-${type === 'info' ? 'primary' : type})`;
    toast.style.boxShadow = '0 10px 25px rgba(17, 22, 56, 0.12)';

    const icon = document.createElement('i');
    icon.className = `bi ${icons[type] || icons.info} fs-4 mt-1 flex-shrink-0`;
    const content = document.createElement('div');
    content.className = 'flex-grow-1';
    const heading = document.createElement('div');
    heading.className = 'fw-bold text-dark small mb-0';
    heading.textContent = title || titles[type];
    const text = document.createElement('div');
    text.className = 'small text-secondary mt-1';
    text.textContent = message;
    content.append(heading, text);
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close btn-sm ms-2';
    close.setAttribute('aria-label', 'Cerrar');
    close.addEventListener('click', () => toast.remove());
    toast.append(icon, content, close);

    container.appendChild(toast);

    setTimeout(() => {
        if (toast && toast.parentElement) {
            toast.style.opacity = '0';
            toast.style.transform = 'translateX(20px)';
            toast.style.transition = 'all 0.3s ease';
            setTimeout(() => toast.remove(), 300);
        }
    }, duration);
}

// Exponer globalmente
window.apiClient = apiClient;

// 24C: fechas relativas en el sitio («hace 3 días») y la exacta para mostrar al pasar el cursor
// («20 oct 2026, 16:30»). Acepta ISO con hora o solo día («2026-10-20», día local). El PDF no las usa.
const MESES_CORTOS = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic'];
function leerFecha(valor) {
    // Solo día: «2026-10-20» o «20/10/2026» (día/mes/año, como lo manda el contexto del PDF; nunca mes/día).
    const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(valor ?? ''));
    const mx = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(String(valor ?? ''));
    const dia = iso ? [iso[1], iso[2], iso[3]] : mx ? [mx[3], mx[2], mx[1]] : null;
    const fecha = dia ? new Date(Number(dia[0]), Number(dia[1]) - 1, Number(dia[2])) : mx === null && iso === null && /^\d{1,2}\//.test(String(valor ?? '')) ? new Date(NaN) : new Date(valor);
    return valor === null || valor === undefined || valor === '' || Number.isNaN(fecha.getTime()) ? null : { fecha, soloDia: !!dia };
}
const diasEntre = (a, b) => Math.round((new Date(b.getFullYear(), b.getMonth(), b.getDate()) - new Date(a.getFullYear(), a.getMonth(), a.getDate())) / 86400000);
const plural = (n, uno, varios) => `${n} ${n === 1 ? uno : varios}`;
function haceDias(dias) {
    if (dias < 30) return `hace ${plural(dias, 'día', 'días')}`;
    if (dias < 365) return `hace ${plural(Math.floor(dias / 30), 'mes', 'meses')}`;
    return `hace ${plural(Math.floor(dias / 365), 'año', 'años')}`;
}
window.cconorFechas = Object.freeze({
    hace(valor, ahora = new Date()) {
        const leida = leerFecha(valor);
        if (!leida) return '';
        const dias = diasEntre(leida.fecha, ahora);
        if (leida.soloDia) return dias <= 0 ? 'hoy' : haceDias(dias);
        const minutos = Math.floor((ahora - leida.fecha) / 60000);
        if (minutos < 1) return 'hace un momento';
        if (minutos < 60) return `hace ${minutos} min`;
        return dias <= 0 ? `hace ${plural(Math.floor(minutos / 60), 'hora', 'horas')}` : haceDias(dias);
    },
    vence(valor, ahora = new Date()) {
        const leida = leerFecha(valor);
        if (!leida) return '';
        const dias = diasEntre(ahora, leida.fecha);
        if (dias === 0) return 'vence hoy';
        if (dias === 1) return 'vence mañana';
        return dias > 0 ? `vence en ${dias} días` : `venció ${haceDias(-dias)}`;
    },
    exacta(valor) {
        const leida = leerFecha(valor);
        if (!leida) return '';
        const f = leida.fecha, dia = `${f.getDate()} ${MESES_CORTOS[f.getMonth()]} ${f.getFullYear()}`;
        return leida.soloDia ? dia : `${dia}, ${String(f.getHours()).padStart(2, '0')}:${String(f.getMinutes()).padStart(2, '0')}`;
    },
});
window.showToast = showToast;
