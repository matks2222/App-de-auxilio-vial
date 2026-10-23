"""Protección para publicar la app en internet. Todo se activa con variables de entorno; sin ellas la app funciona abierta, como en la computadora.

  ADMIN_PASSWORD         contraseña del dueño. Si existe, la app entra en «modo público»: /panel, /qr, /negocio y sus APIs piden iniciar sesión.
  CODIGO_COLABORADORES   código que el dueño da a repartidores y talleres para registrarse (evita que cualquiera se registre).
  SECRET_KEY             (opcional) secreto para firmar la sesión del dueño.
  LIMITE_PEDIDOS_HORA    pedidos que puede hacer una misma conexión por hora en modo público (por omisión 15).
"""
import hashlib
import hmac
import os
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
