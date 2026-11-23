const CENTRO = [19.3727, -99.1587];

async function api(ruta, metodo = "GET", cuerpo = null) {
  const r = await fetch(ruta, { method: metodo, headers: { "Content-Type": "application/json" }, body: cuerpo ? JSON.stringify(cuerpo) : undefined });
  let datos = null;
  try { datos = await r.json(); } catch (e) {}
  if (r.status === 401 && location.pathname !== "/login") { location.href = "/login?next=" + encodeURIComponent(location.pathname); return new Promise(() => {}); }
  if (!r.ok) {
    const d = datos && datos.detail;
    const err = new Error(typeof d === "string" ? d : (d && typeof d === "object" && !Array.isArray(d) && d.mensaje ? d.mensaje : (d ? "Datos no válidos" : r.statusText)));
    err.status = r.status;
    err.detalle = d;
    throw err;
  }
  return datos;
}

// Reto de seguridad del registro: si el servidor lo pide (error 428), muestra la pregunta; al reintentar se mandan reto y respuesta.
function pedirReto(e, ids) {
  if (!e || e.status !== 428 || !e.detalle || !e.detalle.reto) return false;
  document.getElementById(ids.bloque).style.display = "block";
  document.getElementById(ids.pregunta).textContent = e.detalle.reto.pregunta;
  document.getElementById(ids.bloque).dataset.token = e.detalle.reto.token;
  document.getElementById(ids.respuesta).value = "";
  return true;
}
function datosReto(ids) {
  return { reto: document.getElementById(ids.bloque).dataset.token || "", reto_respuesta: document.getElementById(ids.respuesta).value };
}

// Se suscribe por WebSocket (con reconexión automática). Devuelve una función para cancelar.
function suscribir(rol, id, alMensaje) {
  let ws = null, cerrado = false;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  function abrir() {
    ws = new WebSocket(`${proto}://${location.host}/ws?rol=${rol}&id=${encodeURIComponent(id || "")}`);
    ws.onmessage = (e) => alMensaje(JSON.parse(e.data));
    ws.onclose = () => { if (!cerrado) setTimeout(abrir, 2000); };
  }
  abrir();
  return () => { cerrado = true; if (ws) ws.close(); };
}

function crearMapa(div, centro = CENTRO, zoom = 14) {
  const m = L.map(div).setView(centro, zoom);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© Colaboradores de OpenStreetMap", referrerPolicy: "strict-origin-when-cross-origin" }).addTo(m);
  return m;
}

function pin(emoji, color) {
  return L.divIcon({ className: "", html: `<div class="pin" style="background:${color}">${emoji}</div>`, iconSize: [34, 34], iconAnchor: [17, 17] });
}

function fmtEta(s) {
  if (s === null || s === undefined) return "calculando…";
  const m = Math.round(s / 60);
  return m < 1 ? "menos de 1 min" : `${m} min`;
}

function esc(t) { const d = document.createElement("div"); d.textContent = t == null ? "" : t; return d.innerHTML; }

// ---- Ayudas para los tipos de prestador, los talleres y los precios
const ICONO_PRESTADOR = { repartidor: "🛵", grua: "🚛" };
const NOMBRE_PRESTADOR = { repartidor: "Repartidor", grua: "Grúa" };
const LEYENDA = "🚗 cliente · 🛵 repartidor · 🚛 grúa · 🛠️ taller";
function marcarTalleres(mapa, talleres) {
  return talleres.map((t) => L.marker([t.lat, t.lon], { icon: pin("🛠️", "#8a5a2b") }).addTo(mapa).bindTooltip(t.nombre));
}
function fmtMXN(n) { return new Intl.NumberFormat("es-MX", { style: "currency", currency: "MXN" }).format(n || 0); }

// ---- Menú común, debajo del encabezado.
//  · Pantalla del cliente: sin menú.
//  · Repartidor, taller y login: un solo botón «Inicio» que regresa a la página principal (lo ve cualquiera).
//  · Páginas del administrador (menú, panel, QR, negocio): «Menú» y «Pantalla principal», solo para el administrador.
(function () {
  const SIN_MENU = ["/cliente"];
  const SOLO_INICIO = ["/repartidor", "/prestador", "/mecanico", "/taller", "/login"];
  const ADMIN = [["/menu", "Menú"], ["/", "Pantalla principal"]];
  function poner() {
    const h = document.querySelector("header");
    const ruta = location.pathname;
    if (!h || document.querySelector("nav.menu") || document.body.hasAttribute("data-sin-menu") || SIN_MENU.includes(ruta) || ruta.startsWith("/q/")) return;
    const dibujar = (enlaces) => {
      if (document.querySelector("nav.menu")) return;
      const nav = document.createElement("nav");
      nav.className = "menu";
      nav.innerHTML = enlaces.map(([href, txt]) => `<a href="${href}"${href === ruta ? ' class="actual" aria-current="page"' : ""}>${txt}</a>`).join("");
      h.insertAdjacentElement("afterend", nav);
    };
    if (SOLO_INICIO.includes(ruta)) return dibujar([["/", "Inicio"]]);
    // Lo demás son páginas del administrador: solo se muestran enlaces en tu computadora (sin contraseña) o con sesión de administrador.
    fetch("/api/sesion").then((r) => r.json()).then((s) => { if (!s.publico || s.admin) dibujar(ADMIN); }).catch(() => {});
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", poner); else poner();
})();
