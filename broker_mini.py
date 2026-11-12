"""Broker MQTT 3.1.1 mínimo (respaldo para desarrollo).

Para el proyecto se recomienda Mosquitto. Este broker existe para poder correr
la demo en un PC sin permisos de instalación. Implementa lo que usa el sistema:
CONNECT/CONNACK, PUBLISH QoS 0/1 (QoS 2 se acepta y se entrega como QoS 1),
SUBSCRIBE con comodines + y #, UNSUBSCRIBE, mensajes retenidos, Last Will,
keep-alive y PING. No guarda sesiones persistentes ni reintenta entregas.

Cómo funciona. Un broker es un servidor TCP que reenvía mensajes. Cada
cliente abre una conexión y manda paquetes binarios definidos por el estándar
MQTT. Todo paquete empieza con un byte que dice su tipo (CONNECT, PUBLISH,
SUBSCRIBE, ...) y luego su largo; leer_paquete() los separa. Cuando llega un
PUBLISH, el broker revisa qué clientes tienen una suscripción que calce con el
tópico (coincide()) y les reenvía el mensaje.

Para atender a muchos clientes a la vez se usa asyncio: cada conexión es una
corrutina (atender) que espera datos sin bloquear a las demás, todo en un solo hilo.

    python broker_mini.py [--host localhost] [--port 1883]
"""
import argparse
import asyncio
import datetime
import itertools
import struct

# Tipos de paquete del estándar MQTT 3.1.1 (van en los 4 bits altos del primer byte)
CONNECT, CONNACK, PUBLISH, PUBACK, PUBREC, PUBREL, PUBCOMP = 1, 2, 3, 4, 5, 6, 7
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT = 8, 9, 10, 11, 12, 13, 14


class ErrorProtocolo(Exception):
    """Paquete mal formado: el broker cierra esa conexión."""


def codificar_largo(n):
    """Codifica un largo como lo pide MQTT: 7 bits por byte; el bit alto indica si sigue otro byte."""
    salida = bytearray()
    while True:
        digito, n = n % 128, n // 128
        salida.append(digito | (0x80 if n else 0))
        if not n:
            return bytes(salida)


def paquete(tipo, flags, cuerpo=b""):
    """Arma un paquete: [tipo y flags en 1 byte][largo][cuerpo]."""
    return bytes([(tipo << 4) | flags]) + codificar_largo(len(cuerpo)) + cuerpo


def cadena(s):
    """Codifica un texto como en MQTT: 2 bytes con su largo seguidos del texto en UTF-8."""
    b = s.encode("utf-8") if isinstance(s, str) else s
    return struct.pack("!H", len(b)) + b


class Lector:
    """Recorre el cuerpo de un paquete leyendo sus campos en orden (bytes, enteros de 16 bits, textos)."""

    def __init__(self, datos):
        self.d, self.i = datos, 0

    def byte(self):
        if self.i >= len(self.d):
            raise ErrorProtocolo("paquete truncado")
        self.i += 1
        return self.d[self.i - 1]

    def u16(self):
        return (self.byte() << 8) | self.byte()

    def binario(self):
        n = self.u16()
        if self.i + n > len(self.d):
            raise ErrorProtocolo("paquete truncado")
        self.i += n
        return self.d[self.i - n:self.i]

    def texto(self):
        return self.binario().decode("utf-8")

    def resto(self):
        return self.d[self.i:]

    def quedan(self):
        return self.i < len(self.d)


def coincide(filtro, topico):
    """Indica si un tópico calza con un filtro de suscripción.

    Los niveles se separan por "/". En el filtro, "+" reemplaza exactamente un
    nivel y "#" reemplaza todos los niveles que sigan. Por ejemplo,
    "cps/+/medicion" calza con "cps/motor1/medicion", y "cps/#" calza con
    cualquier tópico que empiece por "cps".
    """
    f, t = filtro.split("/"), topico.split("/")
    if t[0].startswith("$") and f[0] in ("+", "#"):
        return False
    for i, nivel in enumerate(f):
        if nivel == "#":
            return True
        if i >= len(t) or (nivel != "+" and nivel != t[i]):
            return False
    return len(f) == len(t)


