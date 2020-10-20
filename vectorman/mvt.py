"""Tool to convert products into Mapbox vector tiles.

Heavily inspired (and modeled from):
* https://github.com/geometalab/geojson2vt
* https://github.com/mapbox/vt-pbf
* https://github.com/mapbox/vector-tile-spec/tree/master/2.1#431-command-integers

"""

import os
import json
import sys
import logging
from enum import IntEnum
from typing import Dict, Any, Optional, Union, List, Tuple

from geojson2vt.geojson2vt import geojson2vt, GeoJsonVt

from vectorman.models import MVTile
from vectorman.constants import TILEJSON_VERSION

try:
    from vectorman.pbf import vector_tile_pb2
except ImportError as exc:
    logging.error(
        "Can not import compiled protobuffer module. Please run `atlas proto` to compile."
    )
    sys.exit(1)


logger = logging.getLogger(__name__)


# https://github.com/geometalab/geojson2vt
MVT_STANDARD_SETTINGS = {
    # max zoom to preserve detail on; can't be higher than 24
    "maxZoom": 18,
    # simplification tolerance (higher means simpler)
    "tolerance": 3,
    # tile extent (both width and height)
    "extent": 4096,
    # tile buffer on each side
    "buffer": 64,
    # whether to enable line metrics tracking for LineString/MultiLineString features
    "lineMetrics": False,
    # name of a feature property to promote to feature.id. Cannot be used with `generateId`
    "promoteId": None,
    # whether to generate feature ids. Cannot be used with `promoteId`
    "generateId": True,
    # max zoom in the initial tile index
    "indexMaxZoom": 5,
    # max number of points per tile in the index
    "indexMaxPoints": 100000,
}
MVT_PRECOMPUTE_SETTINGS = {"maxZoom": 12, "indexMaxZoom": 12, "indexMaxPoints": 0}


class CommandID(IntEnum):
    """The command that should be used for drawing geometry in a encoding command.

    https://github.com/mapbox/vector-tile-spec/tree/master/2.1#433-command-types

    """

    MOVE_TO = 1
    LINE_TO = 2
    CLOSE_PATH = 7


# pylint: disable=too-many-instance-attributes
class LayerContext:
    """A context cache used to keep track of the current pointer for key/value encoding."""

    def __init__(self):
        self.keys = []
        self.values = []
        self.keycache = {}
        self.valuecache = {}
        self.feature = None
        self.layer = None
        self.tile = None
        self.mvt_tile = None

    @property
    def next_key_index(self):
        """Return the next available key index in the key cache."""
        return len(self.keys) - 1

    @property
    def next_value_index(self):
        """Return the next available value index in the value cache."""
        return len(self.values) - 1

    def __dict__(self):
        return {
            "keys": self.keys,
            "values": self.values,
            "keycache": self.keycache,
            "valuecache": self.valuecache,
        }


def command(cmd: Union[int, CommandID], length: int) -> int:
    """Generate a command for drawing a feature.

    Command IDs are 0-7 inclusive, encoded in the last 3 bits of a CommandInteger.
    The number of times to perform the operation are encoded in the remaining 29 bits,
    in the range 0 to 2^29-1, inclusive.
    For more information about this technique, check out:
    https://github.com/mapbox/vector-tile-spec/tree/master/2.1#431-command-integers

    Args:
        cmd: the ID of the command to specify
        length: the number of times to repeat the command as per spec

    Returns:
        The bitwise encoded representation of the commmand.

    """
    return (length << 3) + (cmd & 0x7)


def zigzag(value: int) -> int:
    """Zig-zag encode a parameter to turn signed ints into unsigned ints.

    Zig-zag encoding is required by Geometry commands that require parameters,
    and the number of parameters will be (number_of_commands * number_of_arguments).
    For more information about this technique, check out:
    https://developers.google.com/protocol-buffers/docs/encoding#types

    Args:
        value: the integer value to encode

    Returns:
        The encoded representation of the value.

    """
    return (value << 1) ^ (value >> 31)


