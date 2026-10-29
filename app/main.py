"""Servidor de auxilio vial con repartidores y talleres (FastAPI).

Ejecutar:  uvicorn app.main:app --reload
Páginas:   /  /cliente  /repartidor  /taller  /qr  /negocio  /panel
Sin ADMIN_PASSWORD la app está abierta (solo para uso local). Para publicarla en internet ver app/seguridad.py y el README.
AVISO: sin pagos ni verificación de identidad de repartidores.
"""
import asyncio
import io
import ipaddress
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import motor, seguridad
from .estado import CENTRO, Estado

BASE = Path(__file__).parent
estado = Estado(db_path=os.environ.get("DESPACHO_DB", "despacho.db"))
conexiones = []          # (websocket, rol, id)


async def notificar():
    for c in list(conexiones):
        try:
            await c[0].send_json(estado.snapshot(c[1], c[2]))
        except Exception:
            if c in conexiones:
                conexiones.remove(c)


async def bucle():
    """Cada segundo: mueve bots, expira ofertas y, cada ventana_s, despacha el lote pendiente."""
    ultima_version, t_despacho = -1, 0.0
    while True:
        await asyncio.sleep(1.0)
        try:
            estado.tick(1.0)
            estado.expirar_ofertas()
            if time.time() - t_despacho >= estado.config["ventana_s"]:
                estado.paso_despacho()
                t_despacho = time.time()
            if estado.version != ultima_version:
                ultima_version = estado.version
                await notificar()
        except Exception as e:                      # el bucle no debe morir por un error aislado
            print("Error en el bucle de despacho:", repr(e))


@asynccontextmanager
async def lifespan(app):
    log = logging.getLogger("uvicorn.error")
    for nombre in ("ADMIN_PASSWORD", "CODIGO_COLABORADORES"):
        d = seguridad.diagnostico(nombre)
        if d["definida"]:       # solo el largo, nunca el valor: sirve para comparar con lo que escribes al iniciar sesión
            log.info("%s configurada: %d caracteres%s%s", nombre, d["caracteres"],
                     " (traía espacios al inicio o al final: se ignoran)" if d["espacios_extra"] else "",
                     " (traía comillas: se ignoran)" if d["comillas"] else "")
    if not seguridad.modo_publico():
        log.warning("SIN CONTRASEÑA (ADMIN_PASSWORD no está definida): /panel, /qr y /negocio están abiertos. Úsalo solo en tu computadora.")
    elif not seguridad.codigo_colaboradores():
        log.warning("Falta CODIGO_COLABORADORES: cualquiera puede registrarse como repartidor o taller.")
    tarea = asyncio.create_task(bucle())
    yield
    tarea.cancel()


app = FastAPI(title="Auxilio vial con repartidores", lifespan=lifespan)
class EstaticosSinCache(StaticFiles):
    """Sirve los archivos estáticos pidiendo al navegador que los revalide, para no ver copias viejas tras actualizar."""
    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Cache-Control"] = "no-cache"
        return r


app.mount("/static", EstaticosSinCache(directory=BASE / "static"), name="static")

# Rutas del dueño: en modo público piden iniciar sesión. Las del cliente (/cliente, /q/..., pedir y seguir un pedido) quedan abiertas.
ADMIN_PREFIJOS = ("/menu", "/panel", "/qr", "/negocio", "/letrero", "/api/panel", "/api/negocio", "/api/lugares", "/api/ajustes", "/api/cercanos")


@app.middleware("http")
async def proteger(request: Request, call_next):
    ruta = request.url.path
    if ruta.startswith("/static/") and ruta.endswith(".html"):          # las páginas se sirven por sus rutas, no como archivos sueltos
        return Response(status_code=404)
    if seguridad.modo_publico() and ruta.startswith(ADMIN_PREFIJOS) and not seguridad.es_admin(request.cookies):
        if ruta.startswith("/api/"):
            return JSONResponse({"detail": "Inicia sesión como administrador"}, status_code=401)
        return RedirectResponse("/login?next=" + quote(ruta), status_code=302)
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    # Con "same-origin" los mapas de OpenStreetMap respondían 403: exigen saber de qué sitio viene la petición (su política de uso de mapas).
    # Esta política manda solo el dominio (nunca la ruta) a otros sitios y la ruta completa dentro del tuyo.
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp


def exigir_codigo(codigo: str, request: Request):
    """Registro de repartidores y talleres: con CODIGO_COLABORADORES definido, hay que dar el código (con límite de intentos)."""
    ip = seguridad.ip_cliente(request)
    if seguridad.limitador.excedido("codigo:" + ip, 10, 600):
        raise HTTPException(429, "Demasiados intentos. Espera unos minutos.")
    if not seguridad.codigo_ok(codigo):
        seguridad.limitador.anotar("codigo:" + ip)
        raise HTTPException(403, "Código de acceso incorrecto")


def corre(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except KeyError as e:
        raise HTTPException(404, str(e.args[0]))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ modelos
class NuevaSolicitud(BaseModel):
    nombre: str = Field("", max_length=60)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    tipo: str
    via: str = "calle"
    notas: str = Field("", max_length=300)
    riesgo: bool = False
    opcion: str | None = Field(None, max_length=20)       # combustible (magna, premium, diesel) o tipo de batería
    cantidad: int | None = Field(None, ge=1, le=100)      # litros, solo para gasolina
    lugar: str = Field("", max_length=60)                 # letrero (QR) que escaneó la persona, si vino de uno
    placa: str = Field("", max_length=15)                 # placa del auto del cliente (obligatoria): así el repartidor lo reconoce
    color: str = Field("", max_length=20)                 # color del auto del cliente (obligatorio)


class Calificacion(BaseModel):
    estrellas: int
    comentario: str = Field("", max_length=300)


class VehiculoIn(BaseModel):
    modelo: str = Field("", max_length=40)                 # marca y modelo, ej. "Italika FT150"
    tipo: str = Field("", max_length=20)
    color: str = Field("", max_length=20)
    placa: str = Field("", max_length=15)


class NuevoRepartidor(BaseModel):
    vehiculo: VehiculoIn | None = None
    codigo: str = Field("", max_length=100)
    nombre: str = Field("", max_length=60)
    habilidades: list[str]
    prestador: str = "repartidor"


class Turno(BaseModel):
    activo: bool
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)


class Ubicacion(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class Respuesta(BaseModel):
    solicitud_id: str
    acepta: bool


class Accion(BaseModel):
    accion: str
    nota: str = Field("", max_length=300)
    pin: str = Field("", max_length=8)                      # PIN que el cliente le da al repartidor al llegar


class Config(BaseModel):
    politica: str | None = None
    ventana_s: float | None = Field(None, ge=1, le=120)
    oferta_s: float | None = Field(None, ge=5, le=300)
    radio_km: float | None = Field(None, ge=0.5, le=50)
    rechazo_bot: float | None = Field(None, ge=0, le=1)
    vel_bot_ms: float | None = Field(None, ge=1, le=200)
    prob_escalar: float | None = Field(None, ge=0, le=1)
    reoferta_s: float | None = Field(None, ge=0, le=3600)
    espera_max_s: float | None = Field(None, ge=30, le=86400)


class Login(BaseModel):
    password: str = Field("", max_length=200)


class Cantidad(BaseModel):
    n: int = Field(5, ge=1, le=100)
    prestador: str = "repartidor"


class NuevoTaller(BaseModel):
    codigo: str = Field("", max_length=100)
    nombre: str = Field("", max_length=60)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    telefono: str = Field("", max_length=30)
    direccion: str = Field("", max_length=120)


class DatosTaller(BaseModel):
    nombre: str | None = Field(None, max_length=60)
    telefono: str | None = Field(None, max_length=30)
    direccion: str | None = Field(None, max_length=120)
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)


class Combustible(BaseModel):
    litro: float
    disponible: bool = False


class Bateria(BaseModel):
    id: str | None = Field(None, max_length=12)
    nombre: str = Field(max_length=60)
    categoria: str
    precio: float
    stock: int


class CatalogoTaller(BaseModel):
    combustibles: dict[str, Combustible] = {}
    baterias: list[Bateria] = []


