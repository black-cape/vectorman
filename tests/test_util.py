"""Test utility functions in Vectorman"""

import os
from itertools import chain

import pytest

from vectorman.util import (
    snake,
    camel,
)


@pytest.mark.parametrize(
    "provided,expected",
    [
        ("control", "control"),
        ("testControl", "test_control"),
        ("TestControl", "test_control"),
        ("beaureauOfControl", "beaureau_of_control"),
    ],
)
def test_util_snake(provided, expected):
    assert snake(provided) == expected


@pytest.mark.parametrize(
    "provided,expected",
    [
        ("control", "control"),
        ("test_control", "testControl"),
        ("test_control_", "testControl"),
        ("beaureau_of_control", "beaureauOfControl"),
    ],
)
def test_util_camel(provided, expected):
    assert camel(provided) == expected
