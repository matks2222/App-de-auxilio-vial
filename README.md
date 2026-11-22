# Taller en Ruta: prototipo de auxilio vial

Prototipo académico desarrollado en ESCOM-IPN. Cuando alguien se queda varado con su auto, el sistema le asigna un repartidor que le lleva gasolina, una bomba para inflar las llantas o cables para pasar corriente. Lo hice con Python y FastAPI, y se puede probar completo en una sola computadora con repartidores simulados.

Es un prototipo: no tiene pagos ni verificación de identidad, y no sustituye al 911.

## Servicios

El cliente solo ve tres opciones, cada una con su precio final antes de pedir:

| Servicio | Qué lleva el repartidor | Qué paga el cliente |
|---|---|---|
| Gasolina | 5 L, 10 L o la cantidad que elija (1 a 40 L), en Magna, Premium o Diésel | Litros por precio por litro, más el servicio a domicilio |
| Aire a las llantas | Su bomba para inflar | Servicio a domicilio |
| Paso de corriente | Sus cables pasacorriente | Servicio a domicilio |

La ubicación se pide al abrir la página, o ya viene puesta si la persona entró por un QR. Si nadie atiende el pedido en 15 minutos, se le avisa y se le invita a llamar o reintentar. Batería nueva, cambio de llanta, diagnóstico y grúa siguen en el código pero están apagados (`servicios_activos` en `estado.py`).

## Cómo se asignan los pedidos

1. **Cercanos:** índice geográfico H3 y filtro por radio (5 km por omisión). Sin H3, búsqueda exhaustiva.
2. **Habilidad:** solo entran repartidores que den ese servicio y que no estén en pausa por un rechazo reciente.
3. **Tiempo de llegada (ETA):** distancia en línea recta por un factor de rodeo. Opcionalmente se puede conectar OpenRouteService con la variable `RUTAS_API_KEY`: la consulta se hace en segundo plano y, si falla, se usa la fórmula. Esta parte la probé con respuestas simuladas, no contra el servicio real.
4. **Asignación por lote** cada 5 segundos, con cuatro políticas que se eligen en el panel: `voraz`, `voraz_prio`, `hungaro_sp` y `hungaro` (con SciPy).
5. **Oferta:** el repartidor acepta o rechaza en 30 segundos, con aviso sonoro y vibración.

Un rechazo explícito aparta al repartidor de ese pedido durante 120 s (`reoferta_s`); dejar pasar el tiempo o salir de turno no lo aparta. Un pedido que nadie atiende en 15 min (`espera_max_s`) se cierra como "sin cobertura".

## Seguridad y datos

- El cliente da su nombre, la placa de su auto y su color. El repartidor registra su nombre, vehículo (moto, auto, bicicleta o a pie), marca y modelo, color y placa.
- Al aceptarse el pedido, el cliente ve un **PIN de 4 dígitos** que el repartidor debe pedirle en persona para marcar "Ya llegué". Después de 5 intentos fallidos se bloquea esa llegada.
- Los datos del cliente solo los ve el repartidor asignado, y al terminar el servicio ninguna de las dos partes sigue viendo los datos de la otra.
- Lo que escribe cada persona no está verificado, así que un nombre o una placa se pueden teclear falsos. No guardo INE ni fotos porque son datos personales sensibles y exigirían un aviso de privacidad formal.

**Modo público.** Si se define `ADMIN_PASSWORD`, el panel, los QR, las cuentas y la lista de repartidores piden iniciar sesión. Además:

- El registro de repartidores y talleres pide un código (10 intentos por 10 minutos).
- El inicio de sesión se bloquea tras 5 intentos fallidos.
- Cada conexión tiene un máximo de pedidos por hora.
- Los enlaces son largos para que no se puedan adivinar.
- El registro masivo se frena por capas: límite por IP, un reto con suma al azar, bloqueo automático y un tope de repartidores a la vez.

## Precios y cuentas del negocio

El servidor calcula el precio al crear el pedido, así que el navegador no puede alterarlo. El precio por litro sale del taller registrado más cercano (a menos de 15 km) que tenga ese combustible; si no hay, uso un precio de referencia (Magna $23.80, Premium $28.70, Diésel $27.00, aproximados para CDMX en jul-ago 2026). Los cargos por servicio por omisión son supuestos míos: gasolina $100, aire $100 y paso de corriente $120.

