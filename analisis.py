"""Análisis posterior de una corrida (MQTT u offline): métricas, KPIs y figuras.

    python analisis.py logs            # corrida con MQTT (planta.py + gemelo.py)
    python analisis.py logs/offline    # corrida de simulacion_offline.py

Lee planta.csv, planta_eventos.jsonl, gemelo.csv y gemelo_eventos.jsonl de la
carpeta y deja metricas.json + figuras PNG en <carpeta>/figuras.

La idea es cruzar dos fuentes que durante la corrida estuvieron separadas: lo
que realmente le pasó a la planta (sus registros, con cada cambio de
parámetros) y lo que el gemelo creyó y decidió (sus registros y eventos). Al
compararlas se obtiene cuánto tardó en detectar cada cambio, cuántas alarmas
fueron falsas, qué tan bien estimó los parámetros y, en corridas con MQTT,
la latencia y la pérdida de mensajes. Esos valores son los KPI de la Tabla 2
del informe.
"""
import argparse
import csv
import json
import math
import os

import numpy as np

from cps import config
from cps.modelo import PARAM_NOMBRES
from cps.registro import fecha_hora

COLORES = {"R": "#1f77b4", "L": "#9467bd", "K": "#d62728", "J": "#2ca02c", "b": "#ff7f0e"}


# --------------------------------------------------------------------------
# Lectura
# --------------------------------------------------------------------------
def leer_csv(ruta):
    """Lee un CSV y devuelve un diccionario columna -> arreglo numpy.

    Las columnas numéricas quedan como float (las celdas vacías como NaN) y
    las de texto, como la columna modo, quedan como texto.
    """
    with open(ruta, newline="", encoding="utf-8") as f:
        lector = csv.DictReader(f)
        filas = list(lector)
        columnas = lector.fieldnames
    datos = {}
    for c in columnas:
        valores = [fila[c] for fila in filas]
        try:
            datos[c] = np.array([float(v) if v not in ("", "None") else np.nan for v in valores])
        except ValueError:
            datos[c] = np.array(valores)
    return datos


def leer_jsonl(ruta):
    """Lee un archivo JSONL (un objeto por línea) como lista de diccionarios."""
    if not os.path.exists(ruta):
        return []
    with open(ruta, encoding="utf-8") as f:
        return [json.loads(linea) for linea in f if linea.strip()]


def cargar(carpeta):
    """Carga los cuatro registros de una corrida: (planta, gemelo, cambios reales, eventos del gemelo)."""
    P = leer_csv(os.path.join(carpeta, "planta.csv"))
    G = leer_csv(os.path.join(carpeta, "gemelo.csv"))
    cambios = sorted(leer_jsonl(os.path.join(carpeta, "planta_eventos.jsonl")), key=lambda c: c["t_inicio"])
    eventos = leer_jsonl(os.path.join(carpeta, "gemelo_eventos.jsonl"))
    return P, G, cambios, eventos


# --------------------------------------------------------------------------
# Métricas
# --------------------------------------------------------------------------
def params_reales_en(P, k):
    """Parámetros reales de la planta en la muestra k (búsqueda binaria en la columna k)."""
    idx = int(np.searchsorted(P["k"], k))
    idx = min(idx, len(P["k"]) - 1)
    return {n: float(P[n][idx]) for n in PARAM_NOMBRES}


def error_relativo(estimados, reales):
    """Error relativo en % de cada parámetro estimado respecto del real."""
    return {n: 100.0 * abs(estimados[n] / reales[n] - 1.0) for n in PARAM_NOMBRES}


