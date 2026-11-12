"""PROCESO 2: gemelo digital.

Se suscribe SOLO a las mediciones de la planta (no a sus parámetros reales),
corre el modelo en paralelo, calcula el índice de discrepancia, detecta el
drift y se recalibra. Publica su estado, sus parámetros y sus eventos.

Para cada muestra registra dos latencias, a partir de la marca de tiempo ts
con que la planta la publicó:
  - lat_red_ms:   planta -> broker -> gemelo (llegada del mensaje)
  - lat_total_ms: planta -> estado del gemelo listo para publicar (incluye la
                  espera en cola y el cómputo, p. ej. durante una recalibración)
Si planta y gemelo corren en PCs distintos, los relojes deben estar
sincronizados (NTP) para que estas medidas sean válidas.

    python gemelo.py
"""
import json
import math
import os
import queue
import time

from cps import config
from cps.gemelo_core import GemeloDigital, describir_evento
from cps.mqtt_util import a_json, conectar, desconectar, log, parser
from cps.registro import RegistroGemelo


def main():
    ap = parser(__doc__)
    ap.add_argument("--logdir", default="logs")
    args = ap.parse_args()

    top = config.topicos(args.prefijo)
    # Cola entre el hilo de red de MQTT (que recibe) y el ciclo principal (que
    # procesa). Si una recalibración tarda unos milisegundos, los mensajes que
    # llegan mientras tanto esperan aquí en orden y no se pierden.
    mediciones = queue.Queue()

    def on_message(client, userdata, msg):
        """Corre en el hilo de red: solo anota la hora de llegada y encola el mensaje."""
        t_rx = time.time()
        try:
            datos = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            datos = None
        if msg.topic == top["medicion"]:
            mediciones.put((datos, t_rx))
        elif msg.topic == top["planta_conexion"] and isinstance(datos, dict):
            log("gemelo", f"planta {datos.get('estado')}")

    cliente = conectar("gemelo", args, on_message,
                       suscripciones=[(top["medicion"], 0), (top["planta_conexion"], 1)],
                       topico_conexion=top["gemelo_conexion"])
    gemelo = GemeloDigital(config.PARAMS_NOMINALES, config.TS, config.GEMELO)
    registro = RegistroGemelo(args.logdir)
    log("gemelo", f"parámetros nominales (datos de placa): {config.PARAMS_NOMINALES}")
    log("gemelo", f"umbral DI = {gemelo.di.umbral:.3f} (ventana {gemelo.di.ventana} muestras, "
                  f"alfa={config.GEMELO['alfa']}); registro en {os.path.abspath(args.logdir)}")
    log("gemelo", "esperando mediciones de la planta...")

    n_procesadas = 0
    # Ciclo principal: toma una medición de la cola, la procesa y publica el resultado
    try:
        while True:
            try:
                med, t_rx = mediciones.get(timeout=0.5)  # el timeout deja que Ctrl+C funcione
            except queue.Empty:
                continue
            salida, eventos = gemelo.procesar(med)  # valida el mensaje antes de usarlo
            if salida is not None:
                # Latencias a partir de la hora con que la planta publicó la muestra
                ts_planta = med.get("ts")
                tiempos = {}
                if isinstance(ts_planta, (int, float)) and math.isfinite(ts_planta):
                    tiempos = {"ts_planta": ts_planta,
                               "lat_red_ms": 1000 * (t_rx - ts_planta),
                               "lat_total_ms": 1000 * (time.time() - ts_planta)}
                cliente.publish(top["gemelo_estado"], a_json({**salida, **tiempos}), qos=0)
                registro.muestra(salida, gemelo.parametros()["params"], tiempos)
                n_procesadas += 1
            # Los eventos van con QoS 1 porque no pueden perderse (son pocos e importantes)
            for ev in eventos:
                registro.eventos.escribir(ev)
                cliente.publish(top["gemelo_eventos"], a_json(ev), qos=1)
                log("gemelo", describir_evento(ev))
                # Los parámetros nuevos se publican retenidos: un dashboard que
                # se conecte después los recibe de inmediato
                if ev["tipo"] in ("calibracion_inicial", "recalibrado"):
                    cliente.publish(top["gemelo_parametros"],
                                    a_json({"k": ev["k"], "t": ev["t"], **gemelo.parametros()}),
                                    qos=1, retain=True)
            if mediciones.qsize() > 100:
                log("gemelo", f"advertencia: {mediciones.qsize()} mediciones en cola (procesamiento atrasado)")
    except KeyboardInterrupt:
        pass
    finally:
        log("gemelo", f"deteniendo ({n_procesadas} muestras procesadas, {gemelo.mensajes_perdidos} perdidas, "
                      f"{gemelo.mensajes_rechazados} rechazadas)")
        registro.cerrar()
        desconectar(cliente, top["gemelo_conexion"], "gemelo")


if __name__ == "__main__":
    main()
