"""Núcleo del gemelo digital: detecta cuándo dejó de parecerse a la planta y se recalibra.

Idea general. El gemelo recibe en cada muestra lo que midió la planta (i, w)
y el voltaje que se aplicó. Con su propio modelo predice qué debería haber
medido y compara ambas cosas. La diferencia se llama residuo. Si el modelo
está bien calibrado, el residuo es puro ruido de sensor; si la planta cambió
(se acopló una carga, se calentó el bobinado), el residuo crece de forma
sostenida. Para decidir si creció "de verdad" o fue mala suerte del ruido se
usan dos detectores estadísticos:

- IndiceDiscrepancia (DI): promedio de los residuos al cuadrado en una ventana
  de 2 s, normalizado para valer 1 cuando todo está bien.
- Cusum: suma acumulada que detecta aumentos pequeños pero persistentes y
  además estima en qué momento empezó el cambio.

Cuando alguno da la alarma, el gemelo junta unos segundos de datos posteriores
al cambio y vuelve a estimar los cinco parámetros del motor con mínimos
cuadrados (ajustar_parametros). Si el ajuste es bueno, adopta los parámetros
nuevos y sigue vigilando.

Todo esto se organiza como una máquina de estados (atributo modo):

    SIN_DATOS --1er dato--> CALIBRACION_INICIAL --ajuste--> MONITOREO
                                                              |   ^
                                           drift detectado    v   | ajuste validado
                                                           RECALIBRANDO

Este módulo no depende de MQTT. gemelo.py le pasa los mensajes que llegan
del broker y simulacion_offline.py le pasa las mediciones directamente, así
que la misma lógica se prueba con y sin red.
"""
import math
import time
from collections import deque

import numpy as np
from scipy.optimize import least_squares
from scipy.stats import chi2

from .modelo import PARAM_NOMBRES, a_dict, a_vector, discretizar, simular


class IndiceDiscrepancia:
    """Índice de discrepancia normalizado (DI): el "termómetro" del gemelo.

        q_k  = (1/m) * sum_j (r_jk / sigma_j)^2        residuo normalizado al cuadrado
        DI_k = (1/W) * sum_{n=k-W+1..k} q_n            media móvil de las últimas W muestras

    r_jk es el residuo del canal j (corriente o velocidad) en la muestra k, y
    sigma_j la desviación estándar del ruido de ese sensor. Dividir por sigma
    deja ambos canales en la misma escala aunque midan cosas distintas.

    Si el gemelo representa bien a la planta, cada r/sigma es ruido gaussiano
    estándar, así que m*W*DI sigue una distribución chi-cuadrado con m*W
    grados de libertad y DI vale 1 en promedio. Eso permite fijar el umbral
    con una probabilidad de falsa alarma elegida (alfa) en vez de a ojo: es el
    valor que DI solo supera por azar con probabilidad alfa.
    """

    def __init__(self, ventana, m=2, alfa=1e-4):
        self.ventana = ventana
        self.umbral = float(chi2.ppf(1.0 - alfa, m * ventana) / (m * ventana))
        self._q = deque(maxlen=ventana)  # cola de largo fijo: al llenarse, descarta el valor más antiguo

    def reiniciar(self):
        """Vacía la ventana (se usa al cambiar de modelo, para no mezclar residuos viejos)."""
        self._q.clear()

    def actualizar(self, q):
        """Agrega el residuo normalizado de la muestra nueva y devuelve el DI actualizado."""
        self._q.append(q)
        return self.valor

    @property
    def valor(self):
        """DI actual (promedio de la ventana); NaN si todavía no hay datos."""
        return sum(self._q) / len(self._q) if self._q else math.nan

    @property
    def lleno(self):
        """True cuando la ventana ya tiene W muestras; antes de eso el DI es poco confiable."""
        return len(self._q) == self.ventana


