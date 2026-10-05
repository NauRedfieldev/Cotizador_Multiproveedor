/**
 * ==============================================================================
 * CCONOR - ACCIONES DE LA PROPUESTA COMERCIAL (RESUMEN)
 * ==============================================================================
 * Vincula los 4 botones de la barra superior del Resumen:
 *   1. Editar Partidas        -> Redirección al cotizador (enlace directo).
 *   2. Imprimir               -> Diálogo nativo con estilos @media print.
 *   3. Enviar por Correo      -> POST al endpoint Django con PDF adjunto real.
 *   4. Descargar PDF Oficial  -> Descarga directa del PDF generado por Django.
 */

(function () {
    'use strict';

    /** Obtiene el token CSRF de Django desde la cookie (vista con @ensure_csrf_cookie). */
    function getCsrfToken() {
        const name = 'csrftoken';
        if (!document.cookie) return '';
        const cookies = document.cookie.split(';');
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                return decodeURIComponent(cookie.substring(name.length + 1));
            }
        }
        return '';
    }

    /** Folio de la cotización mostrada en la hoja membretada. */
    function getCurrentFolio() {
        const printable = document.getElementById('printableQuote');
        return printable ? printable.dataset.quoteFolio : null;
    }

    /** Cambia el estado visual de un botón a "cargando" y lo restaura. */
    function setButtonLoading(btn, loadingText) {
        if (!btn) return function () {};
        const originalHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span><span>${loadingText}</span>`;
        return function restore() {
            btn.disabled = false;
            btn.innerHTML = originalHtml;
        };
    }

    // --------------------------------------------------------------------------
    // DESCARGAR PDF OFICIAL
    // --------------------------------------------------------------------------
    function initDownloadPdf() {
        const btn = document.getElementById('downloadPdfBtn');
        if (!btn) return;

        btn.addEventListener('click', function () {
            const folio = getCurrentFolio();
            if (!folio) {
                window.showToast('No se encontró el folio de la cotización.', 'danger');
                return;
            }
            const restore = setButtonLoading(btn, 'Generando PDF...');
            window.showToast('Generando y descargando PDF oficial...', 'info', 'PDF Oficial');
            // Descarga directa: el servidor responde con Content-Disposition: attachment.
            // URL construida por Django (window.CCONOR_URLS, ver resumen.html); el folio es dinámico.
            const urlTemplate = (window.CCONOR_URLS && window.CCONOR_URLS.pdf) || '/cotizaciones/__FOLIO__/pdf/';
            window.location.href = urlTemplate.replace('__FOLIO__', encodeURIComponent(folio));
            setTimeout(restore, 2500);
        });
    }

    // --------------------------------------------------------------------------
    // IMPRIMIR (usa los estilos @media print de custom.css)
    // --------------------------------------------------------------------------
    function initPrint() {
        const btn = document.getElementById('printQuoteBtn');
        if (!btn) return;
        btn.addEventListener('click', function () {
            window.print();
        });
    }

    // --------------------------------------------------------------------------
    // ENVIAR POR CORREO (PDF generado en memoria y adjuntado por Django)
    // --------------------------------------------------------------------------
    function initSendEmail() {
        const btn = document.getElementById('confirmSendEmailBtn');
        if (!btn) return;

        btn.addEventListener('click', async function () {
            const folio = getCurrentFolio();
            const modalEl = document.getElementById('emailModal');

            const email = (document.getElementById('emailRecipientInput')?.value || '').trim();
            const subject = (document.getElementById('emailSubjectInput')?.value || '').trim();
            const message = (document.getElementById('emailMessageInput')?.value || '').trim();

            if (!folio) {
                window.showToast('No se encontró el folio de la cotización.', 'danger');
                return;
            }
            if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
                window.showToast('Ingresa un correo destinatario válido.', 'warning', 'Datos incompletos');
                return;
            }

            const restore = setButtonLoading(btn, 'Enviando cotización...');
            const attachPdf = document.getElementById('attachPdfCheck')?.checked !== false;

            try {
                const emailUrlTemplate = (window.CCONOR_URLS && window.CCONOR_URLS.email) || '/api/cotizaciones/__FOLIO__/enviar-correo/';
                const res = await fetch(emailUrlTemplate.replace('__FOLIO__', encodeURIComponent(folio)), {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': getCsrfToken(),
                    },
                    body: JSON.stringify({ email, subject, message, attach_pdf: attachPdf }),
                });

                let data = {};
                try { data = await res.json(); } catch (e) { /* respuesta no JSON */ }

                if (res.ok && (data.success || data.status === 'success')) {
                    const modal = bootstrap.Modal.getInstance(modalEl);
                    if (modal) modal.hide();
                    window.showToast(
                        data.message || `Cotización enviada exitosamente a ${email}`,
                        'success',
                        'Correo Enviado'
                    );
                } else {
                    window.showToast(
                        data.error || `Error del servidor (HTTP ${res.status}). Intenta de nuevo.`,
                        'danger',
                        'Fallo en el Envío'
                    );
                }
            } catch (err) {
                window.showToast(
                    'Error de red al contactar el servidor: ' + err.message,
                    'danger',
                    'Fallo en el Envío'
                );
            } finally {
                restore();
            }
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        initDownloadPdf();
        initPrint();
        initSendEmail();
    });
})();