def calcular_metricas(P, G, cambios, eventos):
    """Cruza los cambios reales con las decisiones del gemelo.

    Para cada cambio de la planta busca la primera alarma del gemelo que vino
    después (retardo de detección) y la última recalibración antes del cambio
    siguiente (error de los parámetros estimados). Las alarmas que no se
    explican por ningún cambio real se cuentan como falsas alarmas.
    """
    detecciones = [e for e in eventos if e["tipo"] == "drift_detectado"]
    ajustes = [e for e in eventos if e["tipo"] in ("calibracion_inicial", "recalibrado")]
    recal = [e for e in ajustes if e["tipo"] == "recalibrado"]

    # --- Falsas alarmas: detección sin ningún cambio real desde que empezó la
    # ventana de datos del último ajuste aceptado.
    falsas = []
    for d in detecciones:
        previos = [a for a in ajustes if a["t"] < d["t"]]
        t_ref = previos[-1]["t_ventana"][0] if previos else -math.inf
        justificada = any(c["t_inicio"] <= d["t"] and c["t_fin"] >= t_ref for c in cambios)
        if not justificada:
            falsas.append(d)

    # --- Por cada cambio real
    por_cambio = []
    for i, c in enumerate(cambios):
        t_sig = cambios[i + 1]["t_inicio"] if i + 1 < len(cambios) else math.inf
        dets = [d for d in detecciones if c["t_inicio"] <= d["t"] < t_sig and d not in falsas]
        recs = [r for r in recal if c["t_inicio"] <= r["t"] < t_sig]
        # Un cambio que ocurre mientras el gemelo ya está recalibrando no genera una
        # detección propia, pero queda incorporado en el ajuste en curso.
        absorbido = not dets and bool(recs)
        fila = {"t_inicio": c["t_inicio"], "param": c["param"], "factor": c["factor"], "tipo": c["tipo"],
                "desc": c.get("desc", ""), "detectado": bool(dets), "absorbido": absorbido,
                "n_recalibraciones": len(recs)}
        if dets:
            fila["retardo_deteccion_s"] = dets[0]["t"] - c["t_inicio"]
            fila["error_instante_estimado_s"] = dets[0]["t_cambio_estimado"] - c["t_inicio"]
            fila["disparado_por"] = dets[0]["disparado_por"]
        if recs:
            fila["tiempo_hasta_recalibrar_s"] = recs[0]["t"] - c["t_inicio"]
            ultimo = recs[-1]
            err = error_relativo(ultimo["params"], params_reales_en(P, ultimo["k"]))
            fila["error_param_cambiado_pct"] = err[c["param"]]
            fila["error_max_pct"] = max(err.values())
        por_cambio.append(fila)

    duracion = float(P["t"][-1] - P["t"][0]) if len(P["t"]) else 0.0
    t_monitoreo = float(np.mean(G["modo"] == "MONITOREO")) if len(G["modo"]) else 0.0
    error_final = {}
    if ajustes:
        error_final = error_relativo(ajustes[-1]["params"], params_reales_en(P, G["k"][-1]))
    retardos = [f["retardo_deteccion_s"] for f in por_cambio if "retardo_deteccion_s" in f]
    qos = calcular_qos(P, G)
    resumen = {
        "duracion_s": duracion,
        "cambios_reales": len(cambios),
        "cambios_detectados": sum(f["detectado"] for f in por_cambio),
        "cambios_absorbidos": sum(f["absorbido"] for f in por_cambio),
        "detecciones_totales": len(detecciones),
        "recalibraciones": len(recal),
        "falsas_alarmas": len(falsas),
        "falsas_alarmas_por_hora": len(falsas) / duracion * 3600 if duracion else 0.0,
        "retardo_medio_s": float(np.mean(retardos)) if retardos else None,
        "retardo_max_s": float(np.max(retardos)) if retardos else None,
        "fraccion_tiempo_monitoreo": t_monitoreo,
        "error_final_pct": error_final,
        "mensajes_perdidos": sum(e.get("cantidad", 0) for e in eventos if e["tipo"] == "mensajes_perdidos"),
        "mensajes_rechazados": sum(1 for e in eventos if e["tipo"] == "mensaje_rechazado"),
    }
    return {"resumen": resumen, "por_cambio": por_cambio, "qos": qos,
            "falsas_alarmas_t": [d["t"] for d in falsas],
            "kpi": valores_kpi(por_cambio, duracion / 3600, len(falsas), qos)}


def percentiles(x):
    """Resumen estadístico (mediana p50, p95, p99, máximo y media) ignorando NaN.

    El p99 es el valor bajo el cual queda el 99 % de las muestras; para la
    latencia es más informativo que el promedio, porque refleja los peores casos.
    """
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if not len(x):
        return None
    p50, p95, p99 = np.percentile(x, [50, 95, 99])
    return {"n": int(len(x)), "p50": float(p50), "p95": float(p95), "p99": float(p99),
            "max": float(x.max()), "media": float(x.mean())}


