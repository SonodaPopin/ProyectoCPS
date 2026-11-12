"""PROCESO 3: dashboard en tiempo real (matplotlib) con botones para inyectar fallas.

Se suscribe a todos los tópicos, incluida la "verdad" de la planta que el
gemelo no ve, y muestra lado a lado:
- la velocidad y la corriente medidas por la planta y predichas por el gemelo,
- el índice de discrepancia y el CUSUM con sus umbrales,
- los parámetros reales contra los estimados,
- una tabla con el error de cada parámetro, la latencia y el estado de conexión.

Los botones publican una falla en planta/inyeccion para provocar un drift
durante la demostración. El dashboard no participa en la detección: si se
cierra, planta y gemelo siguen funcionando igual.

Funciona con dos hilos. El hilo de red de MQTT guarda cada mensaje en los
buffers de la clase Datos, y el hilo gráfico de matplotlib redibuja cada
150 ms leyendo esos buffers. Un candado (lock) evita que ambos los toquen al
mismo tiempo.

    python dashboard.py
    python dashboard.py --captura dashboard.png --tras 60   # sin ventana: guarda una imagen y sale
"""
import json
import math
import threading
import time
from collections import deque

import matplotlib
import numpy as np

from cps import config
from cps.mqtt_util import a_json, conectar, log, parser
from cps.modelo import PARAM_NOMBRES
from cps.registro import fecha_hora

VENTANA_S = 60.0  # segundos simulados que se ven en los gráficos de tiempo
COLORES = {"R": "#1f77b4", "L": "#9467bd", "K": "#d62728", "J": "#2ca02c", "b": "#ff7f0e"}
COLOR_MODO = {"MONITOREO": "#2ca02c", "RECALIBRANDO": "#d62728",
              "CALIBRACION_INICIAL": "#ff7f0e", "SIN_DATOS": "0.5"}
# Botones de falla: (texto del botón, mensaje que se publica en planta/inyeccion)
FALLAS = [
    ("Acoplar carga\nJ x1.5", {"param": "J", "factor": 1.5, "desc": "Se acopla una carga"}),
    ("Desacoplar carga\nJ x0.67", {"param": "J", "factor": 0.67, "desc": "Se desacopla una carga"}),
    ("Falla rodamiento\nb x2", {"param": "b", "factor": 2.0, "desc": "Falla de rodamiento"}),
    ("Relubricar\nb x0.5", {"param": "b", "factor": 0.5, "desc": "Relubricacion"}),
    ("Calentar bobinado\nR x1.4 (rampa 30 s)", {"param": "R", "factor": 1.4, "rampa_s": 30, "desc": "Calentamiento"}),
    ("Desmagnetizar\nK x0.85", {"param": "K", "factor": 0.85, "desc": "Desmagnetizacion"}),
]


def num(v):
    """Convierte null (None) de JSON en NaN, que matplotlib dibuja como un hueco."""
    return math.nan if v is None else float(v)


class Datos:
    """Buffers compartidos entre el hilo MQTT (que escribe) y el hilo gráfico (que lee).

    Los deque con maxlen guardan solo lo que cabe en la ventana de 60 s; al
    llenarse descartan solos lo más antiguo, así el uso de memoria no crece.
    """

    def __init__(self):
        self.lock = threading.Lock()
        n = int(VENTANA_S / config.TS) + 100
        self.planta = deque(maxlen=n)   # (t, u, i, w)
        self.gemelo = deque(maxlen=n)   # (t, i_hat, w_hat, DI, cusum)
        self.verdad = deque(maxlen=50000)  # (t, params)
        self.estimados = []             # (t, params)
        self.eventos = []               # (t, tipo)
        self.estado = {}
        self.latencias = deque(maxlen=n)  # latencia planta -> gemelo [ms]
        self.conexion = {"planta": "?", "gemelo": "?"}
        self.n_recal = 0