async def leer_paquete(reader):
    """Lee un paquete completo de la conexión y devuelve (tipo, flags, cuerpo)."""
    b1 = (await reader.readexactly(1))[0]
    largo, mult = 0, 1
    for _ in range(4):
        d = (await reader.readexactly(1))[0]
        largo += (d & 0x7F) * mult
        if not d & 0x80:
            break
        mult *= 128
    else:
        raise ErrorProtocolo("largo restante inválido")
    cuerpo = await reader.readexactly(largo) if largo else b""
    return b1 >> 4, b1 & 0x0F, cuerpo


class Cliente:
    """Lo que el broker sabe de un cliente conectado: su id, su conexión, sus suscripciones y su Last Will."""

    def __init__(self, client_id, writer):
        self.id = client_id
        self.writer = writer
        self.subs = {}  # filtro -> qos otorgado
        self.will = None  # (tópico, mensaje, qos, retenido) que se publica si se cae sin avisar
        self._pid = itertools.cycle(range(1, 65536))  # identificadores para los mensajes con QoS 1

    def enviar(self, datos):
        """Escribe bytes en la conexión, salvo que ya se esté cerrando."""
        if not self.writer.is_closing():
            self.writer.write(datos)

    def entregar(self, topico, payload, qos, retain):
        """Envía un PUBLISH a este cliente."""
        flags = (qos << 1) | (1 if retain else 0)
        cuerpo = cadena(topico) + (struct.pack("!H", next(self._pid)) if qos else b"") + payload
        self.enviar(paquete(PUBLISH, flags, cuerpo))


