/**
 * CCONOR Soluciones Tecnológicas
 * Script principal del sistema
 */

document.addEventListener('DOMContentLoaded', function () {
    console.log('CCONOR - Sistema de Cotización Multi-Proveedor cargado correctamente.');

    // Inicializar tooltips de Bootstrap si están presentes
    const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl);
    });
});
