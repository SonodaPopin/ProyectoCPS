"""Paquete cps: la lógica del gemelo digital, separada de los programas que se ejecutan.

    config.py       constantes del experimento (tópicos, parámetros, umbrales, metas de KPI)
    modelo.py       ecuaciones del motor DC y su simulación
    planta_sim.py   la planta "real": física, ruido y cambios de parámetros sin aviso
    gemelo_core.py  el gemelo: índice de discrepancia, detección y recalibración
    mqtt_util.py    conexión al broker MQTT, común a todos los procesos
    registro.py     archivos CSV y JSONL con los datos de cada corrida

Los scripts de la raíz del proyecto (planta.py, gemelo.py, dashboard.py, ...)
importan este paquete y solo se encargan de la comunicación y la consola.
"""
