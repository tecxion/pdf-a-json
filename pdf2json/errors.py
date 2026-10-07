"""Excepciones del dominio, sin dependencias.

Vive aparte para que app.py pueda capturarlas con un `except` normal sin importar
extract (y con él PyMuPDF) en el proceso web.
"""


class PdfError(Exception):
    """El PDF no se puede leer. El mensaje se muestra al usuario tal cual."""


class EncryptedPdf(PdfError):
    pass


class CorruptPdf(PdfError):
    pass


class TooManyPages(PdfError):
    pass
