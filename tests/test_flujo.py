import math
import os
import sys

os.environ["DESPACHO_DB"] = ":memory:"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from fastapi.testclient import TestClient

from app import motor
from app.estado import Estado
from app.main import app

LAT0, LON0 = 19.3727, -99.1587
KM_LON = 1 / (111.32 * math.cos(math.radians(LAT0)))     # grados de longitud por km


class Reloj:
    def __init__(self):
        self.t = 1_700_000_000.0

    def __call__(self):
        return self.t


def punto(km_este=0.0, km_norte=0.0):
    return LAT0 + km_norte / 111.32, LON0 + km_este * KM_LON


@pytest.fixture
def est():
    r = Reloj()
    e = Estado(":memory:", reloj=r, semilla=1)
    e.r = r
    return e


def con_todos_los_servicios(est):
    """Hoy solo se ofrecen gasolina, aire y paso de corriente; la grúa y la escalada siguen en el código, apagadas."""
    est.config["servicios_activos"] = list(motor.TIPOS)
    return est


def mec(est, nombre="M", hab=("paso_de_corriente", "cambio_de_llanta"), km=(0, 0), prestador=None):
    if prestador is None:
        prestador = "grua" if "grua" in hab else "repartidor"
    m = est.registrar_repartidor(nombre, list(hab), prestador=prestador)
    lat, lon = punto(*km)
    est.turno(m["id"], True, lat, lon)
    return m


def sol(est, km=(1, 0), tipo="paso_de_corriente", via="calle"):
    lat, lon = punto(*km)
    return est.crear_solicitud("Ana", lat, lon, tipo, via)


def atender(est, m, s, accion="termine", nota=""):
    est.paso_despacho()
    est.responder_oferta(m["id"], s["id"], True)
    est.avanzar(m["id"], "llegue")
    est.avanzar(m["id"], accion, nota)


# ------------------------------------------------------------------ flujo básico
def test_flujo_completo(est):
    m = mec(est)
    s = sol(est)
    assert est.paso_despacho() == [(s["id"], m["id"])]
    assert s["estado"] == "ofertada" and m["estado"] == "ofertado" and s["eta_s"] > 0
    est.responder_oferta(m["id"], s["id"], True)
    assert s["estado"] == "aceptada" and m["estado"] == "ocupado"
    est.r.t += 300
    est.avanzar(m["id"], "llegue")
    assert s["estado"] == "en_sitio" and s["t_llegada"] - s["t_creada"] == 300
    est.avanzar(m["id"], "termine")
    assert s["estado"] == "terminada" and s["resultado"] == "resuelta" and m["estado"] == "libre" and m["servicios"] == 1
    assert (m["lat"], m["lon"]) == (s["lat"], s["lon"])
    est.calificar(s["id"], 5)
    assert s["calificacion"] == 5


def test_habilidad_requerida(est):
    mec(est, hab=("paso_de_corriente",))
    s = sol(est, tipo="grua")
    assert est.paso_despacho() == [] and s["estado"] == "pendiente"
    g = mec(est, "G", hab=("grua",), km=(3, 0))
    assert est.paso_despacho() == [(s["id"], g["id"])]


def test_repartidor_no_puede_tener_habilidades_de_grua(est):
    with pytest.raises(ValueError):
        est.registrar_repartidor("X", ["grua"], prestador="repartidor")
    m = est.registrar_repartidor("Y", ["aire_llantas", "grua"], prestador="repartidor")
    assert m["habilidades"] == ["aire_llantas"]


def test_rechazo_reasigna_a_otro(est):
    m1, m2 = mec(est, "cerca", km=(0.5, 0)), mec(est, "lejos", km=(4, 0))
    s = sol(est)
    est.paso_despacho()
    assert s["repartidor_id"] == m1["id"]
    est.responder_oferta(m1["id"], s["id"], False)
    assert s["estado"] == "pendiente" and m1["id"] in s["excluidos"] and m1["estado"] == "libre"
    est.paso_despacho()
    assert s["repartidor_id"] == m2["id"]


def test_oferta_que_expira_se_vuelve_a_ofrecer_al_mismo_repartidor(est):
    """Antes, dejar pasar 30 s lo excluía para siempre y la petición se quedaba atorada."""
    m = mec(est)
    s = sol(est)
    est.paso_despacho()
    est.r.t += est.config["oferta_s"] + 1
    est.expirar_ofertas()
    assert s["estado"] == "pendiente" and m["estado"] == "libre" and not s["excluidos"]
    assert est.paso_despacho() == [(s["id"], m["id"])]


def test_salir_y_entrar_de_turno_con_oferta_abierta_no_aparta(est):
    m = mec(est)
    s = sol(est)
    est.paso_despacho()
    est.turno(m["id"], False)
    lat, lon = punto(0, 0)
    est.turno(m["id"], True, lat, lon)
    assert s["estado"] == "pendiente" and not s["excluidos"]
    assert est.paso_despacho() == [(s["id"], m["id"])]


def test_rechazo_aparta_por_un_rato_y_luego_se_vuelve_a_ofrecer(est):
    m = mec(est)
    s = sol(est)
    est.paso_despacho()
    est.responder_oferta(m["id"], s["id"], False)
    assert est.paso_despacho() == []                             # recién dijo que no: no se le insiste
    est.r.t += est.config["reoferta_s"] + 1
    assert est.paso_despacho() == [(s["id"], m["id"])]           # pasado el tiempo y sin nadie más, se le vuelve a ofrecer


def test_peticion_que_nadie_atiende_se_cierra_y_no_estorba(est):
    vieja = sol(est, km=(0.2, 0))
    est.r.t += est.config["espera_max_s"] + 1
    nueva = sol(est, km=(0.5, 0))
    m = mec(est)
    est.expirar_ofertas()
    assert vieja["estado"] == "cancelada" and vieja["resultado"] == "sin_cobertura" and nueva["estado"] == "pendiente"
    assert est.paso_despacho() == [(nueva["id"], m["id"])]       # la oferta es para la petición nueva, no para la vieja


def test_repartidor_ve_cuantos_pedidos_esperan(est):
    m = mec(est, km=(0, 0))
    sol(est, km=(1, 0))                                          # dentro de su radio
    sol(est, km=(30, 0))                                         # muy lejos
    esp = est.snapshot("repartidor", m["id"])["repartidor"]["espera"]
    assert esp["cerca"] == 1 and esp["lejos"] == 1 and esp["pausa"] == 0
    est.paso_despacho()
    est.responder_oferta(m["id"], m["solicitud_id"], False)
    esp = est.snapshot("repartidor", m["id"])["repartidor"]["espera"]
    assert esp["pausa"] == 1 and esp["cerca"] == 0