def calcular_qos(P, G):
    """Latencia y pérdida de mensajes. Sólo existe en corridas con MQTT, cuyas
    filas traen marcas de tiempo de reloj real."""
    if "lat_total_ms" not in G or not np.isfinite(G["lat_total_ms"]).any():
        return None
    k_ini, k_fin = np.nanmin(G["k"]), np.nanmax(G["k"])  # tramo en que el gemelo estuvo activo
    esperadas = int(np.sum((P["k"] >= k_ini) & (P["k"] <= k_fin)))
    procesadas = int(len(np.unique(G["k"])))
    return {"latencia_red_ms": percentiles(G["lat_red_ms"]),
            "latencia_total_ms": percentiles(G["lat_total_ms"]),
            "muestras_publicadas": esperadas, "muestras_procesadas": procesadas,
            "perdida_pct": 100.0 * (esperadas - procesadas) / esperadas if esperadas else 0.0,
            "inicio": fecha_hora(np.nanmin(G["ts_planta"])), "fin": fecha_hora(np.nanmax(G["ts_planta"]))}


# --------------------------------------------------------------------------
# KPIs
# --------------------------------------------------------------------------
def valores_kpi(por_cambio, horas, n_falsas, qos=None):
    """Valor medido de cada KPI de config.METAS_KPI (None si no aplica)."""
    def pct(valores, q):
        return float(np.percentile(valores, q)) if valores else None

    retardos = [f["retardo_deteccion_s"] for f in por_cambio if "retardo_deteccion_s" in f]
    errores = [f["error_param_cambiado_pct"] for f in por_cambio if "error_param_cambiado_pct" in f]
    recuperacion = [f["tiempo_hasta_recalibrar_s"] for f in por_cambio if "tiempo_hasta_recalibrar_s" in f]
    n = len(por_cambio)
    v = {
        # Un cambio absorbido por una recalibración en curso también queda incorporado al gemelo
        "KPI1_tasa_deteccion": 100.0 * sum(f["detectado"] or f["absorbido"] for f in por_cambio) / n if n else None,
        "KPI2_retardo_mediana": pct(retardos, 50),
        "KPI2_retardo_p90": pct(retardos, 90),
        "KPI3_falsas_alarmas": n_falsas / horas if horas else None,
        "KPI4_error_mediana": pct(errores, 50),
        "KPI4_error_p90": pct(errores, 90),
        "KPI5_recuperacion_mediana": pct(recuperacion, 50),
        "KPI6_latencia_p99": qos["latencia_total_ms"]["p99"] if qos and qos["latencia_total_ms"] else None,
        "KPI7_perdida": qos["perdida_pct"] if qos else None,
    }
    return v


def tabla_kpis(valores):
    """Filas (clave, descripción, unidad, meta, valor, cumple) de los KPIs medidos."""
    filas = []
    for clave, (desc, unidad, meta, sentido) in config.METAS_KPI.items():
        valor = valores.get(clave)
        cumple = None if valor is None else (valor >= meta if sentido == "min" else valor <= meta)
        filas.append((clave, desc, unidad, meta, sentido, valor, cumple))
    return filas


def imprimir_kpis(valores):
    """Imprime la tabla de KPI con su meta, el valor medido y si se cumple."""
    print("KPI                                  meta          medido    cumple")
    for _, desc, unidad, meta, sentido, valor, cumple in tabla_kpis(valores):
        signo = ">=" if sentido == "min" else "<="
        medido = "    --" if valor is None else f"{valor:8.2f}"
        estado = "n/a" if cumple is None else ("SI" if cumple else "NO")
        print(f"{desc:<36} {signo} {meta:6.2f} {unidad:<4} {medido}    {estado}")


