# This file is part of the Open Data Cube, see https://opendatacube.org for more information
#
# Copyright (c) 2015-2024 ODC Contributors
# SPDX-License-Identifier: Apache-2.0
from typing import Any, Mapping
from datacube.model.fields import SimpleField, Field, get_dataset_fields as generic_get_dataset_fields
from datacube.utils.changes import Offset


# TODO: SimpleFields cannot handle non-metadata fields because e.g. the extract API expects a doc, not a Dataset model
def get_native_fields() -> dict[str, Field]:
    return {
        "id": SimpleField(
            ["id"],
            str, 'string',
            name="id",
            description="Dataset UUID"
        ),
        "product": SimpleField(
            ["product", "name"],
            str, 'string',
            name="product",
            description="Product name"
        ),
        "label": SimpleField(
            ["label"],
            str, 'string',
            name="label",
            description=""
        ),
        "format": SimpleField(
            ["format", "name"],
            str, 'string',
            name="format",
            description="File format (GeoTIFF, NetCDF)"
        ),
        "metadata_doc": SimpleField(
            [],
            str, 'string',
            name="metadata_doc",
            description="Full metadata document"
        ),
    }


def get_dataset_fields(metadata_definition: Mapping[str, Any]) -> dict[str, Field]:
    fields = get_native_fields()
    fields.update(generic_get_dataset_fields(metadata_definition))
    return fields


def build_custom_fields(custom_offsets: Mapping[str, Offset]) -> dict[str, Field]:
    return {
        name: SimpleField(
            offset,
            lambda x: x, 'any',
            name=name,
            description=f"Custom field: {name}"
        )
        for name, offset in custom_offsets.items()
    }
