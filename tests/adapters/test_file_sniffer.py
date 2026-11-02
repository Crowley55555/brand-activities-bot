"""FileSnifferPort (ADR-23, фаза 3b): адаптер python-magic. Вызывает его воркер фазы 3c.

Библиотека одна и та же на всех платформах: python-magic-bin на Windows, python-magic на Linux
(нужна системная libmagic1). Круговой путь: байты -> объектное хранилище -> байты -> MIME.
"""

from __future__ import annotations

from pathlib import Path

from app.adapters.file_sniffer.python_magic import PythonMagicSniffer
from app.adapters.object_storage.local_fs import LocalFsObjectStorage
from app.core.ports.file_sniffer import FileSnifferPort

# Заголовки форматов: libmagic узнаёт тип по сигнатуре, полный файл не нужен.
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89"
)
PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< >>\n%%EOF\n"


def test_adapter_implements_the_port() -> None:
    assert isinstance(PythonMagicSniffer(), FileSnifferPort)


def test_jpeg_bytes_are_sniffed_as_image_jpeg() -> None:
    info = PythonMagicSniffer().sniff(JPEG)

    assert info.mime == "image/jpeg"
    assert info.is_image is True


def test_png_and_pdf_are_recognised_by_content() -> None:
    sniffer = PythonMagicSniffer()

    assert sniffer.sniff(PNG).mime == "image/png"
    pdf = sniffer.sniff(PDF)
    assert pdf.mime == "application/pdf"
    assert pdf.is_image is False


def test_random_bytes_are_not_images() -> None:
    info = PythonMagicSniffer().sniff(b"\x00\x01\x02\x03 not a picture \xfe\xff")

    assert info.is_image is False


async def test_jpeg_round_trip_through_storage_keeps_its_mime(tmp_path: Path) -> None:
    storage = LocalFsObjectStorage(tmp_path, signing_key="test-signing-key-unused-for-put-get")
    await storage.put("ugc", "photo.jpg", JPEG, "image/jpeg")

    data = await storage.get("ugc", "photo.jpg")

    assert data == JPEG
    assert PythonMagicSniffer().sniff(data).mime == "image/jpeg"
