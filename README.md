# Taller en Ruta: prototipo de auxilio vial

Prototipo académico de Temas Selectos de Inteligencia Artificial (ESCOM-IPN). Lleva ayuda a una persona con el auto varado: un **repartidor** llega hasta donde está
con gasolina, una bomba para las llantas o cables para pasar corriente. El sistema decide a quién asignar cada pedido con los algoritmos del proyecto.

**Aviso:** no tiene pagos ni verificación de identidad y no sustituye al 911. En tu computadora funciona abierta, sin contraseña. **Para ponerla en internet hay que
definir `ADMIN_PASSWORD`** (ver «Publicar en internet»): sin eso, cualquiera podría entrar al panel, reiniciar los datos o cambiar tus QR.

## Idea de esta versión

Un **repartidor** (no necesita ser mecánico) lleva ayuda hasta donde se quedó el auto. El cliente solo ve **tres opciones**, cada una con su precio:

| Servicio | Qué lleva el repartidor | Qué paga el cliente |
|---|---|---|
| ⛽ **Gasolina** | La gasolina: 5 L, 10 L o la cantidad que elija (1 a 40 L), en Magna, Premium o Diésel | Litros × precio por litro + servicio a domicilio |
| 🌬️ **Aire a las llantas** | Su bomba para inflar | Servicio a domicilio |
| ⚡ **Paso de corriente** | Sus cables pasacorriente | Servicio a domicilio |

El cliente da los menos toques posibles: elige una opción y presiona «Pedir ahora». La ubicación se pide sola al abrir la página (o viene ya puesta si entró
por un QR), no se pide nombre, tipo de falla ni situación de riesgo, y el mapa queda chico al final. La pantalla de seguimiento nunca habla de «buscar
repartidor»: dice «Recibimos tu pedido», «va en camino», «ya llegó» y «listo». Si nadie puede atender el pedido en 15 minutos, se le avisa con amabilidad y se le
invita a llamar o reintentar. El número de teléfono y el WhatsApp se conservan en la pantalla para quien prefiera hablar con una persona.

**Apagados, no borrados:** batería nueva, cambio de llanta, diagnóstico, mecánico y grúa siguen en el código pero no se ofrecen (`servicios_activos` en `estado.py`).
Para reactivarlos hay que prender esos servicios y volver a mostrarlos en las pantallas.

## Identificación y seguridad entre cliente y repartidor

Para que cada parte sepa con quién trata:

- **El cliente** escribe su **nombre**, la **placa** de su auto (5 a 8 letras o números; se aceptan guiones y espacios) y toca el **color** antes de pedir. Se guarda en su teléfono, así que solo lo escribe la primera vez.
- **El repartidor** registra su **nombre completo**, su **vehículo** (moto, auto, bicicleta o a pie), la **marca y modelo** (por ejemplo, Italika FT150), el **color** y la **placa**. La placa y el modelo no aplican a bicicleta ni a pie.
- **Lo que ve el cliente cuando el repartidor acepta:** su nombre, su vehículo con marca y modelo, el color y la placa, con el aviso de confirmar que coinciden antes de entregarle nada.
- **Lo que ve el repartidor cuando acepta:** el nombre del cliente y el color y la placa de su auto. En la **oferta** todavía no ve ninguno de esos datos.
- **PIN de seguridad:** al aceptarse el pedido, el cliente ve un PIN de 4 dígitos. El repartidor tiene que **pedírselo en persona** y teclearlo para marcar «Ya llegué»: sin el PIN correcto no se puede empezar el servicio. Así nadie marca llegada sin estar con el cliente y un impostor no puede empezar un pedido ajeno. Tras 5 intentos fallidos se bloquea esa llegada (el cliente puede cancelar). El PIN se muestra solo al cliente, solo mientras el repartidor viene en camino; no lo ven el repartidor, el taller ni el panel. Se puede apagar con `requiere_pin=False` en la configuración, solo para pruebas.
- Los datos del cliente solo los ve el repartidor asignado: no salen en el panel ni los ve el taller ni otro repartidor. Al terminar el servicio, ninguna de las dos partes sigue viendo los datos de la otra.

**Límite importante:** lo que escribe cada quien **no está verificado**: un nombre o una placa se pueden teclear falsos. El PIN protege el encuentro en persona, pero no comprueba quién es nadie. Para verificar a los repartidores antes de darles acceso falta, por ejemplo, un **código personal por repartidor**, entregado después de revisar su INE en persona. La app no guarda INE ni fotos, y no recomendamos pedírselas al cliente: son datos personales sensibles y exigen un aviso de privacidad formal.

