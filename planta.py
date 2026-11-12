"""PROCESO 1: planta física (motor DC simulado en tiempo real).

Cada Ts (50 ms) publica en planta/medicion lo que miden sus sensores y el
voltaje aplicado. Sus parámetros cambian según el escenario elegido sin avisar
al gemelo. El tópico planta/verdad, con los parámetros reales, existe solo para
el dashboard y la evaluación; el gemelo nunca se suscribe a él.

También escucha planta/inyeccion, por donde el dashboard o inyectar_falla.py
pueden provocar una falla durante la demostración.

    python planta.py                                  # escenario demo, tiempo real
    python planta.py --velocidad 4                    # 4x más rápido que tiempo real
    python planta.py --escenario aleatorio --semilla 7
"""
import json
import os
import queue
import time

from cps import config
from cps.mqtt_util import a_json, conectar, desconectar, log, parser
from cps.planta_sim import PlantaSimulada
from cps.registro import RegistroPlanta


def main():
    ap = parser(__doc__)
    ap.add_argument("--escenario", default="demo", choices=["demo", "aleatorio", "sin_drift"])
    ap.add_argument("--semilla", type=int, default=None)
    ap.add_argument("--velocidad", type=float, default=1.0, help="factor sobre tiempo real")
    ap.add_argument("--duracion", type=float, default=None, help="segundos simulados (default: infinito)")
    ap.add_argument("--logdir", default="logs")
    args = ap.parse_args()

    top = config.topicos(args.prefijo)
    # on_message corre en el hilo de red de MQTT; las fallas recibidas se dejan
    # en esta cola y el ciclo principal las aplica, para no tocar la planta
    # desde dos hilos a la vez
    inyecciones = queue.Queue()

    def on_message(client, userdata, msg):
        try:
            inyecciones.put(json.loads(msg.payload))
        except json.JSONDecodeError:
            log("planta", f"inyección ignorada (JSON inválido): {msg.payload!r}")

    cliente = conectar("planta", args, on_message,
                       suscripciones=[(top["inyeccion"], 1)], topico_conexion=top["planta_conexion"])
    planta = PlantaSimulada(args.escenario, args.semilla)
    registro = RegistroPlanta(args.logdir)
    log("planta", f"escenario '{args.escenario}' con {len(planta.eventos_escenario)} cambios programados "
                  f"(el gemelo no los conoce). Ts={config.TS}s, velocidad x{args.velocidad}")
    log("planta", f"registro en {os.path.abspath(args.logdir)}")

    periodo = config.TS / args.velocidad  # tiempo real entre muestras (con --velocidad 2, la mitad)
    n_max = None if args.duracion is None else int(round(args.duracion / config.TS))
    inicio = time.perf_counter()
    # Ciclo principal: una vuelta por muestra hasta completar la duración o hasta Ctrl+C
    try:
        while n_max is None or planta.k < n_max:
            # 1. Aplicar las fallas manuales que hayan llegado por MQTT
            while not inyecciones.empty():
                ev = inyecciones.get()
                if isinstance(ev, dict) and ev.get("param") in config.PARAMS_NOMINALES and "factor" in ev:
                    planta.inyectar(ev)
                else:
                    log("planta", f"inyección ignorada (formato inválido): {ev}")

            # 2. Simular un periodo y publicar la medición. QoS 0 porque una
            #    muestra vale solo 50 ms: si se pierde, retransmitirla no sirve
            med, cambios = planta.paso()
            med["ts"] = time.time()  # instante de publicación (reloj real): para medir latencia
            cliente.publish(top["medicion"], a_json(med), qos=0)
            # Los parámetros reales se publican cada 1 s (20 muestras), y en
            # cada muestra mientras haya un cambio o una rampa en curso
            verdad = planta.verdad()
            if cambios or med["k"] % 20 == 0 or verdad["rampas_activas"]:
                cliente.publish(top["verdad"], a_json(verdad), qos=0, retain=True)

            # 3. Registrar en disco y mostrar en consola
            registro.muestra(med, planta.x_real, verdad["params"])
            for c in cambios:
                registro.eventos.escribir(c)
                rampa = f"en rampa de {c['t_fin'] - c['t_inicio']:.0f}s" if c["tipo"] == "rampa" else "escalon"
                log("planta", f"t={c['t_inicio']:7.2f}s  CAMBIO SIN AVISO: {c['param']} "
                              f"{c['valor_inicial']:.4g} -> {c['valor_final']:.4g} (x{c['factor']:.2f}, {rampa})  {c['desc']}")
            if med["k"] % int(10 / config.TS) == 0:
                ps = "  ".join(f"{n}={v:.4g}" for n, v in verdad["params"].items())
                log("planta", f"k={med['k']:5d} t={med['t']:7.1f}s  V={med['u']:5.2f}  w={med['w']:6.2f}  "
                              f"i={med['i']:5.2f}  | {ps}")

            # 4. Esperar hasta el instante de la próxima muestra. Se calcula
            #    contra la hora de inicio (plazo absoluto) y no con un sleep fijo,
            #    para que los pequeños atrasos de cada vuelta no se acumulen
            espera = inicio + planta.k * periodo - time.perf_counter()
            if espera > 0:
                time.sleep(espera)
    except KeyboardInterrupt:
        pass
    finally:
        log("planta", f"deteniendo ({planta.k} muestras publicadas)")
        registro.cerrar()
        desconectar(cliente, top["planta_conexion"], "planta")


if __name__ == "__main__":
    main()
