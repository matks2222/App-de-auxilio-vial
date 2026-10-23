const CENTRO = [19.3727, -99.1587];

async function api(ruta, metodo = "GET", cuerpo = null) {
  const r = await fetch(ruta, { method: metodo, headers: { "Content-Type": "application/json" }, body: cuerpo ? JSON.stringify(cuerpo) : undefined });
  let datos = null;
  try { datos = await r.json(); } catch (e) {}
  if (r.status === 401 && location.pathname !== "/login") { location.href = "/login?next=" + encodeURIComponent(location.pathname); return new Promise(() => {}); }
  if (!r.ok) {
    const d = datos && datos.detail;
    throw new Error(typeof d === "string" ? d : (d ? "Datos no válidos" : r.statusText));
  }
  return datos;
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
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© Colaboradores de OpenStreetMap" }).addTo(m);
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

// ---- Menú común: se inserta debajo del encabezado en todas las páginas para moverse entre ellas
(function () {
  const ENLACES = [["/menu", "🏠", "Inicio"], ["/cliente", "🚗", "Pedir ayuda"], ["/repartidor", "🛵", "Repartidor"], ["/taller", "🛠️", "Taller"], ["/qr", "🔳", "QR"], ["/negocio", "💰", "Negocio"], ["/panel", "📊", "Panel"]];
  const ALIAS = { "/": "/menu", "/prestador": "/repartidor", "/mecanico": "/repartidor" };
  function poner() {
    const h = document.querySelector("header");
    if (!h || document.querySelector("nav.menu") || document.body.hasAttribute("data-sin-menu")) return;
    // El menú lleva a páginas del administrador: solo se muestra en tu computadora (sin contraseña) o a quien inició sesión como administrador.
    // Si no se puede saber, no se muestra.
    fetch("/api/sesion").then((r) => r.json()).then((s) => {
      if (s.publico && !s.admin) return;
      if (document.querySelector("nav.menu")) return;
      const aqui = ALIAS[location.pathname] || location.pathname;
      const nav = document.createElement("nav");
      nav.className = "menu";
      nav.innerHTML = ENLACES.map(([href, ico, txt]) => `<a href="${href}"${href === aqui ? ' class="actual" aria-current="page"' : ""}><span aria-hidden="true">${ico}</span>${txt}</a>`).join("");
      h.insertAdjacentElement("afterend", nav);
      if (s.publico) {                                               // con la app publicada, quien es administrador puede cerrar sesión
        const a = document.createElement("a"); a.href = "#"; a.innerHTML = "<span aria-hidden='true'>🚪</span>Salir";
        a.onclick = async (e) => { e.preventDefault(); await fetch("/api/logout", { method: "POST" }); location.href = "/login"; };
        nav.appendChild(a);
      }
    }).catch(() => {});
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", poner); else poner();
})();
