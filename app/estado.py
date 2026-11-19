"""Estado del sistema y reglas del servicio (sin dependencia de FastAPI, para poder probarlo solo).

Niveles de servicio:
  1  repartidor (no necesita ser mecánico; lleva el producto o un kit básico hasta el cliente): aire, corriente,
     batería nueva, gasolina, cambio de llanta y revisión básica
  2  grúa: lleva el auto a un taller cuando la falla no se puede resolver en el lugar
Si el repartidor diagnostica una falla mayor, ESCALA: la solicitud original queda 'escalada' y se crea otra
solicitud (hija) para una grúa, en el mismo lugar.
Talleres: se registran solos, publican sus precios y su inventario (baterías, combustible) y de ahí se cotizan los pedidos.

Ciclo de una solicitud:
  pendiente -> ofertada -> aceptada (en camino) -> en_sitio -> terminada | escalada
  (puede pasar a cancelada; si el prestador rechaza o no responde, vuelve a pendiente)
Los eventos se guardan en SQLite para calibrar después el simulador con datos reales.
"""
import copy
import hmac
import json
import math
import random
import re
import secrets
import sqlite3
import threading
import time
import uuid
from collections import Counter, deque
from urllib.parse import urlparse

import numpy as np

from . import motor, rutas

MAX_PIN_INTENTOS = 5          # intentos de PIN por pedido antes de bloquear la llegada

CENTRO = (19.3727, -99.1587)   # centro de demostración (alcaldía Benito Juárez, CDMX)
TALLERES_DEMO = [(0.010, -0.012, "Norte"), (-0.011, 0.009, "Sur"), (0.004, 0.018, "Este"), (-0.003, -0.020, "Oeste")]


def _fusionar(base, nuevo):
    """Mezcla un dict guardado sobre los valores por omisión; ignora claves desconocidas."""
    for k, v in (nuevo or {}).items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _fusionar(base[k], v)
        elif k in base and not isinstance(base[k], dict):
            base[k] = v


def _nuevo_id():
    return uuid.uuid4().hex[:12]            # largo para que no se pueda adivinar el enlace de un pedido ajeno


