"""Pruebas automáticas del sistema.   python -m unittest -v

Cada clase prueba una pieza por separado, de la más simple a la más completa:

    TestModelo         la discretización exacta coincide con la integración RK4 y con el régimen permanente
    TestDrift          los cambios de parámetros (escalón y rampa) se aplican en el momento y con el valor correctos
    TestDetectores     el umbral del DI es razonable y el CUSUM ubica bien el instante de un cambio
    TestRecalibracion  el ajuste por mínimos cuadrados recupera los parámetros reales
    TestGemelo         el gemelo completo: sin falsas alarmas sin drift, detecta un cambio y tolera mensajes perdidos
    TestValidacion     los mensajes falsos o mal formados se rechazan sin alterar al gemelo
    TestKPI            el cálculo de los KPI y de su cumplimiento
    TestBroker         el broker de respaldo enruta mensajes, retenidos y Last Will con un cliente MQTT real
"""
import asyncio
import json
import os
import sys
import threading
import time
import unittest

import numpy as np

# Permite importar cps y broker_mini aunque la prueba se ejecute desde otra carpeta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import broker_mini
from cps import config
from cps.gemelo_core import Cusum, GemeloDigital, IndiceDiscrepancia, ajustar_parametros, validar_medicion
from cps.modelo import a_vector, estado_estacionario, paso_rk4, simular
from cps.planta_sim import GestorDrift, PlantaSimulada


class TestModelo(unittest.TestCase):
    def test_discretizacion_exacta_coincide_con_rk4(self):
        p = a_vector(config.PARAMS_NOMINALES)
        u = np.r_[np.full(40, 12.0), np.full(40, 3.0)]
        X = simular(p, [0.0, 0.0], u, config.TS)
        x = np.zeros(2)
        for n in range(len(u) - 1):
            for _ in range(50):
                x = paso_rk4(x, u[n], p, config.TS / 50)
            np.testing.assert_allclose(X[n + 1], x, atol=1e-6)

    def test_estado_estacionario(self):
        p = a_vector(config.PARAMS_NOMINALES)
        X = simular(p, [0.0, 0.0], np.full(400, 10.0), config.TS)
        np.testing.assert_allclose(X[-1], estado_estacionario(p, 10.0), rtol=1e-4)


class TestDrift(unittest.TestCase):
    def test_escalon_y_rampa(self):
        params = dict(config.PARAMS_PLANTA_INICIALES)
        g = GestorDrift([{"t": 1.0, "param": "J", "factor": 2.0},
                         {"t": 2.0, "param": "R", "factor": 1.5, "rampa_s": 10.0}], params)
        g.aplicar(0.5, params)
        self.assertEqual(params["J"], config.PARAMS_PLANTA_INICIALES["J"])
        g.aplicar(1.0, params)
        self.assertAlmostEqual(params["J"], 2 * config.PARAMS_PLANTA_INICIALES["J"])
        g.aplicar(2.0, params)
        g.aplicar(7.0, params)  # mitad de la rampa
        self.assertAlmostEqual(params["R"], 1.25 * config.PARAMS_PLANTA_INICIALES["R"])
        g.aplicar(20.0, params)
        self.assertAlmostEqual(params["R"], 1.5 * config.PARAMS_PLANTA_INICIALES["R"])


class TestDetectores(unittest.TestCase):
    def test_umbral_chi2(self):
        di = IndiceDiscrepancia(40, m=2, alfa=1e-4)
        self.assertTrue(1.5 < di.umbral < 1.9)

    def test_cusum_estima_instante_de_cambio(self):
        rng = np.random.default_rng(0)
        c = Cusum(0.5, 40)
        k_alarma = None
        for k in range(600):
            q = rng.exponential(1.0) * (4.0 if k >= 300 else 1.0)  # la media de q sube en k=300
            c.actualizar(q, k)
            if c.alarma:
                k_alarma = k
                break
        self.assertIsNotNone(k_alarma)
        self.assertGreaterEqual(k_alarma, 300)
        self.assertLess(abs(c.k_cambio - 300), 10)