class Broker:
    """Estado del broker: clientes conectados y mensajes retenidos."""

    def __init__(self, verbose=False):
        self.clientes = {}   # id del cliente -> Cliente
        self.retenidos = {}  # tópico -> (payload, qos)
        self.verbose = verbose
        self._anonimos = itertools.count(1)

    def log(self, msg):
        ahora = datetime.datetime.now().isoformat(sep=" ", timespec="milliseconds")
        print(f"{ahora} [broker] {msg}", flush=True)

    def publicar(self, topico, payload, qos, retain):
        """Reenvía un mensaje a todos los clientes suscritos a un filtro que calce con el tópico.

        Si viene marcado como retenido, se guarda para entregarlo a quienes se
        suscriban después (un mensaje retenido vacío borra el anterior).
        """
        if retain:
            if payload:
                self.retenidos[topico] = (payload, qos)
            else:
                self.retenidos.pop(topico, None)
        for c in list(self.clientes.values()):
            otorgados = [q for f, q in c.subs.items() if coincide(f, topico)]
            if otorgados:
                c.entregar(topico, payload, min(qos, max(otorgados)), retain=False)

    async def atender(self, reader, writer):
        """Atiende una conexión de principio a fin (una corrutina por cliente).

        El primer paquete tiene que ser CONNECT. Después se responde a cada
        paquete según su tipo hasta que el cliente se despida (DISCONNECT) o
        se caiga. Si se cae sin despedirse, se publica su Last Will.
        """
        cliente, limpio = None, False
        try:
            tipo, _, cuerpo = await asyncio.wait_for(leer_paquete(reader), timeout=10)
            if tipo != CONNECT:
                raise ErrorProtocolo("se esperaba CONNECT")
            r = Lector(cuerpo)
            r.texto()  # "MQTT" (3.1.1) o "MQIsdp" (3.1)
            nivel, flags, keepalive = r.byte(), r.byte(), r.u16()
            if nivel not in (3, 4):
                writer.write(paquete(CONNACK, 0, b"\x00\x01"))  # versión de protocolo no soportada
                await writer.drain()
                return
            client_id = r.texto() or f"anonimo-{next(self._anonimos)}"
            cliente = Cliente(client_id, writer)
            if flags & 0x04:
                cliente.will = (r.texto(), r.binario(), min((flags >> 3) & 3, 1), bool(flags & 0x20))
            # Si ya había un cliente con el mismo id, el estándar dice que el nuevo lo reemplaza
            anterior = self.clientes.get(client_id)
            if anterior:
                anterior.will = None
                anterior.writer.close()
            self.clientes[client_id] = cliente
            cliente.enviar(paquete(CONNACK, 0, b"\x00\x00"))  # conexión aceptada
            if self.verbose:
                self.log(f"conectado {client_id}")

            # Si el cliente pasa más de 1,5 veces su keepalive sin enviar nada, se da por caído
            limite = keepalive * 1.5 if keepalive else None
            while True:
                tipo, flags, cuerpo = await asyncio.wait_for(leer_paquete(reader), timeout=limite)
                r = Lector(cuerpo)
                if tipo == PUBLISH:
                    qos, retain = (flags >> 1) & 3, bool(flags & 1)
                    topico = r.texto()
                    pid = r.u16() if qos else None
                    if qos == 1:
                        cliente.enviar(paquete(PUBACK, 0, struct.pack("!H", pid)))
                    elif qos == 2:
                        cliente.enviar(paquete(PUBREC, 0, struct.pack("!H", pid)))
                    self.publicar(topico, r.resto(), min(qos, 1), retain)
                elif tipo == PUBREL:
                    cliente.enviar(paquete(PUBCOMP, 0, cuerpo[:2]))
                elif tipo == SUBSCRIBE:
                    pid, otorgados, nuevos = r.u16(), bytearray(), []
                    while r.quedan():
                        filtro, qos = r.texto(), min(r.byte() & 3, 1)
                        cliente.subs[filtro] = qos
                        otorgados.append(qos)
                        nuevos.append((filtro, qos))
                    cliente.enviar(paquete(SUBACK, 0, struct.pack("!H", pid) + bytes(otorgados)))
                    for filtro, qos in nuevos:  # mensajes retenidos que coinciden
                        for topico, (payload, q) in list(self.retenidos.items()):
                            if coincide(filtro, topico):
                                cliente.entregar(topico, payload, min(q, qos), retain=True)
                elif tipo == UNSUBSCRIBE:
                    pid = r.u16()
                    while r.quedan():
                        cliente.subs.pop(r.texto(), None)
                    cliente.enviar(paquete(UNSUBACK, 0, struct.pack("!H", pid)))
                elif tipo == PINGREQ:
                    cliente.enviar(paquete(PINGRESP, 0))
                elif tipo == DISCONNECT:
                    limpio = True
                    break
                # PUBACK / PUBREC / PUBCOMP de los clientes: no se reintenta, se ignoran
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, ErrorProtocolo,
                UnicodeDecodeError) as e:
            if self.verbose and not isinstance(e, asyncio.IncompleteReadError):
                self.log(f"conexión cerrada ({type(e).__name__}: {e})")
        finally:
            if cliente is not None:
                if self.clientes.get(cliente.id) is cliente:
                    del self.clientes[cliente.id]
                if not limpio and cliente.will:
                    self.publicar(*cliente.will)
                if self.verbose:
                    self.log(f"desconectado {cliente.id}{'' if limpio else ' (abrupto)'}")
            writer.close()


async def principal(host, port, verbose):
    """Abre el servidor TCP; asyncio lanza atender() por cada conexión que llegue."""
    broker = Broker(verbose)
    servidor = await asyncio.start_server(broker.atender, host, port)
    broker.log(f"escuchando en {host}:{port}  (Ctrl+C para detener)")
    async with servidor:
        await servidor.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="localhost", help="0.0.0.0 para aceptar conexiones de otros PCs")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    try:
        asyncio.run(principal(args.host, args.port, args.verbose))
    except KeyboardInterrupt:
        pass
