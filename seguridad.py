"""Protección para publicar la app en internet. Todo se activa con variables de entorno; sin ellas la app funciona abierta, como en la computadora.

  ADMIN_PASSWORD         contraseña del dueño. Si existe, la app entra en «modo público»: /panel, /qr, /negocio y sus APIs piden iniciar sesión.
  CODIGO_COLABORADORES   código que el dueño da a repartidores y talleres para registrarse (evita que cualquiera se registre).
  SECRET_KEY             (opcional) secreto para firmar la sesión del dueño.
  LIMITE_PEDIDOS_HORA    pedidos que puede hacer una misma conexión por hora en modo público (por omisión 15).

Protección contra registros masivos (solo en modo público; el dueño con sesión iniciada no se ve afectado):
  REGISTROS_POR_IP_HORA  registros que puede hacer una misma conexión por hora (por omisión 5).
  REGISTROS_RETO         si en 10 minutos hay este número de registros (de cualquiera), cada nuevo registro exige resolver un reto al azar (por omisión 6).
  REGISTROS_BLOQUEO      si en 10 minutos hay este número de registros o de códigos fallidos, el registro se cierra solo (por omisión 15).
  BLOQUEO_MINUTOS        cuánto dura ese cierre automático (por omisión 20).
  MAX_REPARTIDORES       tope de repartidores reales registrados a la vez (por omisión 300).
"""
import base64
import hashlib
import hmac
import os
import random
import time
from collections import defaultdict, deque

ROLES_PUBLICOS = {"cliente", "repartidor", "prestador", "mecanico", "taller"}      # roles del WebSocket que se identifican con su id
COOKIE = "admin_sesion"
DURACION_SESION_S = 14 * 24 * 3600