def test_cancelar_libera_prestador(est):
    m = mec(est)
    s = sol(est)
    est.paso_despacho()
    est.responder_oferta(m["id"], s["id"], True)
    est.cancelar_solicitud(s["id"])
    assert s["estado"] == "cancelada" and m["estado"] == "libre" and m["solicitud_id"] is None


def test_no_se_puede_salir_de_turno_ocupado(est):
    m = mec(est)
    s = sol(est)
    est.paso_despacho()
    est.responder_oferta(m["id"], s["id"], True)
    with pytest.raises(ValueError):
        est.turno(m["id"], False)


# ------------------------------------------------------------------ niveles y escalamiento
def test_servicios_del_primer_nivel_incluyen_aire_bateria_y_gasolina():
    assert {"aire_llantas", "bateria_nueva", "gasolina"} <= set(motor.SIMPLES)
    assert motor.NIVEL_SERVICIO["grua"] == 2 and "mecanica_menor" not in motor.TIPOS
    assert set(motor.PRESTADORES) == {"repartidor", "grua"}                 # ya no hay rol de mecánico


def test_repartidor_no_necesita_ser_mecanico_ni_puede_dar_grua():
    m = est_nuevo().registrar_repartidor("Beto", ["paso_de_corriente", "gasolina", "grua"], prestador="repartidor")
    assert m["habilidades"] == ["paso_de_corriente", "gasolina"]


def est_nuevo():
    return Estado(":memory:", reloj=Reloj(), semilla=1)


def test_escalar_a_grua_y_taller_sugerido(est):
    con_todos_los_servicios(est)
    a = mec(est, "repartidor", hab=("diagnostico", "aire_llantas"))
    s = sol(est, tipo="diagnostico", via="calle")
    p0 = s["prioridad"]
    atender(est, a, s, "escalar_grua", "Humo del motor")
    assert s["estado"] == "escalada" and s["resultado"] == "escalada" and a["estado"] == "libre"
    h = est.solicitudes[s["hija_id"]]
    assert h["tipo"] == "grua" and h["padre_id"] == s["id"] and h["estado"] == "pendiente"
    assert (h["lat"], h["lon"]) == (s["lat"], s["lon"]) and h["prioridad"] == min(3, p0 + 1)
    assert "Humo del motor" in h["notas"]
    pub = est.snapshot("cliente", h["id"])["solicitud"]
    assert pub["taller"] and pub["taller"]["distancia_km"] >= 0
    # la atiende una grúa, no el repartidor
    assert est.paso_despacho() == []
    g = mec(est, "grua", hab=("grua",), km=(2, 0))
    assert est.paso_despacho() == [(h["id"], g["id"])]


def test_opciones_de_escalamiento_por_nivel(est):
    con_todos_los_servicios(est)
    a = mec(est, "a", hab=("aire_llantas",))
    g = mec(est, "g", hab=("grua",))
    assert est.opciones_escalar(a) == ["escalar_grua"]
    assert est.opciones_escalar(g) == []
    s = sol(est, tipo="grua")
    est.paso_despacho(); est.responder_oferta(g["id"], s["id"], True); est.avanzar(g["id"], "llegue")
    with pytest.raises(ValueError):
        est.avanzar(g["id"], "escalar_grua")                      # la grúa ya es el último nivel


def test_repartidor_cercano_atiende_servicio_simple(est):
    est.config["radio_km"] = 20
    lejos = mec(est, "lejos", hab=("aire_llantas",), km=(6, 0))
    cerca = mec(est, "cerca", hab=("aire_llantas",), km=(0.5, 0))
    s = sol(est, km=(0, 0), tipo="aire_llantas")
    est.paso_despacho()
    assert s["repartidor_id"] == cerca["id"] and lejos["estado"] == "libre"


def test_metrica_porcentaje_resuelto_sin_grua(est):
    con_todos_los_servicios(est)
    a = mec(est, "a", hab=("aire_llantas", "diagnostico"))
    for _ in range(2):
        s = sol(est, km=(0, 0), tipo="aire_llantas"); atender(est, a, s, "termine")
    s = sol(est, km=(0, 0), tipo="diagnostico"); atender(est, a, s, "escalar_grua")
    m = est.panel()["metricas"]
    assert m["nivel1_resueltas"] == 2 and m["nivel1_escaladas"] == 1 and abs(m["pct_sin_grua"] - 2 / 3) < 1e-9


# ------------------------------------------------------------------ precios y cotización
def test_cotizar_gasolina_usa_el_precio_del_taller_cercano(est):
    lat, lon = punto(0, 0)
    t = est.agregar_taller("Mi taller", lat, lon, demo=False)
    est.actualizar_catalogo(t["id"], {"magna": {"litro": 25.0, "disponible": True}}, [])
    c = est.cotizar("gasolina", lat, lon, "magna", 8)
    assert c["taller"]["id"] == t["id"] and not c["referencia"]
    assert c["subtotal"] == 200.0 and c["servicio"] == est.config["precios"]["servicios"]["gasolina"] and c["total"] == 200.0 + c["servicio"]


def test_los_talleres_de_demostracion_no_fijan_precios_ni_se_le_indican_al_repartidor(est):
    lat, lon = punto(0, 0)
    assert any(x["demo"] for x in est.talleres.values())
    c = est.cotizar("gasolina", lat, lon, "magna", 5)
    assert c["taller"] is None and c["referencia"] and c["subtotal"] == round(5 * est.config["precios"]["combustibles"]["magna"]["litro"], 2)
    est.config["demo_en_cotizacion"] = True                        # solo para demostraciones
    assert est.cotizar("gasolina", lat, lon, "magna", 5)["taller"]["nombre"].startswith("Taller demo")


def test_cotizar_sin_taller_con_producto_usa_precio_de_referencia(est):
    est.talleres.clear()
    lat, lon = punto(0, 0)
    c = est.cotizar("gasolina", lat, lon, "premium", 10)
    assert c["referencia"] and c["taller"] is None
    assert c["subtotal"] == round(est.config["precios"]["combustibles"]["premium"]["litro"] * 10, 2)
    est.actualizar_precios_referencia(combustibles={"premium": 30}, servicios={"gasolina": 0})
    c = est.cotizar("gasolina", lat, lon, "premium", 10)
    assert c["total"] == 300.0


def test_cotizar_bateria_elige_la_mas_barata_con_inventario(est):
    est.talleres.clear()
    lat, lon = punto(0, 0)
    t = est.agregar_taller("Baterías", lat, lon)
    est.actualizar_catalogo(t["id"], {}, [
        dict(nombre="Marca A", categoria="estandar", precio=2500, stock=2),
        dict(nombre="Marca B", categoria="estandar", precio=2100, stock=0),       # sin inventario: no cuenta
        dict(nombre="Marca C", categoria="estandar", precio=2300, stock=1)])
    c = est.cotizar("bateria_nueva", lat, lon, "estandar")
    assert "Marca C" in c["concepto"] and c["subtotal"] == 2300 and c["taller"]["id"] == t["id"]
    assert est.cotizar("bateria_nueva", lat, lon, "camioneta")["referencia"]       # ese taller no tiene camioneta