class TestRecalibracion(unittest.TestCase):
    def test_ajuste_recupera_parametros(self):
        planta = PlantaSimulada("sin_drift", semilla=3)
        meds = [planta.paso()[0] for _ in range(300)]
        u = np.array([m["u"] for m in meds])
        Y = np.array([[m["i"], m["w"]] for m in meds])
        sigma = np.array([config.RUIDO_MEDICION["i"], config.RUIDO_MEDICION["w"]])
        aj = ajustar_parametros(u, Y, a_vector(config.PARAMS_NOMINALES), sigma, config.TS)
        reales = a_vector(config.PARAMS_PLANTA_INICIALES)
        np.testing.assert_allclose(aj["p"], reales, rtol=0.05)
        self.assertLess(aj["di"], 1.3)


def correr_gemelo(escenario, duracion, semilla=1):
    planta = PlantaSimulada(escenario, semilla)
    gemelo = GemeloDigital(config.PARAMS_NOMINALES, config.TS, config.GEMELO)
    eventos = []
    for _ in range(int(duracion / config.TS)):
        med, _ = planta.paso()
        eventos += gemelo.procesar(med)[1]
    return planta, gemelo, eventos


class TestGemelo(unittest.TestCase):
    def test_sin_drift_no_hay_falsas_alarmas(self):
        _, gemelo, eventos = correr_gemelo("sin_drift", 600)
        self.assertEqual([e for e in eventos if e["tipo"] == "drift_detectado"], [])
        self.assertEqual(gemelo.modo, "MONITOREO")

    def test_detecta_y_recalibra_cambio_de_inercia(self):
        planta, gemelo, eventos = correr_gemelo("demo", 100)  # J x1.6 en t = 60 s
        det = [e for e in eventos if e["tipo"] == "drift_detectado"]
        rec = [e for e in eventos if e["tipo"] == "recalibrado"]
        self.assertEqual(len(det), 1)
        self.assertTrue(60.0 <= det[0]["t"] < 63.0)
        self.assertEqual(len(rec), 1)
        self.assertAlmostEqual(rec[0]["params"]["J"] / planta.params["J"], 1.0, delta=0.03)

    def test_mensajes_perdidos_resincronizan(self):
        planta = PlantaSimulada("sin_drift", 2)
        gemelo = GemeloDigital(config.PARAMS_NOMINALES, config.TS, config.GEMELO)
        for _ in range(500):
            med, _ = planta.paso()
            if med["k"] % 97 == 0:
                continue  # se pierde el mensaje
            _, eventos = gemelo.procesar(med)
            self.assertNotIn("drift_detectado", [e["tipo"] for e in eventos])
        self.assertGreater(gemelo.mensajes_perdidos, 0)


class TestValidacion(unittest.TestCase):
    def test_rechaza_mensajes_invalidos(self):
        lim = config.GEMELO["limites_medicion"]
        ok = {"k": 10, "t": 0.5, "u": 5.0, "i": 1.0, "w": 3.0}
        self.assertIsNone(validar_medicion(ok, config.TS, lim))
        casos = [({**ok, "w": 1e6}, "w fuera de rango"),
                 ({**ok, "t": 9.0}, "t no coincide con k*Ts"),
                 ({**ok, "i": float("nan")}, "valor no finito"),
                 ({**ok, "k": -1}, "indice k invalido"),
                 ({**ok, "k": "10"}, "indice k invalido"),
                 ({"k": 1}, "formato invalido"),
                 (None, "formato invalido")]
        for med, motivo in casos:
            self.assertEqual(validar_medicion(med, config.TS, lim), motivo)

    def test_gemelo_descarta_mensaje_falso_sin_alterar_su_estado(self):
        planta = PlantaSimulada("sin_drift", 4)
        gemelo = GemeloDigital(config.PARAMS_NOMINALES, config.TS, config.GEMELO)
        for _ in range(50):
            gemelo.procesar(planta.paso()[0])
        k_antes, x_antes = gemelo.k_prev, gemelo.x.copy()
        falso = {"k": k_antes + 1, "t": (k_antes + 1) * config.TS, "u": 5.0, "i": 0.0, "w": 9999.0}
        salida, eventos = gemelo.procesar(falso)
        self.assertIsNone(salida)
        self.assertEqual(eventos[0]["tipo"], "mensaje_rechazado")
        self.assertEqual(gemelo.k_prev, k_antes)
        np.testing.assert_array_equal(gemelo.x, x_antes)
        self.assertEqual(gemelo.mensajes_rechazados, 1)


