"""Guarda en disco todo lo que pasa en una corrida, para analizarlo después.

Se usan dos formatos de texto, ambos en UTF-8:
- CSV, una fila por muestra (20 por segundo): planta.csv y gemelo.csv.
- JSONL, un objeto JSON por línea, para eventos que ocurren de vez en cuando
  (cambios de la planta, detecciones, recalibraciones). Cada evento trae
  campos distintos, por eso no calzan bien en una tabla.

Cada archivo se escribe línea a línea (buffering=1), así que si un proceso se
cae a mitad de la corrida no se pierde lo registrado hasta ese momento.

En las corridas con MQTT cada fila lleva marcas de tiempo de reloj real (ts,
segundos desde 1970, el "epoch" de Unix): la planta anota cuándo publicó la
muestra y el gemelo cuándo la recibió y terminó de procesarla. Con eso se
miden la latencia y la pérdida de mensajes. En la simulación offline esas
columnas quedan vacías.
"""
import csv
import datetime
import json
import math
import os

from .modelo import PARAM_NOMBRES

COLUMNAS_PLANTA = ["k", "t", "ts", "u", "i_med", "w_med", "i_real", "w_real", *PARAM_NOMBRES]
COLUMNAS_GEMELO = ["k", "t", "ts_planta", "lat_red_ms", "lat_total_ms",
                   "i_hat", "w_hat", "q", "DI", "cusum", "modo", *PARAM_NOMBRES]


def fecha_hora(ts=None):
    """Fecha y hora local legible con milisegundos, p. ej. 2026-11-04 20:15:03.127."""
    t = datetime.datetime.now() if ts is None else datetime.datetime.fromtimestamp(ts)
    return t.isoformat(sep=" ", timespec="milliseconds")


def sin_nan(obj):
    """Reemplaza NaN e infinito por None, recorriendo diccionarios y listas.

    El JSON estándar no admite NaN; json.dumps de Python lo escribe igual,
    pero otros programas (un dashboard web, Node-RED) fallarían al leerlo.
    None se convierte en null, que sí es válido.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: sin_nan(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sin_nan(v) for v in obj]
    return obj


class RegistroCSV:
    """Archivo CSV con columnas fijas; las columnas que falten en una fila quedan vacías."""

    def __init__(self, ruta, columnas):
        os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
        self.columnas = columnas
        self._f = open(ruta, "w", newline="", encoding="utf-8", buffering=1)
        self._w = csv.writer(self._f)
        self._w.writerow(columnas)

    def escribir(self, fila):
        self._w.writerow([fila.get(c, "") for c in self.columnas])

    def cerrar(self):
        self._f.close()


class RegistroJSONL:
    """Archivo JSONL: cada llamada a escribir() agrega un objeto JSON en una línea nueva."""

    def __init__(self, ruta):
        os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
        self._f = open(ruta, "w", encoding="utf-8", buffering=1)

    def escribir(self, obj):
        self._f.write(json.dumps(sin_nan(obj), ensure_ascii=False) + "\n")

    def cerrar(self):
        self._f.close()


class RegistroPlanta:
    """Registros de la planta: planta.csv y planta_eventos.jsonl.

    planta.csv guarda, por muestra, lo medido (con ruido), el estado real (sin
    ruido) y los parámetros reales vigentes. planta_eventos.jsonl guarda cada
    cambio de parámetros. Ninguno de los dos llega al gemelo; sirven para
    evaluarlo después (por ejemplo, para calcular el retardo de detección).
    """

    def __init__(self, carpeta):
        self.csv = RegistroCSV(os.path.join(carpeta, "planta.csv"), COLUMNAS_PLANTA)
        self.eventos = RegistroJSONL(os.path.join(carpeta, "planta_eventos.jsonl"))

    def muestra(self, med, x_real, params):
        self.csv.escribir({"k": med["k"], "t": med["t"], "ts": med.get("ts", ""), "u": med["u"], "i_med": med["i"],
                           "w_med": med["w"], "i_real": x_real[0], "w_real": x_real[1], **params})

    def cerrar(self):
        self.csv.cerrar()
        self.eventos.cerrar()


class RegistroGemelo:
    """Registros del gemelo: gemelo.csv y gemelo_eventos.jsonl.

    gemelo.csv guarda, por muestra, la predicción, el DI, el CUSUM, el modo,
    los parámetros que cree el gemelo y las latencias. gemelo_eventos.jsonl
    guarda las detecciones, recalibraciones y mensajes perdidos o rechazados.
    """

    def __init__(self, carpeta):
        self.csv = RegistroCSV(os.path.join(carpeta, "gemelo.csv"), COLUMNAS_GEMELO)
        self.eventos = RegistroJSONL(os.path.join(carpeta, "gemelo_eventos.jsonl"))

    def muestra(self, salida, params, tiempos=None):
        self.csv.escribir({**salida, **params, **(tiempos or {})})

    def cerrar(self):
        self.csv.cerrar()
        self.eventos.cerrar()