def test_cotizar_aire_y_paso_de_corriente_llevan_solo_el_cargo_por_servicio(est):
    lat, lon = punto(0, 0)
    sv = est.config["precios"]["servicios"]
    for tipo in ("aire_llantas", "paso_de_corriente"):
        c = est.cotizar(tipo, lat, lon)
        assert c["subtotal"] == 0 and c["servicio"] == sv[tipo] and c["total"] == sv[tipo] and c["taller"] is None and not c["referencia"]
    est.actualizar_precios_referencia(servicios={"aire_llantas": 150})
    assert est.cotizar("aire_llantas", lat, lon)["total"] == 150


def test_cotizar_valida_entradas_y_servicios_apagados_no_llevan_precio(est):
    lat, lon = punto(0, 0)
    assert est.cotizar("diagnostico", lat, lon) is None and est.cotizar("grua", lat, lon) is None
    for args in [("gasolina", "inventado", 5), ("gasolina", "magna", 0), ("gasolina", "magna", 999), ("bateria_nueva", "inventada", None)]:
        with pytest.raises(ValueError):
            est.cotizar(args[0], lat, lon, args[1], args[2])


def test_solicitud_guarda_cotizacion_y_al_terminar_baja_el_inventario(est):
    est.talleres.clear()
    lat, lon = punto(0, 0)
    t = est.agregar_taller("Baterías", lat, lon)
    est.actualizar_catalogo(t["id"], {}, [dict(nombre="Marca A", categoria="compacto", precio=1800, stock=2)])
    r = mec(est, "rep", hab=("bateria_nueva",), km=(0, 0))
    la, lo = punto(1, 0)
    s = est.crear_solicitud("Ana", la, lo, "bateria_nueva", opcion="compacto")
    assert s["cotizacion"]["total"] == 1800 + est.config["precios"]["servicios"]["bateria_nueva"]
    vista = est.snapshot("taller", t["id"])["taller"]
    assert [p["id"] for p in vista["pedidos_activos"]] == [s["id"]]               # el taller ve el pedido que debe surtir
    atender(est, r, s)
    assert est.talleres[t["id"]]["baterias"][0]["stock"] == 1
    vista = est.snapshot("taller", t["id"])["taller"]
    assert vista["pedidos_activos"] == [] and len(vista["ventas"]) == 1 and vista["total_vendido"] == s["cotizacion"]["total"]


def test_cancelar_no_descuenta_inventario(est):
    est.talleres.clear()
    lat, lon = punto(0, 0)
    t = est.agregar_taller("Baterías", lat, lon)
    est.actualizar_catalogo(t["id"], {}, [dict(nombre="A", categoria="estandar", precio=2000, stock=1)])
    s = est.crear_solicitud("Ana", lat, lon, "bateria_nueva", opcion="estandar")
    est.cancelar_solicitud(s["id"])
    assert est.talleres[t["id"]]["baterias"][0]["stock"] == 1 and est.talleres[t["id"]]["ventas"] == []


# ------------------------------------------------------------------ talleres
def test_registro_de_taller_y_edicion_del_catalogo(est):
    lat, lon = punto(0, 0)
    t = est.agregar_taller("  Taller Lupita ", lat, lon, "55 1234 5678", "Calle 1, Col. Centro")
    assert t["nombre"] == "Taller Lupita" and t["telefono"] == "55 1234 5678" and not t["demo"]
    assert all(not c["disponible"] for c in t["combustibles"].values()) and all(b["stock"] == 0 for b in t["baterias"])
    est.actualizar_taller(t["id"], nombre="Lupita Auto", lat=lat + 0.001, lon=lon)
    assert est.talleres[t["id"]]["nombre"] == "Lupita Auto"
    with pytest.raises(ValueError):
        est.actualizar_catalogo(t["id"], {"magna": {"litro": 1, "disponible": True}}, [])         # precio absurdo
    with pytest.raises(ValueError):
        est.actualizar_catalogo(t["id"], {}, [dict(nombre="X", categoria="rara", precio=2000, stock=1)])
    with pytest.raises(ValueError):
        est.actualizar_catalogo(t["id"], {}, [dict(nombre="X", categoria="estandar", precio=2000, stock=-1)])
    with pytest.raises(KeyError):
        est.actualizar_taller("noexiste", nombre="x")


def test_talleres_demo_y_mas_cercano(est):
    assert len(est.talleres) == 4 and all(x["demo"] for x in est.talleres.values())
    lat, lon = punto(0, 0)
    t = est.taller_mas_cercano(lat, lon)
    assert t["distancia_km"] == min(motor.distancia_m(lat, lon, x["lat"], x["lon"]) / 1000 for x in est.talleres.values())
    est.agregar_taller("Mío", lat, lon)
    assert est.taller_mas_cercano(lat, lon)["nombre"] == "Mío"


# ------------------------------------------------------------------ algoritmos
def test_hungaro_reduce_espera_total_frente_a_voraz():
    """Mismo caso del ejemplo del simulador: el voraz toma al más cercano y deja a la otra solicitud con el lejano."""
    totales = {}
    for pol in ("voraz", "hungaro_sp"):
        r = Reloj(); e = Estado(":memory:", reloj=r, semilla=1)
        e.config["politica"] = pol
        e.config["radio_km"] = 20.0                    # el caso usa distancias de hasta 8 km
        mec(e, "m0", km=(0, 0)); mec(e, "m1", km=(5, 0))
        s0 = sol(e, km=(2, 0)); r.t += 1
        s1 = sol(e, km=(-3, 0))
        e.paso_despacho()
        totales[pol] = s0["eta_s"] + s1["eta_s"]
    assert totales["hungaro_sp"] < totales["voraz"] * 0.7


def test_prioridad():
    assert motor.prioridad("paso_de_corriente", "via_rapida", 23, False)[0] == 3
    assert motor.prioridad("paso_de_corriente", "via_rapida", 12, False)[0] == 2
    assert motor.prioridad("paso_de_corriente", "calle", 12, False)[0] == 1
    assert motor.prioridad("paso_de_corriente", "calle", 12, True)[0] == 3


def test_ciclo_con_bots(est):
    est.config.update(rechazo_bot=0.0, prob_escalar=0.0)
    est.crear_bots(4, "repartidor")
    s = sol(est, km=(0, 0), tipo="aire_llantas")
    for k in range(900):
        est.r.t += 1
        est.tick(1.0)
        est.expirar_ofertas()
        if k % 5 == 0:
            est.paso_despacho()
        if s["estado"] == "terminada":
            break
    assert s["estado"] == "terminada", s["estado"]
    assert est.panel()["metricas"]["atendidas"] == 1


