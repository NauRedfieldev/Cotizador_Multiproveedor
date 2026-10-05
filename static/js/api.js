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
    CLIENTS: 'cconor_mock_clients_v1',
    QUOTES: 'cconor_mock_quotes_v1'
};

// ==============================================================================
// 1. DATASETS INICIALES NORMALIZADOS (CONTRATOS DE DATOS MOCKS)
// ==============================================================================

/**
 * Contrato de Cliente (Client Schema):
 * {
 *   id: string,                 // Identificador único (ej: "cli-001")
 *   business_name: string,      // Razón social completa
 *   rfc: string,                // RFC mexicano (12 o 13 caracteres)
 *   contact_name: string,       // Nombre de la persona de contacto
 *   email: string,              // Correo electrónico corporativo
 *   phone: string,              // Teléfono con lada
 *   type: string,               // 'Corporativo' | 'Integrador TI' | 'Maquiladora' | 'Distribuidor'
 *   city: string,               // Ciudad / Estado
 *   address: string,            // Domicilio fiscal
 *   status: string,             // 'Activo' | 'Inactivo'
 *   created_at: string          // Fecha ISO
 * }
 */
const INITIAL_MOCK_CLIENTS = [
    {
        id: 'cli-001',
        business_name: 'Constructora e Inmobiliaria del Norte S.A. de C.V.',
        rfc: 'CIN980415AA2',
        contact_name: 'Ing. Carlos Mendoza',
        email: 'compras@cinorte.mx',
        phone: '(664) 625-1100',
        type: 'Corporativo',
        city: 'Tijuana, B.C.',
        address: 'Blvd. Agua Caliente 10450, Col. Aviación',
        status: 'Activo',
        created_at: '2026-01-15T09:30:00Z'
    },
    {
        id: 'cli-002',
        business_name: 'Tecnologías y Enlaces Fronterizos S. de R.L.',
        rfc: 'TEF051120K81',
        contact_name: 'Lic. Mariana Vega',
        email: 'contacto@tefrontera.com',
        phone: '(664) 680-4490',
        type: 'Integrador TI',
        city: 'Tijuana, B.C.',
        address: 'Vía Rápida Poniente 4500, Zona Río',
        status: 'Activo',
        created_at: '2026-02-01T11:20:00Z'
    },
    {
        id: 'cli-003',
        business_name: 'Maquiladora Médica Internacional S.A. de C.V.',
        rfc: 'MMI120308PX5',
        contact_name: 'Ing. Roberto Garza',
        email: 'adquisiciones@medint.com.mx',
        phone: '(664) 647-8900',
        type: 'Maquiladora',
        city: 'Tijuana, B.C.',
        address: 'Parque Industrial Otay, Calle 4 Norte #220',
        status: 'Activo',
        created_at: '2026-02-18T14:15:00Z'
    },
    {
        id: 'cli-004',
        business_name: 'Distribuidora Baja Telecomunicaciones S.A.',
        rfc: 'DBT1607149V3',
        contact_name: 'Ing. Laura Salgado',
        email: 'proyectos@bajatelecom.mx',
        phone: '(686) 555-1289',
        type: 'Distribuidor',
        city: 'Mexicali, B.C.',
        address: 'Calz. Justo Sierra 1205, Fracc. Los Pinos',
        status: 'Inactivo',
        created_at: '2026-03-05T16:45:00Z'
    }
];

/**
 * Contrato de Producto Normalizado Multi-Proveedor (Product Schema):
 * {
 *   id: string,
 *   sku: string,
 *   clave: string,
 *   name: string,
 *   category: string,
 *   description: string,
 *   provider: 'syscom' | 'ct',
 *   provider_label: 'SYSCOM' | 'CT Online',
 *   price_wholesale_mxn: number,
 *   stock: number,
 *   suggested_margin: number,
 *   warranty_months: number,
 *   specs: Array<string>,
 *   // Objeto comparativo en vivo frente al competidor
 *   comparison: {
 *     alternative_provider: 'syscom' | 'ct',
 *     alternative_label: string,
 *     alternative_sku: string,
 *     alternative_price: number,
 *     alternative_stock: number,
 *     price_difference_mxn: number, // Positivo si el principal es más caro, negativo si es más barato
 *     is_best_price: boolean
 *   }
 * }
 */