class Cusum:
    """Detector CUSUM (suma acumulada) sobre el residuo normalizado q.

        g_k = max(0, g_{k-1} + q_k - (1 + nu))      alarma si g_k > h

    Mientras el modelo está bien, q vale 1 en promedio y cada muestra le resta
    nu a g, que se mantiene pegado a cero. Si la planta cambia, q queda sobre
    1 + nu y g empieza a crecer muestra tras muestra hasta pasar el umbral h.
    Así detecta discrepancias pequeñas pero persistentes, como un drift
    gradual, que el DI tardaría en notar.

    También estima cuándo empezó el cambio: la muestra siguiente a la última
    vez que g valió cero. Ese instante marca desde dónde tomar datos para
    recalibrar.
    """

    def __init__(self, nu, h):
        self.nu, self.h = nu, h
        self.g = 0.0
        self.k_cero = 0  # última muestra en que g valió cero

    def reiniciar(self, k):
        """Vuelve g a cero (después de recalibrar el modelo)."""
        self.g = 0.0
        self.k_cero = k

    def actualizar(self, q, k):
        """Incorpora el residuo q de la muestra k y devuelve el nuevo valor de g."""
        self.g = max(0.0, self.g + q - 1.0 - self.nu)
        if self.g == 0.0:
            self.k_cero = k
        return self.g

    @property
    def alarma(self):
        """True si la suma acumulada superó el umbral h."""
        return self.g > self.h

    @property
    def k_cambio(self):
        """Muestra en que probablemente empezó el cambio."""
        return self.k_cero + 1


def ajustar_parametros(u, Y, p_inicial, sigma, ts, p_previo=None, regularizacion=0.0):
    """Recalibración: estima los parámetros del motor a partir de datos medidos.

    Busca los parámetros que, al simular el modelo con los mismos voltajes u
    que recibió la planta, reproducen mejor las mediciones Y. "Mejor" significa
    la menor suma de errores al cuadrado (mínimos cuadrados):

        min_z  sum_n sum_j ((Y_nj - X_nj(z)) / sigma_j)^2  +  lambda^2 * ||ln p - ln p_previo||^2

    Las incógnitas son z = [ln R, ln L, ln K, ln J, ln b, x0/sigma]. Además de
    los cinco parámetros se estima el estado inicial x0 de la ventana, porque
    la medición del primer punto también trae ruido.

    Algunos detalles de diseño:
    - Se trabaja con el logaritmo de cada parámetro: así el optimizador nunca
      prueba valores negativos (sin sentido físico y que harían inestable el
      modelo) y un cambio de 0,1 en ln R significa lo mismo (un 10 %) que en ln J.
    - El término con lambda (regularizacion) castiga alejarse de los
      parámetros anteriores. Es pequeño, pero evita que un parámetro que los
      datos casi no "ven" se vaya a cualquier valor.
    - scipy.optimize.least_squares con el método de Levenberg-Marquardt ("lm")
      resuelve el problema. En cada iteración llama a residuos(z), que simula
      el modelo completo de la ventana.

    Devuelve un diccionario con los parámetros estimados p, el estado simulado
    al final de la ventana (para seguir desde ahí), los residuos, el DI del
    propio ajuste y la incertidumbre relativa de cada parámetro.
    """
    u = np.asarray(u, dtype=float)
    Y = np.asarray(Y, dtype=float)
    n_p = len(PARAM_NOMBRES)
    z0 = np.concatenate([np.log(p_inicial), Y[0] / sigma])  # punto de partida: parámetros actuales y primera medición
    lp_prev = np.log(p_previo) if p_previo is not None else None

    def residuos(z):
        """Vector de errores normalizados que least_squares intenta hacer lo más chico posible."""
        X = simular(np.exp(z[:n_p]), z[n_p:] * sigma, u, ts)
        r = ((Y - X) / sigma).ravel()
        if lp_prev is not None and regularizacion > 0:
            r = np.concatenate([r, regularizacion * (z[:n_p] - lp_prev)])
        return r

    sol = least_squares(residuos, z0, method="lm")
    p = np.exp(sol.x[:n_p])
    X = simular(p, sol.x[n_p:] * sigma, u, ts)
    E = Y - X
    # Incertidumbre de cada parámetro. Con residuos normalizados, la covarianza
    # de la estimación es aproximadamente (J^T J)^-1, donde J es el jacobiano
    # (sensibilidad de los residuos a cada incógnita). Como se estimó ln p, su
    # desviación estándar equivale a un error relativo: 0,01 significa ±1 %.
    try:
        cov = np.linalg.inv(sol.jac.T @ sol.jac)
        incertidumbre = np.sqrt(np.clip(np.diag(cov)[:n_p], 0.0, None))
    except np.linalg.LinAlgError:
        incertidumbre = np.full(n_p, np.nan)
    return {
        "p": p,
        "x_final": X[-1].copy(),
        "residuos": E,
        "di": float(np.mean((E / sigma) ** 2)),
        "incertidumbre_rel": incertidumbre,
        "nfev": int(sol.nfev),
    }


