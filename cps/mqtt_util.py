"""Funciones MQTT que comparten planta.py, gemelo.py, dashboard.py e inyectar_falla.py.

Se usa la biblioteca paho-mqtt (versión 2.x). Conceptos que aparecen aquí:

- Cliente: cada proceso crea uno y se conecta al broker.
- Hilo de red: loop_start() lanza un hilo aparte que mantiene la conexión,
  envía los mensajes publicados y llama a on_message cuando llega uno. Por
  eso on_message corre en otro hilo que el programa principal; planta.py y
  gemelo.py lo resuelven pasando los mensajes por una queue.Queue, que es
  segura entre hilos.
- QoS (calidad de servicio): 0 = se envía una vez y puede perderse;
  1 = el broker confirma y se reenvía hasta que llegue (puede duplicarse).
- keepalive: si el broker no sabe nada de un cliente durante 1,5 veces este
  tiempo, lo da por caído y publica su Last Will.
"""
import argparse
import json
import os
import sys
import uuid

import paho.mqtt.client as mqtt

from . import config
from .registro import fecha_hora, sin_nan


def argumentos_broker(parser):
    """Agrega las opciones de conexión al broker a un ArgumentParser.

    Las credenciales se pueden dar por variables de entorno (MQTT_USUARIO,
    MQTT_CLAVE) para que la clave no quede en el historial de la consola.
    """
    parser.add_argument("--host", default=config.BROKER_HOST, help="broker MQTT (default: %(default)s)")
    parser.add_argument("--port", type=int, default=config.BROKER_PORT)
    parser.add_argument("--prefijo", default=config.PREFIJO, help="prefijo de tópicos (default: %(default)s)")
    parser.add_argument("--usuario", default=os.environ.get("MQTT_USUARIO"),
                        help="usuario MQTT (o variable MQTT_USUARIO)")
    parser.add_argument("--clave", default=os.environ.get("MQTT_CLAVE"),
                        help="clave MQTT (mejor por variable MQTT_CLAVE)")
    parser.add_argument("--tls-ca", default=os.environ.get("MQTT_TLS_CA"),
                        help="certificado de la CA para conectar con TLS (puerto típico 8883)")
    return parser


def a_json(obj):
    """Convierte un diccionario en el texto JSON que viaja por MQTT (sin NaN, con tildes legibles)."""
    return json.dumps(sin_nan(obj), ensure_ascii=False)


def log(nombre, mensaje):
    """Línea de consola con fecha y hora: deja un registro verificable de la ejecución."""
    print(f"{fecha_hora()} [{nombre}] {mensaje}", flush=True)


def conectar(nombre, args, on_message=None, suscripciones=(), topico_conexion=None):
    """Crea un cliente MQTT, lo conecta al broker y arranca su hilo de red.

    Parámetros:
        nombre: nombre del proceso ("planta", "gemelo", ...); se usa en el id del cliente y en los mensajes.
        args: opciones de argumentos_broker (host, port, usuario, clave, tls_ca).
        on_message: función que se llama con cada mensaje recibido.
        suscripciones: lista de (tópico, qos) a los que suscribirse.
        topico_conexion: si se da, al conectar se publica "online" (retenido)
            y se registra un Last Will "offline", para que el broker avise a
            los demás si este proceso muere sin despedirse.

    Si el broker no está disponible, termina el programa con un mensaje que
    explica qué hacer.
    """
    host, port = args.host, args.port
    # El id debe ser único en el broker; se le agrega un sufijo al azar por si
    # hay dos instancias del mismo proceso
    cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                          client_id=f"{nombre}-{uuid.uuid4().hex[:6]}")
    if args.usuario:
        cliente.username_pw_set(args.usuario, args.clave)
    if args.tls_ca:
        cliente.tls_set(ca_certs=args.tls_ca)
    if topico_conexion:
        cliente.will_set(topico_conexion, a_json({"estado": "offline", "proceso": nombre}), qos=1, retain=True)

    # Se llama en cada conexión, incluidas las reconexiones automáticas. Por
    # eso las suscripciones se hacen aquí y no una sola vez al inicio.
    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log(nombre, f"conexión rechazada por el broker: {reason_code}")
            return
        log(nombre, f"conectado a {host}:{port}" + (" con TLS" if args.tls_ca else ""))
        for topico, qos in suscripciones:
            client.subscribe(topico, qos)
        if topico_conexion:
            client.publish(topico_conexion, a_json({"estado": "online", "proceso": nombre}), qos=1, retain=True)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            log(nombre, f"desconectado del broker ({reason_code}); reintentando...")

    cliente.on_connect = on_connect
    cliente.on_disconnect = on_disconnect
    if on_message:
        cliente.on_message = on_message
    cliente.reconnect_delay_set(min_delay=1, max_delay=5)  # si se cae la conexión, reintenta cada 1 a 5 s
    try:
        cliente.connect(host, port, keepalive=10)
    except OSError as e:
        sys.exit(f"[{nombre}] No se pudo conectar al broker MQTT en {host}:{port} ({e}).\n"
                 f"  -> Inicie Mosquitto o el broker de respaldo:  python broker_mini.py")
    cliente.loop_start()
    return cliente


def desconectar(cliente, topico_conexion=None, nombre=""):
    """Cierra la conexión de forma ordenada.

    Una desconexión limpia no dispara el Last Will, así que el "offline" se
    publica a mano antes de desconectar.
    """
    if topico_conexion:
        cliente.publish(topico_conexion, a_json({"estado": "offline", "proceso": nombre}),
                        qos=1, retain=True).wait_for_publish(timeout=2)
    cliente.disconnect()
    cliente.loop_stop()


def parser(descripcion):
    """ArgumentParser con las opciones del broker ya agregadas; cada script suma las suyas."""
    return argumentos_broker(argparse.ArgumentParser(
        description=descripcion, formatter_class=argparse.RawDescriptionHelpFormatter))
