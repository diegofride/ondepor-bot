/* Service worker de "Reservame ésta!"
   Estrategia conservadora para una app VIVA (reservas cambian todo el tiempo):
   - estáticos (/static/): cache-first (rápidos, cambian poco; el CSS ya versiona por ?v=mtime)
   - páginas: SIEMPRE red; si no hay conexión, aviso simple. NUNCA servir reservas viejas. */
const CACHE = "reservame-v1";

self.addEventListener("install", (e) => self.skipWaiting());
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

/* ---- Notificaciones push: llegan aunque la app esté cerrada ---- */
self.addEventListener("push", (e) => {
  let d = { titulo: "Reservame ésta!", cuerpo: "", url: "/reservas" };
  try {
    if (e.data) d = Object.assign(d, e.data.json());
  } catch (_) {
    if (e.data) d.cuerpo = e.data.text();
  }
  e.waitUntil(
    self.registration.showNotification(d.titulo, {
      body: d.cuerpo,
      icon: "/static/icon-192.png",      // el grande, a color, dentro de la notificación
      badge: "/static/badge-96.png",     // el chico de la barra: Android usa SOLO su silueta
                                         // (alpha) -> debe ser monocromático y transparente
      data: { url: d.url || "/reservas" },
      tag: d.tag || "reserva",
      renotify: true,
      vibrate: [80, 40, 80],
    })
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const destino = (e.notification.data && e.notification.data.url) || "/reservas";
  e.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((lista) => {
      for (const c of lista) {
        if ("focus" in c) {
          c.navigate(destino);
          return c.focus();
        }
      }
      return clients.openWindow(destino);
    })
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;

  if (url.pathname.startsWith("/static/")) {
    e.respondWith(
      caches.open(CACHE).then((c) =>
        c.match(e.request).then(
          (hit) =>
            hit ||
            fetch(e.request).then((resp) => {
              if (resp.ok) c.put(e.request, resp.clone());
              return resp;
            })
        )
      )
    );
    return;
  }

  e.respondWith(
    fetch(e.request).catch(
      () =>
        new Response(
          "<!doctype html><html lang='es'><meta charset='utf-8'>" +
            "<meta name='viewport' content='width=device-width,initial-scale=1'>" +
            "<title>Sin conexión</title>" +
            "<body style='font-family:system-ui;background:#0e2038;color:#fff;display:flex;" +
            "align-items:center;justify-content:center;min-height:100vh;margin:0;text-align:center'>" +
            "<div><h1 style='color:#c7e400'>Sin conexión</h1>" +
            "<p>No hay internet. Volvé a intentar cuando tengas señal.</p>" +
            "<p><a href='/' style='color:#c7e400'>Reintentar</a></p></div></body></html>",
          { headers: { "Content-Type": "text/html; charset=utf-8" } }
        )
    )
  );
});