const MOCK_PRODUCTS = [
    {
        id: 'prod-001',
        sku: 'RG-ES224GC-P',
        clave: 'SW-RUIJIE24',
        name: 'Switch Ruijie Reyee 24 puertos Gigabit PoE+ 370W Cloud Managed',
        category: 'switches',
        description: 'Switch gestionado en la nube de 24 puertos Gigabit 802.3at/af con presupuesto PoE de 370W. Monitoreo remoto mediante Ruijie Cloud App.',
        provider: 'syscom',
        provider_label: 'SYSCOM',
        price_wholesale_mxn: 8890.00,
        stock: 28,
        suggested_margin: 25,
        warranty_months: 36,
        specs: ['24 Puertos 10/100/1000 Mbps', 'PoE+ 370W Total', 'Cloud Managed Gratuito', '2 Puertos SFP Combo'],
        comparison: {
            alternative_provider: 'ct',
            alternative_label: 'CT Online',
            alternative_sku: 'TL-SG3428MP',
            alternative_price: 9450.00,
            alternative_stock: 16,
            price_difference_mxn: -560.00,
            is_best_price: true
        }
    },
    {
        id: 'prod-002',
        sku: 'PRO-CAT6-EXT',
        clave: 'CAB-UTP305',
        name: 'Bobina de Cable UTP Cat6 Exterior CMX 100% Cobre 305m Negro',
        category: 'cableado',
        description: 'Bobina de 305 metros Cat6 UTP para intemperie con doble cubierta anti-UV, conductores de cobre sólido electrolítico 23 AWG.',
        provider: 'syscom',
        provider_label: 'SYSCOM',
        price_wholesale_mxn: 2180.00,
        stock: 54,
        suggested_margin: 22,
        warranty_months: 120,
        specs: ['100% Cobre Electrolítico', 'Calibre 23 AWG', 'Protección Rayos UV CMX', 'Certificación ANSI/TIA 568-C.2'],
        comparison: {
            alternative_provider: 'ct',
            alternative_label: 'CT Online',
            alternative_sku: 'CT-CAB6-BLK',
            alternative_price: 2310.00,
            alternative_stock: 32,
            price_difference_mxn: -130.00,
            is_best_price: true
        }
    },
    {
        id: 'prod-003',
        sku: 'DS-2CD2043G2-I',
        clave: 'CAM-IP4MP',
        name: 'Cámara IP Tubular Hikvision AcuSense 4 Megapíxel Lente 2.8mm PoE',
        category: 'camaras',
        description: 'Cámara tipo bala para videovigilancia exterior con analítica de inteligencia artificial AcuSense (clasificación humanos/vehículos), WDR 120dB e infrarrojo EXIR 30m.',
        provider: 'syscom',
        provider_label: 'SYSCOM',
        price_wholesale_mxn: 1890.00,
        stock: 35,
        suggested_margin: 25,
        warranty_months: 24,
        specs: ['Resolución 4 MP (2688x1520)', 'Lente fijo 2.8mm (103°)', 'Filtro AcuSense IA', 'Protección IP67'],
        comparison: {
            alternative_provider: 'ct',
            alternative_label: 'CT Online',
            alternative_sku: 'DAH-IPC-HFW',
            alternative_price: 1980.00,
            alternative_stock: 19,
            price_difference_mxn: -90.00,
            is_best_price: true
        }
    },
    {
        id: 'prod-004',
        sku: 'TL-SG3428MP',
        clave: 'SW-OMADA24',
        name: 'Switch TP-Link Omada 24 puertos Gigabit PoE+ 384W L2+ Managed',
        category: 'switches',
        description: 'Switch corporativo gestionable L2+ con 24 puertos PoE+ 802.3at/af y 4 ranuras SFP gigabit. Integración nativa con controlador Omada SDN.',
        provider: 'ct',
        provider_label: 'CT Online',
        price_wholesale_mxn: 9450.00,
        stock: 16,
        suggested_margin: 20,
        warranty_months: 60,
        specs: ['24 Puertos Gigabit PoE+', 'Presupuesto PoE 384W', '4 Ranuras SFP', 'Gestión Omada SDN'],
        comparison: {
            alternative_provider: 'syscom',
            alternative_label: 'SYSCOM',
            alternative_sku: 'RG-ES224GC-P',
            alternative_price: 8890.00,
            alternative_stock: 28,
            price_difference_mxn: 560.00,
            is_best_price: false
        }
    },
    {
        id: 'prod-005',
        sku: 'DELL-P2422H',
        clave: 'MON-24IPS',
        name: 'Monitor Dell Profesional 23.8" Full HD IPS HDMI/DP/VGA',
        category: 'computo',
        description: 'Monitor corporativo con tecnología ComfortView Plus, panel IPS antireflejo de bisel ultrafino, puertos USB 3.2 y base ergonómica regulable en altura y pivote.',
        provider: 'ct',
        provider_label: 'CT Online',
        price_wholesale_mxn: 3490.00,
        stock: 22,
        suggested_margin: 15,
        warranty_months: 36,
        specs: ['Pantalla 23.8" FHD 1920x1080', 'Panel IPS 99% sRGB', 'Base regulable 4 ejes', 'HDMI, DP, VGA y Hub USB'],
        comparison: {
            alternative_provider: 'syscom',
            alternative_label: 'SYSCOM',
            alternative_sku: 'SYS-MON24-PRO',
            alternative_price: 3650.00,
            alternative_stock: 8,
            price_difference_mxn: -160.00,
            is_best_price: true
        }
    },
    {
        id: 'prod-006',
        sku: 'HPE-DL380-G10',
        clave: 'SRV-DL380',
        name: 'Servidor HPE ProLiant DL380 Gen10 Xeon Silver 4208 16GB 8SFF',
        category: 'servidores',
        description: 'Servidor en rack 2U de alto rendimiento con procesador Intel Xeon Silver 4208 (8 núcleos), 16GB RAM DDR4 SmartMemory, controladora RAID Smart Array P408i-a y fuentes redundantes.',
        provider: 'ct',
        provider_label: 'CT Online',
        price_wholesale_mxn: 52400.00,
        stock: 5,
        suggested_margin: 12,
        warranty_months: 36,
        specs: ['Intel Xeon Silver 4208', '16GB RAM DDR4 ECC', 'Chasis 8 bahías SFF 2.5"', 'Fuentes Platinum 500W redundantes'],
        comparison: {
            alternative_provider: 'syscom',
            alternative_label: 'SYSCOM',
            alternative_sku: 'SYS-SRV-RACK2U',
            alternative_price: 54100.00,
            alternative_stock: 2,
            price_difference_mxn: -1700.00,
            is_best_price: true
        }
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
    if (!localStorage.getItem(STORAGE_KEYS.CLIENTS)) {
        localStorage.setItem(STORAGE_KEYS.CLIENTS, JSON.stringify(INITIAL_MOCK_CLIENTS));
    }
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
// 2. SERVICIOS DE API DESACOPLADOS (CLIENTS, PRODUCTS, QUOTES)
// ==============================================================================

const apiClient = {
    /**
     * MÓDULO DE CLIENTES (CRUD):
     * Contratos y operaciones REST para gestión de empresas y clientes B2B.
     */
    clients: {
        /**
         * GET /api/v1/clients?search=&status=&type=
         * Retorna lista de clientes filtrada.
         * Payload de Respuesta: Array<Client>
         */
        async getAll(filters = {}) {
            if (USE_MOCKS) {
                await delay(300);
                const raw = localStorage.getItem(STORAGE_KEYS.CLIENTS);
                let list = raw ? JSON.parse(raw) : [...INITIAL_MOCK_CLIENTS];

                if (filters.search) {
                    const q = filters.search.toLowerCase().trim();
                    list = list.filter(c => 
                        c.business_name.toLowerCase().includes(q) ||
                        c.rfc.toLowerCase().includes(q) ||
                        c.contact_name.toLowerCase().includes(q) ||
                        c.email.toLowerCase().includes(q)
                    );
                }
                if (filters.status && filters.status !== 'todos') {
                    list = list.filter(c => c.status.toLowerCase() === filters.status.toLowerCase());
                }
                if (filters.type && filters.type !== 'todos') {
                    list = list.filter(c => c.type.toLowerCase() === filters.type.toLowerCase());
                }
                return { success: true, count: list.length, data: list };
            }

            // Llamada real al backend
            const params = new URLSearchParams(filters);
            const res = await fetch(`${API_BASE_URL}/clients/?${params.toString()}`);
            if (!res.ok) throw new Error(`Error HTTP: ${res.status}`);
            return await res.json();
        },

        /**
         * GET /api/v1/clients/{id}/
         * Retorna el detalle de un cliente específico.
         */
        async getById(id) {
            if (USE_MOCKS) {
                await delay(200);
                const raw = localStorage.getItem(STORAGE_KEYS.CLIENTS);
                const list = raw ? JSON.parse(raw) : INITIAL_MOCK_CLIENTS;
                const client = list.find(c => c.id === id);
                if (!client) throw new Error('Cliente no encontrado');
                return { success: true, data: client };
            }

            const res = await fetch(`${API_BASE_URL}/clients/${id}/`);
            if (!res.ok) throw new Error(`Error HTTP: ${res.status}`);
            return await res.json();
        },

        /**
         * POST /api/v1/clients/
         * Payload de Envío: { business_name, rfc, contact_name, email, phone, type, city, address, status }
         * Retorna el cliente creado con ID asignado.
         */
        async create(clientData) {
            if (USE_MOCKS) {
                await delay(400);
                const raw = localStorage.getItem(STORAGE_KEYS.CLIENTS);
                const list = raw ? JSON.parse(raw) : [...INITIAL_MOCK_CLIENTS];

                // Generar nuevo ID correlativo
                const nextNum = list.length + 1;
                const newClient = {
                    ...clientData,
                    id: `cli-${String(nextNum).padStart(3, '0')}`,
                    created_at: new Date().toISOString()
                };

                list.unshift(newClient);
                localStorage.setItem(STORAGE_KEYS.CLIENTS, JSON.stringify(list));
                return { success: true, message: 'Cliente registrado exitosamente', data: newClient };
            }

            const res = await fetch(`${API_BASE_URL}/clients/`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify(clientData)
            });
            if (!res.ok) throw new Error(`Error HTTP: ${res.status}`);
            return await res.json();
        },

        /**
         * PUT /api/v1/clients/{id}/
         * Actualiza los datos de un cliente existente.
         */
        async update(id, clientData) {
            if (USE_MOCKS) {
                await delay(400);
                const raw = localStorage.getItem(STORAGE_KEYS.CLIENTS);
                let list = raw ? JSON.parse(raw) : [...INITIAL_MOCK_CLIENTS];

                const index = list.findIndex(c => c.id === id);
                if (index === -1) throw new Error('Cliente no encontrado para actualizar');

                list[index] = { ...list[index], ...clientData, updated_at: new Date().toISOString() };
                localStorage.setItem(STORAGE_KEYS.CLIENTS, JSON.stringify(list));
                return { success: true, message: 'Cliente actualizado correctamente', data: list[index] };
            }

            const res = await fetch(`${API_BASE_URL}/clients/${id}/`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify(clientData)
            });
            if (!res.ok) throw new Error(`Error HTTP: ${res.status}`);
            return await res.json();
        },

        /**
         * DELETE /api/v1/clients/{id}/
         * Elimina o desactiva un cliente.
         */
        async delete(id) {
            if (USE_MOCKS) {
                await delay(350);
                const raw = localStorage.getItem(STORAGE_KEYS.CLIENTS);
                let list = raw ? JSON.parse(raw) : [...INITIAL_MOCK_CLIENTS];

                list = list.filter(c => c.id !== id);
                localStorage.setItem(STORAGE_KEYS.CLIENTS, JSON.stringify(list));
                return { success: true, message: 'Cliente eliminado del catálogo' };
            }

            const res = await fetch(`${API_BASE_URL}/clients/${id}/`, {
                method: 'DELETE',
                headers: {
                    'X-CSRFToken': getCsrfToken()
                }
            });
            if (!res.ok) throw new Error(`Error HTTP: ${res.status}`);
            return await res.json();
        }
    },

    /**
     * MÓDULO DE PRODUCTOS (CATÁLOGO & COMPARADOR EN VIVO):
     * Contratos para búsqueda normalizada multi-proveedor (SYSCOM / CT Online).
     */
    products: {
        /**
         * GET /api/v1/products/search?q=&provider=&category=
         * Retorna productos coincidentes con metadatos de comparación en vivo.
         */
        async search(filters = {}) {
            if (USE_MOCKS) {
                await delay(450); // Simular latencia de búsqueda en catálogo mayorista
                let list = [...MOCK_PRODUCTS];

                if (filters.query) {
                    const q = filters.query.toLowerCase().trim();
                    list = list.filter(p => 
                        p.name.toLowerCase().includes(q) ||
                        p.sku.toLowerCase().includes(q) ||
                        p.clave.toLowerCase().includes(q) ||
                        p.description.toLowerCase().includes(q)
                    );
                }

                if (filters.provider && filters.provider !== 'todos') {
                    list = list.filter(p => p.provider === filters.provider);
                }

                if (filters.category && filters.category !== 'todos') {
                    list = list.filter(p => p.category === filters.category);
                }

                return { success: true, count: list.length, data: list };
            }

            const params = new URLSearchParams(filters);
            const res = await fetch(`${API_BASE_URL}/products/search/?${params.toString()}`);
            if (!res.ok) throw new Error(`Error HTTP: ${res.status}`);
            return await res.json();
        },

        /**
         * GET /api/v1/products/{sku}/comparison/
         * Retorna comparativa directa entre SYSCOM y CT Online para un producto o equivalente.
         */
        async getComparison(sku) {
            if (USE_MOCKS) {
                await delay(300);
                const prod = MOCK_PRODUCTS.find(p => p.sku === sku);
                if (!prod) throw new Error('Producto no encontrado');
                return {
                    success: true,
                    data: {
                        primary_product: prod,
                        comparison: prod.comparison
                    }
                };
            }

            const res = await fetch(`${API_BASE_URL}/products/${sku}/comparison/`);
            if (!res.ok) throw new Error(`Error HTTP: ${res.status}`);
            return await res.json();
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
         * Guarda una nueva cotización.
         */
        async save(quoteData) {
            if (USE_MOCKS) {
                await delay(500);
                const raw = localStorage.getItem(STORAGE_KEYS.QUOTES);
                const list = raw ? JSON.parse(raw) : [...INITIAL_MOCK_QUOTES];

                // Generar nuevo folio si no viene
                const folio = quoteData.folio || `COT-2026-${String(list.length + 49).padStart(4, '0')}`;
                const newQuote = {
                    ...quoteData,
                    folio,
                    created_at: new Date().toISOString(),
                    status: quoteData.status || 'Pendiente'
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
            if (!res.ok) throw new Error(`Error HTTP: ${res.status}`);
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

// ==============================================================================
// 3. SISTEMA DE TOASTS Y FEEDBACK VISUAL NO INVASIVO (UI KIT GLOBAL)
// ==============================================================================

/**
 * Muestra una notificación Toast animada en la esquina superior derecha.
 * @param {string} message - Mensaje a mostrar
 * @param {'success'|'danger'|'warning'|'info'} type - Tipo de notificación
 * @param {string} title - Título opcional
 * @param {number} duration - Duración en milisegundos (default: 3500ms)
 */
function showToast(message, type = 'success', title = '', duration = 3500) {
    let container = document.getElementById('cconorToastContainer');
    if (!container) {
        container = document.createElement('div');
        container.id = 'cconorToastContainer';
        container.className = 'cconor-toast-container';
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

    toast.innerHTML = `
        <i class="bi ${icons[type] || icons.info} fs-4 mt-1 flex-shrink-0"></i>
        <div class="flex-grow-1">
            <div class="fw-bold text-dark small mb-0">${title || titles[type]}</div>
            <div class="small text-secondary mt-1">${message}</div>
        </div>
        <button type="button" class="btn-close btn-sm ms-2" aria-label="Cerrar" onclick="this.closest('.cconor-toast').remove()"></button>
    `;

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
window.showToast = showToast;