def test_ciclo_con_escalamiento_de_bots(est):
    con_todos_los_servicios(est)
    est.config.update(rechazo_bot=0.0, prob_escalar=1.0)
    est.crear_bots(3, "repartidor"); est.crear_bots(1, "grua")
    s = sol(est, km=(0, 0), tipo="diagnostico")
    hija = None
    for k in range(2000):
        est.r.t += 1
        est.tick(1.0); est.expirar_ofertas()
        if k % 5 == 0:
            est.paso_despacho()
        hija = est.solicitudes.get(s["hija_id"]) if s["hija_id"] else None
        if hija and hija["estado"] == "terminada":
            break
    assert s["estado"] == "escalada" and hija and hija["estado"] == "terminada"
    assert est.repartidores[hija["repartidor_id"]]["prestador"] == "grua"


def test_eventos_guardados_en_bd(est):
    mec(est); sol(est)
    n = est.db.execute("SELECT COUNT(*) FROM eventos").fetchone()[0]
    assert n >= 2


# ------------------------------------------------------------------ búsqueda de cercanos
def test_cercanos_respeta_radio_y_habilidad(est):
    est.config["radio_km"] = 3.0
    mec(est, "cerca", km=(1, 0))
    mec(est, "lejos", km=(8, 0))                                   # fuera del radio
    mec(est, "sin_llanta", hab=("paso_de_corriente",), km=(0.5, 0))
    lat, lon = punto(0, 0)
    r = est.cercanos(lat, lon, "cambio_de_llanta")
    assert r["n"] == 1 and r["eta_min_s"] > 0 and r["repartidores"][0]["prestador"] == "repartidor"
    assert est.cercanos(lat, lon)["n"] == 2                        # sin filtrar por habilidad


def test_despacho_ignora_prestadores_fuera_de_radio(est):
    est.config["radio_km"] = 2.0
    mec(est, "lejos", km=(6, 0))
    s = sol(est, km=(0, 0))
    assert est.paso_despacho() == [] and s["estado"] == "pendiente"
    est.config["radio_km"] = 10.0
    assert len(est.paso_despacho()) == 1


def test_indice_h3_equivale_a_busqueda_exhaustiva(monkeypatch):
    import random
    rng = random.Random(7)
    pts = [(f"m{i}", LAT0 + rng.uniform(-0.12, 0.12), LON0 + rng.uniform(-0.12, 0.12)) for i in range(300)]
    for radio in (500, 1500, 5000, 12000):
        for _ in range(8):
            la, lo = LAT0 + rng.uniform(-0.1, 0.1), LON0 + rng.uniform(-0.1, 0.1)
            con = {i for i, _ in motor.IndiceEspacial(pts).cercanos(la, lo, radio)}
            exh = {i for i, a, b in pts if motor.distancia_m(la, lo, a, b) <= radio}
            assert con == exh
    monkeypatch.setattr(motor, "h3", None)                          # sin H3 el resultado es el mismo
    assert {i for i, _ in motor.IndiceEspacial(pts).cercanos(LAT0, LON0, 3000)} == \
           {i for i, a, b in pts if motor.distancia_m(LAT0, LON0, a, b) <= 3000}


# ------------------------------------------------------------------ API
def test_api_y_websocket():
    c = TestClient(app)
    cfg = c.get("/api/config").json()
    assert set(cfg["tipos"]) == {"gasolina", "aire_llantas", "paso_de_corriente"}               # solo tres servicios al público
    assert list(cfg["prestadores"]) == ["repartidor"] and set(cfg["prestadores"]["repartidor"]["permitidas"]) == set(cfg["tipos"])
    assert cfg["precios"]["combustibles"]["magna"]["litro"] > 0 and set(cfg["precios"]["servicios"]) >= set(cfg["tipos"])
    for ruta in ["/", "/cliente", "/repartidor", "/prestador", "/mecanico", "/taller", "/qr", "/negocio", "/panel"]:
        assert c.get(ruta).status_code == 200
    r = c.post("/api/repartidores", json={"nombre": "Luis", "habilidades": ["aire_llantas", "paso_de_corriente", "grua", "diagnostico"], "prestador": "repartidor"})
    mid = r.json()["id"]
    assert c.get(f"/api/repartidores/{mid}").json()["repartidor"]["habilidades"] == ["aire_llantas", "paso_de_corriente"]
    lat, lon = punto(0, 0)
    assert c.post(f"/api/repartidores/{mid}/turno", json={"activo": True, "lat": lat, "lon": lon}).status_code == 200
    la, lo = punto(1, 1)
    r = c.post("/api/solicitudes", json={"lat": la, "lon": lo, "tipo": "aire_llantas"})            # sin nombre, vía ni notas: lo mínimo
    sid = r.json()["id"]
    assert r.json()["cotizacion"]["total"] == cfg["precios"]["servicios"]["aire_llantas"]
    from app.main import estado
    estado.paso_despacho()
    oferta = c.get(f"/api/repartidores/{mid}").json()
    assert oferta["solicitud"]["id"] == sid and oferta["solicitud"]["estado"] == "ofertada" and oferta["repartidor"]["espera"]["cerca"] == 0
    assert c.post(f"/api/repartidores/{mid}/respuesta", json={"solicitud_id": sid, "acepta": True}).status_code == 200
    cli = c.get(f"/api/solicitudes/{sid}").json()
    assert cli["solicitud"]["estado"] == "aceptada" and cli["repartidor"]["nombre"] == "Luis"
    assert c.post(f"/api/repartidores/{mid}/estado", json={"accion": "llegue"}).status_code == 200
    assert c.post(f"/api/repartidores/{mid}/estado", json={"accion": "termine"}).status_code == 200
    assert c.get(f"/api/solicitudes/{sid}").json()["solicitud"]["estado"] == "terminada"
    with c.websocket_connect(f"/ws?rol=cliente&id={sid}") as ws:
        assert ws.receive_json()["solicitud"]["id"] == sid
    # validaciones
    for apagado in ("diagnostico", "grua", "bateria_nueva", "cambio_de_llanta"):
        assert c.post("/api/solicitudes", json={"lat": la, "lon": lo, "tipo": apagado}).status_code == 400, apagado
    assert c.post("/api/solicitudes", json={"lat": la, "lon": lo, "tipo": "inventado"}).status_code == 400
    assert c.post("/api/solicitudes", json={"lat": 999, "lon": lo, "tipo": "gasolina"}).status_code == 422
    assert c.get("/api/solicitudes/noexiste").status_code == 404
    assert c.post(f"/api/repartidores/{mid}/respuesta", json={"solicitud_id": sid, "acepta": True}).status_code == 400
    assert c.post("/api/repartidores", json={"habilidades": ["grua"], "prestador": "repartidor"}).status_code == 400
    assert c.post("/api/repartidores", json={"habilidades": ["aire_llantas"], "prestador": "mecanico"}).status_code == 400
    assert c.post("/api/panel/config", json={"politica": "xx"}).status_code == 400
    assert c.post("/api/panel/config", json={"politica": "voraz", "espera_max_s": 600, "reoferta_s": 60}).status_code == 200
    cfgp = c.get("/api/panel/estado").json()["config"]
    assert cfgp["politica"] == "voraz" and cfgp["espera_max_s"] == 600 and cfgp["reoferta_s"] == 60
    assert len(c.get("/api/talleres", params={"demo": True}).json()) >= 4 and all(not x["demo"] for x in c.get("/api/talleres").json())
    assert c.post("/api/panel/bots", json={"n": 2, "prestador": "inventado"}).status_code == 400