def main():
    ap = parser(__doc__)
    ap.add_argument("--captura", help="guardar una imagen del dashboard (sin abrir ventana) y salir")
    ap.add_argument("--tras", type=float, default=30.0, help="segundos a esperar antes de la captura")
    args = ap.parse_args()
    if args.captura:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    from matplotlib.widgets import Button

    top = config.topicos(args.prefijo)
    d = Datos()

    def on_message(client, userdata, msg):
        """Hilo de red: decodifica el JSON y lo guarda bajo el candado."""
        try:
            m = json.loads(msg.payload)
            with d.lock:
                guardar(msg.topic, m)
        except (ValueError, KeyError, TypeError):
            pass  # un mensaje malformado no debe detener el hilo de red

    def guardar(topico, m):
        """Guarda el mensaje en el buffer que corresponde a su tópico."""
        if topico == top["medicion"]:
            d.planta.append((m["t"], m["u"], m["i"], m["w"]))
        elif topico == top["gemelo_estado"]:
            d.gemelo.append((m["t"], num(m["i_hat"]), num(m["w_hat"]), num(m["DI"]), num(m["cusum"])))
            d.estado = m
            if m.get("lat_total_ms") is not None:
                d.latencias.append(m["lat_total_ms"])
        elif topico == top["verdad"]:
            d.verdad.append((m["t"], m["params"]))
        elif topico == top["gemelo_parametros"]:
            d.estimados.append((m["t"], m["params"]))
            d.n_recal = m.get("n_recalibraciones", 0)
        elif topico == top["gemelo_eventos"]:
            if m["tipo"] in ("drift_detectado", "recalibrado", "calibracion_inicial"):
                d.eventos.append((m["t"], m["tipo"]))
        elif topico in (top["planta_conexion"], top["gemelo_conexion"]):
            d.conexion[m.get("proceso", "?")] = m.get("estado", "?")

    subs = [(top[n], 1) for n in ("medicion", "gemelo_estado", "verdad", "gemelo_parametros",
                                  "gemelo_eventos", "planta_conexion", "gemelo_conexion")]
    cliente = conectar("dashboard", args, on_message, suscripciones=subs)

    # ------------------------------------------------------------- figura
    # Izquierda, tres gráficos de tiempo (velocidad, corriente, DI y CUSUM).
    # Derecha, los parámetros, la tabla de estado y los botones de falla.
    # Las líneas se crean vacías una sola vez y actualizar() solo les cambia los datos.
    fig = plt.figure(figsize=(15, 8.5))
    fig.canvas.manager.set_window_title("Gemelo digital - motor DC")
    gs = fig.add_gridspec(3, 2, width_ratios=[1.7, 1], hspace=0.32, wspace=0.18,
                          left=0.05, right=0.98, top=0.92, bottom=0.06)
    ax_w = fig.add_subplot(gs[0, 0])
    ax_i = fig.add_subplot(gs[1, 0], sharex=ax_w)
    ax_di = fig.add_subplot(gs[2, 0], sharex=ax_w)
    ax_p = fig.add_subplot(gs[0, 1])
    ax_txt = fig.add_subplot(gs[1, 1])
    ax_txt.axis("off")

    (l_w, ) = ax_w.plot([], [], color="0.55", lw=0.8, label="planta (medido)")
    (l_wh, ) = ax_w.plot([], [], color="#1f77b4", lw=1.4, label="gemelo")
    (l_i, ) = ax_i.plot([], [], color="0.55", lw=0.8, label="planta (medido)")
    (l_ih, ) = ax_i.plot([], [], color="#ff7f0e", lw=1.4, label="gemelo")
    (l_di, ) = ax_di.plot([], [], color="#1f77b4", lw=1.2, label="DI")
    l_umbral = ax_di.axhline(1.0, color="#d62728", ls="--", lw=1, label="umbral")
    ax_di.axhline(1.0, color="0.5", ls=":", lw=1)
    ax_cu = ax_di.twinx()
    (l_cu, ) = ax_cu.plot([], [], color="#9467bd", lw=0.9, alpha=0.7, label="CUSUM")
    ax_cu.axhline(config.GEMELO["cusum_h"], color="#9467bd", ls="--", lw=0.8, alpha=0.6)
    ax_cu.set_ylim(0, 2 * config.GEMELO["cusum_h"])
    ax_cu.set_ylabel("CUSUM", color="#9467bd")
    ax_di.set_yscale("log")
    ax_w.set_ylabel(r"$\omega$ [rad/s]")
    ax_i.set_ylabel("i [A]")
    ax_di.set_ylabel("Indice de discrepancia")
    ax_di.set_xlabel("t [s]")
    for a in (ax_w, ax_i, ax_di):
        a.grid(alpha=0.3, which="both")
    ax_w.legend(loc="upper left", fontsize=8)
    ax_i.legend(loc="upper left", fontsize=8)
    ax_di.legend(handles=[l_di, l_umbral, l_cu], loc="upper left", fontsize=8)

    lineas_real, lineas_est = {}, {}
    for n in PARAM_NOMBRES:
        (lineas_real[n], ) = ax_p.plot([], [], color=COLORES[n], ls="--", lw=1)
        (lineas_est[n], ) = ax_p.plot([], [], color=COLORES[n], lw=2, label=n, drawstyle="steps-post")
    ax_p.axhline(1.0, color="0.6", lw=0.8)
    ax_p.set_title("Parametros / nominal  (- - real, -- gemelo)", fontsize=10)
    ax_p.legend(loc="best", fontsize=8, ncol=5)
    ax_p.grid(alpha=0.3)
    texto = ax_txt.text(0.0, 1.0, "", va="top", ha="left", family="monospace", fontsize=10,
                        transform=ax_txt.transAxes)
    titulo = fig.suptitle("Esperando datos...", fontsize=14, fontweight="bold")

    # Botones de inyección de fallas: cada uno publica su mensaje en planta/inyeccion
    sub = gs[2, 1].subgridspec(3, 2, hspace=0.35, wspace=0.08)
    botones = []
    for idx, (etiqueta, falla) in enumerate(FALLAS):
        b = Button(fig.add_subplot(sub[idx // 2, idx % 2]), etiqueta, color="#f2f2f2", hovercolor="#ffd9d9")
        b.label.set_fontsize(9)
        b.on_clicked(lambda _e, f=falla: cliente.publish(top["inyeccion"], a_json(f), qos=1))
        botones.append(b)

    marcas = []  # líneas verticales de detecciones y recalibraciones: (t, [líneas])
    dibujados = set()

    def actualizar(_frame):
        """Hilo gráfico: copia los buffers (rápido, bajo el candado) y luego redibuja sin bloquear al hilo de red."""
        with d.lock:
            P = list(d.planta)
            G = list(d.gemelo)
            V = list(d.verdad)
            E = list(d.estimados)
            eventos = list(d.eventos)
            latencias = list(d.latencias)
            est, con, n_recal = dict(d.estado), dict(d.conexion), d.n_recal
        if not P:
            return
        t_fin = P[-1][0]
        t_ini = t_fin - VENTANA_S
        tp, _, ip, wp = zip(*P)
        l_w.set_data(tp, wp)
        l_i.set_data(tp, ip)
        if G:
            tg, ig, wg, dig, cug = zip(*G)
            l_wh.set_data(tg, wg)
            l_ih.set_data(tg, ig)
            l_di.set_data(tg, [max(v, 1e-2) if v == v else math.nan for v in dig])
            l_cu.set_data(tg, cug)
            validos = [v for v in dig if v == v]
            ax_di.set_ylim(0.3, max(10.0, 2 * max(validos)) if validos else 10.0)
        if "umbral_DI" in est:
            l_umbral.set_ydata([est["umbral_DI"]] * 2)
        ax_w.set_xlim(t_ini, t_fin + 0.5)
        for a in (ax_w, ax_i):
            a.relim()
            a.autoscale_view(scalex=False)

        # Marcas de detección (rojo) y recalibración (verde)
        for t_ev, tipo in eventos:
            if (t_ev, tipo) in dibujados:
                continue
            dibujados.add((t_ev, tipo))
            color = "#d62728" if tipo == "drift_detectado" else "#2ca02c"
            marcas.append((t_ev, [a.axvline(t_ev, color=color, lw=1.2, alpha=0.8) for a in (ax_w, ax_i, ax_di)]))
        while marcas and marcas[0][0] < t_ini - 5:
            for linea in marcas.pop(0)[1]:
                linea.remove()

        # Parámetros normalizados por el valor nominal
        nominal = config.PARAMS_NOMINALES
        for n in PARAM_NOMBRES:
            if V:
                lineas_real[n].set_data([v[0] for v in V], [v[1][n] / nominal[n] for v in V])
            if E:
                xs = [e[0] for e in E] + [t_fin]
                ys = [e[1][n] / nominal[n] for e in E] + [E[-1][1][n] / nominal[n]]
                lineas_est[n].set_data(xs, ys)
        ax_p.set_xlim(0, max(t_fin, 10))
        ax_p.relim()
        ax_p.autoscale_view(scalex=False)

        # Tabla real vs estimado
        real = V[-1][1] if V else {}
        estim = E[-1][1] if E else {}
        filas = ["Param    Real      Gemelo    Error", "-" * 36]
        for n in PARAM_NOMBRES:
            r, e = real.get(n), estim.get(n)
            err = f"{100 * abs(e / r - 1):5.1f}%" if r and e else "   --"
            filas.append(f"{n:<6}{r if r else math.nan:9.4f}  {e if e else math.nan:9.4f}  {err}")
        di = est.get("DI")
        di_txt = f"{di:6.2f}" if di is not None else "    --"
        filas += ["",
                  f"DI = {di_txt}   (umbral {est.get('umbral_DI', math.nan):.2f})",
                  f"CUSUM = {est.get('cusum', math.nan):6.1f}  (h = {config.GEMELO['cusum_h']:.0f})",
                  f"Recalibraciones: {n_recal}",
                  f"Planta: {con['planta']}   Gemelo: {con['gemelo']}"]
        if latencias:
            p50, p99 = np.percentile(latencias, [50, 99])
            filas.append(f"Latencia planta->gemelo: p50 {p50:.1f} ms, p99 {p99:.1f} ms")
        texto.set_text("\n".join(filas))

        modo = est.get("modo", "SIN_DATOS")
        titulo.set_text(f"Gemelo digital  |  modo: {modo}  |  t = {t_fin:6.1f} s  |  {fecha_hora()[:19]}")
        titulo.set_color(COLOR_MODO.get(modo, "k"))

    if args.captura:
        time.sleep(args.tras)
        actualizar(0)
        fig.savefig(args.captura, dpi=110)
        log("dashboard", f"captura guardada en {args.captura}")
    else:
        # FuncAnimation llama a actualizar() cada 150 ms mientras la ventana esté abierta
        _anim = FuncAnimation(fig, actualizar, interval=150, cache_frame_data=False)
        plt.show()
    cliente.disconnect()
    cliente.loop_stop()


if __name__ == "__main__":
    main()
