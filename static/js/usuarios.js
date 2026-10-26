// B6 (lienzo fila 12): «Usuarios y permisos». El servidor decide y valida todo (apps/quotes/usuarios.py);
// esta página solo muestra y envía los cambios. Los datos de usuarios se pintan con textContent, nunca como HTML.
(function () {
    'use strict';
    const app = document.getElementById('usuariosApp');
    if (!app) return;
    const API = app.dataset.api;

    const ORDEN = ['view_commercial_costs', 'change_commercial_fx', 'set_commercial_sale', 'change_commercial_margin',
        'change_commercial_tax', 'apply_commercial_discount', 'manage_commercial_permissions'];
    const PERMISO = {
        view_commercial_costs: 'Ver costos y cotizar (dólares con el de SYSCOM)',
        change_commercial_fx: 'Cambiar el tipo de cambio',
        set_commercial_sale: 'Partidas manuales y servicios',
        change_commercial_margin: 'Cambiar margen',
        change_commercial_tax: 'Cambiar IVA',
        apply_commercial_discount: 'Dar descuentos',
        manage_commercial_permissions: 'Gestionar usuarios'
    };
    const CORTO = {
        change_commercial_fx: 'tipo de cambio', set_commercial_sale: 'manuales', change_commercial_margin: 'margen',
        change_commercial_tax: 'IVA', apply_commercial_discount: 'descuentos'
    };
    const ROL = {
        none: { nombre: 'Sin acceso', color: '#8A87A0', tinte: '#EEEDF3' },
        personal: { nombre: 'Personal', color: '#5B5F7A', tinte: '#EEEDF3', desc: 'Cotiza con el tipo de cambio de SYSCOM.' },
        asesor: { nombre: 'Asesor', color: '#00796B', tinte: '#E6F2F0', desc: 'Cotiza todo lo de todos los días.' },
        admin: { nombre: 'Administrador', color: '#111638', tinte: '#EAE7F3', desc: 'Todo, y puede nombrar administradores.' },
        owner: { nombre: 'Dueño', color: '#8A4100', tinte: '#FFF3E6', desc: 'Superusuario. Nadie lo edita desde aquí; se asigna en el panel de Django.' }
    };

    let estado = { users: [], roles: [], extras: [], me: null };
    let filtro = '';

    // ---------- utilidades ----------
    function el(tag, props, hijos) {
        const nodo = document.createElement(tag);
        Object.entries(props || {}).forEach(([k, v]) => {
            if (v === undefined || v === null || v === false) return;
            if (k === 'class') nodo.className = v;
            else if (k === 'text') nodo.textContent = v;
            else if (k === 'style') nodo.style.cssText = v;
            else if (k.startsWith('on')) nodo.addEventListener(k.slice(2), v);
            else nodo.setAttribute(k, v === true ? '' : v);
        });
        (hijos || []).forEach(h => { if (h) nodo.append(h); });
        return nodo;
    }
    function icono(nombre) { return el('i', { class: 'bi ' + nombre, 'aria-hidden': 'true' }); }

    function csrf() {
        const par = document.cookie.split(';').map(c => c.trim()).find(c => c.startsWith('csrftoken='));
        return par ? decodeURIComponent(par.split('=')[1]) : '';
    }
    async function pedir(url, metodo, cuerpo) {
        const opciones = { method: metodo || 'GET', credentials: 'same-origin', headers: { 'Accept': 'application/json' } };
        if (cuerpo !== undefined) {
            opciones.headers['Content-Type'] = 'application/json';
            opciones.headers['X-CSRFToken'] = csrf();
            opciones.body = JSON.stringify(cuerpo);
        } else if (metodo && metodo !== 'GET') {
            opciones.headers['X-CSRFToken'] = csrf();
        }
        let respuesta, datos = null;
        try {
            respuesta = await fetch(url, opciones);
            datos = await respuesta.json();
        } catch (e) {
            throw new Error('No se pudo conectar con el servidor.');
        }
        if (!respuesta.ok || !datos || !datos.success) throw new Error((datos && datos.error) || 'No se pudo completar la operación.');
        return datos.data;
    }

    let temporizador = null;
    function aviso(texto, error) {
        const t = document.getElementById('usrToast');
        t.textContent = (error ? '' : '✓ ') + texto;
        t.classList.toggle('is-error', !!error);
        t.setAttribute('role', error ? 'alert' : 'status');
        t.hidden = false;
        clearTimeout(temporizador);
        temporizador = setTimeout(() => { t.hidden = true; }, error ? 7000 : 4500);
    }
    function avisoCorreo(correo, para, accion) {
        if (correo === 'sent') aviso(accion + ' a ' + para + ' desde la cuenta de sistemas.');
        else if (correo === 'console') aviso(accion + ' (correo de prueba: se escribió en la consola del servidor porque falta SISTEMAS_SMTP_HOST).');
        else aviso('No se pudo enviar el correo a ' + para + '. Revisa la cuenta de sistemas e inténtalo de nuevo.', true);
    }

    function hace(iso) {
        if (!iso) return 'nunca';
        const dias = Math.floor((new Date().setHours(0, 0, 0, 0) - new Date(iso).setHours(0, 0, 0, 0)) / 86400000);
        if (dias <= 0) return 'hoy';
        if (dias === 1) return 'ayer';
        if (dias < 31) return 'hace ' + dias + ' días';
        const meses = Math.floor(dias / 30);
        return meses < 12 ? (meses === 1 ? 'hace 1 mes' : 'hace ' + meses + ' meses') : 'hace más de un año';
    }

    function permisosDeRol(clave) {
        const rol = estado.roles.find(r => r.key === clave);
        return new Set(rol ? rol.permissions : []);
    }
    function extrasPermitidos(clave) {
        if (clave !== 'personal' && clave !== 'asesor') return new Set();
        const delRol = permisosDeRol(clave);
        return new Set(estado.extras.filter(c => !delRol.has(c)));
    }
    function etiquetaExtras(lista) { return lista.map(c => CORTO[c]).join(', '); }
    function nombreConExtras(rol, extras) { return ROL[rol].nombre + (extras.length ? ' + ' + etiquetaExtras(extras) : ''); }
    function buscar(id) { return estado.users.find(u => u.id === id); }

    // ---------- tarjetas de roles ----------
    function pintarRoles() {
        const caja = document.getElementById('usrRoles');
        caja.replaceChildren(...estado.roles.map(r => {
            const info = ROL[r.key], tiene = new Set(r.permissions);
            const lista = ORDEN.map(c => el('span', { class: 'usr-permiso' + (tiene.has(c) ? '' : ' is-no') }, tiene.has(c)
                ? [icono('bi-check-lg'), document.createTextNode(PERMISO[c])]
                : [el('span', { 'aria-hidden': 'true', text: '—', style: 'width: 1em; text-align: center' }),
                    el('span', { class: 'visually-hidden', text: 'No incluye: ' }), document.createTextNode(PERMISO[c])]));
            return el('section', { class: 'usr-rol', style: '--rol-color: ' + info.color + '; --rol-tinte: ' + info.tinte, 'aria-label': 'Rol ' + info.nombre }, [
                el('div', { class: 'd-flex align-items-center gap-2' }, [
                    el('strong', { text: info.nombre, style: 'font-size: 16px' }),
                    el('span', { class: 'usr-rol-conteo', text: String(r.count), title: r.count === 1 ? '1 persona' : r.count + ' personas' })
                ]),
                el('span', { class: 'small text-secondary', text: info.desc }),
                ...lista
            ]);
        }));
    }

    // ---------- tabla ----------
    function avatar(u) {
        return el('span', { class: 'usr-avatar', style: 'background: ' + (u.is_active ? ROL[u.role].color : '#A9A6B8'), 'aria-hidden': 'true', text: u.initials });
    }

    function celdaRol(u) {
        if (u.owner) {
            return el('div', { class: 'd-flex flex-column gap-1' }, [
                el('span', { class: 'usr-etiqueta usr-dueno align-self-start' }, [icono('bi-lock-fill'), document.createTextNode('Dueño')]),
                el('span', { class: 'usr-nota', text: 'No se edita desde aquí' })
            ]);
        }
        const select = el('select', { class: 'form-select usr-rol-select', 'aria-label': 'Rol de ' + u.name, disabled: u.me });
        ['none', 'personal', 'asesor', 'admin'].forEach(k => select.append(el('option', { value: k, text: ROL[k].nombre })));
        select.value = u.role;
        select.addEventListener('change', () => confirmarRol(u, select.value, () => { select.value = u.role; }));
        return el('div', { class: 'd-flex flex-column gap-1' }, [
            select,
            u.me ? el('span', { class: 'usr-nota', text: 'No puedes quitarte tu propio rol' }) : null,
            u.extras.length ? el('span', {}, [el('span', { class: 'usr-etiqueta usr-extra', text: '+ ' + u.extras_label })]) : null
        ]);
    }

    function celdaCuenta(u) {
        if (u.pending && u.is_active) {
            return el('div', { class: 'd-flex flex-column gap-1' }, [
                el('span', { class: 'usr-etiqueta usr-pendiente align-self-start', text: 'Invitación pendiente' }),
                u.invited_at ? el('span', { class: 'usr-nota', text: 'Enviada ' + hace(u.invited_at) }) : null
            ]);
        }
        const bloqueado = u.owner || u.me;
        const interruptor = el('button', {
            type: 'button', class: 'usr-switch', role: 'switch', 'aria-checked': String(u.is_active),
            'aria-label': 'Cuenta de ' + u.name, disabled: bloqueado,
            title: bloqueado ? (u.owner ? 'El Dueño no se edita desde aquí' : 'No puedes desactivar tu propia cuenta') : null,
            onclick: () => confirmarCuenta(u)
        });
        return el('div', { class: 'd-flex align-items-center gap-2 small' }, [interruptor, el('span', { text: u.is_active ? 'Activa' : 'Desactivada' })]);
    }

    function celdaAcciones(u) {
        if (u.owner) return el('span', { class: 'text-secondary', text: '—' });
        if (u.pending) {
            return el('div', { class: 'd-flex gap-2' }, [
                el('button', { type: 'button', class: 'btn btn-outline-secondary usr-accion', disabled: !u.is_active, text: 'Reenviar invitación', onclick: e => reenviar(u, e.currentTarget) }),
                !u.is_active && !u.me ? el('button', { type: 'button', class: 'btn btn-outline-secondary usr-accion', text: 'Editar', onclick: () => abrirEditar(u) }) : null
            ]);
        }
        return el('div', { class: 'd-flex gap-2' }, [
            u.me ? null : el('button', { type: 'button', class: 'btn btn-outline-secondary usr-accion', text: 'Editar', onclick: () => abrirEditar(u) }),
            el('button', { type: 'button', class: 'btn btn-outline-secondary usr-accion', disabled: !u.is_active, text: 'Enviar enlace de contraseña', onclick: e => enlace(u, e.currentTarget) })
        ]);
    }

    function pintarFilas() {
        const cuerpo = document.getElementById('usrFilas');
        const texto = filtro.trim().toLowerCase();
        const visibles = estado.users.filter(u => !texto || [u.name, u.username, u.email].some(v => (v || '').toLowerCase().includes(texto)));
        if (!visibles.length) {
            cuerpo.replaceChildren(el('tr', {}, [el('td', { colspan: '6', class: 'text-secondary', text: texto ? 'Nadie coincide con la búsqueda.' : 'Aún no hay usuarios.' })]));
            return;
        }
        cuerpo.replaceChildren(...visibles.map(u => el('tr', { class: u.is_active ? '' : 'is-inactiva' }, [
            el('td', {}, [el('div', { class: 'd-flex align-items-center gap-2 usr-atenuar' }, [
                avatar(u),
                el('div', { class: 'd-flex flex-column', style: 'min-width: 0' }, [
                    el('strong', {}, [document.createTextNode(u.name + ' '), u.me ? el('span', { class: 'usr-etiqueta usr-tu', text: 'Tú' }) : null]),
                    el('span', { class: 'small text-secondary', text: u.username })
                ])
            ])]),
            el('td', {}, [el('div', { class: 'usr-correo usr-atenuar', text: u.email || '—', title: u.email || null })]),
            el('td', {}, [celdaRol(u)]),
            el('td', {}, [celdaCuenta(u)]),
            el('td', { class: 'small text-secondary', text: hace(u.last_login) }),
            el('td', {}, [celdaAcciones(u)])
        ])));
    }

    function pintar() { pintarRoles(); pintarFilas(); }

    async function cargar() {
        try {
            estado = await pedir(API);
            pintar();
        } catch (e) {
            document.getElementById('usrFilas').replaceChildren(el('tr', {}, [el('td', { colspan: '6', class: 'text-danger', text: e.message })]));
        }
    }

    function reemplazar(fila) {
        estado.users = estado.users.map(u => (u.id === fila.id ? Object.assign({}, u, fila) : u));
        estado.roles.forEach(r => { r.count = estado.users.filter(u => u.role === r.key).length; });
        pintar();
    }

    // ---------- confirmaciones ----------
    const modalConfirmar = () => bootstrap.Modal.getOrCreateInstance(document.getElementById('usrConfirmar'));
    let alConfirmar = null, alCancelar = null;
    document.getElementById('usrConfirmar').addEventListener('hidden.bs.modal', () => {
        if (alCancelar) alCancelar();
        alConfirmar = alCancelar = null;
    });
    document.getElementById('usrConfirmarSi').addEventListener('click', async e => {
        if (!alConfirmar) return;
        const boton = e.currentTarget;
        boton.disabled = true;
        try {
            await alConfirmar();
            alCancelar = null;
            modalConfirmar().hide();
        } catch (error) {
            aviso(error.message, true);
        } finally {
            boton.disabled = false;
        }
    });
    function abrirConfirmar(titulo, cuerpo, textoBoton, accion, cancelar) {
        document.getElementById('usrConfirmarTitulo').textContent = titulo;
        document.getElementById('usrConfirmarCuerpo').replaceChildren(...cuerpo);
        document.getElementById('usrConfirmarSi').textContent = textoBoton;
        alConfirmar = accion;
        alCancelar = cancelar || null;
        modalConfirmar().show();
    }

    function lista(titulo, codigos, signo, color) {
        if (!codigos.length) return null;
        return el('div', { class: 'd-flex flex-column gap-1' }, [
            el('strong', { class: 'small', text: titulo }),
            ...codigos.map(c => el('span', { class: 'd-flex align-items-center gap-2' }, [el('b', { text: signo, style: 'color: ' + color + '; width: 12px', 'aria-hidden': 'true' }), document.createTextNode(PERMISO[c])]))
        ]);
    }

    function confirmarRol(u, nuevo, revertir) {
        if (nuevo === u.role) return;
        const antes = permisosDeRol(u.role), despues = permisosDeRol(nuevo), extras = new Set(u.extras);
        const conservables = u.extras.filter(c => extrasPermitidos(nuevo).has(c));
        const gana = ORDEN.filter(c => despues.has(c) && !antes.has(c) && !extras.has(c));
        const pierde = ORDEN.filter(c => (antes.has(c) && !despues.has(c)) || (extras.has(c) && !despues.has(c) && !conservables.includes(c)));
        let conservar = false;
        const cuerpo = [
            el('div', { class: 'd-flex align-items-center gap-2 flex-wrap' }, [
                el('span', { class: 'usr-etiqueta', style: 'font-size: 13px; padding: 4px 12px; background: ' + ROL[u.role].tinte + '; color: ' + ROL[u.role].color, text: nombreConExtras(u.role, u.extras) }),
                el('span', { 'aria-hidden': 'true', text: '→' }),
                el('span', { class: 'usr-etiqueta', id: 'usrDestino', style: 'font-size: 13px; padding: 4px 12px; background: ' + ROL[nuevo].tinte + '; color: ' + ROL[nuevo].color, text: ROL[nuevo].nombre })
            ])
        ];
        if (gana.length || pierde.length) {
            cuerpo.push(el('div', { class: 'usr-cambio' }, [lista('Podrá además:', gana, '+', '#00796B'), lista('Dejará de poder:', pierde, '−', '#B42318')]));
        }
        if (conservables.length) {
            const opcion = (valor, titulo, detalle) => el('label', { class: 'usr-opcion' }, [
                el('input', { type: 'radio', name: 'usrConservar', class: 'form-check-input mt-1', checked: valor === conservar, onchange: () => {
                    conservar = valor;
                    document.getElementById('usrDestino').textContent = valor ? nombreConExtras(nuevo, conservables) : ROL[nuevo].nombre;
                    document.getElementById('usrConfirmarSi').textContent = 'Cambiar a ' + (valor ? nombreConExtras(nuevo, conservables) : ROL[nuevo].nombre);
                } }),
                el('span', {}, [el('b', { text: titulo }), el('br'), el('span', { class: 'small text-secondary', text: detalle })])
            ]);
            const nombres = conservables.map(c => PERMISO[c]).join(', ');
            cuerpo.push(el('fieldset', { class: 'd-flex flex-column gap-2' }, [
                el('legend', { class: 'fs-6 fw-bold mb-1', text: (conservables.length === 1 ? 'Tiene un permiso extra: ' : 'Tiene permisos extra: ') + nombres }),
                opcion(true, conservables.length === 1 ? 'Conservarlo' : 'Conservarlos', 'Quedará como «' + nombreConExtras(nuevo, conservables) + '»'),
                opcion(false, conservables.length === 1 ? 'Quitarlo' : 'Quitarlos', 'Quedará solo como «' + ROL[nuevo].nombre + '»')
            ]));
        }
        if (nuevo === 'admin') cuerpo.push(el('div', { class: 'small text-secondary', text: 'Podrá gestionar usuarios y nombrar a otros administradores.' }));
        if (nuevo === 'none') cuerpo.push(el('div', { class: 'small text-secondary', text: 'Podrá iniciar sesión, pero no cotizar ni ver costos.' }));
        cuerpo.push(el('div', { class: 'small text-secondary', text: 'Sus cotizaciones guardadas no cambian. El cambio aplica en su siguiente acción y queda registrado (quién y cuándo).' }));
        abrirConfirmar('Cambiar el rol de ' + u.name, cuerpo, 'Cambiar a ' + ROL[nuevo].nombre, async () => {
            const datos = { role: nuevo };
            if (conservables.length) datos.keep_extras = conservar;
            const fila = await pedir(API + u.id + '/', 'PATCH', datos);
            reemplazar(fila);
            aviso('Rol de ' + u.name + ': ' + nombreConExtras(fila.role, fila.extras));
        }, revertir);
    }

    function confirmarCuenta(u) {
        if (u.is_active) {
            abrirConfirmar('Desactivar la cuenta de ' + u.name, [
                el('p', { class: 'mb-0', text: 'Ya no podrá iniciar sesión y se cierran sus sesiones abiertas. Sus cotizaciones se conservan y se puede reactivar después.' })
            ], 'Desactivar cuenta', async () => {
                reemplazar(await pedir(API + u.id + '/', 'PATCH', { is_active: false }));
                aviso('Cuenta desactivada: ' + u.name);
            });
        } else {
            pedir(API + u.id + '/', 'PATCH', { is_active: true })
                .then(fila => { reemplazar(fila); aviso('Cuenta reactivada: ' + u.name); })
                .catch(e => aviso(e.message, true));
        }
    }

    async function reenviar(u, boton) {
        boton.disabled = true;
        try {
            const r = await pedir(API + u.id + '/invitation/', 'POST');
            avisoCorreo(r.email, u.email, 'Invitación reenviada');
            await cargar();
        } catch (e) {
            aviso(e.message, true);
        } finally {
            boton.disabled = false;
        }
    }

    async function enlace(u, boton) {
        boton.disabled = true;
        try {
            const r = await pedir(API + u.id + '/password-link/', 'POST');
            avisoCorreo(r.email, u.email, 'Enlace para elegir contraseña enviado');
        } catch (e) {
            aviso(e.message, true);
        } finally {
            boton.disabled = false;
        }
    }

    // ---------- Editar ----------
    const panel = () => bootstrap.Offcanvas.getOrCreateInstance(document.getElementById('usrEditar'));
    let edicion = null;

    function pintarEditar() {
        const { usuario, rol, extras } = edicion;
        const delRol = permisosDeRol(rol), permitidos = extrasPermitidos(rol);
        const filas = ORDEN.filter(c => c !== 'manage_commercial_permissions' || rol === 'admin').map(c => {
            if (delRol.has(c)) {
                return el('div', { class: 'usr-linea' }, [el('span', { class: 'flex-grow-1', text: PERMISO[c] }),
                    el('span', { class: 'small fw-semibold', style: 'color: var(--cconor-eleccion)' }, [icono('bi-check-lg'), document.createTextNode(' Incluido en el rol')])]);
            }
            if (!permitidos.has(c)) {
                return el('div', { class: 'usr-linea text-secondary' }, [el('span', { class: 'flex-grow-1', text: PERMISO[c] }), el('span', { class: 'small', text: 'No' })]);
            }
            const activo = extras.has(c);
            return el('div', { class: 'usr-linea' }, [
                el('span', { class: 'flex-grow-1', text: PERMISO[c] }),
                el('span', { class: 'small fw-semibold', style: 'color: ' + (activo ? '#3446B8' : '#8A87A0'), text: activo ? 'Extra' : 'No' }),
                el('button', { type: 'button', class: 'usr-switch is-extra', role: 'switch', 'aria-checked': String(activo), 'aria-label': 'Extra: ' + PERMISO[c],
                    onclick: () => { if (activo) extras.delete(c); else extras.add(c); pintarEditar(); } })
            ]);
        });
        document.getElementById('usrEditarPermisos').replaceChildren(...filas);
        const vigentes = estado.extras.filter(c => extras.has(c) && permitidos.has(c));
        document.getElementById('usrEditarRolNombre').textContent = ROL[rol].nombre;
        document.getElementById('usrEditarEtiqueta').replaceChildren(vigentes.length ? el('span', { class: 'usr-etiqueta usr-extra', text: '+ ' + etiquetaExtras(vigentes) }) : '');
        const cuenta = document.getElementById('usrEditarCuenta');
        cuenta.textContent = usuario.is_active ? 'Desactivar cuenta' : 'Reactivar cuenta';
        cuenta.classList.toggle('text-danger', usuario.is_active);
    }

    function abrirEditar(u) {
        edicion = { usuario: u, rol: u.role, extras: new Set(u.extras) };
        const av = document.getElementById('usrEditarAvatar');
        av.textContent = u.initials;
        av.style.background = ROL[u.role].color;
        document.getElementById('usrEditarTitulo').textContent = 'Editar a ' + u.name;
        document.getElementById('usrEditarSub').textContent = u.username + (u.email ? ' · ' + u.email : '');
        document.getElementById('usrEditarRol').value = u.role;
        pintarEditar();
        panel().show();
    }

    document.getElementById('usrEditarRol').addEventListener('change', e => { edicion.rol = e.target.value; pintarEditar(); });
    document.getElementById('usrEditarGuardar').addEventListener('click', async e => {
        const boton = e.currentTarget, { usuario, rol, extras } = edicion;
        const permitidos = extrasPermitidos(rol);
        boton.disabled = true;
        try {
            const fila = await pedir(API + usuario.id + '/', 'PATCH', { role: rol, extras: estado.extras.filter(c => extras.has(c) && permitidos.has(c)) });
            reemplazar(fila);
            panel().hide();
            aviso('Guardado: ' + usuario.name + ' es ' + nombreConExtras(fila.role, fila.extras));
        } catch (error) {
            aviso(error.message, true);
        } finally {
            boton.disabled = false;
        }
    });
    document.getElementById('usrEditarCuenta').addEventListener('click', () => {
        const u = buscar(edicion.usuario.id);
        panel().hide();
        confirmarCuenta(u);
    });

    // ---------- Invitar ----------
    const modalInvitar = () => bootstrap.Modal.getOrCreateInstance(document.getElementById('usrInvitarModal'));
    const campo = id => document.getElementById(id);
    let usuarioEditado = false;

    function sugerirUsuario() {
        if (usuarioEditado) return;
        const limpio = t => (t || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9]/g, '');
        const nombre = limpio(campo('invNombre').value.trim().split(/\s+/)[0]);
        const apellido = limpio(campo('invApellidos').value.trim().split(/\s+/)[0]);
        campo('invUsuario').value = nombre ? (apellido ? nombre[0] + apellido : nombre) : '';
    }
    campo('invNombre').addEventListener('input', sugerirUsuario);
    campo('invApellidos').addEventListener('input', sugerirUsuario);
    campo('invUsuario').addEventListener('input', () => { usuarioEditado = campo('invUsuario').value.trim() !== ''; });

    document.getElementById('usrInvitar').addEventListener('click', () => {
        document.getElementById('usrInvitarForm').reset();
        usuarioEditado = false;
        campo('invError').hidden = true;
        modalInvitar().show();
    });
    document.getElementById('usrInvitarModal').addEventListener('shown.bs.modal', () => campo('invNombre').focus());

    document.getElementById('usrInvitarForm').addEventListener('submit', async e => {
        e.preventDefault();
        const error = campo('invError');
        const datos = {
            first_name: campo('invNombre').value.trim(), last_name: campo('invApellidos').value.trim(),
            username: campo('invUsuario').value.trim(), email: campo('invCorreo').value.trim(),
            role: (document.querySelector('input[name="invRol"]:checked') || {}).value
        };
        const falta = !datos.first_name ? 'Escribe el nombre.' : !datos.username ? 'Escribe el usuario para entrar.'
            : !campo('invCorreo').checkValidity() || !datos.email ? 'Escribe un correo válido.' : '';
        if (falta) { error.textContent = falta; error.hidden = false; return; }
        const boton = campo('invEnviar');
        boton.disabled = true;
        error.hidden = true;
        try {
            const r = await pedir(API, 'POST', datos);
            modalInvitar().hide();
            if (r.email === 'failed') aviso('Se creó la cuenta de ' + datos.first_name + ', pero no se pudo enviar el correo. Usa «Reenviar invitación».', true);
            else avisoCorreo(r.email, datos.email, 'Invitación enviada');
            await cargar();
        } catch (err) {
            error.textContent = err.message;
            error.hidden = false;
        } finally {
            boton.disabled = false;
        }
    });

    document.getElementById('usrBuscar').addEventListener('input', e => { filtro = e.target.value; pintarFilas(); });

    cargar();
})();