La página `/negocio` calcula cuánto cuesta cada servicio, cuánto queda de ganancia, el margen y cuántos servicios al mes cubren los gastos fijos. Cada servicio real terminado guarda cuánto esperó el cliente y cuánto tardó el repartidor, y con esos tiempos se pueden recalcular las cuentas. Todos los costos son estimaciones, así que los resultados valen lo que valgan esos números.

## Talleres y códigos QR

- **Talleres** (`/taller`): se registran con su ubicación y marcan qué combustibles venden y a qué precio. Los talleres de demostración son ficticios y no fijan precios.
- **QR** (`/qr`): genero un QR por lugar (gasolineras, estacionamientos, vulcanizadoras) o uno general. Cada QR lleva a un enlace corto `/q/código`, que cuenta el escaneo y abre el formulario con la ubicación del letrero ya marcada. Se pueden imprimir en tamaño carta o tarjeta de 9 x 6 cm, y usan corrección de errores nivel Q.

## Cómo correrlo (Windows)

1. Abre una terminal en la carpeta del proyecto.
2. Instala las dependencias: `python -m pip install -r requirements.txt`
3. Inicia el servidor con `iniciar.bat`, o con `python -m uvicorn app.main:app --reload`.
4. Abre http://localhost:8000.

Páginas principales: `/cliente`, `/repartidor`, `/taller`, `/panel`, `/qr` y `/negocio`.

**Prueba rápida:** en `/panel` agrega repartidores simulados, abre `/cliente` en otra pestaña y pide un servicio. La oferta llega a `/repartidor`; al aceptarla y marcar "Ya llegué" y "Listo", el servicio aparece en `/negocio` con su tiempo real.

El GPS del navegador solo funciona con HTTPS o en localhost. Desde un celular sin HTTPS hay que marcar la ubicación en el mapa.

## Variables de entorno

| Variable | Para qué |
|---|---|
| `ADMIN_PASSWORD` | Contraseña del dueño; activa el modo público |
| `CODIGO_COLABORADORES` | Código para registrarse como repartidor o taller |
| `SECRET_KEY` | Secreto para firmar la sesión |
| `LIMITE_PEDIDOS_HORA` | Pedidos por conexión por hora (15 por omisión) |
| `DESPACHO_DB` | Ruta de la base de datos SQLite |
| `RUTAS_API_KEY` | Clave de OpenRouteService (opcional) |

## Publicarlo en Render

El proyecto incluye `render.yaml` para publicarlo en Render: en *New > Blueprint* se elige el repositorio y Render pide `ADMIN_PASSWORD` y `CODIGO_COLABORADORES`. El plan gratuito no sirve porque el servicio se duerme y el disco se borra, así que hace falta el plan `starter` con disco. La base `despacho.db` guarda los QR, precios y tiempos reales.

Los pedidos, repartidores y talleres viven en memoria, así que cada vez que el servicio se reinicia los repartidores tienen que registrarse otra vez.

## Estructura

- `app/motor.py`: servicios, ETA, prioridad, índice H3 y algoritmos de asignación.
- `app/estado.py`: reglas del servicio, talleres, cotización, repartidores simulados y eventos en SQLite.
- `app/seguridad.py`: contraseña, código de colaboradores y límites de intentos y pedidos.
- `app/main.py`: servidor FastAPI, WebSocket, enlaces cortos y rutas de las páginas.
- `app/static/`: pantallas del cliente, repartidor, taller, QR, cuentas y panel (Leaflet con OpenStreetMap).
- `tests/`: 107 pruebas automáticas.

## Pruebas

```
python -m pytest tests -q
```

## Limitaciones

- No hay pagos ni verificación de identidad de los repartidores.
- El ETA es una aproximación y los repartidores simulados tienen un comportamiento supuesto.
- Los costos y cargos por omisión son supuestos.
- Los mapas son los públicos de OpenStreetMap, aptos solo para pruebas ligeras.

## Trabajo a futuro

- Reemplazar las reglas de prioridad por un sistema difuso.
- Verificar a los repartidores, por ejemplo con un código personal entregado después de revisar su identificación en persona.
- Guardar pedidos y repartidores en la base de datos para que sobrevivan a un reinicio.
