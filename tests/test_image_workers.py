"""Headless image decoding across successive pools; no Qt plugin/thread singleton."""

import io
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image

from apple_music_player.artwork import decode, png


def test_successive_image_worker_pools_and_png_conversion():
    fixtures = []
    for fmt in ("PNG", "JPEG", "WEBP"):
        stream = io.BytesIO()
        Image.new("RGB", (400, 400), "#336699").save(stream, format=fmt)
        fixtures.append(stream.getvalue())

    def convert(data):
        image = decode(data, 100)
        assert image.size == (100, 100)
        return png(image)

    for _ in range(5):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(convert, fixtures * 16))
        assert all(decode(data).size == (100, 100) for data in results)


@pytest.mark.parametrize("fmt", ["GIF", "BMP", "TIFF"])
def test_reject_other_decoders_despite_valid_image_bytes(fmt):
    stream = io.BytesIO()
    Image.new("RGB", (10, 10)).save(stream, format=fmt)
    with pytest.raises(ValueError):
        decode(stream.getvalue())


def test_truncated_image_rejected():
    stream = io.BytesIO()
    Image.new("RGB", (100, 100)).save(stream, format="JPEG")
    with pytest.raises(ValueError):
        decode(stream.getvalue()[:100])