## Precios

- **Precio final y visible antes de pedir**, igual para todos y sin cargos extra. El servidor lo calcula al crear el pedido: el navegador no lo puede alterar.
- **Gasolina:** litros × precio por litro + servicio a domicilio. El precio por litro sale del taller registrado más cercano (a menos de 15 km) que tenga ese combustible
  disponible; si no hay ninguno, del **precio de referencia**: Magna $23.80, Premium $28.70, Diésel $27.00 (promedio aproximado en CDMX, jul-ago 2026).
- **Aire y paso de corriente:** solo el servicio a domicilio, sin producto.
- **Cargos por servicio por omisión** (SUPUESTOS míos, ajústalos en `/negocio` o en `/panel`): gasolina $100, aire $100, paso de corriente $120.
- Los precios, cargos y costos **se guardan en disco** y sobreviven a «Reiniciar todo» y a reiniciar el servidor.
- Los talleres de demostración son ficticios: **no fijan precios ni se le indican al repartidor**, y no salen en los mapas.
- La app **no cobra**: el repartidor cobra exactamente lo que vio el cliente.

## Cuentas del negocio (`/negocio`)

Para saber si hay ganancia. Por cada servicio calcula cuánto cuesta (tiempo del repartidor + traslado + desgaste de la herramienta), cuánto cobras, cuánto te queda,
el margen, el cargo sugerido para tu margen objetivo y cuántos servicios al mes cubren tus gastos fijos. Marca en rojo lo que pierde dinero. La gasolina se compra y se
cobra al mismo precio, así que no deja ganancia: la ganancia está en el cargo por servicio.

Supuestos que puedes cambiar (todos son estimaciones mías, salvo la **bomba de $200**, que es dato tuyo): costo por hora del repartidor ($90), costo por kilómetro ($3),
kilómetros por servicio (6), minutos por servicio (gasolina 35, aire 25, corriente 30), herramienta (bomba $200, cables $300, bidón $120) y su vida útil, margen objetivo (30 %),
gastos fijos al mes ($300) y comisión de pago (0 %).

**Aprende el negocio:** cada servicio real terminado guarda cuánto esperó el cliente, cuánto tardó el repartidor en llegar y cuánto en atender. Con la casilla «usar tiempos
reales» las cuentas se recalculan con esos datos. Los servicios de repartidores simulados **no cuentan**. Cuando sepas cuánto tarda de verdad y cuánto deja, ya puedes
decidir si contratas a alguien. La página incluye una lista de lo que esta tabla no calcula (seguro, permisos para transportar gasolina, impuestos, horas sin pedidos...).


## Panel del taller (`/taller`)

1. **Registro:** nombre, teléfono, dirección y ubicación (GPS o tocando el mapa). El taller queda guardado en este dispositivo.
2. **Combustible:** marca cuáles vende y su precio por litro. **Un taller nuevo parte con todo desmarcado**: no se usa para cotizar hasta que lo marques.
3. **Pedidos por surtir** y **ventas:** los pedidos activos que salen de su taller y los terminados, con el total vendido.
4. El inventario de baterías existe pero está oculto mientras la batería esté apagada.

## Códigos QR y letreros (`/qr`)

Para que quien se quedó varado encuentre el servicio, pon letreros con un QR en gasolineras, estacionamientos, vulcanizadoras
y esquinas donde se quedan los autos.

1. En `/qr` escribe la **dirección pública** de la app (con https) y tu **WhatsApp** y **teléfono**.
2. Crea un QR por lugar: nombre y punto en el mapa donde queda el letrero. Sin punto es un **QR general** para tarjetas,
   imanes o calcomanías que la gente lleva en el coche.
3. Presiona **Imprimir letrero**: hay formato carta y formato tarjeta de 9 × 6 cm. También puedes descargar el QR como SVG.
4. **Prueba el QR con tu celular antes de imprimir muchos.**

Cómo funciona: cada QR codifica un enlace corto, `https://tu-dominio/q/abc123`. Ese enlace cuenta el escaneo y manda a
`/cliente?lugar=...&lat=...&lon=...`, donde la ubicación del letrero ya viene marcada (la persona puede corregirla). El repartidor
ve el punto de referencia en la solicitud. Un código desconocido o borrado lleva al formulario sin ubicación: nadie se queda sin ayuda.
Los QR usan corrección de errores nivel Q, para que sigan leyéndose aunque el letrero se ensucie o se raye un poco.