def _limpio(valor):
    """Quita espacios o saltos de línea de más y comillas que alguien pudo escribir al pegar el valor en el panel del hosting."""
    v = (valor or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


def admin_password():
    return _limpio(os.environ.get("ADMIN_PASSWORD", ""))


def codigo_colaboradores():
    return _limpio(os.environ.get("CODIGO_COLABORADORES", ""))


def diagnostico(nombre):
    """Datos para el registro del servidor (nunca el valor): cuántos caracteres tiene la variable y si traía espacios o comillas."""
    crudo = os.environ.get(nombre, "")
    return dict(definida=bool(crudo.strip()), caracteres=len(_limpio(crudo)), espacios_extra=crudo != crudo.strip(),
                comillas=len(crudo.strip()) >= 2 and crudo.strip()[0] == crudo.strip()[-1] and crudo.strip()[0] in "\"'")


def modo_publico():
    return bool(admin_password())


def limite_pedidos_hora():
    try:
        return max(1, int(os.environ.get("LIMITE_PEDIDOS_HORA", "15")))
    except ValueError:
        return 15


def token_admin():
    """Valor de la cookie de sesión: firma de la contraseña. Si cambias la contraseña, todas las sesiones anteriores dejan de valer."""
    clave = (os.environ.get("SECRET_KEY") or admin_password()).encode()
    return hmac.new(clave, b"admin|" + admin_password().encode(), hashlib.sha256).hexdigest()


def es_admin(cookies):
    if not modo_publico():
        return True                                                     # sin contraseña configurada, todo está abierto (uso local)
    return hmac.compare_digest(cookies.get(COOKIE, ""), token_admin())


def password_ok(intento):
    return hmac.compare_digest(_limpio(intento).encode(), admin_password().encode())


def codigo_ok(intento):
    esperado = codigo_colaboradores()
    return True if not esperado else hmac.compare_digest(_limpio(intento).encode(), esperado.encode())


def ip_cliente(request):
    """IP de quien hace la petición. Detrás de un proxy (Render, Cloudflare) se toma la última dirección de X-Forwarded-For,
    que es la que añadió el proxy de confianza; las anteriores las puede escribir cualquiera."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "desconocida"


class Limitador:
    """Cuenta eventos por clave en una ventana de tiempo (en memoria; basta para un solo proceso)."""

    def __init__(self):
        self.h = defaultdict(deque)

    def _limpiar(self, clave, ventana_s, ahora):
        q = self.h[clave]
        while q and ahora - q[0] > ventana_s:
            q.popleft()
        if not q:
            self.h.pop(clave, None)
        return self.h.get(clave, deque())

    def excedido(self, clave, maximo, ventana_s, ahora=None):
        return len(self._limpiar(clave, ventana_s, time.time() if ahora is None else ahora)) >= maximo

    def anotar(self, clave, ahora=None):
        if len(self.h) > 10000:                                          # evita que crezca sin límite
            self.h.clear()
        self.h[clave].append(time.time() if ahora is None else ahora)

    def permitir(self, clave, maximo, ventana_s, ahora=None):
        """Anota el evento y dice si cabe dentro del límite."""
        if self.excedido(clave, maximo, ventana_s, ahora):
            return False
        self.anotar(clave, ahora)
        return True


limitador = Limitador()


# ------------------------------------------------------------------ protección contra registros masivos
def _entero(nombre, defecto, minimo=1):
    try:
        return max(minimo, int(os.environ.get(nombre, defecto)))
    except ValueError:
        return defecto


class ProteccionRegistro:
    """Cierra la puerta de registro cuando alguien intenta registrarse en masa.

    Capas, de la más suave a la más fuerte:
      1. Límite por conexión (IP): pocos registros por hora.
      2. Reto al azar: si el ritmo de registros de TODOS sube, cada registro nuevo debe resolver una suma al azar firmada y de un solo uso.
      3. Bloqueo automático: si el ritmo sigue subiendo (o hay muchos códigos fallidos), el registro se cierra unos minutos.
      4. Pausa manual y tope de repartidores, que decide el dueño.
    """
    VENTANA_S = 600
    RETO_VIDA_S = 300

    def __init__(self, reloj=time.time):
        self.reloj = reloj
        self.lim = Limitador()
        self.bloqueado_hasta = 0.0
        self.motivo = ""
        self.pausado = False
        self.usados = {}                                             # retos ya resueltos (para que no se repitan)

    # ---- retos
    def _clave(self):
        return (os.environ.get("SECRET_KEY") or admin_password() or "local").encode()

    def nuevo_reto(self):
        a, b = random.randint(2, 9), random.randint(2, 9)
        cuerpo = f"{a}.{b}.{int(self.reloj()) + self.RETO_VIDA_S}.{random.getrandbits(32):08x}"
        firma = hmac.new(self._clave(), b"reto|" + cuerpo.encode(), hashlib.sha256).hexdigest()[:24]
        token = base64.urlsafe_b64encode(f"{cuerpo}.{firma}".encode()).decode()
        return dict(token=token, pregunta=f"¿Cuánto es {a} + {b}?")

    def _reto_valido(self, token, respuesta):
        try:
            crudo = base64.urlsafe_b64decode((token or "").encode()).decode()
            a, b, vence, azar, firma = crudo.split(".")
            cuerpo = f"{a}.{b}.{vence}.{azar}"
            esperada = hmac.new(self._clave(), b"reto|" + cuerpo.encode(), hashlib.sha256).hexdigest()[:24]
            if not hmac.compare_digest(firma, esperada) or int(vence) < self.reloj() or crudo in self.usados:
                return False
            if str(int(a) + int(b)) != str(respuesta or "").strip():
                return False
        except Exception:
            return False
        self.usados = {k: v for k, v in self.usados.items() if v > self.reloj()}
        self.usados[crudo] = int(vence)
        return True

    # ---- estado
    def _n(self, clave):
        return len(self.lim._limpiar(clave, self.VENTANA_S, self.reloj()))

    def _cerrar(self, motivo):
        self.bloqueado_hasta = self.reloj() + _entero("BLOQUEO_MINUTOS", 20) * 60
        self.motivo = motivo

    def bloqueado(self):
        return self.reloj() < self.bloqueado_hasta

    def estado(self, registrados=0):
        return dict(pausado=self.pausado, bloqueado=self.bloqueado(), motivo=self.motivo if self.bloqueado() else "",
                    segundos_bloqueo=max(0, int(self.bloqueado_hasta - self.reloj())), registros_10min=self._n("reg"),
                    codigos_fallidos_10min=self._n("fallo"), reto_activo=self._n("reg") >= _entero("REGISTROS_RETO", 6),
                    repartidores=registrados, tope=_entero("MAX_REPARTIDORES", 300))

    def desbloquear(self):
        self.bloqueado_hasta, self.motivo = 0.0, ""
        self.lim.h.pop("reg", None)
        self.lim.h.pop("fallo", None)

    # ---- decisiones
    def revisar(self, ip, token, respuesta, registrados=0):
        """Devuelve None si puede seguir, o (código_http, mensaje, pedir_reto)."""
        ahora = self.reloj()
        if self.pausado:
            return 423, "El registro está cerrado por ahora. Pide al administrador que lo abra.", False
        if self.bloqueado():
            return 423, f"Demasiados registros en poco tiempo. Intenta de nuevo en {max(1, int((self.bloqueado_hasta - ahora) // 60) + 1)} min.", False
        if registrados >= _entero("MAX_REPARTIDORES", 300):
            return 423, "Ya no hay lugares para nuevos repartidores. Contacta al administrador.", False
        if self.lim.excedido("ip:" + ip, _entero("REGISTROS_POR_IP_HORA", 5), 3600, ahora):
            return 429, "Ya hiciste varios registros desde esta conexión. Intenta más tarde.", False
        if self._n("reg") >= _entero("REGISTROS_RETO", 6) and not self._reto_valido(token, respuesta):
            return 428, ("Respuesta incorrecta. Intenta con este nuevo reto." if token else "Por seguridad, resuelve este reto para registrarte."), True
        return None

    def registro_exitoso(self, ip):
        self.lim.anotar("ip:" + ip, self.reloj())
        self.lim.anotar("reg", self.reloj())
        if self._n("reg") >= _entero("REGISTROS_BLOQUEO", 15):
            self._cerrar("Se detectaron muchos registros seguidos")

    def codigo_fallido(self):
        self.lim.anotar("fallo", self.reloj())
        if self._n("fallo") >= _entero("REGISTROS_BLOQUEO", 15) * 2:
            self._cerrar("Se detectaron muchos intentos con código incorrecto")


proteccion = ProteccionRegistro()
