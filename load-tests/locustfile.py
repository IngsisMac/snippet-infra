#!/usr/bin/env python3
"""
Locust Load Testing Suite for Snippet Searcher Architecture.

Simula carga concurrente de usuarios contra el API Gateway (NGINX) / Microservicios,
tanto en entorno local como en el ambiente de desarrollo (Dev).
Incorpora autenticación M2M OAuth2 con Auth0, inyección de X-Correlation-Id para
trazabilidad distribuida en New Relic APM y tareas con pesos según perfiles de tráfico.
"""

import os
import random
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional, Set

import requests
from dotenv import load_dotenv
from locust import HttpUser, between, task

# Cargar variables de entorno buscando en múltiples ubicaciones relativas
current_dir = Path(__file__).resolve().parent
infra_dir = current_dir.parent

load_dotenv(current_dir / ".env")
load_dotenv(infra_dir / ".env")
load_dotenv()

# Configuración Auth0
AUTH0_ISSUER_URI = os.getenv(
    "AUTH0_ISSUER_URI", "https://dev-o02i7bmya113pn5u.us.auth0.com/"
).rstrip("/") + "/"
AUTH0_AUDIENCE = os.getenv("AUTH0_AUDIENCE", "https://snippet-searcher.ingsis/")
AUTH0_CLIENT_ID = os.getenv("AUTH0_M2M_CLIENT_ID", "")
AUTH0_CLIENT_SECRET = os.getenv("AUTH0_M2M_CLIENT_SECRET", "")


