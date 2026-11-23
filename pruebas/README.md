# Documentos de prueba

Esta carpeta no versiona binarios (ver `.gitignore`). Para ejecutar el plan de
verificación del `README.md` principal, coloca aquí estos archivos con
**exactamente** estos nombres:

| Archivo | Qué debe contener | Qué verifica |
|---|---|---|
| `tabla-multipagina.pdf` | PDF digital (con capa de texto) con una tabla que cruce al menos un salto de página. | Tablas en sintaxis Markdown, TableFormer ACCURATE, imágenes como PNG externos. |
| `escaneado.pdf` | PDF escaneado (solo imagen, sin capa de texto), en español. | Sin `--ocr` sale casi vacío; con `--ocr` aparece el texto (Tesseract spa+eng). |
| `con-imagen.docx` | Documento Word con listas anidadas (2–3 niveles) y una imagen incrustada que contenga texto (p. ej. una captura de pantalla). | Backend nativo de Word; con `--docx-via-pdf`, LibreOffice + pipeline PDF. |
| `imagen-externa.docx` | Documento Word con una imagen **vinculada**, no incrustada (`TargetMode="External"` en `word/_rels/document.xml.rels`). En Word: Insertar → Imágenes → flecha del botón *Insertar* → *Vincular al archivo*. | Convierte sin error fatal y sin intentar acceder a la red. |
| `corrupto.pdf` | Bytes aleatorios. | Se registra como fallido sin cortar el lote y sin dejar temporales. |

Para generar el archivo corrupto:

```bash
head -c 2000 /dev/urandom > pruebas/corrupto.pdf
```

Para comprobar que un `.docx` tiene una imagen externa:

```bash
unzip -p pruebas/imagen-externa.docx word/_rels/document.xml.rels | grep -o 'TargetMode="External"'
```

No uses documentos confidenciales que no deban quedar en `salida/`.
