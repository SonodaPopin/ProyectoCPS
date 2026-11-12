"""Planta física simulada: un motor DC cuyos parámetros cambian sin aviso.

Este módulo hace el papel del "mundo real" del proyecto. Contiene cuatro piezas:

- Escenarios de drift: listas de cambios de parámetros con su instante,
  magnitud y si son bruscos (escalón) o graduales (rampa).
- GestorDrift: aplica esos cambios a los parámetros a medida que avanza el tiempo.
- GeneradorEntrada: decide qué voltaje se aplica al motor en cada momento.
- PlantaSimulada: junta todo, integra la física del motor y entrega mediciones
  con ruido, como lo harían sensores reales.

El módulo no sabe nada de MQTT. Lo usan planta.py, que publica las mediciones
en el broker, y simulacion_offline.py, que corre planta y gemelo en un mismo
proceso para hacer experimentos rápidos.
"""
import numpy as np

from . import config
from .modelo import PARAM_NOMBRES, a_vector, a_dict, paso_rk4

# --------------------------------------------------------------------------
# Escenarios de deriva
# --------------------------------------------------------------------------
# Cada evento indica cuándo empieza (t, en segundos simulados), qué parámetro
# cambia, por cuánto se multiplica su valor actual (factor) y, si trae rampa_s,
# en cuántos segundos llega al valor final. Sin rampa_s el cambio es un escalón.
ESCENARIOS = {
    "demo": [
        {"t": 60.0, "param": "J", "factor": 1.6, "desc": "Se acopla una carga (aumenta la inercia)"},
        {"t": 120.0, "param": "R", "factor": 1.4, "rampa_s": 60.0, "desc": "Calentamiento del bobinado"},
        {"t": 240.0, "param": "b", "factor": 2.0, "desc": "Falla incipiente de rodamiento"},
        {"t": 300.0, "param": "K", "factor": 0.8, "desc": "Desmagnetizacion parcial de imanes"},
    ],
    "sin_drift": [],
}

# Cambios con sentido físico que puede sufrir cada parámetro, usados por el
# escenario aleatorio. Formato: (probabilidad de elegir ese parámetro,
# ((rango del factor si sube), causa), ((rango del factor si baja), causa)).
# K solo puede bajar, porque la desmagnetización de los imanes no se revierte.
FENOMENOS = {
    "R": (0.30, ((1.2, 1.5), "Calentamiento del bobinado"), ((0.75, 0.9), "Enfriamiento del bobinado")),
    "J": (0.30, ((1.3, 1.8), "Se acopla una carga"), ((0.55, 0.8), "Se desacopla una carga")),
    "b": (0.28, ((1.3, 2.0), "Desgaste de rodamientos"), ((0.6, 0.85), "Relubricacion")),
    "K": (0.12, None, ((0.8, 0.92), "Desmagnetizacion parcial de imanes")),  # irreversible
}


def escenario_aleatorio(semilla=None, duracion=900.0, intervalo_medio=75.0, t_primero=45.0):
    """Genera una lista de cambios en instantes, parámetros y magnitudes aleatorias.

    Los instantes siguen un proceso de Poisson: el tiempo entre cambios es
    exponencial, con promedio intervalo_medio segundos (y al menos 40 s, para
    que el gemelo alcance a recalibrarse entre uno y otro). Para que el
    escenario tenga sentido físico, cada parámetro tiende a volver a su valor
    de referencia: si ya subió mucho, lo más probable es que ahora baje (una
    carga que se acopla luego se desacopla). Usar la misma semilla reproduce
    exactamente el mismo experimento.
    """
    rng = np.random.default_rng(semilla)
    nombres = list(FENOMENOS)
    probs = np.array([FENOMENOS[n][0] for n in nombres])
    multiplicador = {n: 1.0 for n in nombres}  # cuánto ha cambiado cada parámetro respecto del inicio
    eventos = []
    t = t_primero + rng.exponential(intervalo_medio / 3)
    while t < duracion:
        # K deja de ser elegible cuando ya bajó a menos del 70 % de su valor inicial
        disponibles = probs * np.array([multiplicador[n] > 0.7 or n != "K" for n in nombres])
        param = str(rng.choice(nombres, p=disponibles / disponibles.sum()))
        _, sube, baja = FENOMENOS[param]
        m = multiplicador[param]
        # Probabilidad de subir: baja si el parámetro ya está alto y sube si está bajo
        p_subir = 0.0 if sube is None else (0.15 if m > 1.3 else 0.85 if m < 0.8 else 0.5)
        (lo, hi), causa = sube if rng.random() < p_subir else baja
        factor = float(rng.uniform(lo, hi))
        multiplicador[param] = m * factor
        ev = {"t": round(float(t), 2), "param": param, "factor": round(factor, 3), "desc": causa}
        if rng.random() < 0.35:  # un tercio de los cambios son graduales
            ev["rampa_s"] = round(float(rng.uniform(20, 60)), 1)
        eventos.append(ev)
        t += max(40.0, rng.exponential(intervalo_medio))
    return eventos