def test_api_escalada_cuando_se_reactivan_los_demas_servicios(monkeypatch):
    """La escalada a grúa sigue funcionando por si se reactiva: se enciende todo solo para esta prueba."""
    from app.main import estado
    monkeypatch.setitem(estado.config, "servicios_activos", list(motor.TIPOS))
    c = TestClient(app)
    mid = c.post("/api/repartidores", json={"nombre": "Luis", "habilidades": ["diagnostico"], "prestador": "repartidor"}).json()["id"]
    lat, lon = punto(0, 0)
    c.post(f"/api/repartidores/{mid}/turno", json={"activo": True, "lat": lat, "lon": lon})
    sid = c.post("/api/solicitudes", json={"lat": lat, "lon": lon, "tipo": "diagnostico", "via": "avenida"}).json()["id"]
    estado.paso_despacho()
    assert c.post(f"/api/repartidores/{mid}/respuesta", json={"solicitud_id": sid, "acepta": True}).status_code == 200
    c.post(f"/api/repartidores/{mid}/estado", json={"accion": "llegue"})
    assert c.post(f"/api/repartidores/{mid}/estado", json={"accion": "escalar_grua", "nota": "No arranca"}).status_code == 200
    padre = c.get(f"/api/solicitudes/{sid}").json()["solicitud"]
    hija = c.get(f"/api/solicitudes/{padre['hija_id']}").json()["solicitud"]
    assert padre["estado"] == "escalada" and hija["tipo"] == "grua" and hija["taller"]["nombre"]


# ------------------------------------------------------------------ precios y cuentas del negocio
def test_precios_y_costos_se_guardan_y_sobreviven_al_reinicio(tmp_path):
    ruta = str(tmp_path / "d.db")
    e = Estado(ruta, semilla=1)
    e.actualizar_precios_referencia(combustibles={"magna": 25.5}, servicios={"aire_llantas": 140})
    e.guardar_costos({"hora_repartidor": 120, "minutos": {"aire_llantas": 20}})
    e.reiniciar()                                                    # «Reiniciar todo» del panel
    assert e.config["precios"]["servicios"]["aire_llantas"] == 140 and e.costos["hora_repartidor"] == 120
    e2 = Estado(ruta, semilla=1)                                     # y al reiniciar el servidor
    assert e2.config["precios"]["combustibles"]["magna"]["litro"] == 25.5 and e2.costos["minutos"]["aire_llantas"] == 20
    assert e2.costos["minutos"]["gasolina"] == motor.COSTOS_DEFAULT["minutos"]["gasolina"]       # lo no cambiado conserva su valor
    assert set(e2.ajustes()) == {"url_publica", "whatsapp", "telefono"}                           # los ajustes públicos no filtran precios ni costos


def test_guardar_costos_valida_y_no_guarda_nada_si_hay_un_error(est):
    antes = dict(est.costos)
    for malo in [dict(margen_objetivo=0.95), dict(hora_repartidor=-1), dict(minutos={"aire_llantas": 0}), dict(kit={"inventado": 5}), dict(costo_km=1000)]:
        with pytest.raises(ValueError):
            est.guardar_costos(malo)
    with pytest.raises(ValueError):
        est.guardar_costos(dict(hora_repartidor=100, margen_objetivo=2))                         # el primer valor es válido pero el segundo no
    assert est.costos["hora_repartidor"] == antes["hora_repartidor"]
    with pytest.raises(ValueError):
        est.actualizar_precios_referencia(servicios={"aire_llantas": 5000})
    with pytest.raises(ValueError):
        est.actualizar_precios_referencia(servicios={"inventado": 10})


def test_negocio_hace_las_cuentas_por_servicio(est):
    est.guardar_costos(dict(hora_repartidor=60, costo_km=2, km_ida_vuelta=10, margen_objetivo=0.5, gastos_fijos_mes=1000,
                            minutos={"aire_llantas": 30}, kit={"bomba": 200}, vida_util_servicios={"bomba": 200}))
    est.actualizar_precios_referencia(servicios={"aire_llantas": 100})
    f = next(x for x in est.negocio()["filas"] if x["clave"] == "aire_llantas")
    assert f["costo_tiempo"] == 30 and f["costo_transporte"] == 20 and f["costo_herramienta"] == 1.0 and f["costo"] == 51.0
    assert f["ganancia"] == 49.0 and abs(f["margen"] - 0.49) < 1e-9
    assert f["cargo_sugerido"] == 105 and f["equilibrio"] == math.ceil(1000 / 49)              # 51 / 0.5 = 102 -> sube a múltiplo de $5
    g5 = next(x for x in est.negocio()["filas"] if x["clave"] == "gasolina_5")
    assert g5["producto"] == round(5 * est.config["precios"]["combustibles"]["magna"]["litro"], 2) and g5["total"] == g5["producto"] + g5["cargo"]
    est.actualizar_precios_referencia(servicios={"aire_llantas": 40})                         # cobrar por debajo del costo se ve como pérdida
    f = next(x for x in est.negocio()["filas"] if x["clave"] == "aire_llantas")
    assert f["ganancia"] < 0 and f["equilibrio"] is None


