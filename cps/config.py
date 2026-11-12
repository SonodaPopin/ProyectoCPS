"""Configuración central del proyecto.

Todos los procesos (planta, gemelo, dashboard, simulación offline) leen sus
constantes de aquí, así que cualquier ajuste del experimento se hace en un
solo lugar y no hay valores repetidos entre archivos.
"""

# --------------------------------------------------------------------------
# Comunicación MQTT
# --------------------------------------------------------------------------
# En MQTT los procesos no se hablan directamente: cada uno se conecta a un
# servidor central (el broker) y publica mensajes en "tópicos", que son
# nombres con forma de ruta. Quien se suscribe a un tópico recibe todo lo que
# se publique en él. Así la planta no necesita saber quién la escucha.
BROKER_HOST = "localhost"
BROKER_PORT = 1883      # puerto estándar de MQTT sin cifrar
PREFIJO = "cps/motor1"  # raíz de todos los tópicos; cambiarla si se usa un broker público compartido


def topicos(prefijo=PREFIJO):
    """Devuelve el nombre completo de cada tópico del sistema.

    Los tópicos de "conexion" usan un mensaje retenido (el broker guarda el
    último y se lo entrega a cada nuevo suscriptor) y el Last Will, un
    mensaje que el broker publica solo si un cliente se desconecta sin avisar.
    """
    return {
        # Planta -> mundo
        "medicion": f"{prefijo}/planta/medicion",          # {k, t, u, i, w}  (20 Hz)
        "verdad": f"{prefijo}/planta/verdad",              # parámetros reales: SOLO dashboard/evaluación
        "planta_conexion": f"{prefijo}/planta/conexion",   # online/offline (LWT, retenido)
        # Mundo -> planta
        "inyeccion": f"{prefijo}/planta/inyeccion",        # inyección manual de fallas (demo)
        # Gemelo -> mundo
        "gemelo_estado": f"{prefijo}/gemelo/estado",       # {k, t, i_hat, w_hat, DI, cusum, modo}
        "gemelo_parametros": f"{prefijo}/gemelo/parametros",  # parámetros estimados (retenido)
        "gemelo_eventos": f"{prefijo}/gemelo/eventos",     # detecciones / recalibraciones
        "gemelo_conexion": f"{prefijo}/gemelo/conexion",   # online/offline (LWT, retenido)
    }


# --------------------------------------------------------------------------
# Tiempo
# --------------------------------------------------------------------------
# Periodo de muestreo: cada cuánto la planta mide y publica [s]. Con 50 ms
# (20 Hz) hay unas 7 muestras por tiempo de subida del motor, suficiente para
# seguir su dinámica sin saturar la red (ver Sección 3.1 del informe).
TS = 0.05

# --------------------------------------------------------------------------
# Motor DC  (x = [i, w])
#   L di/dt = V - R i - K w
#   J dw/dt = K i - b w
# --------------------------------------------------------------------------
# Datos de placa (lo que diría el fabricante). Es lo único que el gemelo
# conoce al arrancar.
PARAMS_NOMINALES = {"R": 1.0, "L": 0.2, "K": 0.5, "J": 0.05, "b": 0.05}

# Valores reales de la planta al inicio. Difieren un poco de los de placa,
# como pasa con las tolerancias de fabricación. El gemelo no los conoce y debe
# estimarlos en su calibración inicial.
PARAMS_PLANTA_INICIALES = {"R": 1.08, "L": 0.21, "K": 0.48, "J": 0.056, "b": 0.046}

# Ruido de los sensores: desviación estándar del error gaussiano de cada medición
RUIDO_MEDICION = {"i": 0.05, "w": 0.10}  # [A], [rad/s]

# Voltaje que aplica la planta: escalones al azar entre v_min y v_max que duran
# entre t_min y t_max segundos. Que cambie seguido mantiene al motor en
# transitorios, lo que permite estimar todos sus parámetros.
ENTRADA = {"v_min": 0.0, "v_max": 12.0, "t_min": 1.0, "t_max": 3.0}

# Rango en que pueden moverse los parámetros de la planta, como múltiplos de
# su valor inicial (evita escenarios sin sentido físico)
LIMITES_DRIFT = (0.4, 3.0)

# --------------------------------------------------------------------------
# Gemelo digital: detección y recalibración
# --------------------------------------------------------------------------
GEMELO = {
    # Desviación estándar supuesta de los sensores antes de la calibración inicial
    "sigma_inicial": {"i": 0.05, "w": 0.10},
    # Índice de discrepancia (media móvil de residuos normalizados al cuadrado)
    "ventana_di": 40,         # muestras (2 s)
    "alfa": 1e-4,             # prob. de falsa alarma por muestra para fijar el umbral (chi-cuadrado)
    "persistencia": 10,       # muestras seguidas sobre el umbral para declarar drift
    # Subir persistencia o bajar alfa reduce las falsas alarmas, pero hace que
    # el gemelo tarde más en detectar un cambio real.
    # CUSUM sobre el residuo normalizado (cambios lentos + estimación del instante de cambio)
    "cusum_nu": 0.5,          # holgura
    "cusum_h": 40.0,          # umbral
    # Calibración / recalibración
    "n_calib_inicial": 400,   # muestras para la calibración inicial (20 s)
    "n_recal_min": 160,       # muestras post-cambio mínimas para recalibrar (8 s)
    "n_recal_max": 300,       # largo máximo de ventana de ajuste (15 s)
    "buffer": 1200,           # historial guardado (60 s)
    "excitacion_min": 0.5,    # desviación estándar mínima del voltaje en la ventana [V]
    "umbral_validacion": 1.5, # DI máximo aceptable del ajuste sobre su propia ventana
    "max_reintentos": 3,      # recalibraciones rechazadas antes de aceptar la mejor disponible
    "regularizacion": 0.5,    # peso del prior hacia los parámetros anteriores (en escala log)
    # Validación de plausibilidad de cada mensaje recibido (contramedida ante
    # mensajes falsos o corruptos): rango físico admisible de cada campo.
    "limites_medicion": {"u": (-1.0, 13.0), "i": (-100.0, 100.0), "w": (-300.0, 300.0)},
}

# --------------------------------------------------------------------------
# KPIs del proyecto (Tabla 2 del informe): (descripción, unidad, meta, sentido)
#   sentido "max": el valor medido debe ser <= meta;  "min": >= meta
# analisis.py y experimentos.py comparan lo medido contra estas metas.
# --------------------------------------------------------------------------
METAS_KPI = {
    "KPI1_tasa_deteccion": ("Tasa de deteccion de cambios", "%", 95.0, "min"),
    "KPI2_retardo_mediana": ("Retardo de deteccion (mediana)", "s", 2.0, "max"),
    "KPI2_retardo_p90": ("Retardo de deteccion (p90)", "s", 10.0, "max"),
    "KPI3_falsas_alarmas": ("Tasa de falsas alarmas", "1/h", 1.0, "max"),
    "KPI4_error_mediana": ("Error de recalibracion (mediana)", "%", 2.0, "max"),
    "KPI4_error_p90": ("Error de recalibracion (p90)", "%", 5.0, "max"),
    "KPI5_recuperacion_mediana": ("Tiempo de recuperacion (mediana)", "s", 12.0, "max"),
    "KPI6_latencia_p99": ("Latencia planta->gemelo (p99)", "ms", 25.0, "max"),
    "KPI7_perdida": ("Perdida de mensajes", "%", 0.1, "max"),
}
