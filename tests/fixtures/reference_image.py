"""Generate a geometric raster fixture instead of relying on private references."""

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw


def write_reference(path):
    path = Path(path)
    image = Image.new('RGB', (200, 240), 'white')
    ImageDraw.Draw(image).rectangle((40, 20, 159, 219), fill='gray')
    if path.exists():
        with Image.open(path) as existing:
            if existing.size != image.size or ImageChops.difference(
                    existing.convert('RGB'), image).getbbox() is not None:
                raise FileExistsError(f'Refusing to replace a different reference: {path}')
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as handle:
            image.save(handle, format='PNG')
    return path