def validar_medicion(med, ts, limites):
    """Revisa que un mensaje recibido tenga sentido antes de usarlo.

    Es la primera contramedida de seguridad del gemelo: un mensaje falso o
    corrupto podría hacer que se recalibre con datos inventados. Se revisa que
    traiga todos los campos con números válidos (no NaN ni infinito), que cada
    valor esté dentro de su rango físico (por ejemplo, el voltaje entre -1 y
    13 V) y que el tiempo coincida con el número de muestra (t = k*Ts).
    Devuelve el motivo del rechazo como texto, o None si el mensaje es válido.
    """
    try:
        k, t = med["k"], float(med["t"])
        valores = {c: float(med[c]) for c in ("u", "i", "w")}
    except (KeyError, TypeError, ValueError):
        return "formato invalido"
    if not isinstance(k, int) or isinstance(k, bool) or k < 0:
        return "indice k invalido"
    if not all(math.isfinite(v) for v in (t, *valores.values())):
        return "valor no finito"
    for campo, (minimo, maximo) in limites.items():
        if not minimo <= valores[campo] <= maximo:
            return f"{campo} fuera de rango"
    if abs(t - k * ts) > ts / 2:
        return "t no coincide con k*Ts"
    return None


def describir_evento(ev):
    """Texto de una línea para mostrar un evento del gemelo en consola."""
    t = f"t={ev['t']:7.2f}s"
    if ev["tipo"] == "drift_detectado":
        return (f"{t}  DRIFT DETECTADO por {ev['disparado_por']} (DI={ev['DI']:.2f}, umbral={ev['umbral_DI']:.2f}, "
                f"CUSUM={ev['cusum']:.1f}); cambio estimado en t={ev['t_cambio_estimado']:.2f}s")
    if ev["tipo"] in ("recalibrado", "calibracion_inicial"):
        ps = "  ".join(f"{k}={v:.4g}" for k, v in ev["params"].items())
        nombre = "CALIBRACION INICIAL" if ev["tipo"] == "calibracion_inicial" else "RECALIBRADO"
        return (f"{t}  {nombre}: {ps}  (DI ajuste={ev['DI_ajuste']:.2f}, {ev['n_muestras']} muestras, "
                f"{ev['duracion_ajuste_ms']:.0f} ms)")
    if ev["tipo"] == "recalibracion_rechazada":
        return f"{t}  recalibracion rechazada (DI ajuste={ev['DI_ajuste']:.2f}); reintento con datos mas recientes"
    if ev["tipo"] == "mensajes_perdidos":
        return f"{t}  se perdieron {ev['cantidad']} mensajes: estado resincronizado con la medicion"
    if ev["tipo"] == "mensaje_rechazado":
        return f"{t}  mensaje rechazado por validacion ({ev['motivo']})"
    return f"{t}  {ev['tipo']}"