class NuevoLugar(BaseModel):
    nombre: str = Field("", max_length=60)
    lat: float | None = Field(None, ge=-90, le=90)         # sin coordenadas = QR general (tarjetas, imanes)
    lon: float | None = Field(None, ge=-180, le=180)


class Ajustes(BaseModel):
    url_publica: str | None = Field(None, max_length=200)
    whatsapp: str | None = Field(None, max_length=30)
    telefono: str | None = Field(None, max_length=30)


class PreciosReferencia(BaseModel):
    combustibles: dict[str, float] | None = None      # pesos por litro
    baterias: dict[str, float] | None = None          # pesos por batería
    servicios: dict[str, float] | None = None         # cargo por servicio a domicilio, por tipo de servicio


class Costos(BaseModel):
    hora_repartidor: float | None = None
    costo_km: float | None = None
    km_ida_vuelta: float | None = None
    margen_objetivo: float | None = None
    gastos_fijos_mes: float | None = None
    comision_pago: float | None = None
    minutos: dict[str, float] | None = None
    kit: dict[str, float] | None = None
    vida_util_servicios: dict[str, float] | None = None


# ------------------------------------------------------------------ API
@app.get("/api/config")
def api_config():
    activos = estado.config["servicios_activos"]
    prestadores = {k: dict(v, permitidas=[h for h in v["permitidas"] if h in activos], por_defecto=[h for h in v["por_defecto"] if h in activos])
                   for k, v in motor.PRESTADORES.items()}
    prestadores = {k: v for k, v in prestadores.items() if v["permitidas"]}          # un rol sin servicios activos no se ofrece
    return dict(tipos={k: v for k, v in motor.TIPOS.items() if k in activos}, niveles=motor.NIVEL_SERVICIO, vias=motor.VIAS,
                politicas=motor.POLITICAS, centro=CENTRO, servicios_activos=activos,
                prestadores=prestadores, precios=estado.config["precios"],
                litros=dict(min=motor.LITROS_MIN, max=motor.LITROS_MAX, defecto=motor.LITROS_DEFAULT),
                contacto={k: v for k, v in estado.ajustes().items() if k != "url_publica"},
                requiere_codigo=bool(seguridad.codigo_colaboradores()),
                colores=motor.COLORES, tipos_vehiculo=motor.TIPOS_VEHICULO)


@app.get("/api/talleres")
def api_talleres(demo: bool = False):
    """Talleres registrados. Los de demostración (ficticios) solo salen si se piden con ?demo=true."""
    return [estado._pub_taller(t) for t in estado.talleres.values() if demo or not t["demo"]]


@app.post("/api/talleres")
async def api_nuevo_taller(d: NuevoTaller, request: Request):
    exigir_codigo(d.codigo, request)
    if not d.nombre.strip():
        raise HTTPException(400, "Escribe el nombre del taller")
    t = estado.agregar_taller(d.nombre, d.lat, d.lon, d.telefono, d.direccion)
    await notificar()
    return dict(id=t["id"])


@app.get("/api/talleres/{tid}")
def api_ver_taller(tid: str):
    corre(estado._t, tid)
    return estado.snapshot("taller", tid)


@app.post("/api/talleres/{tid}/datos")
async def api_datos_taller(tid: str, d: DatosTaller):
    corre(estado.actualizar_taller, tid, d.nombre, d.telefono, d.direccion, d.lat, d.lon)
    await notificar()
    return {"ok": True}


@app.post("/api/talleres/{tid}/catalogo")
async def api_catalogo_taller(tid: str, d: CatalogoTaller):
    corre(estado.actualizar_catalogo, tid, {k: v.model_dump() for k, v in d.combustibles.items()},
          [b.model_dump() for b in d.baterias])
    await notificar()
    return {"ok": True}


@app.get("/api/cotizar")
def api_cotizar(lat: float, lon: float, tipo: str, opcion: str | None = None, cantidad: int | None = None):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(422, "Coordenadas no válidas")
    if tipo not in motor.TIPOS:
        raise HTTPException(400, "Tipo de falla no válido")
    return corre(estado.cotizar, tipo, lat, lon, opcion, cantidad)       # None si el servicio no lleva producto


