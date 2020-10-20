"""Standard objects to be used for response modeling."""
# pylint: disable=no-self-use,too-few-public-methods

from collections import namedtuple
from enum import Enum

from marshmallow import (
    Schema,
    fields,
    validate,
    validates_schema,
    pre_load,
    post_load,
    post_dump,
    ValidationError,
    EXCLUDE,
)
from marshmallow_enum import EnumField

from vectorman import constants
from vectorman.util import snake, camel


class Status(Enum):
    """Statuses in the Response schema should only ever be one of these."""

    FAIL = "FAIL"
    SUCCESS = "SUCCESS"
    MISSING = "MISSING"
    ERROR = "ERROR"


class GeoJSONGeometryType(Enum):
    """Valid GeoJSON types for responses

    For more information:
    https://tools.ietf.org/html/rfc7946

    """

    # Point, LineString, Polygon, MultiPoint,
    # MultiLineString, and MultiPolygon.
    POINT = "Point"
    LINE_STRING = "LineString"
    POLYGON = "Polygon"
    MULTI_POINT = "MultiPoint"
    MULTI_LINE_STRING = "MultiLineString"
    MULTI_POLYGON = "MultiPolygon"


Response = namedtuple("Response", ["status", "message", "result"])
MVTile = namedtuple("MVTile", ["z", "x", "y", "tile"])


class VersionSchema(Schema):
    """Version information schema."""

    name = fields.Str()
    version = fields.Str()
    env = fields.Str()


class ConfigSchema(Schema):
    """Config information schema."""

    env = fields.Str()


class TileJSONSchema(Schema):
    """Marshmallow schema for Mapbox TileJSON.

    https://github.com/mapbox/tilejson-spec/tree/master/2.2.0

    """

    tilejson = fields.Str(required=True)
    name = fields.Str()
    description = fields.Str()
    version = fields.Str()
    attribution = fields.Str()
    template = fields.Str()
    legend = fields.Str()
    scheme = fields.Str(default="xyz")
    tiles = fields.List(fields.Str(), required=True)
    grids = fields.List(fields.Str())
    data = fields.List(fields.Str())
    minzoom = fields.Integer(default=0)
    maxzoom = fields.Integer(default=30)
    bounds = fields.List(fields.Decimal())
    center = fields.List(fields.Decimal())


class GeoJSONGeometrySchema(Schema):
    """Schema for serializing GeoJSON geometries."""

    geo_type = EnumField(GeoJSONGeometryType, by_value=True, data_key="type")
    coordinates = fields.Raw()

    def _are_numbers(self, value) -> bool:
        """Validate that all values in the iterable are numbers."""
        return all(
            [
                isinstance(
                    member,
                    (
                        int,
                        float,
                    ),
                )
                for member in value
            ]
        )

    @validates_schema
    def validate_coordinates(self, data, **_kwargs):
        """Marshmallow model validator for coordinates."""
        value = data["coordinates"]
        if data["geo_type"] == GeoJSONGeometryType.POINT:
            if len(value) != 2 or not self._are_numbers(value):
                raise ValidationError("Point coordinates must be a set of two numbers")
        # Add other validations here


class GeoJSONFeatureBaseSchema(Schema):
    """Class that should be used as base class for Feature derivatives.

    https://tools.ietf.org/html/rfc7946
    """

    # pylint: disable=missing-docstring
    class Meta:
        unknown = EXCLUDE

    bbox = fields.List(fields.Number(), validate=validate.Length(min=4, max=4))
    properties = fields.Dict(
        keys=fields.Str(required=True), values=fields.Raw(required=True)
    )


class GeoJSONFeatureSchema(GeoJSONFeatureBaseSchema):
    """Marshmallow model for serializing GeoJSON Features."""

    feature_type = fields.Str(
        default=constants.GEOJSON_FEATURE, data_key="type", dump_only=True
    )
    geometry = fields.Nested(GeoJSONGeometrySchema, required=True)


class GeoJSONFeatureCollectionSchema(GeoJSONFeatureBaseSchema):
    """Marshmallow model for serializing GeoJSON FeatureCollections."""

    feature_type = fields.Str(
        default=constants.GEOJSON_FEATURE_COLLECTION, data_key="type", dump_only=True
    )
    features = fields.Nested(GeoJSONFeatureSchema, many=True)


class CamelConverterMixin:
    """Mix-in to load camelcase from the serialized object, and use snakecase internally."""

    @pre_load
    def to_snakecase(self, data, **_kwargs):
        """Convert camelCase strings to snake_case"""
        return {snake(key): value for key, value in data.items()}

    @post_dump
    def to_camelcase(self, data, **_kwargs):
        """Convert snake_case strings to camelCase"""
        return {camel(key): value for key, value in data.items()}