**Antes de imprimir:**
- La dirección tiene que ser pública y con **https**. `localhost` solo existe en tu computadora y el GPS del celular exige https.
  `/qr` y el letrero te avisan si la dirección todavía no sirve.
- **No cambies la dirección después de imprimir:** los QR ya impresos apuntan a la anterior. Usa un dominio tuyo.
- Los lugares y ajustes se guardan en `despacho.db` y **sobreviven a «Reiniciar todo»**, pero si publicas la app en un servicio
  cuyo disco se borra al reiniciar, esa base se pierde y los QR dejarán de llevar al lugar. Usa almacenamiento persistente.
- Los escaneos se cuentan de forma aproximada (algunas apps abren el enlace para mostrar una vista previa).
- `/qr` no tiene contraseña: protégela antes de abrir la app a internet.

## Cómo correrlo (Windows)

1. Abre una terminal en esta carpeta.
2. Instala las librerías: `python -m pip install -r requirements.txt`
3. Inicia el servidor: doble clic en `iniciar.bat` (instala lo que falte y abre el navegador) o `python -m uvicorn app.main:app --reload`.
   **Deja abierta la ventana negra:** si la cierras, el navegador muestra `ERR_CONNECTION_REFUSED`.
4. Abre http://localhost:8000: todas las páginas tienen un menú arriba (Inicio, Pedir ayuda, Repartidor, Taller, Panel). También puedes ir directo a:
   - Panel de demostración: http://localhost:8000/panel
   - Cliente (pedir ayuda): http://localhost:8000/cliente
   - Repartidor o grúa: http://localhost:8000/repartidor (también responde en `/prestador` y `/mecanico`)
   - Taller: http://localhost:8000/taller
   - Códigos QR y letreros: http://localhost:8000/qr
   - Cuentas del negocio: http://localhost:8000/negocio

## Demostración rápida (una sola computadora)

1. En `/panel` presiona **+5 repartidores** (aparecen en el mapa, son simulados) o, para hacer tú el papel del repartidor, abre `/repartidor`: elige tus servicios,
   toca el mapa para marcar tu ubicación y entra en turno.
2. En otra pestaña abre `/cliente`. Elige **Gasolina**, **Aire a las llantas** o **Paso de corriente**, y presiona **Pedir ahora**. Si estás en una computadora, toca el mapa
   donde estés si el navegador no te da la ubicación.
3. En `/repartidor` suena y vibra la oferta con lo que hay que llevar y cuánto cobrar. Acéptala, presiona «Ya llegué con el cliente» y «Listo, ya quedó».
4. En `/negocio` aparece el servicio con su tiempo real y las cuentas se actualizan.
5. Para probar el QR: crea un lugar en `/qr`, abre su enlace corto (`/q/código`) y verás el formulario con la ubicación ya puesta.

## Cómo busca y asigna

1. **Búsqueda de cercanos:** índice geográfico H3 y filtro por radio (por omisión 5 km). Sin H3, búsqueda exhaustiva.
2. **Filtro por habilidad:** solo entran repartidores que den ese servicio y no estén en pausa por un rechazo reciente.
3. **Tiempo de llegada (ETA):** línea recta por un factor de rodeo (aproximación; para producción usa un servicio de rutas).
4. **Asignación por lote** cada 5 s: `voraz`, `voraz_prio`, `hungaro_sp` y `hungaro`. Se elige en el panel.
5. **Oferta:** el repartidor acepta o rechaza en 30 s, con **aviso sonoro y vibración** en el celular.

**Por qué a veces «no llegaba la petición» (corregido).** Antes, si el repartidor dejaba pasar los 30 s, rechazaba una vez o salía de turno con una oferta abierta, quedaba
**excluido para siempre** de ese pedido y, con un solo repartidor, el pedido nunca volvía a ofrecerse. Además, los pedidos viejos sin atender se quedaban primero en la fila
y se llevaban la oferta. Ahora: dejar pasar el tiempo o salir de turno **no aparta** a nadie; un rechazo explícito aparta al repartidor solo por un rato (`reoferta_s`, 120 s)
y después se le vuelve a ofrecer si nadie más lo toma; y un pedido que nadie atiende en `espera_max_s` (15 min) se cierra como «sin cobertura». Si el repartidor está a más de
5 km de un pedido, su pantalla le avisa que hay pedidos esperando lejos.

