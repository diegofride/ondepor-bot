// Huella / Windows Hello (WebAuthn) para "Reservame ésta!" — mismo esquema que
// el Jalomi Manager. La huella NUNCA sale del dispositivo: el navegador firma un
// desafío del server con una clave que solo él tiene. Acá solo se traducen los
// campos binarios a base64url (que es lo que viaja por JSON) y viceversa.
window.Passkeys = (function () {
  function b64aBuf(txt) {
    var s = txt.replace(/-/g, '+').replace(/_/g, '/');
    s += '='.repeat((4 - s.length % 4) % 4);
    var bin = atob(s), buf = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    return buf.buffer;
  }
  function bufAB64(buf) {
    var bytes = new Uint8Array(buf), bin = '';
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  async function post(url, cuerpo) {
    var cab = { 'Accept': 'application/json' };
    if (cuerpo) cab['Content-Type'] = 'application/json';
    var r = await fetch(url, {
      method: 'POST', headers: cab,
      body: cuerpo ? JSON.stringify(cuerpo) : null
    });
    var data = null;
    try { data = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error((data && data.detail) || 'Error del servidor');
    return data;
  }

  var MARCA = 'reservame_passkey';   // este navegador ya activó la huella

  function traducir(e) {
    var t = (e && (e.message || e.name) || '') + '';
    if (/credential manager|NotAllowedError|not allowed|no credentials|empty allow/i.test(t))
      return 'No hay ninguna huella activada en este dispositivo (o cancelaste). ' +
             'Entrá con tu usuario y contraseña y activala desde "Huella".';
    if (/NotSupportedError|not supported/i.test(t))
      return 'Este dispositivo no soporta huella para entrar. Usá tu contraseña.';
    if (/InvalidStateError/i.test(t))
      return 'Este dispositivo YA está activado para tu usuario.';
    if (/SecurityError/i.test(t))
      return 'La huella solo funciona desde el dominio de la app (conexión segura).';
    return t || 'No se pudo usar la huella';
  }

  return {
    traducir: traducir,
    // ¿ESTE navegador registró alguna vez la huella? (el server no puede
    // saberlo antes de entrar: por privacidad no dice quién tiene passkey)
    activadaAca: function () {
      try { return localStorage.getItem(MARCA) === '1'; } catch (e) { return false; }
    },
    marcarActivada: function (si) {
      try {
        if (si) localStorage.setItem(MARCA, '1'); else localStorage.removeItem(MARCA);
      } catch (e) {}
    },

    soportado: function () {
      return !!(window.PublicKeyCredential && navigator.credentials &&
                window.isSecureContext);
    },

    // ¿este aparato puede pedir huella/cara/PIN? (Android, Windows Hello, Touch ID)
    disponibleEnEsteAparato: async function () {
      if (!this.soportado()) return false;
      try {
        return await PublicKeyCredential
          .isUserVerifyingPlatformAuthenticatorAvailable();
      } catch (e) { return false; }
    },

    // registrar la huella de ESTE dispositivo (hay que estar logueado)
    registrar: async function () {
      var op = await post('/passkeys/registro/opciones');
      op.challenge = b64aBuf(op.challenge);
      op.user.id = b64aBuf(op.user.id);
      (op.excludeCredentials || []).forEach(function (c) { c.id = b64aBuf(c.id); });
      var cred = await navigator.credentials.create({ publicKey: op });
      this.marcarActivada(true);
      return await post('/passkeys/registro/verificar', {
        id: cred.id, rawId: bufAB64(cred.rawId), type: cred.type,
        response: {
          clientDataJSON: bufAB64(cred.response.clientDataJSON),
          attestationObject: bufAB64(cred.response.attestationObject)
        }
      });
    },

    // entrar con la huella (sin sesión)
    entrar: async function () {
      var op = await post('/passkeys/login/opciones');
      op.challenge = b64aBuf(op.challenge);
      (op.allowCredentials || []).forEach(function (c) { c.id = b64aBuf(c.id); });
      var cred = await navigator.credentials.get({ publicKey: op });
      return await post('/passkeys/login/verificar', {
        id: cred.id, rawId: bufAB64(cred.rawId), type: cred.type,
        response: {
          clientDataJSON: bufAB64(cred.response.clientDataJSON),
          authenticatorData: bufAB64(cred.response.authenticatorData),
          signature: bufAB64(cred.response.signature),
          userHandle: cred.response.userHandle
            ? bufAB64(cred.response.userHandle) : null
        }
      });
    }
  };
})();