# pylint: disable=too-many-instance-attributes
class MVTTransformer:
    """Transform GeoJSON data into Mapbox Vector Tiles."""

    def __init__(
        self,
        geojson: Dict,
        layer_name: str = "geojson",
        extent: int = 4096,
        version: int = 2,
        pregen: bool = False,
        url_root: str = "/",
        **vt_kwargs,
    ):
        self.context = LayerContext()
        self.geojson = geojson
        self.layer_name = layer_name
        self.version = version
        self.extent = extent
        self.pregen = pregen
        self.zoom = None
        self.index = self._bootstrap_index(**vt_kwargs)
        self.tiles = self._bootstrap_tiles() if pregen else []
        self._url_root = url_root

    @property
    def slug(self) -> str:
        return self.geojson["properties"]["slug"]

    @property
    def source(self) -> str:
        return self.geojson["properties"]["filepath"]

    def _bootstrap_index(self, **vt_kwargs) -> GeoJsonVt:
        """Create the geojson2vt JS tile index with the provided arguments.

        The provided arguments are merged with either the baseline args
        for standard (on-the-fly) generation or a pre-computed index.

        Args:
            **: the dictionary of arguments that should be passed to geojson2vt

        Returns:
            The geojson2vt cache

        """
        base_settings = (
            {**MVT_STANDARD_SETTINGS, **MVT_PRECOMPUTE_SETTINGS}
            if self.pregen
            else MVT_STANDARD_SETTINGS
        )
        settings = {**base_settings, **vt_kwargs}
        logger.debug("Creating MVT tile index...")
        for key, value in settings.items():
            logger.debug(f"  {key}\t{value}")
        self.zoom = settings["maxZoom"]
        result = geojson2vt(self.geojson, settings, logging.INFO)
        logger.debug("Tile Index generated.")
        return result

    def _bootstrap_tiles(self) -> List[MVTile]:
        """Turn all of the tiles in the tile cache into easier to use objects.

        Returns:
            Named-tuple encoded versions of all the tiles.

        """
        tiles = [
            MVTile(**t, tile=self.get_tile(**t, name=self.name))
            for t in self.index.tile_coords
        ]
        tiles.sort(key=lambda x: x.z)
        return tiles

    @staticmethod
    def _write_value(pbf_layer: vector_tile_pb2.Tile.Layer, value: Any):
        """Transform the value into one that the PBF container can use, and write it to the layer.

        Values that are not of the supported types (int, float, bool, str) will be escaped
        as JSON strings.

        Args:
            pbf_layer: the Tile layer to use for writing values

        """
        # escape all non-string, non-bool, non-number values to JSON
        should_escape = not any(
            [isinstance(value, checktype) for checktype in [int, float, bool, str]]
        )
        if should_escape:
            value = json.dumps(value)

        # determine the correct type to marshall the value to before setting it in the index
        destinations = {
            "string_value": str,
            "double_value": float,
            "int_value": int,
            "bool_value": bool,
        }
        pbf_value = pbf_layer.values.add()
        chosen_destination = None
        for destination, checktype in destinations.items():
            if isinstance(value, checktype):
                chosen_destination = destination
                break
        if not chosen_destination:
            raise Exception(
                f"Could not select destination for value type {type(value)}, value: '{value}'"
            )
        setattr(pbf_value, chosen_destination, value)

    def _write_properties(
        self, pbf_feature: vector_tile_pb2.Tile.Feature, mvt_feature: Dict
    ):
        """Turn all of the VT-JS transformed tags into spec-encoded PBF tags and write them.

        Args:
            pbf_feature: the protobuf feature that the tags should be written to
            mvt_feature: the vector-tile-js encoded version of the feature to convert

        """
        for key, value in mvt_feature["tags"].items():
            # cache the key and add it to our tags list
            key_index = self.context.keycache.get(key)
            if key_index is None:
                self.context.keys.append(key)
                key_index = self.context.next_key_index
                self.context.keycache[key] = key_index
            pbf_feature.tags.append(key_index)

            # cache the determined value, and add it to the tags
            value_key = f"{type(value).__name__}:{value}"
            value_index = self.context.valuecache.get(value_key)
            if value_index is None:
                self.context.values.append(value)
                value_index = self.context.next_value_index
                self.context.valuecache[value_key] = value_index
            pbf_feature.tags.append(value_index)

    @staticmethod
    def _write_geometry(pbf_feature: vector_tile_pb2.Tile.Feature, mvt_feature: Dict):
        """Write the provided JS geometry as protobuf geometry.

        Performs conversion/encoding of commands and their arguments/values.

        Geometry is encoded using a CommandID and zig-zag encoded values. For more information,
        check out the spec at https://github.com/mapbox/vector-tile-spec/tree/master/2.1#431-command-integers.

        Args:
            pbf_feature: the protobuf feature to write the geometry to
            mvt_feature: the mapbox-vector-tile encoded version of the feature to convert

        """
        mvt_geometry = mvt_feature.get("geometry")
        geometry_type = mvt_feature["type"]
        x, y = 0, 0

        for ring in mvt_geometry:
            position = 1
            # points
            if geometry_type == vector_tile_pb2.Tile.POINT:
                ring = [ring]
                position = len(ring)

            # https://github.com/mapbox/vector-tile-spec/tree/master/2.1#435-example-geometry-encodings
            # [<Encoded Command>, <Encoded x>, <Encoded y>]
            pbf_feature.geometry.append(command(CommandID.MOVE_TO, position))

            line_count = (
                len(ring) - 1
                if geometry_type == vector_tile_pb2.Tile.POLYGON
                else len(ring)
            )
            for i in range(0, line_count):
                if i == 1 and geometry_type != vector_tile_pb2.Tile.POINT:
                    pbf_feature.geometry.append(
                        command(CommandID.LINE_TO, line_count - 1)
                    )

                diff_x = int(ring[i][0]) - x
                diff_y = int(ring[i][1]) - y
                pbf_feature.geometry.append(zigzag(diff_x))
                pbf_feature.geometry.append(zigzag(diff_y))
                x += diff_x
                y += diff_y

            if geometry_type == vector_tile_pb2.Tile.POLYGON:
                pbf_feature.geometry.append(command(CommandID.CLOSE_PATH, 1))

    def _write_feature(self, pbf_layer: vector_tile_pb2.Tile.Layer, mvt_feature: Dict):
        """For a given vector-tile-js feature, write it to the provided protobuf.

        Wraps conversion of all geometry and tags.

        Args:
            pbf_layer: the protobuf layer on the Tile that should be written to
            mvt_feature: the vector-tile-js encoded version of the feature to convert

        """
        pbf_feature = pbf_layer.features.add()
        mvt_feature_id = mvt_feature.get("id")
        if mvt_feature_id:
            pbf_feature.id = int(mvt_feature_id)
        self._write_properties(pbf_feature, mvt_feature)
        pbf_feature.type = mvt_feature["type"]
        self._write_geometry(pbf_feature, mvt_feature)

    def _write_layer(self, pbf_layer: vector_tile_pb2.Tile.Layer, mvt_layer: Dict):
        """Write the provided Vector Tile JS layer as a spec-compliant protobuf layer.

        Args:
            pbf_layer: the protobuffer layer on the Tile that should be written to
            mvt_layer: the vector-tile-js encoded version of the feature to convert

        """
        pbf_layer.version = self.version
        pbf_layer.name = mvt_layer["name"]
        pbf_layer.extent = self.extent

        self.context = LayerContext()

        for mvt_feature in mvt_layer["features"]:
            self.context.feature = mvt_feature
            self._write_feature(pbf_layer, mvt_feature)

        pbf_layer.keys.extend(self.context.keys)
        for value in self.context.values:
            self._write_value(pbf_layer, value)

    def _debug(self):
        """Precompute using the specified settings and print feature counts.

        Only meant to be used as a sanity check for a given file.

        """
        for tile in self.tiles:
            print(f"{tile.z}\t{tile.x}\t{tile.y}\t{len(tile.tile.layers[0].features)}")

    def get_tile(
        self, z: int, x: int, y: int, name: str = "geojson"
    ) -> vector_tile_pb2.Tile:
        """Get the requested tile from the converted geojson as a vector tile.

        Args:
            z: the zoom level attribute for the tile to retrieve
            x: the X value for the tile to retrieve
            y: the Y value for the tile to retrieve
            name: (optional) the name to assign to the generated layer

        Returns:
            The transformed tile in protobuffer form.

        """
        transform = {}
        transform[name] = self.index.get_tile(z, x, y)
        return self.from_geojson_vt(transform)

    @staticmethod
    def _tile_to_disk(tile: vector_tile_pb2.Tile, filepath: str) -> str:
        """Write the given protobuf tile to the given filepath.

        Args:
            tile: the transformed protobuf tile to write
            filepath: the location on disk to write the tile to

        """
        with open(filepath, "wb") as outfile:
            outfile.write(tile.SerializeToString())
        return os.path.abspath(filepath)

    def write_tile(
        self,
        z: int,
        x: int,
        y: int,
        name: str,
        extension: str = "mvt",
        directory: str = None,
    ) -> str:
        """Write the requested tile from the converted geojson as a vector tile to disk.

        Args:
            z: the zoom level attribute for the tile to retrieve
            x: the X value for the tile to retrieve
            y: the Y value for the tile to retrieve
            name: the name to assign to the generated layer
            extension: (optional) the extension to use for the written tile ('mvt' as per spec by default)
            directory: (optional) the directory to write the tile to

        Returns:
            The transformed tile in protobuffer form.

        """
        tile = self.get_tile(z, x, y, name=name)
        filename = f"{name}.{extension}"
        if directory:
            filepath = os.path.join(directory, filename)
        else:
            filepath = os.path.abspath(filename)
        return self._tile_to_disk(tile, filepath)

    def write_all(
        self,
        destination: Optional[str] = None,
        extension: str = "mvt",
        include_geojson: bool = True,
    ) -> str:
        """Write all the tiles for the input GeoJSON to disk, along with source and metadata.

        Args:
            destination: the location on disk to write the tiles to
            extension: (optional) the extension to use for the generated tiles (defaults to 'mvt' as per spec)
            include_geojson: (optional) if the source GeoJSON should be included

        Returns:
            The absolute path to the provided destination.

        """
        destination = destination or self.name
        for tile in self.tiles:
            directory = os.path.join(destination, f"{tile.z}/{tile.x}")
            os.makedirs(directory, exist_ok=True)
            filepath = os.path.join(directory, f"{tile.y}.{extension}")
            self._tile_to_disk(tile.tile, filepath)
        if include_geojson:
            with open(os.path.join(destination, "data.geojson"), "w") as outfile:
                json.dump(self.geojson, outfile, indent=2)
        with open(os.path.join(destination, "metadata.json"), "w") as outfile:
            json.dump(self.tilejson, outfile, indent=2)
        return os.path.abspath(destination)

    def from_vector_tile_js(self, tile: Dict) -> vector_tile_pb2.Tile:
        """Encode the provided vector-tile-js tile into a protobuf version.

        Args:
            tile: the vector-tile-js version of the tile to encode

        Returns:
            The protobuf implementation of the provided tile.

        """
        pbf_tile = vector_tile_pb2.Tile()
        for mvt_layer in tile["layers"].values():
            pbf_layer = pbf_tile.layers.add()  # pylint: disable=no-member
            self._write_layer(pbf_layer, mvt_layer)
        return pbf_tile

    def from_geojson_vt(self, layers: Dict) -> vector_tile_pb2.Tile:
        """Encode the provided geojson2vt tile into a protobuf version.

        Args:
            layers: a mapping of the requested layers to their spec version and extent

        Returns:
            The protobuf encoded version of the mapped geometry.

        """
        mapping = {}
        for layer, tile in layers.items():
            if not tile:
                continue
            mapping[layer] = {
                "features": tile["features"],
                "name": layer,
                "version": self.version,
                "extent": self.extent,
            }
        return self.from_vector_tile_js({"layers": mapping})

    @property
    def name(self) -> str:
        """An alias for the layer name of the current transformer."""
        return self.layer_name

    @property
    def centroid(self) -> Tuple[float, float, int]:
        bounds = self.geojson["bbox"]
        lon = (bounds[0] + bounds[2]) / 2
        lat = (bounds[1] + bounds[3]) / 2
        return lon, lat, self.zoom

    @property
    def tilejson(self) -> Dict[str, Any]:
        """Create a MapBox TileJSON response for the given image path.

        Returns:
            The TileJSON compliant response for the given filepath.

        """
        tile_url = (
            f"{self._url_root}products/mvt/{self.slug}/{self.name}" + "/{z}/{x}/{y}.mvt"
        )
        data_url = f"{self._url_root}products/geojson/{self.slug}/{self.name}"

        return {
            "tilejson": TILEJSON_VERSION,
            "name": self.name,
            "version": "1.0.0",
            "scheme": "xyz",
            "minzoom": 0,
            "maxzoom": self.zoom,
            "bounds": self.geojson["bbox"],
            "center": self.centroid,
            "description": f"Mapbox Vector Tiles generated from GeoJSON ({self.slug})",
            "tiles": [tile_url],
            "data": [data_url],
            "format": "pbf",
            "vector_layers": [
                {
                    "id": "geojson",
                    "description": f"Vector tiles for LOS Product {self.name} ({self.slug})",
                    "minzoom": 0,
                    "maxzoom": self.zoom,
                    "fields": {
                        "row": "Pixel row of observation",
                        "column": "Pixel column of observation",
                        "distance": "Number",
                        "isVisible": "Boolean",
                        "sourceId": "String",
                    },
                }
            ],
        }
