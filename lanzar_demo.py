"""Lanza la demo completa: (broker) + planta + gemelo + dashboard, cada uno
como un proceso independiente (en Windows, cada uno en su propia consola).

Es solo un atajo: ejecuta los mismos scripts que se pueden abrir a mano en
terminales separadas, con subprocess.Popen. El gemelo se inicia antes que la
planta para que ya esté suscrito cuando llegue la primera medición.

    python lanzar_demo.py                         # usa Mosquitto ya corriendo en localhost
    python lanzar_demo.py --broker-mini           # levanta además broker_mini.py
    python lanzar_demo.py --velocidad 4 --escenario aleatorio

Cerrar la ventana del dashboard o presionar Ctrl+C aquí detiene todo.
"""
import argparse
import os
import socket
import subprocess
import sys
import time

AQUI = os.path.dirname(os.path.abspath(__file__))


def broker_disponible(host, port):
    """Intenta abrir una conexión TCP al broker: si responde, hay un broker escuchando."""
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--broker-mini", action="store_true", help="iniciar broker_mini.py")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--escenario", default="demo", choices=["demo", "aleatorio", "sin_drift"])
    ap.add_argument("--semilla", type=int, default=None)
    ap.add_argument("--velocidad", type=float, default=2.0)
    ap.add_argument("--misma-consola", action="store_true", help="no abrir una consola por proceso")
    args = ap.parse_args()

    flags = 0
    if os.name == "nt" and not args.misma_consola:
        flags = subprocess.CREATE_NEW_CONSOLE
    red = ["--host", args.host, "--port", str(args.port)]

    def lanzar(*cmd):
        return subprocess.Popen([sys.executable, *cmd], cwd=AQUI, creationflags=flags)

    procesos = []
    try:
        if args.broker_mini:
            procesos.append(lanzar("broker_mini.py", "--host", args.host, "--port", str(args.port)))
            time.sleep(1.0)
        if not broker_disponible(args.host, args.port):
            sys.exit(f"No hay broker MQTT en {args.host}:{args.port}. Inicie Mosquitto o use --broker-mini.")

        procesos.append(lanzar("gemelo.py", *red))
        time.sleep(1.0)  # que el gemelo esté suscrito antes de que la planta publique
        planta = ["planta.py", *red, "--escenario", args.escenario, "--velocidad", str(args.velocidad)]
        if args.semilla is not None:
            planta += ["--semilla", str(args.semilla)]
        procesos.append(lanzar(*planta))
        dashboard = lanzar("dashboard.py", *red)
        procesos.append(dashboard)
        print("Demo en marcha. Cierre el dashboard o presione Ctrl+C para terminar.")
        dashboard.wait()
        print("Para analizar la corrida:  python analisis.py logs")
    except KeyboardInterrupt:
        pass
    finally:
        for p in reversed(procesos):
            if p.poll() is None:
                p.terminate()


if __name__ == "__main__":
    main()