class GemeloDigital:
    """El gemelo digital completo. Se usa llamando a procesar() con cada medición.

    Parte con los parámetros de placa (los que diría el fabricante), que no
    son exactamente los de la planta, y los corrige en la calibración inicial.
    Después vigila la discrepancia y se recalibra cada vez que detecta un drift.
    """

    def __init__(self, params_nominales, ts, cfg):
        self.ts = ts
        self.cfg = cfg  # parámetros del detector y de la recalibración (config.GEMELO)
        self.p = a_vector(params_nominales)  # parámetros que cree el gemelo [R, L, K, J, b]
        self.sigma = np.array([cfg["sigma_inicial"]["i"], cfg["sigma_inicial"]["w"]])  # ruido de los sensores
        self._sse = np.zeros(2)  # suma de residuos^2 de los ajustes validados (para estimar sigma)
        self._n_sse = 0.0
        self.di = IndiceDiscrepancia(cfg["ventana_di"], m=2, alfa=cfg["alfa"])
        self.cusum = Cusum(cfg["cusum_nu"], cfg["cusum_h"])
        self.buffer = deque(maxlen=cfg["buffer"])  # últimas mediciones (k, t, u, i, w) para recalibrar
        self.modo = "SIN_DATOS"
        self.x = np.zeros(2)      # estado que predice el gemelo [i, w]
        self.k_prev = None        # índice de la última muestra procesada
        self.u_prev = 0.0         # voltaje aplicado desde la última muestra
        self.q = math.nan         # último residuo normalizado
        self.k_inicio_ventana = 0  # desde qué muestra se toman datos para (re)calibrar
        self.persistencia = 0     # muestras seguidas con DI sobre el umbral
        self.reintentos = 0
        self.n_recalibraciones = 0
        self.mensajes_perdidos = 0
        self.mensajes_rechazados = 0
        self._actualizar_modelo()

    # ------------------------------------------------------------------ API
    def procesar(self, med):
        """Procesa una medición {k, t, u, i, w} de la planta. Es lo único que se
        llama desde afuera, una vez por muestra.

        Pasos: (1) validar el mensaje; (2) revisar que el número de muestra k
        sea el esperado (detecta mensajes perdidos, duplicados o un reinicio de
        la planta); (3) predecir con el modelo y calcular el residuo; (4) hacer
        lo que corresponda según el modo (calibrar, vigilar o recalibrar).

        Devuelve (salida, eventos). salida es el estado del gemelo para
        publicar (o None si el mensaje se descartó) y eventos, la lista de
        cosas que ocurrieron (detección de drift, recalibración, ...).
        """
        motivo = validar_medicion(med, self.ts, self.cfg["limites_medicion"])
        if motivo:
            self.mensajes_rechazados += 1
            k_ref = self.k_prev or 0
            return None, [self._evento("mensaje_rechazado", k_ref, k_ref * self.ts, motivo=motivo)]

        k, t, u = int(med["k"]), float(med["t"]), float(med["u"])
        y = np.array([float(med["i"]), float(med["w"])])
        eventos = []
        q = math.nan

        # Paso 2: ¿llegó la muestra que esperábamos?
        if self.k_prev is not None and k == self.k_prev:
            return None, eventos  # mensaje duplicado: se ignora
        if self.k_prev is None or k < self.k_prev:
            # Primera muestra, o k retrocedió porque la planta se reinició
            if self.k_prev is not None:
                eventos.append(self._evento("reinicio_planta", k, t))
            self._resincronizar(y, k)
            if self.modo == "SIN_DATOS":
                self.modo = "CALIBRACION_INICIAL"
        elif k > self.k_prev + 1:
            # Se saltó algún k: se perdieron mensajes y no sabemos qué voltaje
            # se aplicó entremedio, así que el gemelo copia el estado medido
            perdidos = k - self.k_prev - 1
            self.mensajes_perdidos += perdidos
            eventos.append(self._evento("mensajes_perdidos", k, t, cantidad=perdidos))
            self._resincronizar(y, k)
        else:
            # Paso 3: caso normal. El gemelo avanza su modelo una muestra con el
            # voltaje aplicado en [t_k-1, t_k) y compara con lo medido. Nunca se
            # corrige con la medición (lazo abierto), para que una diferencia de
            # parámetros se vaya acumulando en el residuo y se note.
            self.x = self._phi @ self.x + self._gam * self.u_prev
            r = (y - self.x) / self.sigma
            q = 0.5 * float(r @ r)

        self.buffer.append((k, t, u, y[0], y[1]))
        self.k_prev, self.u_prev, self.q = k, u, q

        # Paso 4: según el modo de la máquina de estados
        if self.modo == "CALIBRACION_INICIAL":
            if self._n_ventana(k) >= self.cfg["n_calib_inicial"]:
                eventos += self._calibrar(k, t, inicial=True)
        elif self.modo == "MONITOREO":
            if not math.isnan(q):
                eventos += self._monitorear(q, k, t)
        elif self.modo == "RECALIBRANDO":
            if not math.isnan(q):
                self.di.actualizar(q)  # sólo para visualizar
            if self._n_ventana(k) >= self.cfg["n_recal_min"]:
                eventos += self._calibrar(k, t, inicial=False)

        return self.salida(k, t), eventos

    def salida(self, k, t):
        """Estado del gemelo en la muestra k, tal como se publica en gemelo/estado."""
        return {"k": k, "t": t, "i_hat": float(self.x[0]), "w_hat": float(self.x[1]),
                "q": self.q, "DI": self.di.valor, "umbral_DI": self.di.umbral,
                "cusum": self.cusum.g, "h_cusum": self.cusum.h, "modo": self.modo}

    def parametros(self):
        """Parámetros que cree el gemelo y ruido estimado, tal como se publican en gemelo/parametros."""
        return {"params": a_dict(self.p), "sigma": {"i": float(self.sigma[0]), "w": float(self.sigma[1])},
                "n_recalibraciones": self.n_recalibraciones, "modo": self.modo}

    # ------------------------------------------------------------ internos
    def _actualizar_modelo(self):
        """Recalcula el modelo discreto (Phi, Gam) cada vez que cambian los parámetros."""
        self._phi, self._gam = discretizar(self.p, self.ts)

    def _resincronizar(self, y, k):
        """Copia el estado medido al gemelo y reinicia detectores y ventana.

        Se usa al partir y cuando se pierde la continuidad de los datos
        (mensajes perdidos o reinicio de la planta).
        """
        self.x = y.copy()
        self.buffer.clear()
        self.di.reiniciar()
        self.cusum.reiniciar(k)
        self.persistencia = 0
        self.k_inicio_ventana = k

    def _n_ventana(self, k):
        """Cuántas muestras hay disponibles para (re)calibrar desde k_inicio_ventana hasta k."""
        inicio = max(self.k_inicio_ventana, self.buffer[0][0])
        return k - inicio + 1

    def _acumular_ruido(self, residuos):
        """Estimación combinada de sigma con los residuos de todos los ajustes validados.

        Con una sola ventana corta el error relativo de sigma^2 es ~sqrt(2/N)
        (10 % con N=200), lo que sesga el DI y produce falsas alarmas; al ir
        sumando ventanas la estimación converge al ruido real del sensor.
        """
        n_libres = len(residuos) - (len(PARAM_NOMBRES) + 2) / 2  # grados de libertad por canal
        self._sse += np.sum(residuos ** 2, axis=0)
        self._n_sse += n_libres
        self.sigma = np.maximum(np.sqrt(self._sse / self._n_sse), 1e-6)

    def _evento(self, tipo, k, t, **datos):
        """Arma el diccionario de un evento (se publica en gemelo/eventos y se registra)."""
        return {"tipo": tipo, "k": k, "t": t, **datos}

    def _monitorear(self, q, k, t):
        """Modo MONITOREO: actualiza los detectores y decide si hubo drift.

        Hay drift si el DI pasa el umbral durante varias muestras seguidas
        (la persistencia evita alarmas por un pico aislado de ruido) o si el
        CUSUM supera su límite. Al detectarlo pasa a RECALIBRANDO y fija desde
        qué muestra se tomarán datos: el instante de cambio que estimó el CUSUM.
        """
        di = self.di.actualizar(q)
        g = self.cusum.actualizar(q, k)
        if self.di.lleno and di > self.di.umbral:
            self.persistencia += 1
        else:
            self.persistencia = 0
        por_di = self.persistencia >= self.cfg["persistencia"]
        por_cusum = self.cusum.alarma
        if not (por_di or por_cusum):
            return []

        k0 = max(self.cusum.k_cambio, self.buffer[0][0])
        self.modo = "RECALIBRANDO"
        self.k_inicio_ventana = k0
        self.reintentos = 0
        disparo = "+".join(n for n, f in (("DI", por_di), ("CUSUM", por_cusum)) if f)
        return [self._evento("drift_detectado", k, t, DI=di, umbral_DI=self.di.umbral, cusum=g,
                             disparado_por=disparo, k_cambio_estimado=k0,
                             t_cambio_estimado=t - (k - k0) * self.ts)]

    def _calibrar(self, k, t, inicial):
        """Estima los parámetros con los datos de la ventana y decide si se aceptan.

        En la calibración inicial también se estima el ruido de los sensores
        (sigma) con los residuos del ajuste, y luego se ajusta otra vez con ese
        sigma. En una recalibración, el ajuste se acepta solo si su propio DI
        es bajo (el modelo nuevo explica bien los datos); si no, lo más
        probable es que la ventana mezcle datos de antes y después del cambio,
        y se reintenta un poco más tarde con datos más recientes.
        """
        filas = [f for f in self.buffer if f[0] >= self.k_inicio_ventana]
        n_max = self.cfg["n_calib_inicial"] if inicial else self.cfg["n_recal_max"]
        datos = np.array(filas[-n_max:])
        u, Y = datos[:, 2], datos[:, 3:5]
        if np.std(u) < self.cfg["excitacion_min"]:
            return []  # el voltaje casi no varió: los parámetros no se pueden estimar todavía, se espera

        t0 = time.perf_counter()
        if inicial:
            aj = ajustar_parametros(u, Y, self.p, self.sigma, self.ts)
            # El ruido real de los sensores se estima de los residuos del ajuste
            self._acumular_ruido(aj["residuos"])
            aj = ajustar_parametros(u, Y, aj["p"], self.sigma, self.ts)
            aceptado = True
        else:
            aj = ajustar_parametros(u, Y, self.p, self.sigma, self.ts, p_previo=self.p,
                                    regularizacion=self.cfg["regularizacion"])
            aceptado = (aj["di"] <= self.cfg["umbral_validacion"]
                        or self.reintentos >= self.cfg["max_reintentos"])
            if aj["di"] <= self.cfg["umbral_validacion"]:
                self._acumular_ruido(aj["residuos"])
        info = {"DI_ajuste": aj["di"], "n_muestras": len(u),
                "t_ventana": [float(datos[0, 1]), float(datos[-1, 1])],
                "duracion_ajuste_ms": 1000 * (time.perf_counter() - t0),
                "evaluaciones": aj["nfev"]}

        if not aceptado:
            # Probablemente la ventana mezcla datos de antes y después del cambio
            # (o el cambio sigue en curso): reintentar con datos más recientes.
            self.reintentos += 1
            espera = self.cfg["n_recal_min"] // 2
            self.k_inicio_ventana = k + espera - self.cfg["n_recal_min"] + 1
            return [self._evento("recalibracion_rechazada", k, t, reintento=self.reintentos, **info)]

        # Ajuste aceptado: el gemelo adopta los parámetros nuevos, parte desde el
        # estado que estimó el ajuste y reinicia sus detectores
        p_anterior = self.p
        self.p = aj["p"]
        self._actualizar_modelo()
        self.x = aj["x_final"]
        self.di.reiniciar()
        self.cusum.reiniciar(k)
        self.persistencia = 0
        self.modo = "MONITOREO"
        if not inicial:
            self.n_recalibraciones += 1
        tipo = "calibracion_inicial" if inicial else "recalibrado"
        return [self._evento(tipo, k, t, params=a_dict(self.p), params_anteriores=a_dict(p_anterior),
                             incertidumbre_rel=a_dict(aj["incertidumbre_rel"]),
                             sigma={"i": float(self.sigma[0]), "w": float(self.sigma[1])},
                             validado=aj["di"] <= self.cfg["umbral_validacion"],
                             reintentos=self.reintentos, n_recalibraciones=self.n_recalibraciones,
                             **info)]
