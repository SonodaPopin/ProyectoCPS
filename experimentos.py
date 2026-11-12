"""Evaluación estadística (Monte Carlo): muchas corridas offline con escenarios aleatorios.

    python experimentos.py                  # 10 semillas x 900 s + 1 h sin drift
    python experimentos.py --semillas 30

Una sola corrida puede salir bien o mal por suerte. Por eso se repite el
experimento con varios escenarios aleatorios distintos (cada semilla genera
otros cambios de parámetros y otro ruido) y se juntan todos los cambios para
calcular los KPI 1 a 5. La corrida de 1 hora sin drift mide las falsas alarmas.

Entrega la tabla de resultados por corrida, los KPI agregados con su meta y
un resultados.json con los totales.
"""
import argparse
import contextlib
import io
import json
import os

import numpy as np

import analisis
import simulacion_offline


def correr_caso(escenario, semilla, duracion, carpeta_base):
    """Simula un escenario y devuelve sus métricas (sin imprimir el detalle ni generar figuras)."""
    carpeta = os.path.join(carpeta_base, f"{escenario}_{semilla}")
    simulacion_offline.correr(escenario, semilla, duracion, carpeta, verbose=False)
    with contextlib.redirect_stdout(io.StringIO()):
        return analisis.analizar(carpeta, figuras=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--semillas", type=int, default=10)
    ap.add_argument("--duracion", type=float, default=900.0)
    ap.add_argument("--logdir", default="logs/experimentos")
    args = ap.parse_args()

    print(f"{'escenario':<10} {'sem':>3} {'detect.':>8} {'absorb.':>7} {'FA':>3} {'recal':>5} "
          f"{'ret.med[s]':>10} {'err.med[%]':>10}")
    total = {"cambios": 0, "detectados": 0, "absorbidos": 0, "fa": 0, "horas": 0.0}
    por_cambio = []  # todos los cambios de todas las corridas, para los KPIs agregados
    casos = [("sin_drift", 1, 3600.0)] + [("aleatorio", s, args.duracion) for s in range(1, args.semillas + 1)]
    for escenario, semilla, duracion in casos:
        m = correr_caso(escenario, semilla, duracion, args.logdir)
        r = m["resumen"]
        ret = [f["retardo_deteccion_s"] for f in m["por_cambio"] if "retardo_deteccion_s" in f]
        err = [f["error_param_cambiado_pct"] for f in m["por_cambio"] if "error_param_cambiado_pct" in f]
        por_cambio += m["por_cambio"]
        total["cambios"] += r["cambios_reales"]
        total["detectados"] += r["cambios_detectados"]
        total["absorbidos"] += r["cambios_absorbidos"]
        total["fa"] += r["falsas_alarmas"]
        total["horas"] += r["duracion_s"] / 3600
        print(f"{escenario:<10} {semilla:>3} {r['cambios_detectados']:>4}/{r['cambios_reales']:<3} "
              f"{r['cambios_absorbidos']:>7} {r['falsas_alarmas']:>3} {r['recalibraciones']:>5} "
              f"{np.mean(ret) if ret else float('nan'):>10.2f} {np.median(err) if err else float('nan'):>10.2f}")

    kpi = analisis.valores_kpi(por_cambio, total["horas"], total["fa"])
    print("\n=========== AGREGADO ===========")
    print(f"Horas simuladas    : {total['horas']:.1f}")
    print(f"Cambios detectados : {total['detectados']}/{total['cambios']} "
          f"(+{total['absorbidos']} absorbidos en recalibraciones en curso)")
    print(f"Falsas alarmas     : {total['fa']}\n")
    analisis.imprimir_kpis(kpi)
    print("(KPI6 y KPI7 se miden en una corrida con MQTT: python analisis.py logs)")
    with open(os.path.join(args.logdir, "resultados.json"), "w", encoding="utf-8") as f:
        json.dump({"totales": total, "kpi": kpi, "semillas": args.semillas, "duracion_s": args.duracion},
                  f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
