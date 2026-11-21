# Suite de Pruebas de Carga con Locust (Snippet Platform)

Este directorio contiene la suite de pruebas de carga y estrés para la plataforma de snippets, diseñada para validar rendimiento, concurrencia, resiliencia y el comportamiento del monitoreo en **New Relic APM**.

El script simula tráfico concurrente realista contra el API Gateway (NGINX) y los microservicios, distribuyendo las operaciones según ponderaciones estadísticas representativas del uso del sistema.

---

## 1. Requisitos Previos

- Python 3.10 o superior
- Entorno virtual (recomendado)

### Instalación de Dependencias

```bash
cd snippet/snippet-infra/load-tests
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## 2. Configuración de Autenticación y Variables de Entorno

El script utiliza autenticación Machine-to-Machine (M2M) de Auth0 para interactuar con los endpoints protegidos. Lee automáticamente las credenciales desde el archivo `.env` ubicado en `snippet/snippet-infra/.env` o desde un `.env` local en esta misma carpeta:

```ini
AUTH0_ISSUER_URI=https://dev-o02i7bmya113pn5u.us.auth0.com/
AUTH0_AUDIENCE=https://snippet-searcher.ingsis/
AUTH0_M2M_CLIENT_ID=<tu-client-id>
AUTH0_M2M_CLIENT_SECRET=<tu-client-secret>
```

> **Nota:** La clase `Auth0TokenManager` implementa renovación automática y caché thread-safe del token OAuth2, previniendo saturación de llamadas al endpoint `/oauth/token` de Auth0 durante ejecuciones de alto volumen.

Cada petición saliente inyecta:
- `Authorization: Bearer <token>`
- `X-Correlation-Id: locust-<uuid>`: Facilita la correlación distribuida en NGINX, logs de Spring Boot y trazas de New Relic.

---

## 3. Escenarios y Distribución de Carga

Las tareas se ejecutan con las siguientes ponderaciones (`@task(weight)`):

| Tarea | Endpoint | Peso | Propósito |
|---|---|---|---|
| **A (Listado)** | `GET /api/snippets?page=0&size=10` | **10** | Consulta frecuente de snippets propios/visibles (alto volumen). |
| **B (Catálogo)** | `GET /api/permissions/rules/catalog/lint`<br>`GET /api/permissions/rules/catalog/format` | **5** | Lectura de reglas disponibles en el sistema. |
| **C (Lectura)** | `GET /api/snippets/file-types`<br>`GET /api/snippets/{id}/content` | **4** | Lectura de tipos soportados y contenido almacenado en Asset Service. |
| **D (Creación)** | `POST /api/snippets` | **3** | Creación de snippet y desencadenamiento de linteo asincrónico por Redis Streams. |
| **E (Formato)** | `POST /api/snippets/{id}/format` | **2** | Ejecución sincrónica de formateo de código contra Runner Service. |
| **F (Caos / 5xx)** | `GET /api/snippets/simulate-error?message=LocustStressTest` | **1** | Inyección de error 500 para disparar alertas de observabilidad. |

---

## 4. Modos de Ejecución

### Opción A: Interfaz Gráfica Web (Recomendado para exploración)

Inicia Locust y expone la consola interactiva en el puerto 8089:

```bash
# Contra ambiente de Desarrollo (Dev):
locust -f locustfile.py --host https://snippet-dev.duckdns.org

# Contra ambiente Local (Docker / Gateway):
locust -f locustfile.py --host http://localhost:80
```

1. Abrir en el navegador: [http://localhost:8089](http://localhost:8089)
2. Configurar:
   - **Number of users**: `20` (o la cantidad deseada)
   - **Ramp up**: `2` (usuarios agregados por segundo)
   - **Host**: Ya viene preseleccionado según la flag `--host`
3. Presionar **Start Swarming**.

### Opción B: Modo Headless (Automatizado / CI / CLI)

Ejecuta la prueba en la terminal durante un tiempo fijo sin interfaz web:

```bash
# 20 usuarios concurrentes, aceleración de 2 usuarios/segundo, durante 2 minutos
locust -f locustfile.py \
  --headless \
  -u 20 \
  -r 2 \
  --run-time 2m \
  --host https://snippet-dev.duckdns.org
```

Para guardar los resultados en archivos CSV:

```bash
locust -f locustfile.py \
  --headless \
  -u 30 \
  -r 3 \
  --run-time 3m \
  --host https://snippet-dev.duckdns.org \
  --csv=results_dev
```

---

## 5. Impacto Esperado en New Relic APM

Durante la ejecución de las pruebas, se observarán los siguientes impactos directos en el dashboard de New Relic:

1. **Throughput (RPS - Requests Per Second)**:
   - Salto sostenido en la tasa de peticiones por minuto (RPM) en `snippet-service`, `snippet-permission-service` y `snippet-runner-service`.
2. **Latencia y Percentiles (p95 / p99)**:
   - Aumento en la latencia promedio y percentil 95 debido al procesamiento de transacciones concurrentes (en particular en linteo y formateo de snippets).
3. **Distribución de Trazas Distribuidas (Distributed Tracing)**:
   - Cada transacción registrará el header `X-Correlation-Id: locust-...`, permitiendo seguir el recorrido completo de la solicitud desde el API Gateway NGINX hasta el microservicio correspondiente y llamadas a bases de datos PostgreSQL / Redis Streams.
4. **Error Rate y Alertas de Caos (HTTP 5xx)**:
   - La tarea F dispara intencionalmente errores 500 vía `/api/snippets/simulate-error`.
   - Esto eleva la tasa de error por encima del umbral configurado (ej. > 5%), verificando la activación automática de las políticas de alerta e incidentes en New Relic Alerts.