La prioridad de cada solicitud sale hoy de reglas sencillas. **Pendiente:** reemplazarlas por el sistema difuso del proyecto.

## Publicar en internet (para que siempre funcione)

Tu computadora no tiene que estar prendida: la app corre en un servidor. Estos pasos usan Render (render.com), que ya viene configurado en `render.yaml`.

**Qué se necesita:** una cuenta de GitHub, una cuenta de Render y, si quieres, un dominio propio. El plan gratuito de Render **no sirve** (se duerme y borra el disco, y con él tus QR y precios);
hace falta el plan `starter` con un disco, que se cobran aparte: mira los precios en tu panel de Render antes de confirmar.

1. **Sube el proyecto a GitHub.** Crea un repositorio privado y sube los archivos de esta carpeta (que `render.yaml` quede en la raíz del repositorio). No subas `despacho.db`.
2. **En Render:** *New +* → *Blueprint* → elige tu repositorio. Render lee `render.yaml`.
3. **Te pedirá dos valores.** `ADMIN_PASSWORD`: tu contraseña para `/panel`, `/qr` y `/negocio` (larga y única). `CODIGO_COLABORADORES`: el código que les das a repartidores y talleres para registrarse.
4. **Confirma y espera** a que el servicio diga *Live*. Render te da una dirección `https://...onrender.com` con https automático.
5. **Prueba:** abre esa dirección (ves la pantalla de pedir ayuda), entra a `/login` con tu contraseña y revisa `/panel`.
6. **Dominio propio:** en Render, *Settings → Custom Domains*, agrega tu dominio y crea en tu registrador los registros DNS que Render te indique. Cuando funcione, escribe esa dirección en `/qr` y **recién entonces imprime los letreros**.

Si Render se queja de la versión de Python, cambia `PYTHON_VERSION` en `render.yaml` por una que ofrezca. Otros servicios (Railway, Fly.io, un servidor propio) sirven si tienen disco persistente, una sola instancia y las mismas variables.

**Variables de entorno**

| Variable | Para qué |
|---|---|
| `ADMIN_PASSWORD` | Contraseña del dueño. Si existe, la app entra en **modo público**: `/menu`, `/panel`, `/qr`, `/negocio`, `/letrero` y sus APIs piden iniciar sesión (`/login`), y `/` abre la pantalla de pedir ayuda. |
| `CODIGO_COLABORADORES` | Código para registrarse como repartidor o taller. Sin él, cualquiera podría registrarse. |
| `SECRET_KEY` | Secreto para firmar la sesión (Render lo genera solo). |
| `LIMITE_PEDIDOS_HORA` | Pedidos que puede hacer una misma conexión por hora (por omisión 15). |
| `DESPACHO_DB` | Ruta de la base de datos (en Render, `/var/data/despacho.db`, dentro del disco). |

**Qué protege el modo público:** el panel, los QR, las cuentas y la lista de repartidores con sus ubicaciones (también por WebSocket) quedan detrás de la contraseña; el registro de repartidores y talleres pide
el código (10 intentos por 10 minutos); el inicio de sesión se bloquea tras 5 intentos fallidos; cada conexión tiene un máximo de pedidos por hora; los enlaces de pedidos, repartidores y talleres son largos para que
no se puedan adivinar; y se envían cabeceras de seguridad básicas. La cookie de sesión va marcada `Secure` detrás de https.

**Si no te deja entrar con tu contraseña.** La app ignora espacios o comillas de más en `ADMIN_PASSWORD` y `CODIGO_COLABORADORES`, y al arrancar escribe en el registro del servidor (en Render, pestaña *Logs*)
cuántos caracteres tiene cada una, nunca su valor. Compara ese número con lo que escribes: si no coincide, el servicio sigue usando un valor viejo y falta reiniciarlo. Al cambiar la variable en Render, elige la opción que **guarda y vuelve a desplegar**: con «solo guardar» el valor nuevo no se aplica
hasta el siguiente despliegue. Cada intento fallido también deja un renglón con el largo de lo que se escribió. En el inicio de sesión puedes marcar «Mostrar contraseña» para ver lo que tecleas.

