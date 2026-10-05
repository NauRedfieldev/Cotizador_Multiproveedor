/**
 * ==============================================================================
 * CCONOR Soluciones Tecnológicas - Controlador de Clientes B2B (CRUD)
 * ==============================================================================
 * 
 * Gestiona el ciclo completo de Clientes:
 * - Listado con Skeleton Loader (estado de carga).
 * - Empty State con llamada a la acción (estado vacío).
 * - Búsqueda reactiva por texto y filtros (Estatus, Clasificación B2B).
 * - Alta y Edición en Modal con validación semántica en tiempo real (RFC, email, campos requeridos).
 * - Ver ficha técnica y confirmación de eliminación.
 * - Desacoplado: Consume window.apiClient.clients
 */

document.addEventListener('DOMContentLoaded', function () {
    // Referencias a elementos del DOM
    const skeletonContainer = document.getElementById('clientsSkeletonContainer');
    const tableWrapper = document.getElementById('clientsTableWrapper');
    const emptyState = document.getElementById('clientsEmptyState');
    const emptyStateTitle = document.getElementById('emptyStateTitle');
    const emptyStateDesc = document.getElementById('emptyStateDesc');
    const tableBody = document.getElementById('clientsTableBody');
    const clientsCounter = document.getElementById('clientsCounter');

    // Métricas
    const metricTotal = document.getElementById('metricTotalClients');
    const metricActive = document.getElementById('metricActiveClients');
    const metricIntegrators = document.getElementById('metricIntegratorClients');

    // Filtros y Búsqueda
    const searchInput = document.getElementById('clientSearchInput');
    const clearSearchBtn = document.getElementById('clearClientSearchBtn');
    const typeFilter = document.getElementById('clientTypeFilter');
    const statusFilter = document.getElementById('clientStatusFilter');
    const refreshBtn = document.getElementById('refreshClientsBtn');
    const resetFiltersBtn = document.getElementById('resetFiltersBtn');

    // Modales y Formularios
    const clientModalEl = document.getElementById('clientModal');
    const clientModal = new bootstrap.Modal(clientModalEl);
    const clientModalTitle = document.getElementById('clientModalTitle');
    const clientForm = document.getElementById('clientForm');
    const openCreateBtn = document.getElementById('openCreateClientModalBtn');
    const emptyStateCreateBtn = document.getElementById('emptyStateCreateBtn');

    // Inputs del formulario
    const clientIdInput = document.getElementById('clientIdInput');
    const businessNameInput = document.getElementById('clientBusinessNameInput');
    const rfcInput = document.getElementById('clientRfcInput');
    const typeInput = document.getElementById('clientTypeInput');
    const contactInput = document.getElementById('clientContactInput');
    const emailInput = document.getElementById('clientEmailInput');
    const phoneInput = document.getElementById('clientPhoneInput');
    const cityInput = document.getElementById('clientCityInput');
    const addressInput = document.getElementById('clientAddressInput');
    const statusInput = document.getElementById('clientStatusInput');

    const saveSubmitBtn = document.getElementById('saveClientSubmitBtn');
    const saveSpinner = document.getElementById('saveClientSpinner');
    const saveIcon = document.getElementById('saveClientIcon');
    const saveBtnText = document.getElementById('saveClientBtnText');

    // Modal de Detalle
    const detailModalEl = document.getElementById('clientDetailModal');
    const detailModal = new bootstrap.Modal(detailModalEl);
    const detailBody = document.getElementById('clientDetailBody');
    const detailStartQuoteBtn = document.getElementById('detailStartQuoteBtn');

    // Modal de Eliminación
    const deleteModalEl = document.getElementById('deleteClientModal');
    const deleteModal = new bootstrap.Modal(deleteModalEl);
    const deleteIdInput = document.getElementById('deleteClientId');
    const deleteNameSpan = document.getElementById('deleteClientName');
    const confirmDeleteBtn = document.getElementById('confirmDeleteClientBtn');

    // Patrón Regex para RFC Mexicano (Personas Morales: 12 caracteres, Físicas: 13 caracteres)
    const RFC_REGEX = /^[A-Z&Ñ]{3,4}\d{6}[A-V1-9][A-Z0-9][0-9A]$/i;

    let currentClientsList = [];

    // ==========================================================================
    // 1. CARGA Y RENDERIZADO DE CLIENTES
    // ==========================================================================

    /**
     * Consulta el cliente API y renderiza la vista manejando estados de UI
     */
    async function loadClients() {
        setUiState('loading');

        try {
            const filters = {
                search: searchInput.value.trim(),
                type: typeFilter.value,
                status: statusFilter.value
            };

            const response = await window.apiClient.clients.getAll(filters);
            currentClientsList = response.data || [];

            updateMetrics(currentClientsList);

            if (currentClientsList.length === 0) {
                const isFiltered = filters.search || filters.type !== 'todos' || filters.status !== 'todos';
                if (isFiltered) {
                    emptyStateTitle.textContent = 'Sin coincidencias para la búsqueda';
                    emptyStateDesc.textContent = `No encontramos clientes con "${filters.search || 'los filtros seleccionados'}". Prueba con otro término o limpia los filtros.`;
                } else {
                    emptyStateTitle.textContent = 'Aún no hay clientes registrados';
                    emptyStateDesc.textContent = 'Comienza registrando tu primer cliente para emitir propuestas comerciales en el cotizador.';
                }
                setUiState('empty');
            } else {
                renderTable(currentClientsList);
                setUiState('data');
            }

            clientsCounter.textContent = currentClientsList.length;

        } catch (error) {
            console.error('Error al cargar clientes:', error);
            setUiState('empty');
            emptyStateTitle.textContent = 'Error al consultar clientes';
            emptyStateDesc.textContent = 'No fue posible sincronizar el catálogo de clientes. Por favor intenta nuevamente.';
            window.showToast('No se pudieron obtener los clientes: ' + error.message, 'danger', 'Fallo de Conexión');
        }
    }

    /**
     * Alterna entre los estados de UI: 'loading', 'empty', 'data'
     */
    function setUiState(state) {
        if (state === 'loading') {
            skeletonContainer.style.display = 'block';
            emptyState.style.display = 'none';
            tableWrapper.style.display = 'none';
        } else if (state === 'empty') {
            skeletonContainer.style.display = 'none';
            emptyState.style.display = 'block';
            tableWrapper.style.display = 'none';
        } else if (state === 'data') {
            skeletonContainer.style.display = 'none';
            emptyState.style.display = 'none';
            tableWrapper.style.display = 'block';
        }
    }

    /**
     * Actualiza los widgets de métricas superiores
     */
    function updateMetrics(list) {
        const total = list.length;
        const active = list.filter(c => c.status && c.status.toLowerCase() === 'activo').length;
        const integrators = list.filter(c => c.type && c.type.toLowerCase().includes('integrador')).length;

        metricTotal.textContent = total;
        metricActive.textContent = active;
        metricIntegrators.textContent = integrators;
    }

    /**
     * Renderiza las filas de la tabla de clientes
     */
    function renderTable(clients) {
        tableBody.innerHTML = '';

        clients.forEach((client, index) => {
            const tr = document.createElement('tr');
            tr.dataset.id = client.id;

            const isActive = client.status && client.status.toLowerCase() === 'activo';
            const statusBadge = isActive 
                ? '<span class="badge-status-active"><i class="bi bi-check-circle-fill me-1"></i>Activo</span>'
                : '<span class="badge-status-inactive"><i class="bi bi-slash-circle me-1"></i>Inactivo</span>';

            // Determinar iniciales para avatar
            const initials = client.business_name
                .split(' ')
                .filter(w => !['de', 'la', 'del', 'e', 's.a.', 'c.v.', 's.'].includes(w.toLowerCase()))
                .slice(0, 2)
                .map(w => w[0])
                .join('')
                .toUpperCase() || 'CL';

            tr.innerHTML = `
                <td class="text-center fw-bold text-secondary">${index + 1}</td>
                <td>
                    <div class="d-flex align-items-center gap-2.5">
                        <div class="w-10 h-10 rounded-circle bg-light border d-flex align-items-center justify-content-center fw-bold text-primary flex-shrink-0" style="width: 38px; height: 38px; font-size: 0.8rem; background-color: var(--cconor-lavanda-claro) !important;">
                            ${initials}
                        </div>
                        <div>
                            <div class="fw-bold text-dark text-truncate" style="max-width: 280px;" title="${client.business_name}">
                                ${client.business_name}
                            </div>
                            <div class="small text-muted" style="font-size: 0.75rem;">
                                <i class="bi bi-geo-alt me-0.5"></i> ${client.city || 'Tijuana, B.C.'}
                            </div>
                        </div>
                    </div>
                </td>
                <td>
                    <span class="badge bg-light text-dark font-monospace border px-2 py-1">
                        ${client.rfc || 'SIN RFC'}
                    </span>
                </td>
                <td>
                    <div class="fw-semibold text-dark small">${client.contact_name || 'No asignado'}</div>
                    <div class="text-muted small" style="font-size: 0.75rem;">
                        <a href="mailto:${client.email}" class="text-decoration-none text-muted">
                            <i class="bi bi-envelope me-1"></i>${client.email || '--'}
                        </a>
                    </div>
                </td>
                <td>
                    <div class="small text-dark font-monospace">${client.phone || '--'}</div>
                </td>
                <td>
                    <span class="badge-type-corp">${client.type || 'General'}</span>
                </td>
                <td>
                    ${statusBadge}
                </td>
                <td class="text-end">
                    <div class="btn-group btn-group-sm">
                        <button type="button" class="btn btn-outline-secondary btn-detail-client" data-id="${client.id}" title="Ver Ficha">
                            <i class="bi bi-eye"></i>
                        </button>
                        <button type="button" class="btn btn-outline-primary btn-edit-client" data-id="${client.id}" title="Editar">
                            <i class="bi bi-pencil"></i>
                        </button>
                        <button type="button" class="btn btn-outline-danger btn-delete-client" data-id="${client.id}" data-name="${client.business_name}" title="Eliminar">
                            <i class="bi bi-trash3"></i>
                        </button>
                    </div>
                </td>
            `;

            tableBody.appendChild(tr);
        });

        attachActionEvents();
    }

    /**
     * Vincula los botones de acción en cada fila
     */
    function attachActionEvents() {
        // Ver Detalle
        tableBody.querySelectorAll('.btn-detail-client').forEach(btn => {
            btn.addEventListener('click', () => viewClientDetail(btn.dataset.id));
        });

        // Editar
        tableBody.querySelectorAll('.btn-edit-client').forEach(btn => {
            btn.addEventListener('click', () => openEditClientModal(btn.dataset.id));
        });

        // Eliminar
        tableBody.querySelectorAll('.btn-delete-client').forEach(btn => {
            btn.addEventListener('click', () => {
                deleteIdInput.value = btn.dataset.id;
                deleteNameSpan.textContent = btn.dataset.name;
                deleteModal.show();
            });
        });
    }

    // ==========================================================================
    // 2. MODAL DE ALTA Y EDICIÓN (VALIDACIONES EN VIVO)
    // ==========================================================================

    function resetClientForm() {
        clientForm.reset();
        clientForm.classList.remove('was-validated');
        clientIdInput.value = '';
        rfcInput.classList.remove('is-invalid', 'is-valid');
        businessNameInput.classList.remove('is-invalid', 'is-valid');
        emailInput.classList.remove('is-invalid', 'is-valid');
        cityInput.value = 'Tijuana, B.C.';
        statusInput.value = 'Activo';
        typeInput.value = 'Corporativo';
    }

    function openCreateModal() {
        resetClientForm();
        clientModalTitle.innerHTML = '<i class="bi bi-building-add text-primary me-2"></i>Alta de Nuevo Cliente B2B';
        saveBtnText.textContent = 'Registrar Cliente';
        clientModal.show();
        setTimeout(() => businessNameInput.focus(), 300);
    }

    async function openEditClientModal(id) {
        resetClientForm();
        try {
            const res = await window.apiClient.clients.getById(id);
            const client = res.data;

            clientIdInput.value = client.id;
            businessNameInput.value = client.business_name || '';
            rfcInput.value = client.rfc || '';
            typeInput.value = client.type || 'Corporativo';
            contactInput.value = client.contact_name || '';
            emailInput.value = client.email || '';
            phoneInput.value = client.phone || '';
            cityInput.value = client.city || 'Tijuana, B.C.';
            addressInput.value = client.address || '';
            statusInput.value = client.status || 'Activo';

            clientModalTitle.innerHTML = `<i class="bi bi-pencil-square text-primary me-2"></i>Editar Cliente: <span class="fw-bold">${client.business_name}</span>`;
            saveBtnText.textContent = 'Guardar Cambios';
            clientModal.show();

        } catch (error) {
            window.showToast('No se pudo cargar la información del cliente', 'danger');
        }
    }

    // Validación interactiva de RFC en tiempo real (autocapitalizar y validar regex)
    rfcInput.addEventListener('input', function () {
        this.value = this.value.toUpperCase().trim();
        if (this.value.length >= 12) {
            if (RFC_REGEX.test(this.value)) {
                this.classList.remove('is-invalid');
                this.classList.add('is-valid');
            } else {
                this.classList.remove('is-valid');
                this.classList.add('is-invalid');
            }
        } else {
            this.classList.remove('is-valid');
        }
    });

    // Envío del formulario de cliente (Crear / Actualizar)
    clientForm.addEventListener('submit', async function (e) {
        e.preventDefault();

        // Validar campos requeridos
        let isValid = true;
        if (!businessNameInput.value.trim()) {
            businessNameInput.classList.add('is-invalid');
            isValid = false;
        } else {
            businessNameInput.classList.remove('is-invalid');
        }

        const rfcVal = rfcInput.value.trim().toUpperCase();
        if (!rfcVal || !RFC_REGEX.test(rfcVal)) {
            rfcInput.classList.add('is-invalid');
            isValid = false;
        } else {
            rfcInput.classList.remove('is-invalid');
        }

        if (!contactInput.value.trim()) {
            contactInput.classList.add('is-invalid');
            isValid = false;
        }

        if (!emailInput.value.trim() || !emailInput.checkValidity()) {
            emailInput.classList.add('is-invalid');
            isValid = false;
        }

        if (!isValid) {
            window.showToast('Verifica los campos obligatorios y el formato del RFC.', 'warning', 'Datos incompletos');
            return;
        }

        // Preparar payload
        const payload = {
            business_name: businessNameInput.value.trim(),
            rfc: rfcVal,
            type: typeInput.value,
            contact_name: contactInput.value.trim(),
            email: emailInput.value.trim(),
            phone: phoneInput.value.trim(),
            city: cityInput.value.trim(),
            address: addressInput.value.trim(),
            status: statusInput.value
        };

        const isEditing = Boolean(clientIdInput.value);

        // Feedback de carga en el botón
        saveSubmitBtn.disabled = true;
        saveSpinner.classList.remove('d-none');
        saveIcon.classList.add('d-none');

        try {
            if (isEditing) {
                await window.apiClient.clients.update(clientIdInput.value, payload);
                window.showToast(`Cliente "${payload.business_name}" actualizado con éxito`, 'success');
            } else {
                await window.apiClient.clients.create(payload);
                window.showToast(`Cliente "${payload.business_name}" registrado correctamente`, 'success');
            }

            clientModal.hide();
            await loadClients();

        } catch (error) {
            window.showToast('Error al procesar cliente: ' + error.message, 'danger');
        } finally {
            saveSubmitBtn.disabled = false;
            saveSpinner.classList.add('d-none');
            saveIcon.classList.remove('d-none');
        }
    });

    // ==========================================================================
    // 3. FICHA TÉCNICA DEL CLIENTE (MODAL DETALLE)
    // ==========================================================================

    async function viewClientDetail(id) {
        try {
            const res = await window.apiClient.clients.getById(id);
            const client = res.data;

            detailBody.innerHTML = `
                <div class="d-flex align-items-center gap-3 mb-4 pb-3 border-bottom">
                    <div class="w-12 h-12 rounded-circle bg-primary bg-opacity-10 text-primary d-flex align-items-center justify-content-center fs-3" style="width: 52px; height: 52px;">
                        <i class="bi bi-building"></i>
                    </div>
                    <div>
                        <h5 class="fw-bold mb-0 text-dark">${client.business_name}</h5>
                        <div class="d-flex align-items-center gap-2 mt-1">
                            <span class="badge bg-light text-dark border font-monospace">${client.rfc}</span>
                            <span class="badge-type-corp">${client.type}</span>
                        </div>
                    </div>
                </div>

                <div class="row g-3 small">
                    <div class="col-6">
                        <span class="text-muted d-block">Contacto Principal:</span>
                        <strong class="text-dark">${client.contact_name}</strong>
                    </div>
                    <div class="col-6">
                        <span class="text-muted d-block">Estatus Comercial:</span>
                        <span class="${client.status === 'Activo' ? 'text-success fw-bold' : 'text-danger fw-bold'}">
                            <i class="bi bi-circle-fill me-1" style="font-size: 0.5rem;"></i>${client.status}
                        </span>
                    </div>
                    <div class="col-6">
                        <span class="text-muted d-block">Correo Electrónico:</span>
                        <a href="mailto:${client.email}">${client.email}</a>
                    </div>
                    <div class="col-6">
                        <span class="text-muted d-block">Teléfono:</span>
                        <span class="font-monospace">${client.phone || '--'}</span>
                    </div>
                    <div class="col-12">
                        <span class="text-muted d-block">Domicilio Fiscal:</span>
                        <span class="text-dark">${client.address || 'No registrado'} - ${client.city}</span>
                    </div>
                </div>
            `;

            detailStartQuoteBtn.href = `/cotizador/?cliente=${client.id}`;
            detailModal.show();

        } catch (error) {
            window.showToast('No se pudo abrir la ficha del cliente', 'danger');
        }
    }

    // ==========================================================================
    // 4. ELIMINACIÓN DE CLIENTE
    // ==========================================================================

    confirmDeleteBtn.addEventListener('click', async function () {
        const id = deleteIdInput.value;
        if (!id) return;

        confirmDeleteBtn.disabled = true;
        confirmDeleteBtn.textContent = 'Eliminando...';

        try {
            await window.apiClient.clients.delete(id);
            deleteModal.hide();
            window.showToast('Cliente eliminado del catálogo', 'success');
            await loadClients();
        } catch (error) {
            window.showToast('Error al eliminar cliente: ' + error.message, 'danger');
        } finally {
            confirmDeleteBtn.disabled = false;
            confirmDeleteBtn.textContent = 'Eliminar';
        }
    });

    // ==========================================================================
    // 5. EVENTOS DE BÚSQUEDA Y FILTROS REACTIVOS
    // ==========================================================================

    let searchTimeout = null;
    searchInput.addEventListener('input', function () {
        clearSearchBtn.style.display = this.value ? 'block' : 'none';
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(loadClients, 300);
    });

    clearSearchBtn.addEventListener('click', function () {
        searchInput.value = '';
        this.style.display = 'none';
        loadClients();
    });

    typeFilter.addEventListener('change', loadClients);
    statusFilter.addEventListener('change', loadClients);
    refreshBtn.addEventListener('click', loadClients);

    resetFiltersBtn.addEventListener('click', function () {
        searchInput.value = '';
        typeFilter.value = 'todos';
        statusFilter.value = 'todos';
        clearSearchBtn.style.display = 'none';
        loadClients();
    });

    openCreateBtn.addEventListener('click', openCreateModal);
    emptyStateCreateBtn.addEventListener('click', openCreateModal);

    // Carga inicial al montar la página
    loadClients();
});