def test_negocio_aprende_los_tiempos_reales_y_excluye_los_simulados(est):
    m = mec(est, "a", hab=("aire_llantas",))
    s = sol(est, km=(0, 0), tipo="aire_llantas")
    est.paso_despacho()
    est.r.t += 60                                                    # el cliente esperó 1 min a que alguien aceptara
    est.responder_oferta(m["id"], s["id"], True)
    est.r.t += 600; est.avanzar(m["id"], "llegue")                   # 10 min de camino
    est.r.t += 300; est.avanzar(m["id"], "termine")                  # 5 min de servicio
    r = est.tiempos_reales()["aire_llantas"]
    assert r["n"] == 1 and abs(r["viaje_min"] - 10) < 1e-9 and abs(r["servicio_min"] - 5) < 1e-9 and abs(r["total_min"] - 15) < 1e-9 and abs(r["espera_min"] - 1) < 1e-9
    neg = est.negocio(usar_reales=True)
    f = next(x for x in neg["filas"] if x["clave"] == "aire_llantas")
    assert f["minutos"] == 15 and f["fuente"].startswith("real")
    assert next(x for x in est.negocio()["filas"] if x["clave"] == "aire_llantas")["fuente"] == "supuesto"
    assert neg["resumen"]["servicios"] == 1 and neg["resumen"]["cargos"] == est.config["precios"]["servicios"]["aire_llantas"]
    # un servicio de un repartidor simulado no cuenta para aprender
    est.crear_bots(1, "repartidor")
    bot = next(x for x in est.repartidores.values() if x["bot"])
    bot["habilidades"] = ["paso_de_corriente"]
    s2 = sol(est, km=(0.1, 0), tipo="paso_de_corriente")
    est.paso_despacho(); est.responder_oferta(bot["id"], s2["id"], True); est.avanzar(bot["id"], "llegue"); est.avanzar(bot["id"], "termine")
    assert "paso_de_corriente" not in est.tiempos_reales() and est.negocio()["resumen"]["simulados"] == 1


def test_api_negocio():
    c = TestClient(app)
    n = c.get("/api/negocio").json()
    assert [f["clave"] for f in n["filas"]] == ["gasolina_5", "gasolina_10", "aire_llantas", "paso_de_corriente"] and n["costos"]["margen_objetivo"] == 0.3
    r = c.post("/api/negocio", json={"hora_repartidor": 100, "minutos": {"aire_llantas": 20}})
    assert r.status_code == 200 and r.json()["costos"]["hora_repartidor"] == 100
    assert c.post("/api/negocio", json={"margen_objetivo": 5}).status_code == 400
    assert c.post("/api/negocio", json={"kit": {"inventado": 1}}).status_code == 400
    assert c.post("/api/panel/precios", json={"servicios": {"paso_de_corriente": 130}}).json()["servicios"]["paso_de_corriente"] == 130
    assert c.post("/api/panel/precios", json={"servicios": {"paso_de_corriente": -1}}).status_code == 400
    c.post("/api/negocio", json={"hora_repartidor": 90, "minutos": {"aire_llantas": 25}})
    c.post("/api/panel/precios", json={"servicios": {"paso_de_corriente": 120}})


def test_api_cercanos():
    c = TestClient(app)
    assert c.get("/api/cercanos", params={"lat": LAT0, "lon": LON0}).status_code == 200
    assert c.get("/api/cercanos", params={"lat": LAT0, "lon": LON0, "tipo": "inventado"}).status_code == 400
    assert c.get("/api/cercanos", params={"lat": 999, "lon": LON0}).status_code == 422


# ------------------------------------------------------------------ lugares con QR y contacto
def test_lugares_crear_listar_escanear_borrar(est):
    a = est.crear_lugar("  Gasolinera Centro ", *punto(0.5, 0.5))
    g = est.crear_lugar("Tarjetas del taller")                     # sin coordenadas: QR general
    assert a["nombre"] == "Gasolinera Centro" and not a["general"] and len(a["codigo"]) == 6
    assert g["general"] and g["lat"] is None
    assert {x["codigo"] for x in est.listar_lugares()} == {a["codigo"], g["codigo"]}
    assert est.escanear(a["codigo"])["escaneos"] == 1 and est.escanear(a["codigo"])["escaneos"] == 2
    assert est.escanear("noexiste") is None
    est.borrar_lugar(a["codigo"])
    assert est.lugar(a["codigo"]) is None
    with pytest.raises(KeyError):
        est.borrar_lugar(a["codigo"])


def test_lugares_validaciones(est):
    with pytest.raises(ValueError):
        est.crear_lugar("   ")
    with pytest.raises(ValueError):
        est.crear_lugar("X", 19.3, None)                           # coordenadas a medias
    with pytest.raises(ValueError):
        est.crear_lugar("X", 999, -99)


def test_los_lugares_sobreviven_al_reinicio_de_datos(est):
    a = est.crear_lugar("Esquina", *punto(0, 0))
    est.reiniciar()                                                # "Reiniciar todo" del panel no debe romper los QR impresos
    assert est.lugar(a["codigo"]) is not None


def test_ajustes_de_contacto_y_direccion(est):
    r = est.guardar_ajustes("https://auxilio.example.com/", "55 1234 5678", "(55) 1234-5678")
    assert r["url_publica"] == "https://auxilio.example.com" and r["whatsapp"] == "525512345678" and r["telefono"] == "(55) 1234-5678"
    assert est.guardar_ajustes(whatsapp="+52 1 55 1234 5678")["whatsapp"] == "5215512345678"      # con código de país
    assert est.guardar_ajustes(url_publica="")["url_publica"] == ""                                # se puede borrar
    for malo in [dict(url_publica="auxilio.com"), dict(url_publica="ftp://x.com"), dict(url_publica="https://x.com/?a=1"),
                 dict(whatsapp="12345"), dict(telefono="llamame!")]:
        with pytest.raises(ValueError):
            est.guardar_ajustes(**malo)


def test_el_lugar_viaja_con_la_solicitud_y_la_hija(est):
    con_todos_los_servicios(est)
    a = mec(est, "a", hab=("diagnostico",))
    lat, lon = punto(0, 0)
    s = est.crear_solicitud("Ana", lat, lon, "diagnostico", lugar="  Gasolinera Centro ")
    assert est.snapshot("cliente", s["id"])["solicitud"]["lugar"] == "Gasolinera Centro"
    atender(est, a, s, "escalar_grua", "No arranca")
    assert est.solicitudes[s["hija_id"]]["lugar"] == "Gasolinera Centro"