class TestKPI(unittest.TestCase):
    def test_valores_y_cumplimiento(self):
        import analisis
        por_cambio = [
            {"detectado": True, "absorbido": False, "retardo_deteccion_s": 1.0,
             "error_param_cambiado_pct": 1.0, "tiempo_hasta_recalibrar_s": 9.0},
            {"detectado": False, "absorbido": True, "error_param_cambiado_pct": 3.0,
             "tiempo_hasta_recalibrar_s": 11.0},
        ]
        v = analisis.valores_kpi(por_cambio, horas=2.0, n_falsas=1)
        self.assertEqual(v["KPI1_tasa_deteccion"], 100.0)
        self.assertEqual(v["KPI3_falsas_alarmas"], 0.5)
        self.assertEqual(v["KPI4_error_mediana"], 2.0)
        self.assertIsNone(v["KPI6_latencia_p99"])
        cumple = {fila[0]: fila[-1] for fila in analisis.tabla_kpis(v)}
        self.assertTrue(cumple["KPI1_tasa_deteccion"])
        self.assertIsNone(cumple["KPI7_perdida"])


class TestBroker(unittest.TestCase):
    def test_comodines(self):
        c = broker_mini.coincide
        self.assertTrue(c("cps/+/medicion", "cps/motor1/medicion"))
        self.assertTrue(c("cps/#", "cps/motor1/planta/medicion"))
        self.assertTrue(c("cps/#", "cps"))
        self.assertFalse(c("cps/+", "cps/motor1/planta"))
        self.assertFalse(c("#", "$SYS/broker"))

    def test_publicar_suscribir_retenido_y_will(self):
        import paho.mqtt.client as mqtt

        puerto = 18830 + os.getpid() % 1000
        bucle = asyncio.new_event_loop()
        listo = threading.Event()

        def servir():
            asyncio.set_event_loop(bucle)
            servidor = bucle.run_until_complete(
                asyncio.start_server(broker_mini.Broker().atender, "127.0.0.1", puerto))
            listo.set()
            bucle.run_forever()
            servidor.close()
            bucle.run_until_complete(servidor.wait_closed())
            bucle.run_until_complete(bucle.shutdown_asyncgens())
            for tarea in asyncio.all_tasks(bucle):
                tarea.cancel()
            bucle.run_until_complete(asyncio.sleep(0.1))
            bucle.close()

        hilo = threading.Thread(target=servir, daemon=True)
        hilo.start()
        listo.wait(5)
        recibidos = []

        def nuevo(nombre):
            return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=nombre)

        pub = nuevo("pub")
        pub.will_set("t/estado", "offline", qos=1, retain=False)
        pub.connect("127.0.0.1", puerto)
        pub.loop_start()
        pub.publish("t/retenido", "hola", qos=1, retain=True).wait_for_publish(2)

        sub = nuevo("sub")
        sub.on_message = lambda c, u, m: recibidos.append((m.topic, m.payload.decode(), m.retain))
        sub.connect("127.0.0.1", puerto)
        sub.subscribe("t/#", qos=1)
        sub.loop_start()
        time.sleep(0.5)
        pub.publish("t/dato", json.dumps({"x": 1}), qos=0)
        time.sleep(0.3)
        pub.loop_stop()
        pub.socket().close()  # caída abrupta -> el broker publica el Last Will
        time.sleep(0.5)
        sub.disconnect()
        sub.loop_stop()
        bucle.call_soon_threadsafe(bucle.stop)
        hilo.join(5)

        self.assertIn(("t/retenido", "hola", True), recibidos)
        self.assertIn(("t/dato", '{"x": 1}', False), recibidos)
        self.assertIn(("t/estado", "offline", False), recibidos)


if __name__ == "__main__":
    unittest.main()
