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
    // Descarga REAL del PDF que genera Django (fetch + blob + <a download>).
    // - No navega fuera de la página: la pestaña del Resumen nunca se cambia.
    // - NUNCA llama a window.print(): eso es exclusivo del botón "Imprimir",
    //   así que aquí no se abre el diálogo de impresión del navegador.
    // - Si el servidor falla (404/500) o la respuesta no es un PDF, se avisa
    //   con toast en lugar de mostrar una página de error.
    // --------------------------------------------------------------------------
    function initDownloadPdf() {
        const btn = document.getElementById('downloadPdfBtn');
        if (!btn) return;

        btn.addEventListener('click', async function () {
            const folio = getCurrentFolio();
            if (!folio) {
                window.showToast('No se encontró el folio de la cotización.', 'danger');
                return;
            }
            const restore = setButtonLoading(btn, 'Generando PDF...');
            window.showToast('Generando y descargando PDF oficial...', 'info', 'PDF Oficial');

            // URL construida por Django (window.CCONOR_URLS, ver resumen.html); el folio es dinámico.
            const urlTemplate = (window.CCONOR_URLS && window.CCONOR_URLS.pdf) || '/cotizaciones/__FOLIO__/pdf/';
            const url = urlTemplate.replace('__FOLIO__', encodeURIComponent(folio));

            try {
                const res = await fetch(url, { credentials: 'same-origin' });
                if (!res.ok) {
                    window.showToast(
                        `El servidor respondió HTTP ${res.status}; no se generó el PDF.`,
                        'danger', 'Descarga Cancelada'
                    );
                    return;
                }
                if (!/pdf/i.test(res.headers.get('Content-Type') || '')) {
                    window.showToast(
                        'La respuesta del servidor no es un PDF válido.',
                        'danger', 'Descarga Cancelada'
                    );
                    return;
                }

                const blob = await res.blob();

                // El servidor responde Content-Disposition: attachment con el nombre
                // del archivo basado en el folio; si no viniera, se arma con el folio actual.
                const disposition = res.headers.get('Content-Disposition') || '';
                const match = disposition.match(/filename="?([^";]+)"?/i);
                const filename = (match && match[1]) || `Cotizacion_${folio}.pdf`;

                const objectUrl = URL.createObjectURL(blob);
                const anchor = document.createElement('a');
                anchor.href = objectUrl;
                anchor.download = filename;
                document.body.appendChild(anchor);
                anchor.click();
                anchor.remove();
                setTimeout(function () { URL.revokeObjectURL(objectUrl); }, 1000);

                window.showToast(`PDF "${filename}" descargado correctamente.`, 'success', 'Descarga Completa');
            } catch (err) {
                window.showToast('Error de red al descargar el PDF: ' + err.message, 'danger', 'Descarga Fallida');
            } finally {
                restore();
            }
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
