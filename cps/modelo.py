"""Modelo físico del motor DC. Lo usan tanto la planta como el gemelo.

El motor se describe con dos variables de estado, la corriente del bobinado i
[A] y la velocidad angular del eje w [rad/s], y una entrada, el voltaje
aplicado V [V]. Las ecuaciones salen de dos leyes conocidas:

    L di/dt = V - R i - K w     (Kirchhoff en el circuito de armadura)
    J dw/dt = K i - b w         (Newton para la rotación del eje)

La primera dice que el voltaje aplicado se reparte entre la resistencia (R i),
la inductancia (L di/dt) y la fuerza contraelectromotriz que genera el motor
al girar (K w). La segunda dice que el torque eléctrico (K i) acelera la
inercia del eje (J dw/dt) y se pierde en parte por fricción (b w).

Parámetros y unidades:
    R  resistencia de armadura          [ohm]
    L  inductancia de armadura          [H]
    K  constante de torque y de fem     [N·m/A] = [V·s/rad]
    J  inercia del rotor y la carga     [kg·m²]
    b  fricción viscosa                 [N·m·s]

En forma matricial queda dx/dt = A x + B u con x = [i, w]. Si todos los
parámetros son positivos, traza(A) < 0 y det(A) > 0, así que el sistema es
siempre estable (sus dos polos tienen parte real negativa).

En el código los parámetros se manejan de dos formas: como diccionario
{"R": ..., "L": ...} (cómodo para leer y para enviar por MQTT en JSON) o como
vector numpy [R, L, K, J, b] (cómodo para hacer cálculos). a_vector() y
a_dict() convierten entre ambas.
"""
import numpy as np
from scipy.linalg import expm

# Orden fijo de los parámetros dentro de los vectores numpy
PARAM_NOMBRES = ("R", "L", "K", "J", "b")


def a_vector(params):
    """Convierte un diccionario de parámetros en un vector numpy [R, L, K, J, b]."""
    return np.array([float(params[n]) for n in PARAM_NOMBRES])


def a_dict(p):
    """Convierte un vector numpy [R, L, K, J, b] en un diccionario {"R": ..., ...}."""
    return {n: float(v) for n, v in zip(PARAM_NOMBRES, p)}


def matrices(p):
    """Devuelve las matrices continuas (A, B) del modelo dx/dt = A x + B u.

    Se obtienen despejando di/dt y dw/dt de las dos ecuaciones del motor.
    """
    R, L, K, J, b = p
    A = np.array([[-R / L, -K / L],
                  [K / J, -b / J]])
    B = np.array([1.0 / L, 0.0])
    return A, B


def derivada(x, v, p):
    """Lado derecho de las ecuaciones: devuelve [di/dt, dw/dt] para el estado x
    y el voltaje v. Es lo que necesita el integrador numérico."""
    R, L, K, J, b = p
    i, w = x
    return np.array([(v - R * i - K * w) / L,
                     (K * i - b * w) / J])


def paso_rk4(x, v, p, dt):
    """Avanza el estado un paso dt con Runge-Kutta de cuarto orden.

    RK4 evalúa la derivada en cuatro puntos del intervalo (inicio, dos veces
    en la mitad y al final) y los promedia con pesos 1-2-2-1. Es mucho más
    preciso que Euler con el mismo dt. La planta lo usa para simular el motor
    "real" en tiempo continuo, con el voltaje v constante durante el paso.
    """
    k1 = derivada(x, v, p)
    k2 = derivada(x + 0.5 * dt * k1, v, p)
    k3 = derivada(x + 0.5 * dt * k2, v, p)
    k4 = derivada(x + dt * k3, v, p)
    return x + dt / 6.0 * (k1 + 2 * k2 + 2 * k3 + k4)


def discretizar(p, ts):
    """Convierte el modelo continuo en uno discreto exacto, x[n+1] = Phi x[n] + Gam u[n].

    Si el voltaje se mantiene constante entre muestras (retención de orden
    cero, ZOH), la solución exacta de dx/dt = A x + B u durante un periodo ts
    es Phi = e^(A ts) y Gam = (integral de e^(A s) ds de 0 a ts) B. Ambas se
    obtienen de una sola exponencial de la matriz aumentada [[A, B], [0, 0]]:
    el bloque superior izquierdo del resultado es Phi y la última columna es
    Gam. A diferencia de Euler, aquí no hay error de discretización.
    """
    A, B = matrices(p)
    M = np.zeros((3, 3))
    M[:2, :2] = A
    M[:2, 2] = B
    E = expm(M * ts)
    return E[:2, :2], E[:2, 2]


def simular(p, x0, u, ts):
    """Simula el modelo discreto desde el estado x0 con la secuencia de voltajes u.

    Devuelve una matriz X de N filas y 2 columnas (i, w), con X[0] = x0 y
    X[n] = estado en el instante t_n. El voltaje u[n] se aplica durante el
    intervalo [t_n, t_n+1). La recalibración del gemelo llama a esta función
    cientos de veces, por eso el ciclo trabaja con números de Python sueltos
    (a11, a12, ...) en vez de multiplicar matrices numpy en cada paso, que
    sería varias veces más lento para matrices de 2x2.
    """
    Phi, Gam = discretizar(p, ts)
    a11, a12, a21, a22 = Phi.ravel().tolist()
    g1, g2 = Gam.tolist()
    x1, x2 = float(x0[0]), float(x0[1])
    filas = []
    for un in np.asarray(u, dtype=float).tolist():
        filas.append((x1, x2))
        x1, x2 = a11 * x1 + a12 * x2 + g1 * un, a21 * x1 + a22 * x2 + g2 * un
    return np.array(filas)


def estado_estacionario(p, v):
    """Corriente y velocidad a las que llega el motor si el voltaje v se mantiene.

    En régimen permanente las derivadas valen cero; al resolver las dos
    ecuaciones resulta w = K v / (R b + K²) e i = b w / K.
    """
    R, L, K, J, b = p
    w = K * v / (R * b + K * K)
    return np.array([b * w / K, w])
