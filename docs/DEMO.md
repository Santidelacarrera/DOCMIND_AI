# Demo con documentos ficticios — Nexus Security & AI Lab

Todo es inventado: dos empresas ficticias (*Aurora Demo S.L.* y *Orion Demo Ltd*), personas ficticias, direcciones `@example.test` y un IBAN de ejemplo publicado. Se ejecuta sin red, sin cuentas y sin coste.

```bash
cd backend
pip install -e '.[dev]'                       # una vez
python -m app.ingestion.demo                   # imprime el informe
python -m app.ingestion.demo --output-dir demo_output   # además guarda informe y los documentos ficticios (PDF y Word)
```

Termina con código 0 solo si **todas** las respuestas citan el documento y la página esperados y el dato correcto (y la pregunta sin respuesta se abstiene).

## Qué enseña

| Documento (empresa ficticia) | Formato | Páginas | PII enmascarada antes de indexar |
|---|---|---:|---|
| `politica_vacaciones.docx` (Aurora) | Word | 3 | correo, nombre |
| `contrato_servicios.pdf` (Aurora) | PDF | 4 | IBAN, nombre |
| `manual_seguridad.pdf` (Aurora) | PDF | 2 | — |
| `politica_vacaciones.pdf` (Orion) | PDF | 1 | — |

Resultado de la última ejecución (10 preguntas): **10/10 correctas** con criterio estricto — la *primera* cita es el documento y la página esperados **y** el texto de la respuesta contiene el dato.

| Pregunta (Aurora) | Respuesta (extracto del documento) | Fuente |
|---|---|---|
| ¿Cuántos días de vacaciones tienen los empleados? | «Los empleados tienen derecho a 23 días laborables de vacaciones al año.» | politica_vacaciones.docx, p.2 |
| ¿Con cuánta antelación se solicitan las vacaciones? | «Las solicitudes se envían con 15 días de antelación a **[EMAIL_1]**.» | politica_vacaciones.docx, p.2 |
| ¿Cuál es el precio total del contrato? | «El precio total del contrato es de 48.000 euros, pagaderos trimestralmente.» | contrato_servicios.pdf, p.3 |
| ¿Con cuántos días de preaviso se puede resolver el contrato? | «Cualquiera de las partes puede resolver el contrato con 60 días de preaviso por escrito.» | contrato_servicios.pdf, p.4 |
| ¿Cuál es el color favorito del gerente? | «No encontré información sobre eso en los documentos disponibles.» | — (abstención) |

La misma pregunta sobre vacaciones en *Orion* responde **30 días** citando el documento de Orion: cada empresa solo ve lo suyo. Los correos, nombres e IBAN no aparecen en ninguna respuesta (salen como `[EMAIL_1]`, `[PERSON_1]`, `[IBAN_1]`).

## Qué no demuestra

* Son 10 preguntas sobre 4 documentos escritos junto con el sistema: sirve como prueba de regresión y de forma, **no** como medida de precisión sobre documentos reales.
* El motor de búsqueda de la demo es léxico (`hashing`); con un modelo de embeddings local la recuperación será distinta (mejor en sinónimos, no verificada aquí).

---
Relacionado: [[empresa]]