def test_api_qr_enlace_corto_y_letrero():
    c = TestClient(app)
    lat, lon = punto(0.3, 0.3)
    l = c.post("/api/lugares", json={"nombre": "Gasolinera Ñandú & Hijos", "lat": lat, "lon": lon}).json()
    g = c.post("/api/lugares", json={"nombre": "Tarjetas"}).json()
    assert c.post("/api/lugares", json={"nombre": " "}).status_code == 400
    assert c.post("/api/lugares", json={"nombre": "X", "lat": lat}).status_code == 400
    # el enlace corto manda al formulario con la ubicación y el nombre del lugar bien codificados
    r = c.get(f"/q/{l['codigo']}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/cliente?")
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["lugar"] == ["Gasolinera Ñandú & Hijos"] and abs(float(q["lat"][0]) - lat) < 1e-5 and abs(float(q["lon"][0]) - lon) < 1e-5
    q = parse_qs(urlparse(c.get(f"/q/{g['codigo']}", follow_redirects=False).headers["location"]).query)
    assert "lat" not in q and q["lugar"] == ["Tarjetas"]                   # el QR general no fija ubicación
    assert c.get("/q/zzzzzz", follow_redirects=False).headers["location"] == "/cliente"        # código desconocido: no se deja a nadie sin ayuda
    lista = c.get("/api/lugares").json()
    assert next(x for x in lista["lugares"] if x["codigo"] == l["codigo"])["escaneos"] == 1
    # la imagen del QR es un SVG y cambia al configurar la dirección pública
    svg1 = c.get(f"/qr/{l['codigo']}.svg")
    assert svg1.status_code == 200 and svg1.headers["content-type"].startswith("image/svg+xml") and b"<svg" in svg1.content
    assert c.get("/qr/zzzzzz.svg").status_code == 404
    assert c.post("/api/ajustes", json={"url_publica": "https://auxilio.example.com", "whatsapp": "5512345678"}).status_code == 200
    info = c.get(f"/api/lugares/{l['codigo']}").json()
    assert info["url"] == f"https://auxilio.example.com/q/{l['codigo']}" and info["contacto"]["whatsapp"] == "525512345678"
    assert c.get(f"/qr/{l['codigo']}.svg").content != svg1.content
    assert c.get("/api/lugares").json()["publica"] is True
    assert c.get("/api/config").json()["contacto"]["whatsapp"] == "525512345678" and "url_publica" not in c.get("/api/config").json()["contacto"]
    assert c.post("/api/ajustes", json={"url_publica": "no-es-url"}).status_code == 400
    for ruta in ["/qr", f"/letrero/{l['codigo']}"]:
        assert c.get(ruta).status_code == 200
    assert c.delete(f"/api/lugares/{l['codigo']}").status_code == 200 and c.delete(f"/api/lugares/{l['codigo']}").status_code == 404
    c.post("/api/ajustes", json={"url_publica": "", "whatsapp": "", "telefono": ""})


def test_es_publica():
    from app.main import es_publica
    assert es_publica("https://auxilio.example.com") and es_publica("https://8.8.8.8")
    for no in ["http://auxilio.example.com", "https://localhost:8000", "http://127.0.0.1:8000", "https://192.168.1.20", "https://10.0.0.5", "https://mi-pc.local"]:
        assert not es_publica(no), no
    c = TestClient(app)
    c.post("/api/panel/solicitud_aleatoria")
    s = c.post("/api/solicitudes", json={"lat": LAT0, "lon": LON0, "tipo": "aire_llantas", "lugar": "Gasolinera Centro"}).json()["id"]
    assert c.get(f"/api/solicitudes/{s}").json()["solicitud"]["lugar"] == "Gasolinera Centro"


# ------------------------------------------------------------------ protección para publicar en internet
@pytest.fixture
def publico(monkeypatch):
    """App publicada: con contraseña, código de colaboradores y un limitador limpio."""
    from app import seguridad
    monkeypatch.setenv("ADMIN_PASSWORD", "clave-larga-123")
    monkeypatch.setenv("CODIGO_COLABORADORES", "cod-colab")
    monkeypatch.setattr(seguridad, "limitador", seguridad.Limitador())
    return TestClient(app)


def test_sin_contrasena_la_app_sigue_abierta_para_uso_local():
    c = TestClient(app)
    assert c.get("/panel", follow_redirects=False).status_code == 200 and c.get("/api/panel/estado").status_code == 200
    assert c.get("/api/sesion").json() == {"publico": False, "admin": True}
    assert "Panel de demostración" in c.get("/").text or "Pedir ayuda" in c.get("/").text         # en local / es el menú
    assert c.get("/menu").status_code == 200 and c.get("/salud").json() == {"ok": True}


