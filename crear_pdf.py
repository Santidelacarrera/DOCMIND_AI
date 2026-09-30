from pathlib import Path

lines = [
    "DOCMIND AI - DOCUMENTO DE PRUEBA",
    "Nombre: Juan Perez",
    "RUT: 12.345.678-9",
    "Fecha: 29 de septiembre de 2026",
    "Monto: 150000",
    "Este documento se utiliza para probar DocMind AI.",
]

objects = []

objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>")

stream = "BT\n/F1 12 Tf\n60 730 Td\n"
for i, line in enumerate(lines):
    if i > 0:
        stream += "0 -25 Td\n"
    safe = line.replace("(", r"\(").replace(")", r"\)")
    stream += f"({safe}) Tj\n"
stream += "ET\n"

stream_bytes = stream.encode("latin-1")
objects.append(
    f"<< /Length {len(stream_bytes)} >>\nstream\n".encode("latin-1")
    + stream_bytes
    + b"endstream"
)
objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

pdf = bytearray(b"%PDF-1.4\n")
offsets = [0]

for number, obj in enumerate(objects, start=1):
    offsets.append(len(pdf))
    pdf += f"{number} 0 obj\n".encode("latin-1")
    pdf += obj
    pdf += b"\nendobj\n"

xref = len(pdf)
pdf += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
pdf += b"0000000000 65535 f \n"

for offset in offsets[1:]:
    pdf += f"{offset:010d} 00000 n \n".encode("latin-1")

pdf += (
    f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
    f"startxref\n{xref}\n%%EOF\n"
).encode("latin-1")

Path("documento_prueba.pdf").write_bytes(pdf)
print("PDF creado:", Path("documento_prueba.pdf").resolve())
print("Tamano:", len(pdf), "bytes")