**Mapas en blanco o con «403 Access blocked».** OpenStreetMap exige que el navegador le diga de qué sitio viene la petición. La app manda la cabecera `Referrer-Policy: strict-origin-when-cross-origin` y los mapas la repiten; si la cambias
a `same-origin` o `no-referrer`, los mapas dejan de cargar. Los mapas públicos de OpenStreetMap son para uso ligero: si el servicio crece, usa un proveedor de mapas con llave.

**Lo que sigue pendiente:** no hay pagos ni verificación de identidad de repartidores; el código de colaboradores es uno solo para todos (si se filtra, cámbialo en Render); los pedidos, repartidores y talleres siguen
viviendo en memoria, así que **cada vez que Render reinicia o despliega el servicio** los repartidores tienen que registrarse otra vez (los QR, precios y tiempos reales sí se conservan en el disco); y detrás de un teléfono
con IP compartida, el límite de pedidos por conexión podría afectar a varios clientes a la vez.

## Probar en el celular

El GPS del navegador solo funciona con HTTPS (o en localhost). En el celular, sin HTTPS, usa el mapa para marcar la
ubicación a mano. Para probar con GPS real necesitas un túnel HTTPS (por ejemplo cloudflared o ngrok); eso expone el
prototipo a internet y, como no tiene autenticación, úsalo solo un rato y con cuidado.

## Estructura

- `app/motor.py`: servicios, niveles, catálogo de precios de referencia, ETA, triaje, índice H3 y algoritmos de asignación.
- `app/estado.py`: reglas del servicio, escalamiento, talleres con inventario, cotización, repartidores simulados y eventos guardados en SQLite.
- `app/seguridad.py`: contraseña del dueño, código de colaboradores, límites de intentos y de pedidos.
- `render.yaml`: configuración para publicar en Render.
- `app/main.py`: servidor FastAPI, WebSocket, enlaces cortos `/q/código`, imágenes QR y rutas de las páginas.
- `app/static/`: páginas del cliente, el repartidor, el taller, los QR (`qr.html`), el letrero imprimible (`letrero.html`), las cuentas (`negocio.html`) y el panel (Leaflet con OpenStreetMap).
- `tests/`: 92 pruebas automáticas (`python -m pytest tests -q`).

Los eventos de la tabla `eventos` (en `despacho.db`) sirven para calibrar después el simulador con datos reales.

## Cambios respecto a la versión anterior

- El cliente solo ve **tres opciones con precio** (gasolina, aire, paso de corriente). Se quitaron el nombre, el tipo de falla, la situación de riesgo, la vía, las notas,
  el aviso de «escaneaste el letrero», el texto de «ubicación marcada» y el aviso de repartidores cercanos. El mapa es más chico y va al final.
- **Corregido el fallo de las ofertas** (ver «Cómo busca y asigna»).
- Gasolina por litros (5 L, 10 L u otra cantidad) y tipo de combustible; precio final visible antes de pedir.
- Nueva página `/negocio` con las cuentas, y registro de los tiempos reales de cada servicio.
- Se conserva el número de teléfono y el WhatsApp. Los precios y costos se guardan en disco.
- Batería, cambio de llanta, diagnóstico y grúa quedan **apagados** (no se ofrecen), pero siguen en el código.
- El API de precios cambió: el «cargo de envío» único es ahora un cargo por servicio (`precios.servicios`) y `/api/panel/precios` recibe `servicios`.
- Los talleres de demostración ya no salen en mapas ni fijan precios. Una `despacho.db` anterior sigue funcionando.
- Los pedidos, repartidores y talleres viven en memoria: se pierden al reiniciar el servidor o presionar **Reiniciar todo**.

## Límites del prototipo

Sin pagos (los precios son informativos), sin verificación de identidad de repartidores, ETA aproximado, repartidores simulados con comportamiento supuesto.
- Todos los costos y cargos por omisión son **supuestos**: las cuentas valen lo que valgan los números que les pongas.
- El ETA y la asignación no incluyen ir a comprar la gasolina; el tiempo medido (de aceptar a llegar) sí lo incluye.
- La gasolina se paga al precio por litro del taller más cercano registrado, o al de referencia: no se verifica contra una gasolinera real.
- El QR general no sabe dónde está la persona: depende de su GPS o de que toque el mapa.
- Las teselas del mapa son las públicas de OpenStreetMap, aptas para pruebas ligeras.