def obtener_escenario(nombre, semilla=None):
    """Devuelve la lista de eventos del escenario pedido ("demo", "sin_drift" o "aleatorio")."""
    if nombre == "aleatorio":
        return escenario_aleatorio(semilla)
    return [dict(e) for e in ESCENARIOS[nombre]]


class GestorDrift:
    """Aplica los cambios de parámetros (eventos de drift) a medida que avanza el tiempo.

    Guarda los eventos pendientes ordenados por instante. En cada muestra,
    aplicar() revisa si a alguno ya le tocó: si es un escalón cambia el
    parámetro de inmediato y si es una rampa lo va moviendo de a poco, en
    línea recta, hasta llegar al valor final.
    """

    def __init__(self, eventos, params_iniciales):
        self.pendientes = sorted((dict(e) for e in eventos), key=lambda e: e["t"])
        self.rampas = {}  # parámetro -> (t_inicio, t_fin, valor_inicial, valor_final) de su rampa en curso
        self.ref = dict(params_iniciales)  # valores iniciales, para acotar los cambios
        self.lim = config.LIMITES_DRIFT

    def inyectar(self, evento, t):
        """Agrega un cambio manual (llega por MQTT desde el dashboard o
        inyectar_falla.py). Empieza en el instante actual t."""
        ev = {"param": evento["param"], "factor": float(evento["factor"]),
              "desc": evento.get("desc", "Inyeccion manual"), "t": t}
        if evento.get("rampa_s"):
            ev["rampa_s"] = float(evento["rampa_s"])
        self.pendientes.insert(0, ev)

    def _acotar(self, nombre, valor):
        """Limita el valor a un rango físico razonable (config.LIMITES_DRIFT veces el valor inicial)."""
        return float(np.clip(valor, self.lim[0] * self.ref[nombre], self.lim[1] * self.ref[nombre]))

    def aplicar(self, t, params):
        """Aplica los cambios que correspondan al instante t.

        Modifica directamente el diccionario params de la planta y devuelve la
        lista de cambios que empezaron justo ahora. Esa lista se registra para
        evaluar al gemelo, pero nunca se le envía.
        """
        iniciados = []
        while self.pendientes and self.pendientes[0]["t"] <= t + 1e-9:
            ev = self.pendientes.pop(0)
            n = ev["param"]
            if n not in PARAM_NOMBRES:
                continue
            self.rampas.pop(n, None)  # un evento nuevo cancela una rampa en curso
            valor_ini = params[n]
            valor_fin = self._acotar(n, valor_ini * ev["factor"])
            rampa = float(ev.get("rampa_s", 0.0))
            if rampa > 0:
                self.rampas[n] = (t, t + rampa, valor_ini, valor_fin)
            else:
                params[n] = valor_fin
            iniciados.append({"param": n, "factor": ev["factor"], "desc": ev.get("desc", ""),
                              "t_inicio": t, "t_fin": t + rampa, "valor_inicial": valor_ini,
                              "valor_final": valor_fin, "tipo": "rampa" if rampa > 0 else "escalon"})
        # Avance de las rampas en curso: interpolación lineal entre el valor inicial y el final
        for n, (t0, t1, v0, v1) in list(self.rampas.items()):
            if t >= t1:
                params[n] = v1
                del self.rampas[n]
            else:
                params[n] = v0 + (v1 - v0) * (t - t0) / (t1 - t0)
        return iniciados


