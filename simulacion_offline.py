"""Corre planta y gemelo en un mismo proceso, sin MQTT ni tiempo real.

Usa exactamente las mismas clases que los procesos MQTT (PlantaSimulada y
GemeloDigital), pero en vez de publicar cada medición en el broker se la pasa
directamente al gemelo, y en vez de esperar 50 ms entre muestras avanza lo más
rápido posible. Así, 6 minutos simulados se calculan en unos 3 segundos. Sirve
para ajustar el detector, correr muchos experimentos y generar las figuras del
informe. Lo que no prueba es la comunicación: eso se valida con la corrida MQTT.

    python simulacion_offline.py --escenario demo --duracion 360
    python simulacion_offline.py --escenario aleatorio --semilla 3 --duracion 900
"""
import argparse
import os
import time

from cps import config
from cps.gemelo_core import GemeloDigital, describir_evento
from cps.planta_sim import PlantaSimulada
from cps.registro import RegistroGemelo, RegistroPlanta


def correr(escenario="demo", semilla=1, duracion=360.0, carpeta="logs/offline", verbose=True):
    """Simula `duracion` segundos y deja los registros en `carpeta`, con el mismo formato que una corrida MQTT."""
    planta = PlantaSimulada(escenario, semilla)
    gemelo = GemeloDigital(config.PARAMS_NOMINALES, config.TS, config.GEMELO)
    reg_p, reg_g = RegistroPlanta(carpeta), RegistroGemelo(carpeta)

    n = int(round(duracion / config.TS))
    t0 = time.perf_counter()
    for _ in range(n):
        med, cambios = planta.paso()
        reg_p.muestra(med, planta.x_real, planta.verdad()["params"])
        for c in cambios:
            reg_p.eventos.escribir(c)
            if verbose:
                print(f"[PLANTA] t={c['t_inicio']:7.2f}s  {c['param']} x{c['factor']:.2f} ({c['tipo']})  {c['desc']}")

        salida, eventos = gemelo.procesar(med)
        if salida is not None:
            reg_g.muestra(salida, gemelo.parametros()["params"])
        for ev in eventos:
            reg_g.eventos.escribir(ev)
            if verbose:
                print(f"[GEMELO] {describir_evento(ev)}")
    reg_p.cerrar()
    reg_g.cerrar()
    if verbose:
        print(f"\n{n} muestras simuladas en {time.perf_counter() - t0:.1f} s  ->  {os.path.abspath(carpeta)}")
    return carpeta


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--escenario", default="demo", choices=["demo", "aleatorio", "sin_drift"])
    ap.add_argument("--semilla", type=int, default=1)
    ap.add_argument("--duracion", type=float, default=360.0, help="segundos simulados")
    ap.add_argument("--logdir", default="logs/offline")
    ap.add_argument("--sin-analisis", action="store_true", help="no generar métricas ni figuras")
    args = ap.parse_args()

    carpeta = correr(args.escenario, args.semilla, args.duracion, args.logdir)
    if not args.sin_analisis:
        import analisis
        analisis.analizar(carpeta)
