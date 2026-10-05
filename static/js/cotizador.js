/**
 * ==============================================================================
 * CCONOR Soluciones Tecnológicas - Lógica Dinámica del Cotizador B2B
 * ==============================================================================
 * 
 * Funcionalidades del Módulo:
 * - Carga dinámica de clientes desde `apiClient.clients`.
 * - Búsqueda de productos en tiempo real con Skeleton Loader y Empty State.
 * - Comparativa de precios y stock en vivo (SYSCOM vs CT Online).
 * - Selección interactiva de productos y adición a la cotización.
 * - Recálculo reactivo de costos, márgenes comerciales, IVA y totales.
 * - Manejo de estado vacío en la tabla de cotización.
 * - Guardado formal de la cotización mediante `apiClient.quotes.save()`.
 * - Notificaciones Toast no invasivas.
 */

document.addEventListener('DOMContentLoaded', function () {
    // Referencias a elementos del DOM
    const clientSelect = document.getElementById('clientSelect');
    const quoteFolioInput = document.getElementById('quoteFolio');
    const quoteValiditySelect = document.getElementById('quoteValidity');
    
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

    // Formulario de Cliente Rápido en Modal
    const newClientForm = document.getElementById('newClientForm');
    const saveClientBtn = document.getElementById('saveClientBtn');
    const newClientModalEl = document.getElementById('newClientModal');
    const newClientModal = newClientModalEl ? new bootstrap.Modal(newClientModalEl) : null;

    // Formateador monetario MXN
    function formatMoney(amount) {
        return '$' + Number(amount || 0).toLocaleString('es-MX', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
    }

    // ==========================================================================
    // 1. CARGA DINÁMICA DE CLIENTES B2B
    // ==========================================================================

    async function loadClientOptions() {
        if (!clientSelect) return;

        try {
            const res = await window.apiClient.clients.getAll();
            const clients = res.data || [];

            // Leer parámetro de URL si viene redirigido desde el módulo de clientes (?cliente=cli-002)
            const urlParams = new URLSearchParams(window.location.search);
            const preselectedClientId = urlParams.get('cliente');

            clientSelect.innerHTML = '';
            clients.forEach(c => {
                const opt = document.createElement('option');
                opt.value = c.id;
                opt.textContent = `${c.business_name} (${c.rfc})`;
                opt.dataset.email = c.email;
                if (preselectedClientId && c.id === preselectedClientId) {
                    opt.selected = true;
                }
                clientSelect.appendChild(opt);
            });

            // Opción de alta rápida
            const newOpt = document.createElement('option');
            newOpt.value = 'nuevo';
            newOpt.textContent = '+ Registrar Nuevo Cliente...';
            clientSelect.appendChild(newOpt);

        } catch (error) {
            console.error('Error al cargar selector de clientes:', error);
        }
    }

    // Detectar selección de "nuevo cliente" en el dropdown
    if (clientSelect) {
        clientSelect.addEventListener('change', function () {
            if (this.value === 'nuevo' && newClientModal) {
                newClientModal.show();
            }
        });
    }

    // Guardado desde el modal rápido de cliente
    if (saveClientBtn && newClientForm) {
        saveClientBtn.addEventListener('click', async function () {
            const inputs = newClientForm.querySelectorAll('input');
            const businessName = inputs[0] ? inputs[0].value.trim() : '';
            const rfc = inputs[1] ? inputs[1].value.trim().toUpperCase() : 'XAXX010101000';
            const phone = inputs[2] ? inputs[2].value.trim() : '';
            const email = inputs[3] ? inputs[3].value.trim() : '';
            const city = inputs[4] ? inputs[4].value.trim() : 'Tijuana, B.C.';

            if (!businessName || !email) {
                window.showToast('Por favor completa la razón social y el correo del cliente.', 'warning');
                return;
            }

            try {
                const res = await window.apiClient.clients.create({
                    business_name: businessName,
                    rfc,
                    contact_name: 'Contacto General',
                    email,
                    phone,
                    city,
                    type: 'Corporativo',
                    status: 'Activo'
                });

                window.showToast(`Cliente "${businessName}" registrado con éxito.`, 'success');
                if (newClientModal) newClientModal.hide();
                newClientForm.reset();

                // Recargar y preseleccionar el cliente recién creado
                await loadClientOptions();
                clientSelect.value = res.data.id;

            } catch (err) {
                window.showToast('Error al registrar cliente: ' + err.message, 'danger');
            }
        });
    }

    // ==========================================================================
    // 2. BÚSQUEDA REACTIVA DE PRODUCTOS Y COMPARATIVA EN VIVO
    // ==========================================================================

    let searchDebounceTimer = null;

    async function executeProductSearch() {
        const query = searchInput.value.trim();
        const provider = providerFilter.value;

        if (!query && provider === 'todos') {
            resultsBox.style.display = 'none';
            return;
        }

        // Mostrar caja de resultados y activar skeleton loader
        resultsBox.style.display = 'block';
        searchSkeletonLoader.style.display = 'block';
        searchEmptyState.style.display = 'none';
        resultsList.style.display = 'none';
        resultsCountBadge.textContent = 'Buscando...';

        try {
            const res = await window.apiClient.products.search({ query, provider });
            const products = res.data || [];

            searchSkeletonLoader.style.display = 'none';

            if (products.length === 0) {
                searchEmptyState.style.display = 'block';
                resultsList.style.display = 'none';
                resultsCountBadge.textContent = '0 productos';
            } else {
                searchEmptyState.style.display = 'none';
                resultsList.style.display = 'block';
                resultsCountBadge.textContent = `${products.length} producto${products.length > 1 ? 's' : ''}`;
                renderSearchResults(products);
            }

        } catch (error) {
            searchSkeletonLoader.style.display = 'none';
            searchEmptyState.style.display = 'block';
            window.showToast('Error en la búsqueda de productos.', 'danger');
        }
    }

    function renderSearchResults(products) {
        resultsList.innerHTML = '';

        products.forEach(p => {
            const item = document.createElement('div');
            item.className = 'list-group-item list-group-item-action p-3 border mb-2 rounded-3 bg-white shadow-sm';

            // Etiqueta de comparador en vivo (SYSCOM vs CT Online)
            let comparisonBadge = '';
            if (p.comparison) {
                if (p.comparison.is_best_price) {
                    comparisonBadge = `
                        <span class="badge badge-comparison-best me-2">
                            <i class="bi bi-star-fill me-1"></i> Mejor Precio vs ${p.comparison.alternative_label} (Ahorro $${Math.abs(p.comparison.price_difference_mxn).toLocaleString('es-MX')})
                        </span>
                    `;
                } else {
                    comparisonBadge = `
                        <span class="badge bg-light text-muted border me-2">
                            Equiv. en ${p.comparison.alternative_label}: $${p.comparison.alternative_price.toLocaleString('es-MX')}
                        </span>
                    `;
                }
            }

            const providerBadgeClass = p.provider === 'syscom' ? 'badge-syscom' : 'badge-ct';

            item.innerHTML = `
                <div class="d-flex flex-column flex-md-row justify-content-between align-items-md-center gap-3">
                    <div class="flex-grow-1">
                        <div class="d-flex flex-wrap align-items-center gap-2 mb-1">
                            <span class="badge badge-provider ${providerBadgeClass}">${p.provider_label}</span>
                            <span class="badge bg-light text-dark border font-monospace">${p.sku}</span>
                            <span class="badge bg-light text-secondary border font-monospace">${p.clave}</span>
                            ${comparisonBadge}
                            <span class="text-success small fw-bold">
                                <i class="bi bi-box-seam me-0.5"></i> ${p.stock} en stock
                            </span>
                        </div>
                        <h6 class="fw-bold mb-1 text-dark">${p.name}</h6>
                        <p class="text-muted small mb-0 text-truncate" style="max-width: 600px;">
                            ${p.description}
                        </p>
                    </div>

                    <div class="text-md-end flex-shrink-0">
                        <div class="small text-muted">Precio Mayoreo c/IVA:</div>
                        <div class="fw-bold fs-5 text-primary mb-2">${formatMoney(p.price_wholesale_mxn)}</div>
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

        // Verificar si el producto ya está en la cotización
        const existingRow = tableBody.querySelector(`tr[data-sku="${product.sku}"]`);
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
        tr.dataset.sku = product.sku;
        tr.dataset.price = product.price_wholesale_mxn;
        tr.dataset.provider = product.provider;

        const providerBadgeClass = product.provider === 'syscom' ? 'badge-syscom' : 'badge-ct';
        const defaultMargin = product.suggested_margin || 20;

        tr.innerHTML = `
            <td class="text-center fw-bold text-secondary row-index"></td>
            <td><span class="font-monospace fw-semibold text-dark">${product.sku}</span></td>
            <td><span class="badge bg-light text-dark border">${product.clave}</span></td>
            <td class="text-wrap" style="max-width: 320px;">
                <strong>${product.name}</strong>
            </td>
            <td><span class="badge badge-provider ${providerBadgeClass}">${product.provider_label}</span></td>
            <td class="text-end fw-semibold item-unit-price">${formatMoney(product.price_wholesale_mxn)}</td>
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
        const unitCost = parseFloat(row.dataset.price) || 0;
        const marginInput = row.querySelector('.item-margin-input');
        const qtyInput = row.querySelector('.item-qty-input');
        const totalCell = row.querySelector('.item-row-total');

        const marginPercent = parseFloat(marginInput ? marginInput.value : 0) || 0;
        const qty = parseInt(qtyInput ? qtyInput.value : 1) || 1;

        // Precio unitario cliente = Costo base * (1 + margen / 100)
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

        let totalCostBase = 0;
        let totalFinalWithTax = 0;
        let totalClientSubtotalWithoutTax = 0;

        rows.forEach((row, index) => {
            const numCell = row.querySelector('.row-index') || row.cells[0];
            if (numCell) numCell.textContent = index + 1;

            const unitCost = parseFloat(row.dataset.price) || 0;
            const marginInput = row.querySelector('.item-margin-input');
            const qtyInput = row.querySelector('.item-qty-input');

            const marginPercent = parseFloat(marginInput ? marginInput.value : 0) || 0;
            const qty = parseInt(qtyInput ? qtyInput.value : 1) || 1;

            const rowCostBase = unitCost * qty;
            const unitPriceClient = unitCost * (1 + (marginPercent / 100));
            const rowTotalWithTax = unitPriceClient * qty;
            const rowClientSubtotal = rowTotalWithTax / 1.16;

            totalCostBase += rowCostBase;
            totalFinalWithTax += rowTotalWithTax;
            totalClientSubtotalWithoutTax += rowClientSubtotal;
        });

        const totalTaxIva = totalFinalWithTax - totalClientSubtotalWithoutTax;
        const totalNetMargin = (totalFinalWithTax - totalCostBase);

        if (costBaseDisplay) costBaseDisplay.textContent = formatMoney(totalCostBase) + ' MXN';
        if (marginDisplay) marginDisplay.textContent = '+' + formatMoney(totalNetMargin) + ' MXN';
        if (subtotalDisplay) subtotalDisplay.textContent = formatMoney(totalClientSubtotalWithoutTax) + ' MXN';
        if (ivaDisplay) ivaDisplay.textContent = formatMoney(totalTaxIva) + ' MXN';
        if (totalFinalDisplay) totalFinalDisplay.textContent = formatMoney(totalFinalWithTax) + ' MXN';
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

            const clientSelectedOption = clientSelect ? clientSelect.selectedOptions[0] : null;
            const clientId = clientSelectedOption ? clientSelectedOption.value : 'cli-001';
            const clientName = clientSelectedOption ? clientSelectedOption.textContent : 'Cliente General';
            const folio = quoteFolioInput ? quoteFolioInput.value : 'COT-2026-0048';
            const validityDays = quoteValiditySelect ? parseInt(quoteValiditySelect.value) : 30;

            const items = [];
            rows.forEach(r => {
                items.push({
                    sku: r.dataset.sku,
                    provider: r.dataset.provider,
                    price: parseFloat(r.dataset.price),
                    margin: parseFloat(r.querySelector('.item-margin-input')?.value || 20),
                    qty: parseInt(r.querySelector('.item-qty-input')?.value || 1)
                });
            });

            const quotePayload = {
                folio,
                client_id: clientId,
                client_name: clientName,
                validity_days: validityDays,
                items_count: items.length,
                items
            };

            saveQuoteBtn.disabled = true;
            saveQuoteBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Guardando...';

            try {
                const res = await window.apiClient.quotes.save(quotePayload);
                window.showToast(res.message || `Cotización ${folio} guardada con éxito.`, 'success');
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
        providerFilter.addEventListener('change', executeProductSearch);
    }

    // Inicializar clientes
    loadClientOptions();
});
