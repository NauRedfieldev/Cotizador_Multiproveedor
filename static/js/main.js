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

    // Avisos emergentes: un botón con data-enfocar="id" cierra su aviso y lleva al campo.
    document.addEventListener('click', function (event) {
        const boton = event.target.closest('[data-enfocar]');
        if (!boton) return;
        const aviso = boton.closest('.alert');
        if (aviso) bootstrap.Alert.getOrCreateInstance(aviso).close();
        const campo = document.getElementById(boton.dataset.enfocar);
        if (campo) campo.focus();
    });
});
