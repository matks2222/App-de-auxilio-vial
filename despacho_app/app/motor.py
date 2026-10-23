"""Motor de despacho: tiempo de llegada (ETA), triaje de prioridad y asignación por lote.

Es la misma lógica del simulador (despacho_sim.py), adaptada a coordenadas geográficas.
El ETA usa distancia en línea recta por un factor de rodeo; para producción debe
reemplazarse por un servicio de rutas (OSRM, GraphHopper, etc.).
"""
import math

import numpy as np
from scipy.optimize import linear_sum_assignment

try:                      # H3 (índice geográfico hexagonal de código abierto); es opcional
    import h3
    if not hasattr(h3, "latlng_to_cell"):
        h3 = None
except ImportError:
    h3 = None

RES_H3 = 8                # resolución de las celdas (alrededor de medio km de arista)

GRANDE = 1e7            # costo de un par imposible (sin habilidad o excluido)
RODEO = 1.35            # factor que convierte línea recta en distancia aproximada por calles
PESO_PRIORIDAD = {1: 1.0, 2: 1.5, 3: 3.0}
NOMBRE_PRIORIDAD = {1: "baja", 2: "media", 3: "crítica"}
POLITICAS = ["voraz", "voraz_prio", "hungaro_sp", "hungaro"]

TIPOS = {
    # Nivel 1: los resuelve un repartidor en el lugar (no necesita ser mecánico; lleva el producto o el kit básico)
    "aire_llantas": "Aire en las llantas",
    "paso_de_corriente": "Paso de corriente",
    "bateria_nueva": "Batería nueva (entrega e instalación)",
    "gasolina": "Gasolina (entrega a domicilio)",
    "cambio_de_llanta": "Cambio de llanta",
    "diagnostico": "No sé qué tiene: revisión básica",
    # Nivel 2: falla mayor; se lleva el auto a un taller
    "grua": "Grúa (llevar mi auto a un taller)",
}
NIVEL_SERVICIO = {"aire_llantas": 1, "paso_de_corriente": 1, "bateria_nueva": 1, "gasolina": 1, "cambio_de_llanta": 1,
                  "diagnostico": 1, "grua": 2}
SIMPLES = [k for k, n in NIVEL_SERVICIO.items() if n == 1]
VIAS = {"calle": "Calle local", "avenida": "Avenida", "via_rapida": "Vía rápida o periférico"}

# Quién da el servicio. 'permitidas' = habilidades que puede ofrecer; 'por_defecto' = las que aparecen marcadas.
PRESTADORES = {
    "repartidor": dict(nombre="Repartidor (no necesita ser mecánico)", nivel=1,
                       permitidas=SIMPLES, por_defecto=[k for k in SIMPLES if k != "bateria_nueva"]),
    "grua": dict(nombre="Operador de grúa", nivel=2, permitidas=["grua"], por_defecto=["grua"]),
}

# ------------------------------------------------------------------ precios (MXN)
# Valores de referencia que se usan cuando ningún taller cercano tiene el producto. Cada taller fija los suyos.
# Combustibles: promedio aproximado en CDMX (PetroIntelligence, jul-ago 2026). Baterías: estimaciones, sin fuente oficial.
PRECIOS_DEFAULT = {
    "combustibles": {
        "magna": {"nombre": "Magna (regular)", "litro": 23.8},
        "premium": {"nombre": "Premium", "litro": 28.7},
        "diesel": {"nombre": "Diésel", "litro": 27.0},
    },
    "baterias": {
        "compacto": {"nombre": "Batería para auto compacto", "precio": 1700.0},
        "estandar": {"nombre": "Batería para sedán o hatchback", "precio": 2300.0},
        "camioneta": {"nombre": "Batería para camioneta o SUV", "precio": 3000.0},
    },
    # Cargo fijo por el servicio a domicilio (lo que cubre al repartidor, el traslado y la ganancia). SON SUPUESTOS: ajústalos en /negocio
    # con tus números reales. El producto (la gasolina) se cobra aparte, por litro. Aire y paso de corriente solo llevan este cargo.
    "servicios": {"gasolina": 100.0, "aire_llantas": 100.0, "paso_de_corriente": 120.0, "bateria_nueva": 90.0},
}
# Servicios que hoy se ofrecen al cliente. Los demás (batería, llanta, grúa...) siguen en el código, apagados, por si se reactivan.
ACTIVOS = ["gasolina", "aire_llantas", "paso_de_corriente"]

