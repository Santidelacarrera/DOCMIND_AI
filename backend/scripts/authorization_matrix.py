"""Generate docs/AUTHORIZATION_MATRIX.md from the running application's routes.

Each row is one endpoint with the membership check its handler calls. A row without a check is only acceptable
for endpoints that authenticate in another way (login, registration, invitation tokens) or that are public.

    python scripts/authorization_matrix.py            # writes docs/AUTHORIZATION_MATRIX.md
"""

import inspect
import re
from pathlib import Path

from fastapi.routing import APIRoute

from app import main

OUT = Path(__file__).resolve().parents[2] / "docs" / "AUTHORIZATION_MATRIX.md"
CHECK = re.compile(r"membership\([^)]*\)")
NO_CHECK_BY_DESIGN = {
    "/api/v1/auth/login",
    "/api/v1/auth/register",
    "/api/v1/auth/logout",
    "/api/v1/auth/me",
    "/api/v1/organizations",
    "/api/v1/invitations/accept",
    "/health",
    "/ready",
    "/metrics",
}


def rows() -> list[tuple[str, str, str]]:
    out = []
    for route in main.app.routes:
        if not isinstance(route, APIRoute):
            continue
        try:
            source = inspect.getsource(route.endpoint)
        except OSError:
            source = ""
        checks = sorted({re.sub(r"\s+", " ", c) for c in CHECK.findall(source)})
        out.append((min(route.methods), route.path, "; ".join(checks)))
    return sorted(out, key=lambda r: (r[1], r[0]))


def main_() -> None:
    lines = [
        "# Matriz de autorización (generada)",
        "",
        "Generada por `backend/scripts/authorization_matrix.py` a partir de las rutas de la aplicación. No editar a mano.",
        "",
        "Lectura de `scope=` para personas: en la función `membership()`, un usuario (no una API key) solo está",
        'limitado por su rol mínimo; el `scope` se aplica únicamente a las claves API. Por eso `scope="usage:read"`',
        "permite a **cualquier miembro**, incluido un viewer, leer los totales de uso. Ver la sección de uso.",
        "",
        "| Método | Ruta | Comprobación en el handler |",
        "|---|---|---|",
    ]
    unchecked = []
    for method, path, check in rows():
        lines.append(f"| {method} | `{path}` | {check or '**ninguna**'} |")
        if not check and path not in NO_CHECK_BY_DESIGN:
            unchecked.append(path)
    if unchecked:
        raise SystemExit(
            f"endpoints without a membership check and not listed as by-design: {unchecked}"
        )
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(lines) - 6} routes)")


if __name__ == "__main__":
    main_()