class Estado:
    def __init__(self, db_path=":memory:", reloj=time.time, semilla=None):
        self.reloj = reloj
        self.rng = random.Random(semilla)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS eventos (t REAL, tipo TEXT, solicitud_id TEXT, repartidor_id TEXT, datos TEXT)")
        # Lugares con QR y ajustes de contacto: se guardan en disco y NO se borran al reiniciar los datos de demostración,
        # porque los QR ya impresos tienen que seguir funcionando.
        self.db.execute("CREATE TABLE IF NOT EXISTS lugares (codigo TEXT PRIMARY KEY, nombre TEXT, lat REAL, lon REAL, escaneos INTEGER DEFAULT 0, creado REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT)")
        # Un renglón por servicio terminado: de aquí se aprende cuánto tarda y cuánto deja cada servicio de verdad.
        self.db.execute("CREATE TABLE IF NOT EXISTS servicios_hechos (id TEXT PRIMARY KEY, tipo TEXT, creado REAL, acepto REAL, llego REAL, "
                        "fin REAL, subtotal REAL, servicio REAL, total REAL, simulado INTEGER)")
        self.db.commit()
        self.reiniciar()

    # ------------------------------------------------------------------ utilidades
    def reiniciar(self):
        with self.lock:
            self.solicitudes, self.repartidores, self.talleres = {}, {}, {}
            self.cont = Counter()
            self.log = deque(maxlen=60)
            self.version = 0
            self.config = dict(politica="hungaro", ventana_s=5.0, oferta_s=30.0, vel_kmh=18.0, radio_km=5.0,
                               rechazo_bot=0.10, vel_bot_ms=25.0, servicio_bot_s=(8.0, 15.0), prob_escalar=0.30,
                               radio_taller_km=15.0, reoferta_s=120.0, espera_max_s=900.0, demo_en_cotizacion=False, requiere_pin=True,
                               servicios_activos=list(motor.ACTIVOS),
                               precios=self._cargar("precios", motor.PRECIOS_DEFAULT))
            self.costos = self._cargar("costos", motor.COSTOS_DEFAULT)      # precios y costos sobreviven a «Reiniciar todo»
            for dlat, dlon, zona in TALLERES_DEMO:
                self.agregar_taller(f"Taller demo {zona}", CENTRO[0] + dlat, CENTRO[1] + dlon, demo=True)

    def _cargar(self, clave, defecto):
        base = copy.deepcopy(defecto)
        fila = self.db.execute("SELECT valor FROM ajustes WHERE clave=?", (clave,)).fetchone()
        if fila:
            try:
                _fusionar(base, json.loads(fila[0]))
            except (ValueError, TypeError):
                pass
        return base

    def _guardar(self, clave, valor):
        self.db.execute("INSERT INTO ajustes VALUES (?,?) ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor", (clave, json.dumps(valor)))
        self.db.commit()

    def evento(self, ev, sid=None, mid=None, **datos):
        t = self.reloj()
        self.cont[ev] += 1
        self.version += 1
        self.log.appendleft(dict(t=t, evento=ev, solicitud_id=sid, repartidor_id=mid, datos=datos))
        self.db.execute("INSERT INTO eventos VALUES (?,?,?,?,?)", (t, ev, sid, mid, json.dumps(datos, default=str)))
        self.db.commit()

    def _s(self, sid):
        if sid not in self.solicitudes:
            raise KeyError("Solicitud no encontrada")
        return self.solicitudes[sid]

    def _m(self, mid):
        if mid not in self.repartidores:
            raise KeyError("Prestador no encontrado")
        return self.repartidores[mid]

    # ------------------------------------------------------------------ talleres
    def _catalogo_inicial(self, demo):
        """Catálogo con que arranca un taller. Los talleres de demostración tienen todo disponible (con una pequeña
        variación de precio); uno recién registrado parte de los precios de referencia y marca lo que tiene."""
        pr = self.config["precios"]
        factor = (1.0, 1.03, 0.97, 1.05)[len(self.talleres) % 4] if demo else 1.0
        comb = {k: dict(litro=round(v["litro"] * factor, 2), disponible=demo) for k, v in pr["combustibles"].items()}
        bats = [dict(id=_nuevo_id(), nombre=v["nombre"], categoria=k, precio=round(v["precio"] * factor, 2),
                     stock=4 if demo else 0) for k, v in pr["baterias"].items()]
        return comb, bats

    def agregar_taller(self, nombre, lat, lon, telefono="", direccion="", demo=False):
        with self.lock:
            tid = _nuevo_id()
            comb, bats = self._catalogo_inicial(demo)
            self.talleres[tid] = dict(id=tid, nombre=(nombre or "Taller").strip()[:60] or "Taller", lat=lat, lon=lon,
                                      telefono=(telefono or "").strip()[:30], direccion=(direccion or "").strip()[:120],
                                      demo=demo, combustibles=comb, baterias=bats, ventas=[], creado=self.reloj())
            if not demo:
                self.evento("taller_registrado", tid=tid)
            self.version += 1
            return self.talleres[tid]

    def _t(self, tid):
        if tid not in self.talleres:
            raise KeyError("Taller no encontrado")
        return self.talleres[tid]

    def actualizar_taller(self, tid, nombre=None, telefono=None, direccion=None, lat=None, lon=None):
        with self.lock:
            t = self._t(tid)
            if nombre is not None and nombre.strip():
                t["nombre"] = nombre.strip()[:60]
            if telefono is not None:
                t["telefono"] = telefono.strip()[:30]
            if direccion is not None:
                t["direccion"] = direccion.strip()[:120]
            if lat is not None and lon is not None:
                t["lat"], t["lon"] = lat, lon
            self.version += 1
            return t

    def actualizar_catalogo(self, tid, combustibles, baterias):
        """Reemplaza los precios de combustible y el inventario de baterías de un taller (valida todo antes de guardar)."""
        ref = self.config["precios"]
        comb = {}
        for k, v in (combustibles or {}).items():
            if k not in ref["combustibles"]:
                raise ValueError(f"Combustible no válido: {k}")
            litro = float(v.get("litro", 0))
            if not 5 <= litro <= 100:
                raise ValueError("El precio por litro debe estar entre 5 y 100 pesos")
            comb[k] = dict(litro=round(litro, 2), disponible=bool(v.get("disponible")))
        bats = []
        if len(baterias or []) > 12:
            raise ValueError("Máximo 12 baterías por taller")
        for b in baterias or []:
            if b.get("categoria") not in ref["baterias"]:
                raise ValueError("Categoría de batería no válida")
            nombre = str(b.get("nombre", "")).strip()[:60]
            if not nombre:
                raise ValueError("Cada batería necesita un nombre")
            precio, stock = float(b.get("precio", 0)), int(b.get("stock", 0))
            if not 100 <= precio <= 50000:
                raise ValueError("El precio de la batería debe estar entre 100 y 50,000 pesos")
            if not 0 <= stock <= 999:
                raise ValueError("El inventario va de 0 a 999")
            bid = str(b.get("id") or "")
            bats.append(dict(id=bid if bid.isalnum() and len(bid) <= 12 else _nuevo_id(), nombre=nombre,
                             categoria=b["categoria"], precio=round(precio, 2), stock=stock))
        with self.lock:
            t = self._t(tid)
            for k in t["combustibles"]:                       # lo que no se envía se conserva
                comb.setdefault(k, t["combustibles"][k])
            t["combustibles"], t["baterias"] = comb, bats
            self.evento("taller_catalogo", tid=tid)
            return t

    def actualizar_precios_referencia(self, combustibles=None, baterias=None, servicios=None):
        """Precios de referencia: litro de cada combustible, batería por categoría y cargo por servicio. Se guardan en disco."""
        with self.lock:
            pr = self.config["precios"]
            for k, v in (combustibles or {}).items():
                if k not in pr["combustibles"] or not 5 <= float(v) <= 100:
                    raise ValueError("El precio por litro debe estar entre 5 y 100 pesos")
            for k, v in (baterias or {}).items():
                if k not in pr["baterias"] or not 100 <= float(v) <= 50000:
                    raise ValueError("Precio de batería no válido")
            for k, v in (servicios or {}).items():
                if k not in pr["servicios"] or not 0 <= float(v) <= 2000:
                    raise ValueError("El cargo por servicio debe estar entre 0 y 2,000 pesos")
            for k, v in (combustibles or {}).items():
                pr["combustibles"][k]["litro"] = round(float(v), 2)
            for k, v in (baterias or {}).items():
                pr["baterias"][k]["precio"] = round(float(v), 2)
            for k, v in (servicios or {}).items():
                pr["servicios"][k] = round(float(v), 2)
            self._guardar("precios", pr)
            self.version += 1

    def taller_mas_cercano(self, lat, lon):
        with self.lock:
            if not self.talleres:
                return None
            t = min(self.talleres.values(), key=lambda t: motor.distancia_m(lat, lon, t["lat"], t["lon"]))
            return dict(nombre=t["nombre"], lat=t["lat"], lon=t["lon"], distancia_km=motor.distancia_m(lat, lon, t["lat"], t["lon"]) / 1000)

    # ------------------------------------------------------------------ precios y cotización
    def cotizar(self, tipo, lat, lon, opcion=None, cantidad=None):
        """Cotiza gasolina o batería nueva (otros servicios devuelven None).

        El producto sale del taller más cercano (dentro de radio_taller_km) que lo tenga disponible, con SU precio.
        Si ninguno lo tiene, se cotiza con el precio de referencia y se marca `referencia`.
        total = producto + cargo de envío. La app no cobra: el repartidor cobra el total que vio el cliente."""
        if tipo not in ("gasolina", "bateria_nueva", "aire_llantas", "paso_de_corriente"):
            return None
        with self.lock:
            pr, radio_m = self.config["precios"], self.config["radio_taller_km"] * 1000
            servicio = pr["servicios"][tipo]
            if tipo in ("aire_llantas", "paso_de_corriente"):        # sin producto: el repartidor lleva la bomba o los cables
                concepto = "Aire a las llantas (llevamos bomba)" if tipo == "aire_llantas" else "Paso de corriente (llevamos cables)"
                return dict(tipo=tipo, opcion=None, cantidad=None, concepto=concepto, detalle="", subtotal=0.0, servicio=servicio,
                            total=round(servicio, 2), moneda="MXN", taller=None, referencia=False, bateria_id=None)
            # Los talleres de demostración son ficticios: no fijan precios ni se le indican a un repartidor, salvo que se pida.
            cerca = sorted(((motor.distancia_m(lat, lon, t["lat"], t["lon"]), t) for t in self.talleres.values()
                            if not t["demo"] or self.config["demo_en_cotizacion"]), key=lambda x: x[0])
            cerca = [(d, t) for d, t in cerca if d <= radio_m]
            bateria_id = None
            if tipo == "gasolina":
                opcion = opcion or "magna"
                if opcion not in pr["combustibles"]:
                    raise ValueError("Combustible no válido")
                litros = motor.LITROS_DEFAULT if cantidad is None else cantidad
                if not isinstance(litros, int) or not motor.LITROS_MIN <= litros <= motor.LITROS_MAX:
                    raise ValueError(f"Los litros van de {motor.LITROS_MIN} a {motor.LITROS_MAX}")
                cantidad, nombre = litros, pr["combustibles"][opcion]["nombre"]
                fuente = next(((d, t) for d, t in cerca if t["combustibles"].get(opcion, {}).get("disponible")), None)
                precio_u = fuente[1]["combustibles"][opcion]["litro"] if fuente else pr["combustibles"][opcion]["litro"]
                subtotal, concepto = round(precio_u * litros, 2), f"{litros} L de gasolina {nombre}" if opcion != "diesel" else f"{litros} L de diésel"
                detalle = f"${precio_u:.2f} por litro"
            else:
                opcion = opcion or "estandar"
                if opcion not in pr["baterias"]:
                    raise ValueError("Tipo de batería no válido")
                cantidad = 1
                fuente, mejor = None, None
                for d, t in cerca:
                    opciones = [b for b in t["baterias"] if b["categoria"] == opcion and b["stock"] > 0]
                    if opciones:
                        fuente, mejor = (d, t), min(opciones, key=lambda b: b["precio"])
                        break
                precio_u = mejor["precio"] if mejor else pr["baterias"][opcion]["precio"]
                nombre = mejor["nombre"] if mejor else pr["baterias"][opcion]["nombre"]
                bateria_id = mejor["id"] if mejor else None
                subtotal, concepto, detalle = round(precio_u, 2), f"{nombre} (instalación incluida)", ""
            taller = None
            if fuente:
                d, t = fuente
                taller = dict(id=t["id"], nombre=t["nombre"], lat=t["lat"], lon=t["lon"], telefono=t["telefono"],
                              direccion=t["direccion"], distancia_km=d / 1000)
            return dict(tipo=tipo, opcion=opcion, cantidad=cantidad, concepto=concepto, detalle=detalle, subtotal=subtotal,
                        servicio=servicio, total=round(subtotal + servicio, 2), moneda="MXN", taller=taller,
                        referencia=taller is None, bateria_id=bateria_id)

    # ------------------------------------------------------------------ cuentas del negocio
    _NOMBRES_COSTOS = dict(hora_repartidor="Costo por hora del repartidor", costo_km="Costo por kilómetro", km_ida_vuelta="Kilómetros por servicio",
                           margen_objetivo="Margen objetivo", gastos_fijos_mes="Gastos fijos al mes", comision_pago="Comisión de pago")

    def guardar_costos(self, cambios):
        """Valida y guarda los supuestos de costos (se usan en /negocio). Nada se guarda si algún valor es inválido."""
        c = copy.deepcopy(self.costos)
        rangos = dict(hora_repartidor=(0, 2000), costo_km=(0, 100), km_ida_vuelta=(0, 200), margen_objetivo=(0, 0.9),
                      gastos_fijos_mes=(0, 1e6), comision_pago=(0, 0.5))
        for k, (lo, hi) in rangos.items():
            if cambios.get(k) is not None:
                v = float(cambios[k])
                if not lo <= v <= hi:
                    esc = 100 if k in ("margen_objetivo", "comision_pago") else 1          # estos se escriben en porcentaje
                    raise ValueError(f"{self._NOMBRES_COSTOS[k]}: debe estar entre {lo * esc:g}{' %' if esc > 1 else ''} y {hi * esc:g}{' %' if esc > 1 else ''}")
                c[k] = v
        for grupo, lo, hi in (("minutos", 1, 600), ("kit", 0, 100000), ("vida_util_servicios", 1, 100000)):
            for k, v in (cambios.get(grupo) or {}).items():
                if k not in c[grupo]:
                    raise ValueError(f"Dato desconocido: {k}")
                if not lo <= float(v) <= hi:
                    raise ValueError(f"{k}: debe estar entre {lo:g} y {hi:g}")
                c[grupo][k] = float(v)
        with self.lock:
            self.costos = c
            self._guardar("costos", c)
            return c

    def tiempos_reales(self):
        """Promedios de los servicios reales terminados (sin los simulados): cuánto espera el cliente y cuánto tarda cada servicio."""
        with self.lock:
            filas = self.db.execute(
                "SELECT tipo, COUNT(*), AVG(acepto - creado), AVG(llego - acepto), AVG(fin - llego), AVG(fin - acepto) FROM servicios_hechos "
                "WHERE simulado = 0 AND acepto IS NOT NULL AND llego IS NOT NULL GROUP BY tipo").fetchall()
            return {f[0]: dict(n=f[1], espera_min=f[2] / 60, viaje_min=f[3] / 60, servicio_min=f[4] / 60, total_min=f[5] / 60) for f in filas}

    def negocio(self, usar_reales=False):
        """Cuentas por servicio: costo, cargo actual, ganancia, margen, cargo sugerido y cuántos servicios al mes cubren los gastos fijos."""
        with self.lock:
            c, pr, reales = self.costos, self.config["precios"], self.tiempos_reales()
            magna = pr["combustibles"]["magna"]["litro"]
            herramienta = dict(gasolina="bidon", aire_llantas="bomba", paso_de_corriente="cables")
            escenarios = [("gasolina_5", "Gasolina 5 L", "gasolina", 5), ("gasolina_10", "Gasolina 10 L", "gasolina", 10),
                          ("aire_llantas", "Aire a las llantas", "aire_llantas", 0), ("paso_de_corriente", "Paso de corriente", "paso_de_corriente", 0)]
            filas, costo_por_tipo = [], {}
            for clave, nombre, tipo, litros in escenarios:
                minutos, fuente = c["minutos"][tipo], "supuesto"
                if usar_reales and tipo in reales:
                    minutos, fuente = reales[tipo]["total_min"], f"real ({reales[tipo]['n']} servicios)"
                producto = round(litros * magna, 2)           # la gasolina se compra y se cobra al mismo precio: no deja ganancia
                cargo = pr["servicios"][tipo]
                h = herramienta[tipo]
                t_costo, tr_costo = minutos / 60 * c["hora_repartidor"], c["km_ida_vuelta"] * c["costo_km"]
                herr, comision = c["kit"][h] / c["vida_util_servicios"][h], (producto + cargo) * c["comision_pago"]
                costo = t_costo + tr_costo + herr + comision
                ganancia = cargo - costo
                sugerido = math.ceil(costo / (1 - c["margen_objetivo"]) / 5) * 5          # redondeado hacia arriba a múltiplos de $5
                filas.append(dict(clave=clave, nombre=nombre, tipo=tipo, litros=litros, minutos=minutos, fuente=fuente, producto=producto,
                                  cargo=cargo, total=round(producto + cargo, 2), costo_tiempo=t_costo, costo_transporte=tr_costo,
                                  costo_herramienta=herr, comision=comision, costo=costo, ganancia=ganancia,
                                  margen=ganancia / cargo if cargo else None, cargo_sugerido=sugerido, total_sugerido=round(producto + sugerido, 2),
                                  equilibrio=math.ceil(c["gastos_fijos_mes"] / ganancia) if ganancia > 0 else None))
                costo_por_tipo.setdefault(tipo, costo)
            hechos = self.db.execute("SELECT tipo, COUNT(*), SUM(total), SUM(servicio) FROM servicios_hechos WHERE simulado = 0 GROUP BY tipo").fetchall()
            n = sum(f[1] for f in hechos)
            ingresos, cargos = sum(f[2] or 0 for f in hechos), sum(f[3] or 0 for f in hechos)
            costos_est = sum(f[1] * costo_por_tipo.get(f[0], 0) for f in hechos)
            simulados = self.db.execute("SELECT COUNT(*) FROM servicios_hechos WHERE simulado = 1").fetchone()[0]
            return dict(filas=filas, reales=reales, costos=c, cargos=pr["servicios"], usar_reales=usar_reales,
                        resumen=dict(servicios=n, ingresos=ingresos, cargos=cargos, costo_estimado=costos_est,
                                     ganancia_estimada=cargos - costos_est, simulados=simulados))

    # ------------------------------------------------------------------ lugares con QR y ajustes de contacto
    MAX_LUGARES = 200

    @staticmethod
    def _fila_lugar(f):
        return dict(codigo=f[0], nombre=f[1], lat=f[2], lon=f[3], escaneos=f[4], creado=f[5], general=f[2] is None)

    def crear_lugar(self, nombre, lat=None, lon=None):
        """Un lugar es un punto físico con letrero (una gasolinera, una esquina). Sin coordenadas es un QR general
        (tarjetas, imanes): al escanearlo la persona marca su ubicación con el GPS."""
        nombre = (nombre or "").strip()[:60]
        if not nombre:
            raise ValueError("Escribe el nombre del lugar")
        if (lat is None) != (lon is None):
            raise ValueError("Marca la ubicación completa o déjala vacía")
        if lat is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Coordenadas no válidas")
        with self.lock:
            if self.db.execute("SELECT COUNT(*) FROM lugares").fetchone()[0] >= self.MAX_LUGARES:
                raise ValueError(f"Máximo {self.MAX_LUGARES} lugares")
            codigo = _nuevo_id()[:6]
            while self.db.execute("SELECT 1 FROM lugares WHERE codigo=?", (codigo,)).fetchone():
                codigo = _nuevo_id()[:6]
            self.db.execute("INSERT INTO lugares VALUES (?,?,?,?,0,?)", (codigo, nombre, lat, lon, self.reloj()))
            self.db.commit()
            self.evento("lugar_creado", lugar=codigo, general=lat is None)
            return self.lugar(codigo)

    def lugar(self, codigo):
        with self.lock:
            f = self.db.execute("SELECT codigo, nombre, lat, lon, escaneos, creado FROM lugares WHERE codigo=?", (codigo,)).fetchone()
            return self._fila_lugar(f) if f else None

    def listar_lugares(self):
        with self.lock:
            return [self._fila_lugar(f) for f in self.db.execute(
                "SELECT codigo, nombre, lat, lon, escaneos, creado FROM lugares ORDER BY creado DESC, codigo")]

    def borrar_lugar(self, codigo):
        with self.lock:
            if not self.lugar(codigo):
                raise KeyError("Lugar no encontrado")
            self.db.execute("DELETE FROM lugares WHERE codigo=?", (codigo,))
            self.db.commit()

    def escanear(self, codigo):
        """Cuenta un escaneo del QR (aproximado: también cuenta vistas previas de enlaces). Devuelve el lugar o None."""
        with self.lock:
            if not self.lugar(codigo):
                return None
            self.db.execute("UPDATE lugares SET escaneos = escaneos + 1 WHERE codigo=?", (codigo,))
            self.db.commit()
            self.evento("qr_escaneado", lugar=codigo)
            return self.lugar(codigo)

    def ajustes(self):
        with self.lock:
            base = dict(url_publica="", whatsapp="", telefono="")
            base.update({k: v for k, v in self.db.execute("SELECT clave, valor FROM ajustes") if k in base})
            return base

    def guardar_ajustes(self, url_publica=None, whatsapp=None, telefono=None):
        """Valida y guarda: dirección pública de la app (la que codifican los QR) y datos de contacto."""
        nuevos = {}
        if url_publica is not None:
            u = url_publica.strip()
            if u:
                pu = urlparse(u)
                if pu.scheme not in ("http", "https") or not pu.netloc or " " in u or pu.query or pu.fragment or len(u) > 200:
                    raise ValueError("La dirección debe verse así: https://tudominio.com")
                u = f"{pu.scheme}://{pu.netloc}{pu.path.rstrip('/')}"
            nuevos["url_publica"] = u
        if whatsapp is not None:
            d = re.sub(r"\D", "", whatsapp)
            if d and len(d) == 10:
                d = "52" + d                                # número mexicano de 10 dígitos
            if d and not 11 <= len(d) <= 15:
                raise ValueError("El WhatsApp debe tener 10 dígitos (con lada) o incluir el código de país")
            nuevos["whatsapp"] = d
        if telefono is not None:
            tel = telefono.strip()
            if not re.fullmatch(r"[0-9+\-() ]{0,30}", tel):
                raise ValueError("El teléfono solo puede llevar números, espacios, +, - y paréntesis")
            nuevos["telefono"] = tel
        with self.lock:
            for k, v in nuevos.items():
                self.db.execute("INSERT INTO ajustes VALUES (?,?) ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor", (k, v))
            self.db.commit()
            return self.ajustes()

    # ------------------------------------------------------------------ solicitudes
    def crear_solicitud(self, nombre, lat, lon, tipo, via="calle", notas="", riesgo=False, padre_id=None,
                        opcion=None, cantidad=None, lugar="", placa=None, color=None):
        if tipo not in motor.TIPOS:
            raise ValueError("Tipo de falla no válido")
        if via not in motor.VIAS:
            raise ValueError("Tipo de vía no válido")
        with self.lock:
            cotizacion = self.cotizar(tipo, lat, lon, opcion, cantidad)       # el precio lo fija el servidor, no el cliente
            vehiculo = None if placa is None and color is None else dict(placa=motor.normalizar_placa(placa), color=motor.validar_color(color))
            hora = time.localtime(self.reloj()).tm_hour
            nivel, razones = motor.prioridad(tipo, via, hora, riesgo)
            if padre_id and nivel < 3:                                  # ya esperó atención previa: sube la prioridad
                nivel, razones = nivel + 1, razones + ["Ya esperó atención previa: sube la prioridad"]
            sid = _nuevo_id()
            s = dict(id=sid, nombre=(nombre or "Cliente")[:60], lat=lat, lon=lon, tipo=tipo, nivel=motor.NIVEL_SERVICIO[tipo],
                     via=via, notas=notas[:300], riesgo=bool(riesgo), prioridad=nivel, razones=razones, estado="pendiente",
                     t_creada=self.reloj(), t_aceptada=None, t_llegada=None, t_fin=None, repartidor_id=None, excluidos=[], excluido_hasta={},
                     oferta_expira=None, eta_s=None, calificacion=None, comentario="", padre_id=padre_id, hija_id=None,
                     resultado=None, diagnostico="", cotizacion=cotizacion, vehiculo=vehiculo,
                     pin=f"{secrets.randbelow(10000):04d}", pin_intentos=0, pin_obligatorio=bool(self.config.get("requiere_pin", True)),
                     lugar=(lugar or "").strip()[:60])
            self.solicitudes[sid] = s
            self.evento("solicitud_creada", sid, tipo=tipo, prioridad=nivel, via=via, padre=padre_id,
                        total=cotizacion["total"] if cotizacion else None)
            return s

    def cancelar_solicitud(self, sid):
        with self.lock:
            s = self._s(sid)
            if s["estado"] in ("terminada", "cancelada", "escalada"):
                raise ValueError("La solicitud ya no se puede cancelar")
            m = self.repartidores.get(s["repartidor_id"]) if s["repartidor_id"] else None
            if m:
                m["estado"], m["solicitud_id"] = "libre", None
            s["estado"], s["repartidor_id"], s["oferta_expira"] = "cancelada", None, None
            self.evento("solicitud_cancelada", sid, m["id"] if m else None)

    def calificar(self, sid, estrellas, comentario=""):
        with self.lock:
            s = self._s(sid)
            if s["estado"] != "terminada":
                raise ValueError("Solo se puede calificar un servicio terminado")
            if not 1 <= estrellas <= 5:
                raise ValueError("La calificación va de 1 a 5")
            s["calificacion"], s["comentario"] = estrellas, comentario[:300]
            self.evento("calificacion", sid, s["repartidor_id"], estrellas=estrellas)

    # ------------------------------------------------------------------ prestadores (repartidor, grúa)
    @staticmethod
    def _vehiculo_repartidor(v):
        """Valida el vehículo de un repartidor: tipo, color y placa (la placa no aplica a bicicleta ni a pie)."""
        if v is None:
            return None
        tipo = (v.get("tipo") or "").strip().lower()
        if tipo not in motor.TIPOS_VEHICULO:
            raise ValueError("Elige tu tipo de vehículo")
        placa = "" if tipo in motor.SIN_PLACA else motor.normalizar_placa(v.get("placa"))
        modelo = "" if tipo in motor.SIN_PLACA else motor.validar_modelo(v.get("modelo"))
        return dict(tipo=tipo, color=motor.validar_color(v.get("color")), placa=placa, modelo=modelo)

    def registrar_repartidor(self, nombre, habilidades, bot=False, lat=None, lon=None, prestador="repartidor", vehiculo=None):
        if prestador not in motor.PRESTADORES:
            raise ValueError("Tipo de prestador no válido")
        vehiculo = self._vehiculo_repartidor(vehiculo)
        hab = [h for h in habilidades if h in motor.PRESTADORES[prestador]["permitidas"]]
        if not hab:
            raise ValueError("Elige al menos un servicio que puedas dar")
        with self.lock:
            mid = _nuevo_id()
            self.repartidores[mid] = dict(id=mid, nombre=(nombre or motor.PRESTADORES[prestador]["nombre"])[:60], habilidades=hab,
                                       prestador=prestador, bot=bot, estado="fuera", lat=lat, lon=lon, solicitud_id=None,
                                       servicios=0, _acepta_en=None, _servicio_hasta=None, vehiculo=vehiculo)
            self.evento("prestador_registrado", mid=mid, bot=bot, prestador=prestador)
            return self.repartidores[mid]

    def turno(self, mid, activo, lat=None, lon=None):
        with self.lock:
            m = self._m(mid)
            if lat is not None and lon is not None:
                m["lat"], m["lon"] = lat, lon
            if activo:
                if m["lat"] is None:
                    raise ValueError("Necesitamos tu ubicación para ponerte en turno")
                if m["estado"] == "fuera":
                    m["estado"] = "libre"
                    self.evento("turno_inicio", mid=mid)
            else:
                if m["estado"] == "ocupado":
                    raise ValueError("Termina tu servicio antes de salir de turno")
                if m["estado"] == "ofertado":
                    self._devolver_oferta(m, "prestador_salio")
                m["estado"] = "fuera"
                self.evento("turno_fin", mid=mid)

    def ubicacion(self, mid, lat, lon):
        with self.lock:
            m = self._m(mid)
            m["lat"], m["lon"] = lat, lon
            self.version += 1

    def _devolver_oferta(self, m, motivo):
        s = self.solicitudes.get(m["solicitud_id"])
        if s and s["estado"] == "ofertada":
            s["estado"], s["repartidor_id"], s["oferta_expira"] = "pendiente", None, None
            if motivo == "oferta_rechazada":
                # Solo un "no" explícito aparta al repartidor, y por un rato (reoferta_s): si no hay nadie más, se le vuelve a ofrecer.
                # Dejar pasar el tiempo o salir de turno NO lo aparta; antes quedaba excluido para siempre y la petición se atoraba.
                if m["id"] not in s["excluidos"]:
                    s["excluidos"].append(m["id"])
                s["excluido_hasta"][m["id"]] = self.reloj() + self.config["reoferta_s"]
        m["solicitud_id"] = None
        self.evento(motivo, s["id"] if s else None, m["id"])

    def responder_oferta(self, mid, sid, acepta):
        with self.lock:
            m, s = self._m(mid), self._s(sid)
            if m["estado"] != "ofertado" or m["solicitud_id"] != sid or s["estado"] != "ofertada":
                raise ValueError("La oferta ya no está vigente")
            if acepta:
                s["estado"], s["t_aceptada"] = "aceptada", self.reloj()
                m["estado"] = "ocupado"
                self.evento("oferta_aceptada", sid, mid, eta_s=s["eta_s"])
            else:
                self._devolver_oferta(m, "oferta_rechazada")
                m["estado"] = "libre"

    def _excluido(self, s, mid, ahora):
        return mid in s["excluidos"] and ahora < s["excluido_hasta"].get(mid, float("inf"))

    def _liberar(self, m, s):
        m["estado"], m["solicitud_id"] = "libre", None
        m["servicios"] += 1
        m["lat"], m["lon"] = s["lat"], s["lon"]

    def opciones_escalar(self, m):
        """Un repartidor que encuentra una falla mayor pide una grúa; la grúa ya es el último nivel."""
        if "grua" not in self.config["servicios_activos"]:
            return []
        nivel = motor.PRESTADORES[m["prestador"]]["nivel"]
        return ["escalar_grua"] if motor.NIVEL_SERVICIO["grua"] > nivel else []

    def _guardar_servicio_hecho(self, s, m):
        c = s.get("cotizacion") or {}
        self.db.execute("INSERT OR REPLACE INTO servicios_hechos VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (s["id"], s["tipo"], s["t_creada"], s["t_aceptada"], s["t_llegada"], s["t_fin"],
                         c.get("subtotal"), c.get("servicio"), c.get("total"), int(bool(m["bot"]))))
        self.db.commit()

    def _registrar_venta(self, s):
        """Al terminar un pedido con producto, descuenta la batería del inventario del taller y anota la venta."""
        c = s.get("cotizacion")
        if not c or not c["taller"]:
            return
        t = self.talleres.get(c["taller"]["id"])
        if not t:
            return
        if c["bateria_id"]:
            for b in t["baterias"]:
                if b["id"] == c["bateria_id"]:
                    b["stock"] = max(0, b["stock"] - 1)
        t["ventas"].insert(0, dict(solicitud_id=s["id"], concepto=c["concepto"], subtotal=c["subtotal"], total=c["total"], t=self.reloj()))
        del t["ventas"][50:]

    def avanzar(self, mid, accion, nota="", pin=None):
        """Acciones del prestador: llegue (con el PIN que le da el cliente), termine, escalar_grua."""
        with self.lock:
            m = self._m(mid)
            if m["estado"] != "ocupado" or not m["solicitud_id"]:
                raise ValueError("No tienes un servicio activo")
            s = self.solicitudes[m["solicitud_id"]]
            ahora = self.reloj()
            if accion == "llegue" and s["estado"] == "aceptada":
                if s.get("pin_obligatorio"):
                    # El PIN lo ve solo el cliente y se lo da al repartidor cuando este llega: sin él no se puede marcar la llegada,
                    # así que nadie cobra por un servicio sin estar con el cliente, ni un impostor puede empezar un pedido ajeno.
                    if s["pin_intentos"] >= MAX_PIN_INTENTOS:
                        raise ValueError("Demasiados intentos con el PIN. Avisa al cliente para que cancele o llama al administrador.")
                    if not hmac.compare_digest(str(pin or "").strip(), s["pin"]):
                        s["pin_intentos"] += 1
                        self.evento("pin_incorrecto", s["id"], mid, intentos=s["pin_intentos"])
                        quedan = MAX_PIN_INTENTOS - s["pin_intentos"]
                        raise ValueError("PIN incorrecto. Pídeselo al cliente." + (f" Te quedan {quedan} intentos." if quedan else " Ya no te quedan intentos."))
                s["estado"], s["t_llegada"] = "en_sitio", ahora
                self.evento("prestador_llego", s["id"], mid, espera_s=ahora - s["t_creada"])
            elif accion == "termine" and s["estado"] == "en_sitio":
                s["estado"], s["t_fin"], s["resultado"] = "terminada", ahora, "resuelta"
                self._liberar(m, s)
                self._registrar_venta(s)
                self._guardar_servicio_hecho(s, m)
                self.evento("servicio_terminado", s["id"], mid, duracion_s=ahora - s["t_llegada"], nivel=s["nivel"],
                            prestador=m["prestador"])
            elif accion == "escalar_grua" and s["estado"] == "en_sitio":
                if accion not in self.opciones_escalar(m):
                    raise ValueError("Esa escalada no corresponde a tu nivel")
                destino = "grua"
                s["estado"], s["t_fin"], s["resultado"], s["diagnostico"] = "escalada", ahora, "escalada", nota[:300]
                self._liberar(m, s)
                hija = self.crear_solicitud(s["nombre"], s["lat"], s["lon"], destino, s["via"],
                                            f"Escalada desde la solicitud {s['id']}. Diagnóstico: {nota[:200] or 'falla mayor'}",
                                            s["riesgo"], padre_id=s["id"], lugar=s["lugar"],
                                            **(dict(placa=s["vehiculo"]["placa"], color=s["vehiculo"]["color"]) if s.get("vehiculo") else {}))
                s["hija_id"] = hija["id"]
                self.evento("solicitud_escalada", s["id"], mid, destino=destino, hija=hija["id"], nivel=s["nivel"])
            else:
                raise ValueError("Acción no válida en el estado actual")

    # ------------------------------------------------------------------ despacho
    def paso_despacho(self):
        """Asigna el lote de solicitudes pendientes a los prestadores libres con la política configurada."""
        with self.lock:
            pend = sorted((s for s in self.solicitudes.values() if s["estado"] == "pendiente"), key=lambda s: s["t_creada"])
            libres = [m for m in self.repartidores.values() if m["estado"] == "libre" and m["lat"] is not None]
            if not pend or not libres:
                return []
            vel, radio_m = self.config["vel_kmh"], self.config["radio_km"] * 1000
            # 1) búsqueda de cercanos con el índice espacial; 2) filtro por habilidad; 3) matriz de ETA real
            indice = motor.IndiceEspacial((m["id"], m["lat"], m["lon"]) for m in libres)
            col = {m["id"]: j for j, m in enumerate(libres)}
            ahora0 = self.reloj()
            eta = np.full((len(pend), len(libres)), motor.GRANDE)
            for i, s in enumerate(pend):
                for mid, d in indice.cercanos(s["lat"], s["lon"], radio_m):
                    m = self.repartidores[mid]
                    if s["tipo"] in m["habilidades"] and not self._excluido(s, mid, ahora0):
                        eta[i, col[mid]] = motor.eta_desde_distancia(d, vel)
            # 4) costo de asignación = ETA (la prioridad la pondera la política elegida)
            costo = eta
            pares = motor.asignar(self.config["politica"], costo, [s["prioridad"] for s in pend], [s["t_creada"] for s in pend])
            ahora, salida = self.reloj(), []
            for i, j in pares:
                s, m = pend[i], libres[j]
                s.update(estado="ofertada", repartidor_id=m["id"], oferta_expira=ahora + self.config["oferta_s"], eta_s=float(eta[i, j]))
                m.update(estado="ofertado", solicitud_id=s["id"], _acepta_en=None)
                self.evento("oferta_enviada", s["id"], m["id"], eta_s=float(eta[i, j]), politica=self.config["politica"],
                            lote=len(pend), candidatos=len(libres), prestador=m["prestador"])
                salida.append((s["id"], m["id"]))
            return salida

    def cercanos(self, lat, lon, tipo=None):
        """Prestadores libres cerca de un punto (para mostrar disponibilidad antes de pedir ayuda).
        Las posiciones se redondean (~100 m) por privacidad."""
        with self.lock:
            libres = [m for m in self.repartidores.values() if m["estado"] == "libre" and m["lat"] is not None
                      and (tipo is None or tipo in m["habilidades"])]
            radio_m = self.config["radio_km"] * 1000
            cerca = motor.IndiceEspacial((m["id"], m["lat"], m["lon"]) for m in libres).cercanos(lat, lon, radio_m)
            lista = sorted(({"lat": round(self.repartidores[i]["lat"], 3), "lon": round(self.repartidores[i]["lon"], 3),
                             "prestador": self.repartidores[i]["prestador"],
                             "eta_s": motor.eta_desde_distancia(d, self.config["vel_kmh"])} for i, d in cerca),
                           key=lambda x: x["eta_s"])
            return dict(n=len(lista), radio_km=self.config["radio_km"],
                        eta_min_s=lista[0]["eta_s"] if lista else None, repartidores=lista)

    def expirar_ofertas(self):
        with self.lock:
            ahora = self.reloj()
            for s in list(self.solicitudes.values()):
                if s["estado"] == "pendiente" and ahora - s["t_creada"] > self.config["espera_max_s"]:
                    s["estado"], s["resultado"] = "cancelada", "sin_cobertura"      # nadie pudo atenderla: se cierra en vez de estorbar en la fila
                    self.evento("solicitud_sin_cobertura", s["id"])
                    self.version += 1
                if s["estado"] == "ofertada" and s["oferta_expira"] and ahora > s["oferta_expira"]:
                    m = self.repartidores[s["repartidor_id"]]
                    self._devolver_oferta(m, "oferta_expirada")
                    m["estado"] = "libre"

    # ------------------------------------------------------------------ prestadores simulados (bots)
    def tick(self, dt):
        """Hace que los prestadores simulados respondan y se muevan. Los reales actualizan su GPS por la API."""
        with self.lock:
            ahora = self.reloj()
            for m in list(self.repartidores.values()):
                if not m["bot"]:
                    continue
                if m["estado"] == "ofertado":
                    if m["_acepta_en"] is None:
                        m["_acepta_en"] = ahora + self.rng.uniform(1, 4)
                    elif ahora >= m["_acepta_en"]:
                        m["_acepta_en"] = None
                        self.responder_oferta(m["id"], m["solicitud_id"], self.rng.random() > self.config["rechazo_bot"])
                elif m["estado"] == "ocupado":
                    s = self.solicitudes[m["solicitud_id"]]
                    if s["estado"] == "aceptada":
                        d = motor.distancia_m(m["lat"], m["lon"], s["lat"], s["lon"])
                        paso = self.config["vel_bot_ms"] * dt
                        if d <= max(paso, 25):
                            m["lat"], m["lon"] = s["lat"], s["lon"]
                            self.avanzar(m["id"], "llegue", pin=s["pin"])        # los repartidores simulados conocen el PIN
                            m["_servicio_hasta"] = ahora + self.rng.uniform(*self.config["servicio_bot_s"])
                        else:
                            f = paso / d
                            m["lat"] += (s["lat"] - m["lat"]) * f
                            m["lon"] += (s["lon"] - m["lon"]) * f
                            self.version += 1
                    elif s["estado"] == "en_sitio" and m["_servicio_hasta"] and ahora >= m["_servicio_hasta"]:
                        self._decidir_bot(m, s)

    def _decidir_bot(self, m, s):
        """El bot decide si resolvió la falla o si hay que escalar (probabilidades configurables y supuestas)."""
        nivel = motor.PRESTADORES[m["prestador"]]["nivel"]
        prob = self.config["prob_escalar"] if nivel == 1 and "grua" in self.config["servicios_activos"] else 0
        if prob and self.rng.random() < prob:
            self.avanzar(m["id"], "escalar_grua", "Diagnóstico simulado: falla mayor")
        else:
            self.avanzar(m["id"], "termine")

    def _punto_aleatorio(self, radio_m=2500):
        ang, r = self.rng.uniform(0, 2 * math.pi), radio_m * math.sqrt(self.rng.random())
        return (CENTRO[0] + (r * math.sin(ang)) / 111320, CENTRO[1] + (r * math.cos(ang)) / (111320 * math.cos(math.radians(CENTRO[0]))))

    def crear_bots(self, n, prestador="repartidor"):
        if prestador not in motor.PRESTADORES:
            raise ValueError("Tipo de prestador no válido")
        with self.lock:
            base = sum(1 for m in self.repartidores.values() if m["bot"] and m["prestador"] == prestador)
            info = motor.PRESTADORES[prestador]
            for k in range(n):
                hab = list(info["por_defecto"])
                if prestador == "repartidor" and self.rng.random() < 0.35:
                    hab.append("bateria_nueva")
                lat, lon = self._punto_aleatorio()
                n = base + k + 1
                m = self.registrar_repartidor(f"{prestador.capitalize()} {n} (sim.)", hab, bot=True, lat=lat, lon=lon, prestador=prestador,
                                              vehiculo=dict(tipo="moto", color=motor.COLORES[n % len(motor.COLORES)], placa=f"SIM{n:03d}", modelo="Simulada 150"))
                self.turno(m["id"], True)

    def quitar_bots(self):
        with self.lock:
            for mid in [i for i, m in self.repartidores.items() if m["bot"] and m["estado"] in ("libre", "fuera")]:
                del self.repartidores[mid]
            self.version += 1

    def solicitud_aleatoria(self):
        lat, lon = self._punto_aleatorio()
        tipo = self.rng.choice(list(self.config["servicios_activos"]))
        via = self.rng.choices(list(motor.VIAS), weights=[55, 30, 15])[0]
        n = self.rng.randint(100, 999)
        return self.crear_solicitud(f"Prueba {n}", lat, lon, tipo, via, placa=f"PRB{n}", color=motor.COLORES[n % len(motor.COLORES)])

    # ------------------------------------------------------------------ vistas para la interfaz
    def _pub_solicitud(self, s):
        m = self.repartidores.get(s["repartidor_id"]) if s["repartidor_id"] else None
        eta = s["eta_s"]
        if m and s["estado"] == "aceptada" and m["lat"] is not None:
            por_calles = rutas.rutas.eta(m["lat"], m["lon"], s["lat"], s["lon"])         # None si no está activado o aún no llega la consulta
            eta = por_calles if por_calles is not None else motor.eta_s(m["lat"], m["lon"], s["lat"], s["lon"], self.config["vel_kmh"])
        return dict(id=s["id"], nombre=s["nombre"], lat=s["lat"], lon=s["lon"], tipo=s["tipo"], tipo_texto=motor.TIPOS[s["tipo"]],
                    nivel=s["nivel"], via=s["via"], notas=s["notas"], riesgo=s["riesgo"], prioridad=s["prioridad"],
                    prioridad_texto=motor.NOMBRE_PRIORIDAD[s["prioridad"]], razones=s["razones"], estado=s["estado"],
                    eta_s=eta, repartidor_id=s["repartidor_id"], calificacion=s["calificacion"],
                    padre_id=s["padre_id"], hija_id=s["hija_id"], resultado=s["resultado"], diagnostico=s["diagnostico"],
                    taller=self.taller_mas_cercano(s["lat"], s["lon"]) if s["tipo"] == "grua" else None,
                    cotizacion=s.get("cotizacion"), lugar=s.get("lugar", ""),
                    espera_s=(s["t_llegada"] - s["t_creada"]) if s["t_llegada"] else None,
                    expira_en_s=max(0.0, s["oferta_expira"] - self.reloj()) if s["estado"] == "ofertada" and s["oferta_expira"] else None)

    def _pub_repartidor(self, m):
        return dict(id=m["id"], nombre=m["nombre"], habilidades=m["habilidades"], prestador=m["prestador"],
                    nivel=motor.PRESTADORES[m["prestador"]]["nivel"], estado=m["estado"], lat=m["lat"], lon=m["lon"],
                    bot=m["bot"], servicios=m["servicios"], solicitud_id=m["solicitud_id"], opciones_escalar=self.opciones_escalar(m),
                    vehiculo=m.get("vehiculo"))

    def _espera(self, m):
        """Pedidos pendientes que este repartidor podría atender: dentro de su radio, fuera de él, o en pausa por un rechazo reciente."""
        radio_m, ahora, cerca, lejos, pausa = self.config["radio_km"] * 1000, self.reloj(), 0, 0, 0
        if m["lat"] is not None:
            for s in self.solicitudes.values():
                if s["estado"] != "pendiente" or s["tipo"] not in m["habilidades"]:
                    continue
                if motor.distancia_m(m["lat"], m["lon"], s["lat"], s["lon"]) > radio_m:
                    lejos += 1
                elif self._excluido(s, m["id"], ahora):
                    pausa += 1
                else:
                    cerca += 1
        return dict(cerca=cerca, lejos=lejos, pausa=pausa, radio_km=self.config["radio_km"])

    def _pub_taller(self, t, privado=False):
        d = dict(id=t["id"], nombre=t["nombre"], lat=t["lat"], lon=t["lon"], telefono=t["telefono"],
                 direccion=t["direccion"], demo=t["demo"], combustibles=t["combustibles"], baterias=t["baterias"])
        if privado:
            activos = [self._pub_solicitud(s) for s in self.solicitudes.values()
                       if s.get("cotizacion") and s["cotizacion"]["taller"] and s["cotizacion"]["taller"]["id"] == t["id"]
                       and s["estado"] in ("pendiente", "ofertada", "aceptada", "en_sitio")]
            for a in activos:
                a["nombre"] = ""                                      # el taller ve qué debe surtir, no quién es el cliente
            d.update(ventas=t["ventas"], total_vendido=round(sum(v["total"] for v in t["ventas"]), 2), pedidos_activos=activos)
        return d

    def snapshot(self, rol, id_=""):
        with self.lock:
            if rol == "cliente":
                s = self.solicitudes.get(id_)
                if not s:
                    return dict(rol=rol, solicitud=None, repartidor=None)
                m = self.repartidores.get(s["repartidor_id"]) if s["repartidor_id"] and s["estado"] in ("aceptada", "en_sitio") else None
                sol = self._pub_solicitud(s)
                if s["estado"] == "aceptada":
                    sol["pin"] = s["pin"]            # el PIN solo lo ve el cliente, y solo hasta que el repartidor llega
                return dict(rol=rol, solicitud=sol,
                            repartidor=dict(nombre=m["nombre"], prestador=m["prestador"], lat=m["lat"], lon=m["lon"],
                                            habilidades=m["habilidades"], vehiculo=m.get("vehiculo")) if m else None)
            if rol in ("repartidor", "prestador", "mecanico"):         # "mecanico" se acepta solo por compatibilidad
                m = self.repartidores.get(id_)
                if not m:
                    return dict(rol=rol, repartidor=None, solicitud=None)
                s = self.solicitudes.get(m["solicitud_id"]) if m["solicitud_id"] else None
                sol = self._pub_solicitud(s) if s else None
                if sol:
                    sol["nombre"] = ""                                # el nombre del cliente solo se ve tras aceptar, no en la oferta
                if s and s["estado"] in ("aceptada", "en_sitio") and s["repartidor_id"] == m["id"]:
                    sol["vehiculo_cliente"] = s.get("vehiculo")       # la placa del cliente solo se ve tras aceptar, no en la oferta
                    sol["cliente_nombre"] = s["nombre"]
                return dict(rol=rol, repartidor=dict(self._pub_repartidor(m), espera=self._espera(m)), solicitud=sol)
            if rol == "taller":
                t = self.talleres.get(id_)
                return dict(rol=rol, taller=self._pub_taller(t, privado=True) if t else None)
            return self.panel()

    def panel(self):
        with self.lock:
            sols = sorted(self.solicitudes.values(), key=lambda s: -s["t_creada"])
            esperas = [s["t_llegada"] - s["t_creada"] for s in self.solicitudes.values() if s["t_llegada"]]
            resp = self.cont["oferta_aceptada"] + self.cont["oferta_rechazada"] + self.cont["oferta_expirada"]
            # ¿qué porcentaje de las solicitudes de nivel 1 se resuelve en el lugar, sin necesitar grúa?
            raiz1 = [s for s in self.solicitudes.values() if s["padre_id"] is None and s["nivel"] == 1]
            res = sum(1 for s in raiz1 if s["resultado"] == "resuelta")
            esc = sum(1 for s in raiz1 if s["resultado"] == "escalada")
            return dict(rol="panel", config={**self.config, "servicio_bot_s": list(self.config["servicio_bot_s"])},
                        solicitudes=[self._pub_solicitud(s) for s in sols[:60]],
                        repartidores=[self._pub_repartidor(m) for m in self.repartidores.values()],
                        talleres=[self._pub_taller(t) for t in self.talleres.values()],
                        metricas=dict(solicitudes=len(self.solicitudes),
                                      por_estado=dict(Counter(s["estado"] for s in self.solicitudes.values())),
                                      repartidores_por_estado=dict(Counter(m["estado"] for m in self.repartidores.values())),
                                      prestadores=dict(Counter(m["prestador"] for m in self.repartidores.values())),
                                      espera_media_min=(sum(esperas) / len(esperas) / 60) if esperas else None,
                                      atendidas=len(esperas),
                                      aceptacion=(self.cont["oferta_aceptada"] / resp) if resp else None,
                                      nivel1_resueltas=res, nivel1_escaladas=esc,
                                      pct_sin_grua=(res / (res + esc)) if (res + esc) else None),
                        log=list(self.log)[:25])