# Supuestos para las cuentas del negocio (/negocio). TODOS son estimaciones hasta tener datos reales, salvo la bomba de $200 que dio el dueño.
COSTOS_DEFAULT = {
    "hora_repartidor": 90.0,        # pesos por hora de trabajo del repartidor
    "costo_km": 3.0,                # gasolina, desgaste y mantenimiento de la moto, por km
    "km_ida_vuelta": 6.0,           # kilómetros promedio de un servicio (ida y vuelta)
    "minutos": {"gasolina": 35, "aire_llantas": 25, "paso_de_corriente": 30},   # de que acepta el pedido a que termina
    "kit": {"bomba": 200.0, "cables": 300.0, "bidon": 120.0},                    # inversión inicial en herramienta
    "vida_util_servicios": {"bomba": 300, "cables": 300, "bidon": 500},          # servicios que aguanta cada herramienta
    "margen_objetivo": 0.30,        # ganancia deseada sobre el cargo por servicio
    "gastos_fijos_mes": 300.0,      # hosting, dominio, impresión de letreros...
    "comision_pago": 0.0,           # si algún día se cobra con tarjeta (porcentaje del total, 0.035 = 3.5 %)
}
LITROS_MIN, LITROS_MAX, LITROS_DEFAULT = 1, 40, 10


def distancia_m(lat1, lon1, lat2, lon2):
    """Distancia en metros entre dos puntos (fórmula de haversine)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def eta_desde_distancia(d_m, vel_kmh):
    return d_m * RODEO / (vel_kmh / 3.6)


def eta_s(lat1, lon1, lat2, lon2, vel_kmh):
    return eta_desde_distancia(distancia_m(lat1, lon1, lat2, lon2), vel_kmh)


class IndiceEspacial:
    """Prestadores indexados por celda hexagonal H3 para buscar a los cercanos sin recorrerlos a todos.

    Si H3 no está instalado, hace una búsqueda exhaustiva (mismo resultado, más lenta con muchos prestadores).
    Con pocas decenas de prestadores no se nota la diferencia; sirve cuando la red crece.
    """

    def __init__(self, items):
        self.items = {i: (la, lo) for i, la, lo in items}
        self.celdas = {}
        if h3:
            for i, (la, lo) in self.items.items():
                self.celdas.setdefault(h3.latlng_to_cell(la, lo, RES_H3), []).append(i)

    def cercanos(self, lat, lon, radio_m):
        """Devuelve [(id, distancia_m)] de los prestadores a menos de radio_m."""
        if h3:
            arista_m = h3.average_hexagon_edge_length(RES_H3, unit="km") * 1000
            k = int(math.ceil(radio_m / (arista_m * math.sqrt(3)))) + 1
            ids = [i for c in h3.grid_disk(h3.latlng_to_cell(lat, lon, RES_H3), k) for i in self.celdas.get(c, [])]
        else:
            ids = list(self.items)
        salida = []
        for i in ids:
            d = distancia_m(lat, lon, *self.items[i])
            if d <= radio_m:
                salida.append((i, d))
        return salida


def prioridad(tipo, via, hora, riesgo):
    """Triaje inicial por reglas. Devuelve (nivel 1-3, lista de razones).

    PENDIENTE: reemplazar por el sistema de inferencia difuso (scikit-fuzzy) del proyecto.
    Los umbrales son supuestos y no están validados.
    """
    if riesgo:
        return 3, ["Reportó riesgo para su seguridad"]
    puntos, razones = 0, []
    if via == "via_rapida":
        puntos += 2; razones.append("Vía rápida: riesgo de choque")
    elif via == "avenida":
        puntos += 1; razones.append("Avenida con tránsito")
    if hora >= 21 or hora < 6:
        puntos += 1; razones.append("Es de noche")
    nivel = 3 if puntos >= 3 else 2 if puntos == 2 else 1
    return nivel, razones or ["Sin factores de riesgo adicionales"]


def asignar(politica, eta, prioridades, llegadas):
    """Asigna un lote. eta: matriz (solicitudes x prestadores). Devuelve pares (i, j)."""
    n_s, n_m = eta.shape
    if politica in ("voraz", "voraz_prio"):
        clave = (lambda i: (-prioridades[i], llegadas[i])) if politica == "voraz_prio" else (lambda i: llegadas[i])
        libres, pares = set(range(n_m)), []
        for i in sorted(range(n_s), key=clave):
            if not libres:
                break
            j = min(libres, key=lambda j: eta[i, j])
            if eta[i, j] < GRANDE:
                pares.append((i, j)); libres.remove(j)
        return pares
    costo = eta.copy()
    if politica == "hungaro":
        costo = costo * np.array([PESO_PRIORIDAD[p] for p in prioridades])[:, None]
    filas, cols = linear_sum_assignment(costo)
    return [(int(i), int(j)) for i, j in zip(filas, cols) if eta[i, j] < GRANDE]
