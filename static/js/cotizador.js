/**
 * ==============================================================================
 * CCONOR Soluciones Tecnológicas - Lógica Dinámica del Cotizador B2B
 * ==============================================================================
 * 
 * Funcionalidades del Módulo:
 * - Búsqueda de productos en tiempo real con Skeleton Loader y Empty State.
 * - Contrato JSON M7b: dinero como strings, contexto FX y proveedores caídos.
 * - Filtro de proveedor local sobre el atributo provider de cada resultado.
 * - Auto-búsqueda al llegar del comparador (?sku=&tipo_cambio=).
 * - Selección interactiva de productos y adición a la cotización.
 * - Recálculo: importe = precio_mxn * (1 + margen/100) * cantidad;
 *   IVA 16% sumado aparte sobre el subtotal (redondeo solo al mostrar).
 * - Advertencia al cambiar el tipo de cambio con partidas en la tabla.
 * - Guardado con el payload exacto del modelo Quote (números como strings).
 * - Notificaciones Toast no invasivas.
 */

document.addEventListener('DOMContentLoaded', function () {
    // Referencias a elementos del DOM (cabecera de la cotización)
    const clientNameInput = document.getElementById('clientName');
    const clientRfcInput = document.getElementById('clientRfc');
    const clientEmailInput = document.getElementById('clientEmail');
    const validDaysInput = document.getElementById('validDays');
    const exchangeRateInput = document.getElementById('exchangeRate');
    const quoteNotesInput = document.getElementById('quoteNotes');
    
    // Búsqueda de Productos
    const searchInput = document.getElementById('productSearchInput');
    const searchBtn = document.getElementById('searchProductBtn');
    const clearSearchBtn = document.getElementById('clearSearchBtn');
    const providerFilter = document.getElementById('providerFilter');
    const resultsBox = document.getElementById('searchResultsBox');
    const resultsList = document.getElementById('resultsList');
    const resultsCountBadge = document.getElementById('resultsCountBadge');
    const searchSkeletonLoader = document.getElementById('searchSkeletonLoader');
    const searchEmptyState = document.getElementById('searchEmptyState');
    const searchFxInfo = document.getElementById('searchFxInfo');
    const searchWarning = document.getElementById('searchWarning');

    // Tabla de Partidas
    const tableBody = document.getElementById('quoteItemsBody');
    const emptyTableState = document.getElementById('quoteTableEmptyState');

    // Totales
    const costBaseDisplay = document.getElementById('costBaseDisplay');
    const marginDisplay = document.getElementById('marginDisplay');
    const subtotalDisplay = document.getElementById('subtotalDisplay');
    const ivaDisplay = document.getElementById('ivaDisplay');
    const totalFinalDisplay = document.getElementById('totalFinalDisplay');

    // Botones de acción
    const saveQuoteBtn = document.getElementById('saveQuoteBtn');
    const clearTableBtn = document.getElementById('clearTableBtn');

    // Constantes financieras (M7b)
    const IVA_RATE = 0.16;      // IVA trasladado sobre el subtotal
    const DEFAULT_MARGIN = 20;  // Margen % por defecto en partidas nuevas

    // Formateador monetario MXN
    function formatMoney(amount) {
        return '$' + Number(amount || 0).toLocaleString('es-MX', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
    }

    // ==========================================================================
    // UNIDADES DE VENTA (PRODUCTOS DIVISIBLES)
    // ==========================================================================
    // Costo unitario vigente de la fila (data-price = precio_base MXN):
    //   unidad base  -> costo = precio_base
    //   unidad fracc -> costo = precio_base / factor_conversion (2 dec.)
    function getRowUnitCost(row) {
        const precioBase = parseFloat(row.dataset.price) || 0;
        const seleccionada = row.dataset.unidadSeleccionada;
        const base = (row.dataset.unidadBase || '').toLowerCase();
        if (row.dataset.esDivisible === '1' && seleccionada && seleccionada !== base) {
            const factor = parseFloat(row.dataset.factorConversion) || 0;
            if (factor > 0) return Math.round((precioBase / factor) * 100) / 100;
        }
        return precioBase;
    }

    // Actualiza la celda "Costo Mayoreo MXN" al cambiar de unidad.
    function updateRowUnitPrice(row) {
        const cell = row.querySelector('.item-unit-price');
        if (cell) cell.textContent = formatMoney(getRowUnitCost(row));
    }

    // ==========================================================================
    // 1. BÚSQUEDA REACTIVA DE PRODUCTOS Y COMPARATIVA EN VIVO
    // ==========================================================================

    let searchDebounceTimer = null;

    async function executeProductSearch() {
        const query = searchInput.value.trim();

        if (!query) {
            resultsBox.style.display = 'none';
            return;
        }

        // Mostrar caja de resultados y activar skeleton loader
        resultsBox.style.display = 'block';
        searchSkeletonLoader.style.display = 'block';
        searchEmptyState.style.display = 'none';
        resultsList.style.display = 'none';
        if (searchFxInfo) searchFxInfo.style.display = 'none';
        if (searchWarning) searchWarning.style.display = 'none';
        resultsCountBadge.textContent = 'Buscando...';

        try {
            // El tipo de cambio de la cabecera viaja en la búsqueda (contexto FX).
            const tipoCambio = exchangeRateInput ? exchangeRateInput.value.trim() : '';
            const res = await window.apiClient.products.search({ query, tipo_cambio: tipoCambio });
            const products = res.data || [];

            searchSkeletonLoader.style.display = 'none';
            renderSearchContext(res);

            if (products.length === 0) {
                searchEmptyState.style.display = 'block';
                resultsList.style.display = 'none';
                resultsCountBadge.textContent = '0 productos';
            } else {
                searchEmptyState.style.display = 'none';
                resultsList.style.display = 'block';
                renderSearchResults(products);
                applyProviderFilter();
            }

        } catch (error) {
            searchSkeletonLoader.style.display = 'none';
            searchEmptyState.style.display = 'block';
            // Los errores 400/429/503 traen el mensaje legible del backend.
            window.showToast('Error en la búsqueda: ' + error.message, 'danger');
        }
    }

    // Contexto de la respuesta M7b: tipo de cambio aplicado y proveedores caídos.
    function renderSearchContext(res) {
        if (searchFxInfo) {
            if (res.tipo_cambio) {
                const fecha = res.tipo_cambio.fecha ? new Date(res.tipo_cambio.fecha) : null;
                const fechaTxt = fecha && !isNaN(fecha) ? fecha.toLocaleString('es-MX') : '';
                searchFxInfo.innerHTML = `<i class="bi bi-arrow-repeat me-1"></i>Tipo de cambio: ${res.tipo_cambio.valor} MXN/USD (${res.tipo_cambio.origen}${fechaTxt ? ', ' + fechaTxt : ''})`;
                searchFxInfo.style.display = 'block';
            } else {
                searchFxInfo.style.display = 'none';
            }
        }
        if (searchWarning) {
            const caidos = res.proveedores_caidos_nombres || [];
            if (caidos.length > 0) {
                searchWarning.innerHTML = `<i class="bi bi-cloud-slash-fill me-1"></i><span>No respondieron: ${caidos.join(', ')}. Se muestran los demás proveedores.</span>`;
                searchWarning.style.display = 'flex';
            } else {
                searchWarning.style.display = 'none';
            }
        }
    }

    // Filtro de proveedor local: oculta/muestra resultados por data-provider.
    function applyProviderFilter() {
        const filtro = providerFilter ? providerFilter.value : 'todos';
        const items = resultsList.querySelectorAll('[data-provider]');
        if (items.length === 0) return;
        let visibles = 0;
        items.forEach(it => {
            const coincide = filtro === 'todos' || it.dataset.provider === filtro;
            it.style.display = coincide ? '' : 'none';
            if (coincide) visibles++;
        });
        if (resultsCountBadge) {
            resultsCountBadge.textContent = `${visibles} producto${visibles === 1 ? '' : 's'}`;
        }
    }

    function renderSearchResults(products) {
        resultsList.innerHTML = '';

        products.forEach(p => {
            const item = document.createElement('div');
            item.className = 'list-group-item list-group-item-action p-3 border mb-2 rounded-3 bg-white shadow-sm';
            item.dataset.provider = p.provider; // filtro local por proveedor

            const providerBadgeClass = p.provider === 'syscom' ? 'badge-syscom' : 'badge-ct';

            // Insignia de mejor precio (contrato M7b)
            const bestBadge = p.es_mejor_precio
                ? `<span class="badge badge-comparison-best me-2"><i class="bi bi-star-fill me-1"></i> Mejor Precio</span>`
                : '';

            // Disponibilidad (null = el proveedor no informa)
            let stockHtml = '';
            if (p.stock === null || p.stock === undefined) {
                stockHtml = '<span class="text-secondary small"><i class="bi bi-question-circle me-1"></i> Disponibilidad no informada</span>';
            } else if (p.stock === 0) {
                stockHtml = '<span class="text-danger small fw-bold"><i class="bi bi-x-octagon me-1"></i> Sin existencias</span>';
            } else {
                stockHtml = `<span class="text-success small fw-bold"><i class="bi bi-box-seam me-1"></i> ${p.stock} en stock</span>`;
            }

            // Coincidencia con el catálogo canónico (M5b/M5c)
            let matchHtml = '';
            if (p.match && p.match.estado === 'AUTO_MATCH' && p.match.canonico) {
                matchHtml = `<div class="small text-success mt-1"><i class="bi bi-patch-check-fill me-1"></i> Identificado: ${p.match.canonico.descripcion}</div>`;
            } else if (p.match && p.match.estado === 'REVISION_MANUAL') {
                matchHtml = `<div class="small mt-1" style="color: var(--cconor-naranja);"><i class="bi bi-patch-question-fill me-1"></i> Posible coincidencia: requiere revisión</div>`;
            }

            item.innerHTML = `
                <div class="d-flex flex-column flex-md-row justify-content-between align-items-md-center gap-3">
                    <div class="flex-grow-1">
                        <div class="d-flex flex-wrap align-items-center gap-2 mb-1">
                            <span class="badge badge-provider ${providerBadgeClass}">${p.provider_label}</span>
                            <span class="badge bg-light text-dark border font-monospace">${p.external_id}</span>
                            ${p.model && p.model !== p.external_id ? `<span class="badge bg-light text-secondary border font-monospace">${p.model}</span>` : ''}
                            ${bestBadge}
                            ${stockHtml}
                        </div>
                        <h6 class="fw-bold mb-1 text-dark">${p.name}</h6>
                        ${matchHtml}
                    </div>

                    <div class="text-md-end flex-shrink-0">
                        <div class="small text-muted">Costo Mayoreo MXN:</div>
                        <div class="fw-bold fs-5 text-primary mb-2">${formatMoney(p.precio_mxn)}</div>
                        <button type="button" class="btn btn-cconor-primary btn-sm px-3 rounded-pill btn-add-product" data-product='${JSON.stringify(p).replace(/'/g, "&#39;")}'>
                            <i class="bi bi-plus-lg me-1"></i> Agregar
                        </button>
                    </div>
                </div>
            `;

            resultsList.appendChild(item);
        });

        // Eventos en los botones "Agregar"
        resultsList.querySelectorAll('.btn-add-product').forEach(btn => {
            btn.addEventListener('click', function () {
                const prod = JSON.parse(this.dataset.product);
                addProductToQuote(prod);
            });
        });
    }

    // ==========================================================================
    // 3. AGREGAR PRODUCTO A LA TABLA DE COTIZACIÓN
    // ==========================================================================

    function addProductToQuote(product) {
        // Ocultar empty state de la tabla si estaba visible
        if (emptyTableState) emptyTableState.style.display = 'none';

        // Verificar si el producto ya está en la cotización (clave: external_id)
        const existingRow = tableBody.querySelector(`tr[data-sku="${product.external_id}"]`);
        if (existingRow) {
            const qtyInput = existingRow.querySelector('.item-qty-input');
            if (qtyInput) {
                qtyInput.value = parseInt(qtyInput.value || 1) + 1;
                updateRowTotal(existingRow);
                window.showToast(`Se incrementó la cantidad de "${product.name.substring(0, 35)}..." a ${qtyInput.value}.`, 'info');
                recalculateTotals();
                return;
            }
        }

        const tr = document.createElement('tr');
        tr.dataset.sku = product.external_id;
        tr.dataset.externalId = product.external_id;
        tr.dataset.model = product.model || '';
        tr.dataset.description = product.name;
        tr.dataset.price = String(product.precio_base); // precio_base MXN (unidad base)
        tr.dataset.provider = product.provider;
        tr.dataset.esDivisible = product.es_divisible ? '1' : '0';
        if (product.es_divisible) {
            tr.dataset.unidadBase = product.unidad_base;
            tr.dataset.unidadBaseEtiqueta = product.unidad_base_etiqueta || product.unidad_base;
            tr.dataset.factorConversion = product.factor_conversion;
            tr.dataset.unidadFraccion = product.unidad_fraccion;
            tr.dataset.simboloFraccion = product.simbolo_fraccion || '';
            tr.dataset.unidadSeleccionada = String(product.unidad_base).toLowerCase(); // por defecto: unidad base
        }

        const providerBadgeClass = product.provider === 'syscom' ? 'badge-syscom' : 'badge-ct';
        const defaultMargin = DEFAULT_MARGIN;

        // Celda "Unidad": select con las opciones de división, o etiqueta estática.
        const unitCellHtml = product.es_divisible
            ? `<td class="text-center">
                   <select class="item-unit-select" title="Unidad de venta">
                       <option value="${String(product.unidad_base).toLowerCase()}" selected>${product.unidad_base_etiqueta || product.unidad_base} (${product.factor_conversion} ${product.simbolo_fraccion})</option>
                       <option value="${String(product.unidad_fraccion).toLowerCase()}">${product.unidad_fraccion} (${product.simbolo_fraccion})</option>
                   </select>
               </td>`
            : `<td class="text-center"><span class="badge bg-light text-dark border item-unit-badge">Pieza (Pza)</span></td>`;

        tr.innerHTML = `
            <td class="text-center fw-bold text-secondary row-index"></td>
            <td><span class="font-monospace fw-semibold text-dark">${product.external_id}</span></td>
            <td><span class="badge bg-light text-dark border">${product.model || '—'}</span></td>
            <td class="text-wrap" style="max-width: 320px;">
                <strong>${product.name}</strong>
            </td>
            <td><span class="badge badge-provider ${providerBadgeClass}">${product.provider_label}</span></td>
            ${unitCellHtml}
            <td class="text-end fw-semibold item-unit-price">${formatMoney(product.precio_base)}</td>
            <td class="text-center">
                <input type="number" class="form-control form-control-sm text-center item-margin-input mx-auto" style="width: 70px;" value="${defaultMargin}" min="0" max="100">
            </td>
            <td class="text-center">
                <input type="number" class="item-qty-input item-qty" value="1" min="1" max="999">
            </td>
            <td class="text-end fw-bold text-primary item-row-total">$0.00</td>
            <td class="text-center">
                <button type="button" class="btn-remove-row" title="Eliminar partida">
                    <i class="bi bi-trash3"></i>
                </button>
            </td>
        `;

        tableBody.appendChild(tr);
        attachRowEvents(tr);
        updateRowTotal(tr);
        recalculateTotals();

        window.showToast(`Producto "${product.name.substring(0, 30)}..." agregado a la cotización.`, 'success');
    }

    // ==========================================================================
    // 4. RECÁLCULO DINÁMICO DE TOTALES Y MÁRGENES
    // ==========================================================================

    function updateRowTotal(row) {
        const unitCost = getRowUnitCost(row);
        const marginInput = row.querySelector('.item-margin-input');
        const qtyInput = row.querySelector('.item-qty-input');
        const totalCell = row.querySelector('.item-row-total');

        const marginPercent = parseFloat(marginInput ? marginInput.value : 0) || 0;
        const qty = parseInt(qtyInput ? qtyInput.value : 1) || 1;

        // Importe (sin IVA) = precio_mxn * (1 + margen / 100) * cantidad
        const unitPriceClient = unitCost * (1 + (marginPercent / 100));
        const rowTotal = unitPriceClient * qty;

        if (totalCell) {
            totalCell.textContent = formatMoney(rowTotal);
        }
    }

    function recalculateTotals() {
        const rows = tableBody.querySelectorAll('tr:not(#quoteTableEmptyState)');
        
        // Manejar empty state de la tabla
        if (rows.length === 0) {
            if (emptyTableState) emptyTableState.style.display = 'table-row';
            if (costBaseDisplay) costBaseDisplay.textContent = '$0.00 MXN';
            if (marginDisplay) marginDisplay.textContent = '$0.00 MXN';
            if (subtotalDisplay) subtotalDisplay.textContent = '$0.00 MXN';
            if (ivaDisplay) ivaDisplay.textContent = '$0.00 MXN';
            if (totalFinalDisplay) totalFinalDisplay.textContent = '$0.00 MXN';
            return;
        } else {
            if (emptyTableState) emptyTableState.style.display = 'none';
        }

        // Cálculo M7b: importe = precio_mxn * (1 + margen/100) * cantidad;
        // subtotal = suma de importes; IVA 16% sumado aparte. El redondeo a 2
        // decimales ocurre únicamente al mostrar (formatMoney).
        let totalCostBase = 0;
        let totalSubtotal = 0;

        rows.forEach((row, index) => {
            const numCell = row.querySelector('.row-index') || row.cells[0];
            if (numCell) numCell.textContent = index + 1;

            const unitCost = getRowUnitCost(row);
            const marginInput = row.querySelector('.item-margin-input');
            const qtyInput = row.querySelector('.item-qty-input');

            const marginPercent = parseFloat(marginInput ? marginInput.value : 0) || 0;
            const qty = parseInt(qtyInput ? qtyInput.value : 1) || 1;

            totalCostBase += unitCost * qty;
            totalSubtotal += unitCost * (1 + marginPercent / 100) * qty;
        });

        const totalIva = totalSubtotal * IVA_RATE;
        const totalFinal = totalSubtotal + totalIva;
        const totalNetMargin = totalSubtotal - totalCostBase;

        if (costBaseDisplay) costBaseDisplay.textContent = formatMoney(totalCostBase) + ' MXN';
        if (marginDisplay) marginDisplay.textContent = '+' + formatMoney(totalNetMargin) + ' MXN';
        if (subtotalDisplay) subtotalDisplay.textContent = formatMoney(totalSubtotal) + ' MXN';
        if (ivaDisplay) ivaDisplay.textContent = formatMoney(totalIva) + ' MXN';
        if (totalFinalDisplay) totalFinalDisplay.textContent = formatMoney(totalFinal) + ' MXN';
    }

    function attachRowEvents(row) {
        const qtyInput = row.querySelector('.item-qty-input');
        const marginInput = row.querySelector('.item-margin-input');
        const removeBtn = row.querySelector('.btn-remove-row');

        if (qtyInput) {
            qtyInput.addEventListener('input', () => {
                if (qtyInput.value < 1) qtyInput.value = 1;
                updateRowTotal(row);
                recalculateTotals();
            });
        }

        // Cambio de unidad (solo productos divisibles)
        const unitSelect = row.querySelector('.item-unit-select');
        if (unitSelect) {
            unitSelect.addEventListener('change', () => {
                row.dataset.unidadSeleccionada = unitSelect.value;
                updateRowUnitPrice(row);
                updateRowTotal(row);
                recalculateTotals();
            });
        }

        if (marginInput) {
            marginInput.addEventListener('input', () => {
                if (marginInput.value < 0) marginInput.value = 0;
                updateRowTotal(row);
                recalculateTotals();
            });
        }

        if (removeBtn) {
            removeBtn.addEventListener('click', () => {
                row.remove();
                recalculateTotals();
                window.showToast('Partida eliminada de la cotización.', 'info');
            });
        }
    }

    // Vincular eventos a filas ya existentes en el HTML inicial
    tableBody.querySelectorAll('tr:not(#quoteTableEmptyState)').forEach(row => {
        attachRowEvents(row);
        updateRowTotal(row);
    });
    recalculateTotals();

    // ==========================================================================
    // 5. GUARDAR COTIZACIÓN FORMAL (CONSUMO DESACOPLADO)
    // ==========================================================================

    if (saveQuoteBtn) {
        saveQuoteBtn.addEventListener('click', async function () {
            const rows = tableBody.querySelectorAll('tr:not(#quoteTableEmptyState)');
            if (rows.length === 0) {
                window.showToast('Agrega al menos un producto antes de guardar la cotización.', 'warning');
                return;
            }

            // Datos del cliente leídos directo de la cabecera (modelo Quote)
            const clientName = clientNameInput ? clientNameInput.value.trim() : '';
            if (!clientName) {
                window.showToast('La razón social del cliente es obligatoria.', 'warning');
                if (clientNameInput) clientNameInput.focus();
                return;
            }
            const clientRfc = clientRfcInput ? clientRfcInput.value.trim().toUpperCase() : '';
            if (clientRfc && !/^[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}$/.test(clientRfc)) {
                window.showToast('El RFC no tiene un formato válido (3-4 letras, 6 dígitos, 3 alfanuméricos).', 'warning');
                if (clientRfcInput) clientRfcInput.focus();
                return;
            }
            const clientEmail = clientEmailInput ? clientEmailInput.value.trim() : '';
            const validityDays = validDaysInput ? (parseInt(validDaysInput.value) || 15) : 15;
            const notes = quoteNotesInput ? quoteNotesInput.value.trim() : '';

            // Payload M7b exacto del modelo Quote: números como strings y sin
            // folio, client_id, subtotal ni IVA (los calcula el servidor).
            const items = [];
            rows.forEach(r => {
                const marginInput = r.querySelector('.item-margin-input');
                const qtyInput = r.querySelector('.item-qty-input');
                items.push({
                    provider: r.dataset.provider,
                    external_id: r.dataset.externalId || r.dataset.sku,
                    description: r.dataset.description || '',
                    model: r.dataset.model || '',
                    quantity: String(parseInt(qtyInput ? qtyInput.value : 1) || 1),
                    costo_mxn: getRowUnitCost(r).toFixed(4),
                    margen: String((marginInput ? parseFloat(marginInput.value) : DEFAULT_MARGIN) || 0)
                });
            });

            const quotePayload = {
                client_name: clientName,
                client_rfc: clientRfc,
                client_email: clientEmail,
                valid_days: validityDays,
                notes,
                tipo_cambio: exchangeRateInput ? exchangeRateInput.value.trim() : '',
                items
            };

            saveQuoteBtn.disabled = true;
            saveQuoteBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Guardando...';

            try {
                const res = await window.apiClient.quotes.save(quotePayload);
                window.showToast(res.message || 'Cotización guardada con éxito.', 'success');
            } catch (err) {
                window.showToast('Error al guardar cotización: ' + err.message, 'danger');
            } finally {
                saveQuoteBtn.disabled = false;
                saveQuoteBtn.innerHTML = '<i class="bi bi-floppy me-1"></i> Guardar Cotización';
            }
        });
    }

    // Limpiar toda la tabla
    if (clearTableBtn) {
        clearTableBtn.addEventListener('click', function () {
            const rows = tableBody.querySelectorAll('tr:not(#quoteTableEmptyState)');
            if (rows.length === 0) return;

            if (confirm('¿Deseas vaciar todas las partidas de la cotización actual?')) {
                rows.forEach(r => r.remove());
                recalculateTotals();
                window.showToast('Se limpiaron todas las partidas.', 'info');
            }
        });
    }

    // ==========================================================================
    // 6. EVENTOS DE BÚSQUEDA REACTIVA
    // ==========================================================================

    if (searchInput) {
        searchInput.addEventListener('input', function () {
            clearSearchBtn.style.display = this.value ? 'block' : 'none';
            clearTimeout(searchDebounceTimer);
            searchDebounceTimer = setTimeout(executeProductSearch, 350);
        });
    }

    if (clearSearchBtn) {
        clearSearchBtn.addEventListener('click', function () {
            searchInput.value = '';
            this.style.display = 'none';
            resultsBox.style.display = 'none';
        });
    }

    if (searchBtn) {
        searchBtn.addEventListener('click', executeProductSearch);
    }

    if (providerFilter) {
        providerFilter.addEventListener('change', applyProviderFilter);
    }

    // ==========================================================================
    // 7. TIPO DE CAMBIO Y AUTO-BÚSQUEDA DESDE EL COMPARADOR (M7b)
    // ==========================================================================

    // Cambiar el tipo de cambio con partidas en la tabla invalida los precios
    // mostrados: se advierte y se solicita recargar la búsqueda.
    if (exchangeRateInput) {
        exchangeRateInput.addEventListener('change', function () {
            const rows = tableBody.querySelectorAll('tr:not(#quoteTableEmptyState)');
            if (rows.length === 0) return;
            window.showToast(
                'El tipo de cambio cambió: los precios de la tabla provienen de la búsqueda anterior. Vuelve a buscar para recalcularlos.',
                'warning', 'Tipo de cambio actualizado', 6000
            );
            const query = searchInput ? searchInput.value.trim() : '';
            if (query && typeof window.confirm === 'function'
                && window.confirm('¿Volver a buscar con el nuevo tipo de cambio?')) {
                executeProductSearch();
            }
        });
    }

    // Auto-búsqueda al llegar del comparador: /cotizador/?sku=X&tipo_cambio=Y
    (function autoBuscarDesdeComparador() {
        const params = new URLSearchParams(window.location.search);
        const sku = (params.get('sku') || '').trim();
        const tipoCambio = (params.get('tipo_cambio') || '').trim();
        if (tipoCambio && exchangeRateInput) exchangeRateInput.value = tipoCambio;
        if (!sku || !searchInput) return;
        searchInput.value = sku;
        if (clearSearchBtn) clearSearchBtn.style.display = 'block';
        executeProductSearch();
    })();
});