class GeneradorEntrada:
    """Genera el voltaje aplicado al motor: escalones de altura y duración aleatorias.

    Cambiar el voltaje seguido es importante. Para estimar parámetros como J
    o b, el motor tiene que pasar por transitorios (acelerar y frenar); si el
    voltaje quedara fijo, la velocidad se estabilizaría y esos parámetros
    dejarían de notarse en las mediciones. A esto se le llama excitación
    persistente.
    """

    def __init__(self, rng, v_min, v_max, t_min, t_max):
        self.rng = rng
        self.v_min, self.v_max, self.t_min, self.t_max = v_min, v_max, t_min, t_max
        self.v = 0.0
        self.t_cambio = 0.0

    def __call__(self, t):
        """Devuelve el voltaje vigente en t; si ya pasó la duración del escalón, sortea uno nuevo."""
        if t >= self.t_cambio:
            self.v = float(self.rng.uniform(self.v_min, self.v_max))
            self.t_cambio = t + float(self.rng.uniform(self.t_min, self.t_max))
        return self.v


class PlantaSimulada:
    """El motor DC "real": integra la física en tiempo continuo y mide con ruido.

    Cada llamada a paso() equivale a un periodo de muestreo Ts. Dentro de ese
    periodo la física se integra con RK4 en sub_pasos pasos pequeños, que es
    más fino que lo que usa el gemelo; así la planta no es una copia exacta del
    modelo del gemelo, igual que en la realidad.
    """

    def __init__(self, escenario="demo", semilla=None, ts=config.TS, sub_pasos=20):
        self.ts = ts
        self.sub_pasos = sub_pasos
        self.rng = np.random.default_rng(semilla)  # generador aleatorio propio: misma semilla, mismo experimento
        self.params = dict(config.PARAMS_PLANTA_INICIALES)  # parámetros reales, que irán cambiando
        self.eventos_escenario = obtener_escenario(escenario, semilla)
        self.gestor = GestorDrift(self.eventos_escenario, self.params)
        self.entrada = GeneradorEntrada(self.rng, **config.ENTRADA)
        self.sigma = np.array([config.RUIDO_MEDICION["i"], config.RUIDO_MEDICION["w"]])
        self.x = np.zeros(2)  # estado real [i, w]; el motor parte detenido
        self.k = 0            # número de muestra

    @property
    def t(self):
        """Tiempo simulado de la muestra actual [s]."""
        return self.k * self.ts

    def inyectar(self, evento):
        """Recibe una falla manual y la programa para el instante actual."""
        self.gestor.inyectar(evento, self.t)

    def paso(self):
        """Avanza un periodo de muestreo y devuelve (medicion, cambios).

        El orden reproduce lo que pasa en un sistema real: (1) se aplican los
        cambios de parámetros que tocan ahora, (2) se elige el voltaje, (3) los
        sensores miden el estado actual con ruido y (4) la física avanza hasta
        la muestra siguiente con ese voltaje constante.

        medicion es el diccionario que se publica por MQTT: {k, t, u, i, w},
        donde u es el voltaje que se aplica entre esta muestra y la siguiente.
        cambios es la lista de cambios de parámetros que empezaron en este
        instante; se registra para la evaluación y el gemelo nunca la recibe.
        """
        t = self.t
        cambios = self.gestor.aplicar(t, self.params)
        u = self.entrada(t)
        y = self.x + self.rng.normal(0.0, self.sigma)  # medición = estado real + ruido gaussiano del sensor
        medicion = {"k": self.k, "t": round(t, 6), "u": u, "i": float(y[0]), "w": float(y[1])}
        self.x_real = self.x.copy()  # estado sin ruido, solo para los registros de evaluación

        p = a_vector(self.params)
        dt = self.ts / self.sub_pasos
        for _ in range(self.sub_pasos):
            self.x = paso_rk4(self.x, u, p, dt)
        self.k += 1
        return medicion, cambios

    def verdad(self):
        """Parámetros reales vigentes en la última muestra (solo para dashboard y evaluación)."""
        k = max(self.k - 1, 0)
        return {"k": k, "t": round(k * self.ts, 6), "params": a_dict(a_vector(self.params)),
                "rampas_activas": sorted(self.gestor.rampas)}