def imprimir(m):
    """Imprime en consola el resumen de la corrida, la tabla por cambio, la QoS y los KPI."""
    r = m["resumen"]
    print("\n================ RESUMEN ================")
    print(f"Duracion simulada        : {r['duracion_s']:.0f} s")
    absorbidos = f"  (+{r['cambios_absorbidos']} absorbidos en una recalibracion en curso)" if r["cambios_absorbidos"] else ""
    print(f"Cambios reales detectados: {r['cambios_detectados']}/{r['cambios_reales']}{absorbidos}")
    print(f"Detecciones / recalib.   : {r['detecciones_totales']} / {r['recalibraciones']}")
    print(f"Falsas alarmas           : {r['falsas_alarmas']} ({r['falsas_alarmas_por_hora']:.1f} por hora)")
    if r["retardo_medio_s"] is not None:
        print(f"Retardo de deteccion     : medio {r['retardo_medio_s']:.2f} s, max {r['retardo_max_s']:.2f} s")
    print(f"Tiempo en MONITOREO      : {100 * r['fraccion_tiempo_monitoreo']:.1f} %")
    if r["error_final_pct"]:
        print("Error final parametros   : " + "  ".join(f"{k}={v:.1f}%" for k, v in r["error_final_pct"].items()))
    if m["por_cambio"]:
        print("\n  t[s]   cambio        tipo     retardo  t_cambio_est  recal  err.param")
        for f in m["por_cambio"]:
            ret = (f"{f['retardo_deteccion_s']:6.2f}s" if "retardo_deteccion_s" in f
                   else "absorb." if f["absorbido"] else "   --  ")
            est = f"{f['error_instante_estimado_s']:+6.2f}s" if "error_instante_estimado_s" in f else "   --  "
            err = f"{f['error_param_cambiado_pct']:5.1f}%" if "error_param_cambiado_pct" in f else "  --"
            print(f"{f['t_inicio']:7.1f}  {f['param']} x{f['factor']:<6.3g}  {f['tipo']:<8} {ret}   {est}     "
                  f"{f['n_recalibraciones']:>3}   {err}")
    q = m.get("qos")
    if q:
        print(f"\nCorrida MQTT del {q['inicio']} al {q['fin']}")
        print(f"Muestras publicadas / procesadas: {q['muestras_publicadas']} / {q['muestras_procesadas']}  "
              f"(perdida {q['perdida_pct']:.3f} %, rechazadas {r['mensajes_rechazados']})")
        for nombre, clave in (("red  ", "latencia_red_ms"), ("total", "latencia_total_ms")):
            L = q[clave]
            if L:
                print(f"Latencia {nombre} [ms]: p50 {L['p50']:.2f}  p95 {L['p95']:.2f}  p99 {L['p99']:.2f}  "
                      f"max {L['max']:.2f}  (n={L['n']})")
    print()
    imprimir_kpis(m["kpi"])
    print("=========================================\n")


# --------------------------------------------------------------------------
# Figuras
# --------------------------------------------------------------------------
def _marcar_eventos(ax, cambios, eventos, leyenda=False):
    """Cambios reales (punteado gris, rampas sombreadas), detecciones (rojo) y recalibraciones (verde)."""
    etiquetas = {"cambio": "cambio real", "drift_detectado": "drift detectado", "recalibrado": "recalibrado"}
    usadas = set() if leyenda else set(etiquetas)

    def etiqueta(clave):
        if clave in usadas:
            return None
        usadas.add(clave)
        return etiquetas[clave]

    for c in cambios:
        if c["tipo"] == "rampa":
            ax.axvspan(c["t_inicio"], c["t_fin"], color="0.85", zorder=0)
        ax.axvline(c["t_inicio"], color="0.4", ls=":", lw=1.2, label=etiqueta("cambio"))
    for e in eventos:
        if e["tipo"] == "drift_detectado":
            ax.axvline(e["t"], color="#d62728", lw=1, alpha=0.8, label=etiqueta("drift_detectado"))
        elif e["tipo"] == "recalibrado":
            ax.axvline(e["t"], color="#2ca02c", lw=1, alpha=0.8, label=etiqueta("recalibrado"))