def test_modo_publico_protege_las_rutas_del_dueno(publico):
    c = publico
    for ruta in ["/panel", "/qr", "/negocio", "/menu", "/letrero/abc123"]:
        r = c.get(ruta, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"].startswith("/login?next="), ruta
    for ruta in ["/api/panel/estado", "/api/lugares", "/api/negocio", "/api/ajustes", "/api/cercanos?lat=19&lon=-99&tipo=aire_llantas", "/qr/abc123.svg"]:
        assert c.get(ruta, follow_redirects=False).status_code in (401, 302), ruta
    assert c.post("/api/panel/reiniciar").status_code == 401 and c.post("/api/panel/bots", json={"n": 1}).status_code == 401
    assert c.post("/api/lugares", json={"nombre": "x"}).status_code == 401 and c.post("/api/ajustes", json={"telefono": "1"}).status_code == 401
    assert c.post("/api/negocio", json={"costo_km": 5}).status_code == 401 and c.post("/api/panel/precios", json={"servicios": {"gasolina": 1}}).status_code == 401
    assert c.get("/static/panel.html").status_code == 404 and c.get("/static/qr.html").status_code == 404      # tampoco como archivos sueltos
    assert c.get("/api/sesion").json() == {"publico": True, "admin": False}


def test_modo_publico_deja_abierto_lo_que_necesita_el_cliente(publico):
    c = publico
    lat, lon = punto(0, 0)
    for ruta in ["/cliente", "/login", "/salud", "/static/comun.js", "/api/config", f"/api/cotizar?lat={lat}&lon={lon}&tipo=aire_llantas"]:
        assert c.get(ruta).status_code == 200, ruta
    assert "Pedir ayuda" in c.get("/").text and "Panel de demostración" not in c.get("/").text            # / es la pantalla del cliente
    l = estado_lugar()
    assert c.get(f"/q/{l}", follow_redirects=False).status_code == 302
    r = c.post("/api/solicitudes", json={"lat": lat, "lon": lon, "tipo": "aire_llantas"})
    assert r.status_code == 200
    sid = r.json()["id"]
    assert len(sid) == 12 and c.get(f"/api/solicitudes/{sid}").status_code == 200 and c.post(f"/api/solicitudes/{sid}/cancelar").status_code == 200
    cfg = c.get("/api/config").json()
    assert "url_publica" not in cfg["contacto"] and cfg["requiere_codigo"] is True
    h = c.get("/cliente").headers
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY" and h["referrer-policy"] == "same-origin"


def estado_lugar():
    from app.main import estado
    return estado.crear_lugar("Prueba pública", *punto(0.1, 0.1))["codigo"]


def test_login_abre_el_acceso_y_logout_lo_cierra(publico):
    c = publico
    assert c.post("/api/login", json={"password": "mala"}).status_code == 401
    r = c.post("/api/login", json={"password": "clave-larga-123"})
    assert r.status_code == 200 and "admin_sesion" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"] and "samesite=lax" in r.headers["set-cookie"].lower()
    assert "secure" not in r.headers["set-cookie"].lower()                                               # por http local no se marca Secure
    for ruta in ["/panel", "/qr", "/negocio", "/menu", "/api/panel/estado", "/api/lugares"]:
        assert c.get(ruta, follow_redirects=False).status_code == 200, ruta
    assert c.get("/api/sesion").json() == {"publico": True, "admin": True}
    c.post("/api/logout")
    assert c.get("/panel", follow_redirects=False).status_code == 302 and c.get("/api/sesion").json()["admin"] is False


def test_la_cookie_va_marcada_secure_detras_de_https(publico):
    r = publico.post("/api/login", json={"password": "clave-larga-123"}, headers={"x-forwarded-proto": "https"})
    assert "secure" in r.headers["set-cookie"].lower()


def test_una_cookie_falsa_o_de_otra_contrasena_no_sirve(publico, monkeypatch):
    c = publico
    c.cookies.set("admin_sesion", "inventada")
    assert c.get("/api/panel/estado").status_code == 401
    c.cookies.clear()
    c.post("/api/login", json={"password": "clave-larga-123"})
    assert c.get("/api/panel/estado").status_code == 200
    monkeypatch.setenv("ADMIN_PASSWORD", "otra-clave-nueva")                                             # al cambiar la contraseña caen todas las sesiones
    assert c.get("/api/panel/estado").status_code == 401


def test_el_login_limita_los_intentos(publico):
    for _ in range(5):
        assert publico.post("/api/login", json={"password": "mala"}).status_code == 401
    assert publico.post("/api/login", json={"password": "mala"}).status_code == 429
    assert publico.post("/api/login", json={"password": "clave-larga-123"}).status_code == 429          # ni la buena pasa mientras dure el bloqueo


def test_websocket_del_panel_solo_para_el_dueno(publico):
    from starlette.websockets import WebSocketDisconnect
    c = publico
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws?rol=panel"):
            pass
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws"):                                                                 # sin rol también entrega el panel
            pass
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws?rol=inventado"):
            pass
    with c.websocket_connect("/ws?rol=cliente&id=nada") as ws:                                           # el cliente sí, con su propio id
        assert ws.receive_json()["solicitud"] is None
    c.post("/api/login", json={"password": "clave-larga-123"})
    with c.websocket_connect("/ws?rol=panel") as ws:
        assert "repartidores" in ws.receive_json()


def test_registro_de_repartidores_y_talleres_pide_el_codigo(publico):
    c = publico
    lat, lon = punto(0, 0)
    rep = {"nombre": "Luis", "habilidades": ["aire_llantas"], "prestador": "repartidor"}
    tal = {"nombre": "Taller", "lat": lat, "lon": lon}
    assert c.post("/api/repartidores", json=rep).status_code == 403
    assert c.post("/api/repartidores", json={**rep, "codigo": "mal"}).status_code == 403
    assert c.post("/api/repartidores", json={**rep, "codigo": "cod-colab"}).status_code == 200
    assert c.post("/api/talleres", json=tal).status_code == 403
    assert c.post("/api/talleres", json={**tal, "codigo": "cod-colab"}).status_code == 200


def test_adivinar_el_codigo_tiene_limite(publico):
    rep = {"nombre": "Luis", "habilidades": ["aire_llantas"], "prestador": "repartidor"}
    for i in range(10):
        assert publico.post("/api/repartidores", json={**rep, "codigo": f"x{i}"}).status_code == 403
    assert publico.post("/api/repartidores", json={**rep, "codigo": "cod-colab"}).status_code == 429


def test_sin_codigo_configurado_cualquiera_puede_registrarse(monkeypatch):
    monkeypatch.delenv("CODIGO_COLABORADORES", raising=False)
    c = TestClient(app)
    assert c.post("/api/repartidores", json={"habilidades": ["aire_llantas"]}).status_code == 200 and c.get("/api/config").json()["requiere_codigo"] is False


def test_limite_de_pedidos_por_conexion(publico, monkeypatch):
    monkeypatch.setenv("LIMITE_PEDIDOS_HORA", "2")
    lat, lon = punto(0, 0)
    cuerpo = {"lat": lat, "lon": lon, "tipo": "aire_llantas"}
    assert publico.post("/api/solicitudes", json=cuerpo).status_code == 200 and publico.post("/api/solicitudes", json=cuerpo).status_code == 200
    r = publico.post("/api/solicitudes", json=cuerpo)
    assert r.status_code == 429 and "llámanos" in r.json()["detail"]
    # otra conexión distinta (detrás de un proxy se usa la última IP de X-Forwarded-For) no se ve afectada; una IP falsa al inicio no sirve para evadir el límite
    assert publico.post("/api/solicitudes", json=cuerpo, headers={"x-forwarded-for": "203.0.113.50"}).status_code == 200
    assert publico.post("/api/solicitudes", json=cuerpo, headers={"x-forwarded-for": "1.2.3.4, 198.51.100.7"}).status_code == 200
    assert publico.post("/api/solicitudes", json=cuerpo, headers={"x-forwarded-for": "9.9.9.9, 198.51.100.7"}).status_code == 200
    assert publico.post("/api/solicitudes", json=cuerpo, headers={"x-forwarded-for": "8.8.8.8, 198.51.100.7"}).status_code == 429


def test_el_limitador():
    from app.seguridad import Limitador
    l = Limitador()
    assert l.permitir("a", 2, 10, ahora=0) and l.permitir("a", 2, 10, ahora=1) and not l.permitir("a", 2, 10, ahora=2)
    assert l.permitir("a", 2, 10, ahora=12)                                                              # pasada la ventana vuelve a caber
    assert l.permitir("b", 2, 10, ahora=2) and not l.excedido("b", 2, 10, ahora=3)


def test_los_ids_son_largos():
    est = Estado(":memory:", semilla=1)
    t = est.agregar_taller("Taller", *punto(0, 0))
    assert len(t["id"]) == 12 and len(est.crear_lugar("x")["codigo"]) == 6


def test_render_yaml_tiene_lo_necesario():
    import re
    from pathlib import Path
    y = (Path(__file__).parent.parent / "render.yaml").read_text(encoding="utf-8")
    for pieza in ["runtime: python", "healthCheckPath: /salud", "mountPath: /var/data", "DESPACHO_DB", "ADMIN_PASSWORD", "CODIGO_COLABORADORES",
                  "--proxy-headers", "$PORT", "pip install -r requirements.txt"]:
        assert pieza in y, pieza
    assert re.search(r"plan: starter", y) and "plan: free" not in y                                      # el gratuito borraría el disco
