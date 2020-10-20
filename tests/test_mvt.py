
import json

from vectorman.mvt import MVTTransformer
from vectorman.util import coord_to_tile
from tests import SAMPLE_GEOJSON_FILE


def test_mvt_geojson_valid():
    with open(SAMPLE_GEOJSON_FILE) as infile:
        sample = json.load(infile)
    tiles = MVTTransformer(sample)
    # tile = tiles.get_tile(0, 0, 0)
    # tile = tiles.get_tile(18, 131079, 131055)
    lon, lat = sample["features"][0]["geometry"]["coordinates"]
    z, x, y = coord_to_tile(lon, lat, 18)
    tile = tiles.get_tile(z, x, y)
    pbf = tile.SerializeToString()
    with open('test.pbf', 'wb') as outfile:
        outfile.write(pbf)
    with open('test.pbf.txt', 'w') as outfile:
        outfile.write(f'{z} {x} {y}')