def graficar(P, G, cambios, eventos, carpeta_fig):
    """Genera las figuras PNG de la corrida (salidas, discrepancia, parámetros, detalle y latencia).

    Usa el backend "Agg" de matplotlib, que dibuja directo a archivo sin abrir
    ventanas; así el análisis también corre en un PC sin pantalla.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(carpeta_fig, exist_ok=True)

    # 1) Salidas planta vs gemelo
    fig, ax = plt.subplots(3, 1, figsize=(13, 8), sharex=True)
    ax[0].step(P["t"], P["u"], where="post", color="k", lw=0.8)
    ax[0].set_ylabel("V [V]")
    ax[1].plot(P["t"], P["w_med"], color="0.6", lw=0.6, label="planta (medido)")
    ax[1].plot(G["t"], G["w_hat"], color="#1f77b4", lw=0.9, label="gemelo")
    ax[1].set_ylabel(r"$\omega$ [rad/s]")
    ax[2].plot(P["t"], P["i_med"], color="0.6", lw=0.6, label="planta (medido)")
    ax[2].plot(G["t"], G["i_hat"], color="#ff7f0e", lw=0.9, label="gemelo")
    ax[2].set_ylabel("i [A]")
    ax[2].set_xlabel("t [s]")
    for a in ax:
        _marcar_eventos(a, cambios, eventos, leyenda=(a is ax[0]))
        a.grid(alpha=0.3)
    ax[0].legend(loc="upper right", fontsize=8, ncol=3)
    ax[1].legend(loc="upper right", fontsize=8)
    ax[2].legend(loc="upper right", fontsize=8)
    ax[0].set_title("Planta vs gemelo digital")
    fig.tight_layout()
    fig.savefig(os.path.join(carpeta_fig, "salidas.png"), dpi=130)
    plt.close(fig)

    # 2) Índice de discrepancia y CUSUM
    umbral = next((e["umbral_DI"] for e in eventos if "umbral_DI" in e), None)
    fig, ax = plt.subplots(2, 1, figsize=(13, 6), sharex=True)
    ax[0].semilogy(G["t"], G["DI"], color="#1f77b4", lw=0.9, label="DI")
    if umbral:
        ax[0].axhline(umbral, color="#d62728", ls="--", lw=1, label=f"umbral = {umbral:.2f}")
    ax[0].axhline(1.0, color="0.5", ls=":", lw=1, label="E[DI] sin drift = 1")
    ax[0].set_ylabel("Indice de discrepancia")
    ax[0].legend(loc="upper right", fontsize=8)
    ax[1].plot(G["t"], G["cusum"], color="#9467bd", lw=0.9, label="CUSUM g")
    ax[1].axhline(config.GEMELO["cusum_h"], color="#d62728", ls="--", lw=1, label="h")
    ax[1].set_ylim(0, 2.5 * config.GEMELO["cusum_h"])
    ax[1].set_ylabel("CUSUM")
    ax[1].set_xlabel("t [s]")
    ax[1].legend(loc="upper right", fontsize=8)
    for a in ax:
        _marcar_eventos(a, cambios, eventos)
        a.grid(alpha=0.3, which="both")
    ax[0].set_title("Deteccion de drift")
    fig.tight_layout()
    fig.savefig(os.path.join(carpeta_fig, "discrepancia.png"), dpi=130)
    plt.close(fig)

    # 3) Parámetros reales vs estimados
    fig, ax = plt.subplots(len(PARAM_NOMBRES), 1, figsize=(13, 9), sharex=True)
    unidades = {"R": "[ohm]", "L": "[H]", "K": "[N m/A]", "J": "[kg m2]", "b": "[N m s]"}
    for a, n in zip(ax, PARAM_NOMBRES):
        a.plot(P["t"], P[n], color="k", lw=1.5, ls="--", label="real (planta)")
        a.step(G["t"], G[n], where="post", color=COLORES[n], lw=1.6, label="estimado (gemelo)")
        a.set_ylabel(f"{n} {unidades[n]}")
        _marcar_eventos(a, cambios, eventos)
        a.grid(alpha=0.3)
    ax[0].legend(loc="upper left", fontsize=8, ncol=2)
    ax[0].set_title("Recalibracion: parametros reales vs estimados")
    ax[-1].set_xlabel("t [s]")
    fig.tight_layout()
    fig.savefig(os.path.join(carpeta_fig, "parametros.png"), dpi=130)
    plt.close(fig)

    # 4) Zoom al primer cambio: cómo se ve la discrepancia y la recalibración
    if cambios:
        c = cambios[0]
        t0, t1 = c["t_inicio"] - 5, c["t_inicio"] + 25
        mp = (P["t"] >= t0) & (P["t"] <= t1)
        mg = (G["t"] >= t0) & (G["t"] <= t1)
        ev = [e for e in eventos if t0 <= e["t"] <= t1]
        fig, ax = plt.subplots(2, 1, figsize=(11, 6), sharex=True)
        ax[0].plot(P["t"][mp], P["w_med"][mp], color="0.5", lw=0.8, label="planta (medido)")
        ax[0].plot(G["t"][mg], G["w_hat"][mg], color="#1f77b4", lw=1.2, label="gemelo")
        ax[0].set_ylabel(r"$\omega$ [rad/s]")
        ax[1].semilogy(G["t"][mg], G["DI"][mg], color="#1f77b4", lw=1.2, label="DI")
        if umbral:
            ax[1].axhline(umbral, color="#d62728", ls="--", lw=1, label="umbral")
        ax[1].set_ylabel("DI")
        ax[1].set_xlabel("t [s]")
        for a in ax:
            _marcar_eventos(a, [c], ev, leyenda=(a is ax[0]))
            a.grid(alpha=0.3, which="both")
            a.legend(loc="upper left", fontsize=8)
        ax[0].set_title(f"Detalle: {c['param']} x{c['factor']:.2f} en t={c['t_inicio']:.0f} s ({c.get('desc', '')})")
        fig.tight_layout()
        fig.savefig(os.path.join(carpeta_fig, "detalle_primer_cambio.png"), dpi=130)
        plt.close(fig)

    # 5) Latencia (sólo corridas con MQTT)
    if "lat_total_ms" in G and np.isfinite(G["lat_total_ms"]).any():
        fig, ax = plt.subplots(1, 2, figsize=(11, 3.6), gridspec_kw={"width_ratios": [2, 1]})
        ok = np.isfinite(G["lat_total_ms"])
        ax[0].plot(G["t"][ok], G["lat_red_ms"][ok], color="#1f77b4", lw=0.6, label="red (planta -> gemelo)")
        ax[0].plot(G["t"][ok], G["lat_total_ms"][ok], color="#ff7f0e", lw=0.6, alpha=0.8,
                   label="total (incluye cola y computo)")
        ax[0].axhline(config.METAS_KPI["KPI6_latencia_p99"][2], color="#d62728", ls="--", lw=1, label="meta p99")
        ax[0].set_xlabel("t [s]")
        ax[0].set_ylabel("latencia [ms]")
        ax[0].legend(loc="upper right", fontsize=8)
        _marcar_eventos(ax[0], [], eventos)
        bins = np.linspace(0, max(5.0, np.nanpercentile(G["lat_total_ms"], 99.9)), 60)
        ax[1].hist(G["lat_red_ms"][ok], bins=bins, color="#1f77b4", alpha=0.7, label="red")
        ax[1].hist(G["lat_total_ms"][ok], bins=bins, color="#ff7f0e", alpha=0.5, label="total")
        ax[1].set_yscale("log")
        ax[1].set_xlabel("latencia [ms]")
        ax[1].set_ylabel("muestras")
        ax[1].legend(fontsize=8)
        for a in ax:
            a.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(carpeta_fig, "latencia.png"), dpi=130)
        plt.close(fig)


def analizar(carpeta, figuras=True):
    """Análisis completo de una carpeta de registros: métricas, metricas.json y figuras."""
    P, G, cambios, eventos = cargar(carpeta)
    m = calcular_metricas(P, G, cambios, eventos)
    imprimir(m)
    with open(os.path.join(carpeta, "metricas.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, ensure_ascii=False)
    if figuras:
        carpeta_fig = os.path.join(carpeta, "figuras")
        graficar(P, G, cambios, eventos, carpeta_fig)
        print(f"Figuras en {os.path.abspath(carpeta_fig)}")
    return m


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("carpeta", nargs="?", default="logs")
    ap.add_argument("--sin-figuras", action="store_true")
    args = ap.parse_args()
    analizar(args.carpeta, figuras=not args.sin_figuras)