@app.get("/api/cercanos")
def api_cercanos(lat: float, lon: float, tipo: str | None = None):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(422, "Coordenadas no válidas")
    if tipo is not None and tipo not in motor.TIPOS:
        raise HTTPException(400, "Tipo de falla no válido")
    return estado.cercanos(lat, lon, tipo)


@app.post("/api/solicitudes")
async def crear_solicitud(d: NuevaSolicitud, request: Request):
    if d.tipo not in estado.config["servicios_activos"]:
        raise HTTPException(400, "Ese servicio no está disponible")
    if len(" ".join(d.nombre.split())) < 2:
        raise HTTPException(400, "Escribe tu nombre")
    if not d.placa.strip():
        raise HTTPException(400, "Escribe la placa de tu auto")
    if not d.color.strip():
        raise HTTPException(400, "Elige el color de tu auto")
    if seguridad.modo_publico() and not seguridad.limitador.permitir("pedido:" + seguridad.ip_cliente(request), seguridad.limite_pedidos_hora(), 3600):
        raise HTTPException(429, "Hiciste varios pedidos seguidos desde esta conexión. Si es una emergencia, llámanos.")
    s = corre(estado.crear_solicitud, " ".join(d.nombre.split()), d.lat, d.lon, d.tipo, d.via, d.notas, d.riesgo,
              opcion=d.opcion, cantidad=d.cantidad, lugar=d.lugar, placa=d.placa, color=d.color)
    await notificar()
    return dict(id=s["id"], prioridad=s["prioridad"], cotizacion=s["cotizacion"])


@app.get("/api/solicitudes/{sid}")
def ver_solicitud(sid: str):
    corre(estado._s, sid)
    return estado.snapshot("cliente", sid)


@app.post("/api/solicitudes/{sid}/cancelar")
async def cancelar(sid: str):
    corre(estado.cancelar_solicitud, sid)
    await notificar()
    return {"ok": True}


@app.post("/api/solicitudes/{sid}/calificar")
async def calificar(sid: str, d: Calificacion):
    corre(estado.calificar, sid, d.estrellas, d.comentario)
    await notificar()
    return {"ok": True}


@app.post("/api/repartidores")
async def crear_repartidor(d: NuevoRepartidor, request: Request):
    exigir_codigo(d.codigo, request)
    habilidades = [h for h in d.habilidades if h in estado.config["servicios_activos"]]
    if len(" ".join(d.nombre.split())) < 2:
        raise HTTPException(400, "Escribe tu nombre completo")
    if d.vehiculo is None:
        raise HTTPException(400, "Registra tu vehículo: tipo, marca y modelo, color y placa")
    m = corre(estado.registrar_repartidor, " ".join(d.nombre.split()), habilidades, prestador=d.prestador, vehiculo=d.vehiculo.model_dump())
    return dict(id=m["id"])


@app.get("/api/repartidores/{mid}")
def ver_repartidor(mid: str):
    corre(estado._m, mid)
    return estado.snapshot("repartidor", mid)


@app.post("/api/repartidores/{mid}/turno")
async def turno(mid: str, d: Turno):
    corre(estado.turno, mid, d.activo, d.lat, d.lon)
    await notificar()
    return {"ok": True}


@app.post("/api/repartidores/{mid}/ubicacion")
async def ubicacion(mid: str, d: Ubicacion):
    corre(estado.ubicacion, mid, d.lat, d.lon)
    return {"ok": True}


@app.post("/api/repartidores/{mid}/respuesta")
async def respuesta(mid: str, d: Respuesta):
    corre(estado.responder_oferta, mid, d.solicitud_id, d.acepta)
    await notificar()
    return {"ok": True}


@app.post("/api/repartidores/{mid}/estado")
async def avanzar(mid: str, d: Accion):
    corre(estado.avanzar, mid, d.accion, d.nota, d.pin)
    await notificar()
    return {"ok": True}


@app.get("/api/panel/estado")
def panel_estado():
    return estado.panel()


