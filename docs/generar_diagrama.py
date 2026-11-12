"""Genera docs/figuras/arquitectura.png (diagrama de arquitectura CPS del informe).

    python docs/generar_diagrama.py

El detalle de cada señal (tópico, período, QoS) está en la tabla de señales del
informe; aquí las flechas llevan sólo un nombre corto.
"""
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

AQUI = os.path.dirname(os.path.abspath(__file__))
AZUL, NARANJO, VERDE, MORADO, ROJO, GRIS = "#1f5f99", "#b36b00", "#2a7a3b", "#6b3f8f", "#b03030", "#555555"
FONDO = {AZUL: "#e8f1fb", NARANJO: "#fff4e0", VERDE: "#e9f6ec", MORADO: "#f3ecf8", ROJO: "#fdeaea", GRIS: "#f2f2f2"}
F = 7.8  # tamaño de letra base


def caja(ax, x, y, w, h, texto, color, fondo=None, negrita=False, tam=F, lw=0.8):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.2,rounding_size=0.7",
                                facecolor=fondo or "white", edgecolor=color, linewidth=lw))
    if texto:
        ax.text(x + w / 2, y + h / 2, texto, ha="center", va="center", fontsize=tam,
                fontweight="bold" if negrita else "normal", linespacing=1.2)


def bloque(ax, x, y, w, h, titulo, subtitulo, color):
    caja(ax, x, y, w, h, "", color, FONDO[color], lw=1.6)
    ax.text(x + 1, y + h - 1.0, titulo, ha="left", va="top", fontsize=F + 0.4, fontweight="bold", color=color)
    if subtitulo:
        ax.text(x + 1, y + h - 4.3, subtitulo, ha="left", va="top", fontsize=F - 0.8, color=color)


def flecha(ax, p1, p2, color, texto="", pos=(0, 0), ls="-", rad=0.0, alin="center", tam=F - 0.5):
    ax.add_patch(FancyArrowPatch(p1, p2, arrowstyle="-|>", mutation_scale=9, color=color, lw=1.2,
                                 linestyle=ls, connectionstyle=f"arc3,rad={rad}", shrinkA=0, shrinkB=0))
    if texto:
        ax.text(pos[0], pos[1], texto, ha=alin, va="center", fontsize=tam, color=color, linespacing=1.15)


def main():
    fig, ax = plt.subplots(figsize=(7.4, 4.1))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 56)
    ax.axis("off")

    # ---------------- Proceso 1: planta
    bloque(ax, 0.5, 9, 25, 46.5, "Proceso 1: planta.py", "planta física simulada", AZUL)
    caja(ax, 2, 40, 22, 6.5, "Actuador: driver PWM\nV ∈ [0, 12] V", AZUL)
    caja(ax, 2, 29.5, 22, 7.5, "Motor DC\nL di/dt = V − R i − K ω\nJ dω/dt = K i − b ω", AZUL)
    caja(ax, 2, 19.5, 22, 7.0, "Sensores\ncorriente i · encoder ω", AZUL)
    caja(ax, 2, 11, 22, 5.5, "Drift sin aviso\nR, J, b, K", ROJO, FONDO[ROJO])
    flecha(ax, (13, 40), (13, 37.1), "0.3")
    flecha(ax, (13, 29.5), (13, 26.6), "0.3")
    flecha(ax, (13, 16.5), (13, 19.4), ROJO)

    # ---------------- Broker
    caja(ax, 35, 21, 16, 17, "Broker MQTT\n(Mosquitto)\n\nTCP/IP 1883\nTLS 8883\nauth + ACL\nLast Will",
         NARANJO, FONDO[NARANJO], lw=1.6, tam=F - 0.3)

    # ---------------- Proceso 2: gemelo
    bloque(ax, 59, 17, 40.5, 38.5, "Proceso 2: gemelo.py (cómputo)", None, VERDE)
    ax.text(69.8, 50.0, "nivel 5C", ha="right", va="center", fontsize=F - 1.3, style="italic", color=VERDE)
    filas = [("Validación de mensajes", "Conexión"),
             ("Modelo  x̂ₖ = Φ x̂ₖ₋₁ + Γ uₖ₋₁", "Ciber"),
             ("Residuo → índice DI + CUSUM", "Conversión"),
             ("Detección de drift", "Cognición"),
             ("Recalibración (mín. cuadrados)", "Configuración")]
    y0, paso, alto = 44.5, 6.0, 4.4
    x0, ancho = 71.0, 25.0
    for n, (texto, nivel) in enumerate(filas):
        y = y0 - n * paso
        caja(ax, x0, y, ancho, alto, texto, VERDE, tam=F - 0.9)
        ax.text(69.8, y + alto / 2, nivel, ha="right", va="center", fontsize=F - 1.2, style="italic", color=VERDE)
        if n:
            flecha(ax, (x0 + ancho / 2, y + paso), (x0 + ancho / 2, y + alto), VERDE)
    # lazo: la recalibración actualiza el modelo
    flecha(ax, (x0 + ancho + 0.3, y0 - 4 * paso + alto / 2), (x0 + ancho + 0.3, y0 - paso + alto / 2), VERDE,
           rad=0.3)

    # ---------------- Proceso 3: dashboard
    bloque(ax, 59, 1.5, 40.5, 11, "Proceso 3: dashboard.py", "visualización + inyección de fallas", MORADO)

    # ---------------- Almacenamiento
    caja(ax, 0.5, 0.5, 51, 6.0, "Almacenamiento local: registros de planta.py y gemelo.py\n"
                               "(CSV / JSONL, UTF-8, marca de tiempo por muestra)", GRIS, FONDO[GRIS],
         tam=F - 0.8, lw=1.2)

    # ---------------- Flujos MQTT
    flecha(ax, (25.5, 33), (35, 33), AZUL, "medición\n(50 ms)", (30.2, 37.2))
    flecha(ax, (51, 33), (59, 36), AZUL, "medición", (54.6, 38.4), tam=F - 1)
    flecha(ax, (59, 26), (51, 26), VERDE, "estado\nparámetros\neventos", (54.6, 22.0), tam=F - 1.4)
    flecha(ax, (47, 21), (59, 10.5), MORADO, "suscripción\n(lectura)", (54.2, 14.6), alin="right", tam=F - 1)
    flecha(ax, (59, 4.5), (25.6, 13.5), MORADO, ls="--")
    ax.text(41, 10.6, "inyección de fallas (vía broker)", ha="center", va="center", fontsize=F - 1.2,
            color=MORADO, rotation=-15)

    # ---------------- Registro a disco
    flecha(ax, (6, 9), (6, 6.6), GRIS, ls=":")

    salida = os.path.join(AQUI, "figuras", "arquitectura.png")
    fig.savefig(salida, dpi=220, bbox_inches="tight", pad_inches=0.03)
    print(f"diagrama guardado en {salida}")


if __name__ == "__main__":
    main()