class Auth0TokenManager:
    """
    Gestor thread-safe para obtención y renovación proactiva de tokens OAuth2 M2M.
    Evita saturar el endpoint /oauth/token de Auth0 durante pruebas con alto volumen.
    """

    def __init__(self, issuer_uri: str, audience: str, client_id: str, client_secret: str):
        self.token_url = f"{issuer_uri}oauth/token"
        self.audience = audience
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def get_token(self) -> Optional[str]:
        if not self.client_id or not self.client_secret:
            return None

        # Si el token actual sigue vigente (con buffer de 60 segundos), reutilizarlo
        now = time.time()
        if self._token and now < (self._expires_at - 60):
            return self._token

        with self._lock:
            # Doble chequeo tras adquirir el lock
            now = time.time()
            if self._token and now < (self._expires_at - 60):
                return self._token

            payload = {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "audience": self.audience,
            }
            headers = {"Content-Type": "application/json"}

            try:
                resp = requests.post(
                    self.token_url, json=payload, headers=headers, timeout=10
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self._token = data.get("access_token")
                    expires_in = data.get("expires_in", 86400)
                    self._expires_at = now + expires_in
                    print(
                        f"[Auth0] Token M2M obtenido exitosamente. Expira en {expires_in}s."
                    )
                    return self._token
                else:
                    print(
                        f"[Auth0] Error al obtener token: HTTP {resp.status_code} - {resp.text}",
                        file=sys.stderr,
                    )
            except Exception as e:
                print(
                    f"[Auth0] Excepción de conexión al solicitar token: {e}",
                    file=sys.stderr,
                )

            return self._token


token_manager = Auth0TokenManager(
    issuer_uri=AUTH0_ISSUER_URI,
    audience=AUTH0_AUDIENCE,
    client_id=AUTH0_CLIENT_ID,
    client_secret=AUTH0_CLIENT_SECRET,
)

# Pool compartido de IDs de snippets creados durante la prueba
shared_snippet_ids: Set[str] = set()
ids_lock = threading.Lock()


class SnippetUser(HttpUser):
    """
    Usuario simulado que ejecuta operaciones sobre Snippet Platform.
    Tiempos de espera entre 0.5s y 2.0s simulan comportamiento de usuario real.
    """

    wait_time = between(0.5, 2.0)

    def on_start(self):
        """Inicialización por usuario: verificar token y recolectar snippets iniciales."""
        self.token = token_manager.get_token()
        self._seed_initial_snippets()

    def get_headers(self) -> dict:
        """
        Genera headers HTTP obligatorios:
        - X-Correlation-Id: ID único por request para correlación en New Relic y NGINX.
        - Authorization: Bearer token OAuth2 de Auth0 si está configurado.
        """
        token = token_manager.get_token() or self.token
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Correlation-Id": f"locust-{uuid.uuid4()}",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _seed_initial_snippets(self):
        """Consulta snippets existentes para alimentar el pool de IDs desde el inicio."""
        try:
            resp = self.client.get(
                "/api/snippets?page=0&size=10",
                headers=self.get_headers(),
                name="/api/snippets [Seed]",
            )
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("content", []) if isinstance(data, dict) else []
                with ids_lock:
                    for item in items:
                        if "id" in item:
                            shared_snippet_ids.add(str(item["id"]))
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Tarea A (Peso 10 - Alto volumen): Listar snippets propios y compartidos
    # -------------------------------------------------------------------------
    @task(10)
    def list_snippets(self):
        headers = self.get_headers()
        with self.client.get(
            "/api/snippets?page=0&size=10",
            headers=headers,
            name="/api/snippets [List]",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
                try:
                    data = response.json()
                    items = data.get("content", []) if isinstance(data, dict) else []
                    with ids_lock:
                        for item in items:
                            if "id" in item:
                                shared_snippet_ids.add(str(item["id"]))
                except Exception:
                    pass
            elif response.status_code == 401:
                response.failure("401 Unauthorized - Token vencido o ausente")
            else:
                response.failure(f"HTTP {response.status_code}: {response.text[:100]}")

    # -------------------------------------------------------------------------
    # Tarea B (Peso 5 - Catálogo): Reglas de linting y formateo
    # -------------------------------------------------------------------------
    @task(5)
    def get_rules_catalogs(self):
        headers = self.get_headers()
        self.client.get(
            "/api/permissions/rules/catalog/lint",
            headers=headers,
            name="/api/permissions/rules/catalog/lint",
        )
        self.client.get(
            "/api/permissions/rules/catalog/format",
            headers=headers,
            name="/api/permissions/rules/catalog/format",
        )

    # -------------------------------------------------------------------------
    # Tarea C (Peso 4 - Lectura): Tipos de archivo y contenido de snippet
    # -------------------------------------------------------------------------
    @task(4)
    def read_snippet_content_and_types(self):
        headers = self.get_headers()
        self.client.get(
            "/api/snippets/file-types",
            headers=headers,
            name="/api/snippets/file-types",
        )

        snippet_id = None
        with ids_lock:
            if shared_snippet_ids:
                snippet_id = random.choice(list(shared_snippet_ids))

        if snippet_id:
            with self.client.get(
                f"/api/snippets/{snippet_id}/content",
                headers=headers,
                name="/api/snippets/[id]/content",
                catch_response=True,
            ) as response:
                if response.status_code in (200, 404):
                    # 404 es aceptable si otro worker borró o el ID expiró
                    response.success()
                else:
                    response.failure(f"Error {response.status_code}")

    # -------------------------------------------------------------------------
    # Tarea D (Peso 3 - Creación y linteo async): POST /api/snippets
    # -------------------------------------------------------------------------
    @task(3)
    def create_snippet(self):
        headers = self.get_headers()
        random_suffix = uuid.uuid4().hex[:8]
        payload = {
            "name": f"LoadTest_{random_suffix}",
            "description": "Generated by Locust load test",
            "language": "printscript",
            "version": "1.1",
            "extension": "prs",
            "content": "let x: number = 42;\nprintln(x);\n",
        }

        with self.client.post(
            "/api/snippets",
            json=payload,
            headers=headers,
            name="/api/snippets [Create]",
            catch_response=True,
        ) as response:
            if response.status_code in (200, 201):
                response.success()
                try:
                    data = response.json()
                    snippet_id = data.get("id")
                    if snippet_id:
                        with ids_lock:
                            shared_snippet_ids.add(str(snippet_id))
                except Exception:
                    pass
            else:
                response.failure(f"Create failed: HTTP {response.status_code}")

    # -------------------------------------------------------------------------
    # Tarea E (Peso 2 - Formateo sincrónico): POST /api/snippets/{id}/format
    # -------------------------------------------------------------------------
    @task(2)
    def format_snippet(self):
        headers = self.get_headers()
        snippet_id = None
        with ids_lock:
            if shared_snippet_ids:
                snippet_id = random.choice(list(shared_snippet_ids))

        if snippet_id:
            format_payload = {
                "content": "let x: number = 42; println(x);\n"
            }
            with self.client.post(
                f"/api/snippets/{snippet_id}/format",
                json=format_payload,
                headers=headers,
                name="/api/snippets/[id]/format",
                catch_response=True,
            ) as response:
                if response.status_code in (200, 404):
                    response.success()
                else:
                    response.failure(f"Format failed: HTTP {response.status_code}")

    # -------------------------------------------------------------------------
    # Tarea F (Peso 1 - Caos / Alerta New Relic 5xx): GET /api/snippets/simulate-error
    # -------------------------------------------------------------------------
    @task(1)
    def trigger_simulated_error(self):
        """
        Dispara un error HTTP 500 deliberado en el backend para validar
        el incremento del error rate y el disparo de alertas en New Relic.
        Se captura con catch_response=True marcándolo como esperado en Locust.
        """
        headers = self.get_headers()
        with self.client.get(
            "/api/snippets/simulate-error?message=LocustStressTest",
            headers=headers,
            name="/api/snippets/simulate-error",
            catch_response=True,
        ) as response:
            if response.status_code == 500:
                # El 500 es el comportamiento deliberado del caos endpoint
                response.success()
            else:
                response.failure(
                    f"Se esperaba HTTP 500 pero se recibió {response.status_code}"
                )