@app.post("/api/panel/config")
async def panel_config(d: Config):
    if d.politica is not None:
        if d.politica not in motor.POLITICAS:
            raise HTTPException(400, "Política no válida")
        estado.config["politica"] = d.politica
    for campo in ("ventana_s", "oferta_s", "radio_km", "rechazo_bot", "vel_bot_ms", "prob_escalar", "reoferta_s", "espera_max_s"):
        v = getattr(d, campo)
        if v is not None:
            estado.config[campo] = v
    estado.version += 1
    await notificar()
    return {"ok": True}


# ------------------------------------------------------------------ QR, letreros y contacto
def base_publica(request: Request) -> str:
    """Dirección que codifican los QR: la configurada o, si no hay, la desde la que se está usando el panel."""
    return estado.ajustes()["url_publica"] or str(request.base_url).rstrip("/")


def es_publica(base: str) -> bool:
    """True si la dirección sirve desde el celular de otra persona (https y no es localhost ni red local)."""
    host = urlparse(base).hostname or ""
    if not base.startswith("https://") or host in ("", "localhost") or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local)
    except ValueError:
        return True


def qr_svg(texto: str) -> bytes:
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
    except ImportError:
        raise HTTPException(503, "Falta la librería de QR. Ejecuta: python -m pip install -r requirements.txt")
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_Q, border=4, box_size=10)   # nivel Q: aguanta letreros sucios o rayados
    qr.add_data(texto)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(image_factory=SvgPathImage).save(buf)
    return buf.getvalue()


@app.get("/api/lugares")
def api_lugares(request: Request):
    base = base_publica(request)
    return dict(base_url=base, publica=es_publica(base), ajustes=estado.ajustes(),
                lugares=[dict(l, url=f"{base}/q/{l['codigo']}") for l in estado.listar_lugares()])


@app.post("/api/lugares")
def api_crear_lugar(d: NuevoLugar):
    return corre(estado.crear_lugar, d.nombre, d.lat, d.lon)


@app.get("/api/lugares/{codigo}")
def api_lugar(codigo: str, request: Request):
    l = estado.lugar(codigo)
    if not l:
        raise HTTPException(404, "Lugar no encontrado")
    a = estado.ajustes()
    return dict(l, url=f"{base_publica(request)}/q/{codigo}", contacto=dict(whatsapp=a["whatsapp"], telefono=a["telefono"]))


@app.delete("/api/lugares/{codigo}")
def api_borrar_lugar(codigo: str):
    corre(estado.borrar_lugar, codigo)
    return {"ok": True}


@app.get("/api/ajustes")
def api_ajustes():
    return estado.ajustes()


@app.post("/api/ajustes")
def api_guardar_ajustes(d: Ajustes):
    return corre(estado.guardar_ajustes, d.url_publica, d.whatsapp, d.telefono)


@app.get("/q/{codigo}", include_in_schema=False)
def ir_a_lugar(codigo: str):
    """Destino corto de cada QR: cuenta el escaneo y manda al formulario de pedir ayuda con el lugar ya marcado.
    Un código desconocido (por ejemplo, un lugar borrado) lleva al formulario sin ubicación: nadie se queda sin ayuda."""
    l = estado.escanear(codigo)
    if not l:
        return RedirectResponse("/cliente", status_code=302)
    params = {"lugar": l["nombre"]}
    if not l["general"]:
        params.update(lat=f"{l['lat']:.6f}", lon=f"{l['lon']:.6f}")
    return RedirectResponse("/cliente?" + urlencode(params), status_code=302)


@app.get("/qr/{codigo}.svg", include_in_schema=False)
def imagen_qr(codigo: str, request: Request):
    if not estado.lugar(codigo):
        raise HTTPException(404, "Lugar no encontrado")
    return Response(qr_svg(f"{base_publica(request)}/q/{codigo}"), media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})


@app.get("/api/negocio")
def api_negocio(reales: bool = False):
    return estado.negocio(reales)


@app.post("/api/negocio")
def api_guardar_negocio(d: Costos, reales: bool = False):
    corre(estado.guardar_costos, d.model_dump(exclude_none=True))
    return estado.negocio(reales)


