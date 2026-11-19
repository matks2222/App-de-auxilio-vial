"""Tiempo de llegada por calles reales (opcional).

Sin configuración no hace nada: la app sigue usando la fórmula (distancia en línea recta × rodeo ÷ velocidad).
Con RUTAS_API_KEY definida consulta un servicio de rutas (OpenRouteService, perfil de coche/moto) y guarda el resultado en memoria.

  RUTAS_API_KEY    clave del servicio (si no existe, todo queda como antes).
  RUTAS_URL        (opcional) dirección del servicio; por omisión OpenRouteService.
  RUTAS_MAX_DIA    máximo de consultas por día (por omisión 1000) para no pasarte de la cuota gratuita.

Nunca bloquea la pantalla: se contesta con la fórmula y la consulta real se hace en segundo plano; la siguiente actualización ya trae el dato.
Si el servicio falla o se agota la cuota, se sigue con la fórmula.
"""
import os
import time

URL_ORS = "https://api.openrouteservice.org/v2/directions/driving-car"
VIDA_S = 45                       # cuánto vale una consulta antes de repetirla
FALLOS_PARA_PAUSA = 5


def _clave(lat1, lon1, lat2, lon2):
    return (round(lat1, 3), round(lon1, 3), round(lat2, 3), round(lon2, 3))     # ≈ 100 m: movimientos pequeños reutilizan la consulta


class Rutas:
    def __init__(self, reloj=time.time):
        self.reloj = reloj
        self.cache = {}              # clave -> (segundos, vence)
        self.pendientes = {}
        self.dia, self.usadas = None, 0
        self.fallos = 0
        self.pausa_hasta = 0.0
        self.consultas = 0

    @property
    def activo(self):
        return bool((os.environ.get("RUTAS_API_KEY") or "").strip())

    def _tope(self):
        try:
            return max(1, int(os.environ.get("RUTAS_MAX_DIA", "1000")))
        except ValueError:
            return 1000

    def _cupo(self):
        hoy = time.strftime("%Y-%m-%d", time.gmtime(self.reloj()))
        if hoy != self.dia:
            self.dia, self.usadas = hoy, 0
        return self.usadas < self._tope() and self.reloj() >= self.pausa_hasta

    def eta(self, lat1, lon1, lat2, lon2):
        """Segundos por calles si ya se consultó; si no, None (y deja la consulta anotada)."""
        if not self.activo:
            return None
        k = _clave(lat1, lon1, lat2, lon2)
        hit = self.cache.get(k)
        if hit and hit[1] > self.reloj():
            return hit[0]
        if len(self.pendientes) < 50:
            self.pendientes[k] = (lat1, lon1, lat2, lon2)
        return hit[0] if hit else None

    async def procesar(self, cliente):
        """Atiende las consultas anotadas (se llama desde el ciclo de fondo de la app). `cliente` es un httpx.AsyncClient."""
        if not self.activo:
            self.pendientes.clear()
            return
        pend, self.pendientes = list(self.pendientes.items())[:5], {}
        for k, (lat1, lon1, lat2, lon2) in pend:
            if not self._cupo():
                return
            self.usadas += 1
            try:
                r = await cliente.post(os.environ.get("RUTAS_URL") or URL_ORS, timeout=4,
                                       headers={"Authorization": os.environ["RUTAS_API_KEY"].strip()},
                                       json={"coordinates": [[lon1, lat1], [lon2, lat2]]})
                r.raise_for_status()
                seg = float(r.json()["routes"][0]["summary"]["duration"])
                self.cache[k] = (seg, self.reloj() + VIDA_S)
                self.fallos = 0
                self.consultas += 1
            except Exception:
                self.fallos += 1
                if self.fallos >= FALLOS_PARA_PAUSA:                 # el servicio no responde: descansa 5 min y usa la fórmula
                    self.pausa_hasta, self.fallos = self.reloj() + 300, 0
        if len(self.cache) > 2000:
            self.cache = {k: v for k, v in self.cache.items() if v[1] > self.reloj()}

    def estado(self):
        return dict(activo=self.activo, consultas_hoy=self.usadas, tope_dia=self._tope(), exitosas=self.consultas, en_pausa=self.reloj() < self.pausa_hasta)


rutas = Rutas()
