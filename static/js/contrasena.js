// B6: «Elige tu contraseña». Solo marca en vivo las reglas; quien decide es el servidor (validadores de Django).
(function () {
    'use strict';
    const form = document.getElementById('elegirContrasena');
    if (!form) return;
    const uno = document.getElementById('nuevaContrasena1');
    const dos = document.getElementById('nuevaContrasena2');
    const usuario = (form.dataset.usuario || '').toLowerCase();
    const reglas = {
        largo: v => v.length >= 8,
        numeros: v => v.length > 0 && !/^\d+$/.test(v),
        usuario: v => v.length > 0 && !(usuario && v.toLowerCase().includes(usuario)),
        iguales: v => v.length > 0 && v === dos.value
    };
    function marcar() {
        const valor = uno.value;
        document.querySelectorAll('#reglasContrasena [data-regla]').forEach(li => {
            li.classList.toggle('is-ok', reglas[li.dataset.regla](valor));
        });
    }
    uno.addEventListener('input', marcar);
    dos.addEventListener('input', marcar);
    marcar();
})();