@app.post("/api/panel/precios")
async def panel_precios(d: PreciosReferencia):
    corre(estado.actualizar_precios_referencia, d.combustibles, d.baterias, d.servicios)
    await notificar()
    return estado.config["precios"]


@app.post("/api/panel/bots")
async def panel_bots(d: Cantidad):
    corre(estado.crear_bots, d.n, d.prestador)
    await notificar()
    return {"ok": True}


@app.delete("/api/panel/bots")
async def panel_quitar_bots():
    estado.quitar_bots()
    await notificar()
    return {"ok": True}


@app.post("/api/panel/taller_demo")
async def panel_taller_demo():
    lat, lon = estado._punto_aleatorio(3000)
    t = estado.agregar_taller(f"Taller demo {len(estado.talleres) + 1}", lat, lon, demo=True)
    await notificar()
    return dict(id=t["id"])


@app.post("/api/panel/solicitud_aleatoria")
async def panel_solicitud_aleatoria():
    s = estado.solicitud_aleatoria()
    await notificar()
    return dict(id=s["id"])


@app.post("/api/panel/reiniciar")
async def panel_reiniciar():
    estado.reiniciar()
    await notificar()
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, rol: str = "panel", id: str = ""):
    if rol not in seguridad.ROLES_PUBLICOS and not seguridad.es_admin(ws.cookies):      # cualquier otro rol entrega el panel completo
        await ws.close(code=4401)
        return
    await ws.accept()
    conexiones.append((ws, rol, id))
    try:
        await ws.send_json(estado.snapshot(rol, id))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        conexiones[:] = [c for c in conexiones if c[0] is not ws]


# ------------------------------------------------------------------ páginas
PAGINAS = [("/menu", "index.html"), ("/login", "login.html"), ("/cliente", "cliente.html"), ("/repartidor", "repartidor.html"),
           ("/prestador", "repartidor.html"), ("/mecanico", "repartidor.html"),      # rutas anteriores, siguen funcionando
           ("/taller", "taller.html"), ("/qr", "qr.html"), ("/negocio", "negocio.html"), ("/panel", "panel.html")]
for ruta, archivo in PAGINAS:
    app.add_api_route(ruta, (lambda a=archivo: FileResponse(BASE / "static" / a, headers={"Cache-Control": "no-cache"})), methods=["GET"], include_in_schema=False)


@app.get("/letrero/{codigo}", include_in_schema=False)
def pagina_letrero(codigo: str):
    return FileResponse(BASE / "static" / "letrero.html", headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
def inicio():
    """Quien escribe la dirección de la app llega a pedir ayuda; el menú del dueño queda en /menu. En tu computadora (sin contraseña) / sigue siendo el menú."""
    return FileResponse(BASE / "static" / ("cliente.html" if seguridad.modo_publico() else "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/salud", include_in_schema=False)
def salud():
    return {"ok": True}                                       # para que el hosting sepa que la app está viva


@app.post("/api/login")
def api_login(d: Login, request: Request):
    if not seguridad.modo_publico():
        return {"ok": True, "sin_contrasena": True}
    ip = seguridad.ip_cliente(request)
    if seguridad.limitador.excedido("login:" + ip, 5, 600):
        raise HTTPException(429, "Demasiados intentos. Espera unos minutos.")
    if not seguridad.password_ok(d.password):
        seguridad.limitador.anotar("login:" + ip)
        logging.getLogger("uvicorn.error").warning("Inicio de sesión fallido: se escribieron %d caracteres (la contraseña configurada tiene %d)",
                                                   len(d.password.strip()), seguridad.diagnostico("ADMIN_PASSWORD")["caracteres"])
        raise HTTPException(401, "Contraseña incorrecta")
    resp = JSONResponse({"ok": True})
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(seguridad.COOKIE, seguridad.token_admin(), max_age=seguridad.DURACION_SESION_S, httponly=True, samesite="lax", secure=https)
    return resp


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(seguridad.COOKIE)
    return resp


@app.get("/api/sesion")
def api_sesion(request: Request):
    return {"publico": seguridad.modo_publico(), "admin": seguridad.es_admin(request.cookies)}
