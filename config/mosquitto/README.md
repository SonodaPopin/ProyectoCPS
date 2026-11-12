# Broker seguro (Mosquitto)

Amenaza que se mitiga: con un broker anónimo, cualquier cliente de la red puede publicar en
`cps/motor1/planta/medicion` (mediciones falsas que hacen que el gemelo se recalibre mal o que
oculte una falla real) o en `cps/motor1/planta/inyeccion` (alterar la planta).

Contramedidas:

1. **Autenticación**: `allow_anonymous false` + un usuario por proceso.
2. **Autorización (ACL)**: `acl.conf` da a cada usuario sólo los tópicos que necesita.
3. **TLS** (puerto 8883) cuando los procesos corren en PCs distintos.
4. **Validación en el gemelo** (ya implementada en `cps/gemelo_core.py`): esquema, rango físico y
   coherencia `t = k·Ts` de cada mensaje; los mensajes inválidos se descartan y se registran.

## Puesta en marcha

```bash
mosquitto_passwd -c config/mosquitto/passwd planta
mosquitto_passwd config/mosquitto/passwd gemelo
mosquitto_passwd config/mosquitto/passwd operador
mosquitto -c config/mosquitto/mosquitto.conf -v
```

Cada proceso toma sus credenciales de variables de entorno (así la clave no queda en el historial):

```powershell
$env:MQTT_USUARIO = "planta";  $env:MQTT_CLAVE = "<clave>";  python planta.py
$env:MQTT_USUARIO = "gemelo";  $env:MQTT_CLAVE = "<clave>";  python gemelo.py
$env:MQTT_USUARIO = "operador"; $env:MQTT_CLAVE = "<clave>"; python dashboard.py
```

Con TLS se agrega `--port 8883 --tls-ca config/mosquitto/certs/ca.crt` (o la variable `MQTT_TLS_CA`).
Los certificados se generan con OpenSSL (CA propia + certificado del servidor cuyo CN sea el
nombre o IP del broker). Ni el archivo `passwd` ni las claves privadas se suben al repositorio
(ver `.gitignore`).

Prueba prevista antes de la presentación final (actividad A9): un cliente sin credenciales y otro con credenciales de
`operador` intentan publicar en `planta/medicion`; ambos deben ser rechazados por el broker y el
gemelo no debe registrar mensajes ajenos.
