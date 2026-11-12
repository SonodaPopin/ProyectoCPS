"""Inyecta un cambio de parámetros en la planta desde la línea de comandos.

Publica un único mensaje en planta/inyeccion y termina. La planta lo recibe y
cambia el parámetro indicado (multiplicándolo por el factor), igual que con
los botones del dashboard. El gemelo no recibe este mensaje: tiene que darse
cuenta del cambio por sí mismo.

    python inyectar_falla.py J 1.5               # se acopla una carga (escalón)
    python inyectar_falla.py R 1.4 --rampa 30    # calentamiento gradual en 30 s

Equivale a:  mosquitto_pub -t cps/motor1/planta/inyeccion -m '{"param":"J","factor":1.5}'
"""
from cps import config
from cps.modelo import PARAM_NOMBRES
from cps.mqtt_util import a_json, conectar, log, parser


def main():
    ap = parser(__doc__)
    ap.add_argument("param", choices=PARAM_NOMBRES)
    ap.add_argument("factor", type=float, help="multiplicador sobre el valor actual")
    ap.add_argument("--rampa", type=float, default=0.0, help="duración de la rampa [s] (0 = escalón)")
    args = ap.parse_args()

    falla = {"param": args.param, "factor": args.factor, "desc": "Inyeccion manual (CLI)"}
    if args.rampa > 0:
        falla["rampa_s"] = args.rampa
    cliente = conectar("inyector", args)
    cliente.publish(config.topicos(args.prefijo)["inyeccion"], a_json(falla), qos=1).wait_for_publish(timeout=5)
    log("inyector", f"enviado: {falla}")
    cliente.disconnect()
    cliente.loop_stop()


if __name__ == "__main__":
    main()
