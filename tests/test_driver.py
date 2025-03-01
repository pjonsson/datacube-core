# This file is part of the Open Data Cube, see https://opendatacube.org for more information
#
# Copyright (c) 2015-2024 ODC Contributors
# SPDX-License-Identifier: Apache-2.0
import pytest
import yaml

from types import SimpleNamespace

from datacube.drivers import new_datasource, reader_drivers, writer_drivers
from datacube.drivers import index_drivers, index_driver_by_name
from datacube.drivers.indexes import IndexDriverCache
from datacube.storage import BandInfo
from datacube.storage._rio import RasterDatasetDataSource
from datacube.testutils import mk_sample_dataset, suppress_deprecations
from datacube.model import MetadataType


def test_new_datasource_fallback():
    bands = [dict(name='green',
                  path='')]
    dataset = mk_sample_dataset(bands, 'file:///foo', format='GeoTiff')

    assert dataset.uri_scheme == 'file'

    rdr = new_datasource(BandInfo(dataset, 'green'))
    assert rdr is not None
    assert isinstance(rdr, RasterDatasetDataSource)

    # check that None format works
    band = BandInfo(mk_sample_dataset(bands, 'file:///file', format=None), 'green')
    rdr = new_datasource(band)
    assert rdr is not None
    assert isinstance(rdr, RasterDatasetDataSource)


def test_reader_drivers():
    available_drivers = reader_drivers()
    assert isinstance(available_drivers, list)


def test_writer_drivers():
    available_drivers = writer_drivers()
    assert 'netcdf' in available_drivers
    assert 'NetCDF CF' in available_drivers


def test_index_drivers():
    available_drivers = index_drivers()
    assert 'default' in available_drivers
    assert 'null' in available_drivers
    assert 'memory' in available_drivers


def test_default_injection():
    cache = IndexDriverCache('datacube.plugins.index-no-such-prefix')
    assert set(cache.drivers()) == set(['default', 'postgres', 'legacy', 'postgis', 'memory'])


def test_netcdf_driver_import():
    try:
        import datacube.drivers.netcdf.driver
    except ImportError:
        assert False and 'Failed to load netcdf writer driver'

    assert datacube.drivers.netcdf.driver.reader_driver_init is not None


def test_writer_driver_mk_uri():
    from datacube.drivers.netcdf.driver import NetcdfWriterDriver
    writer_driver = NetcdfWriterDriver()

    assert writer_driver.uri_scheme == 'file'

    file_path = '/path/to/my_file.nc'
    file_uri = writer_driver.mk_uri(file_path=file_path)
    assert file_uri == f'file://{file_path}'


def test_metadata_type_from_doc():
    metadata_doc = yaml.safe_load('''
name: minimal
description: minimal metadata definition
dataset:
    id: [id]
    sources: [lineage, source_datasets]
    label: [label]
    creation_dt: [creation_dt]
    search_fields:
        some_custom_field:
            description: some custom field
            offset: [a,b,c,custom]
    ''')

    for name in index_drivers():
        driver = index_driver_by_name(name)
        with suppress_deprecations():
            metadata = driver.metadata_type_from_doc(metadata_doc)  # deprecated method
            assert isinstance(metadata, MetadataType)
            assert metadata.id is None
            assert metadata.name == 'minimal'
            assert 'some_custom_field' in metadata.dataset_fields


def test_reader_cache_throws_on_missing_fallback():
    from datacube.drivers.readers import rdr_cache

    rdrs = rdr_cache()
    assert rdrs is not None

    with pytest.raises(KeyError):
        rdrs('file', 'aint-such-format')


def test_driver_singleton():
    from datacube.drivers._tools import singleton_setup
    from unittest.mock import MagicMock

    result = object()
    factory = MagicMock(return_value=result)
    obj = SimpleNamespace()

    assert singleton_setup(obj, 'xx', factory) is result
    assert singleton_setup(obj, 'xx', factory) is result
    assert singleton_setup(obj, 'xx', factory) is result
    assert obj.xx is result

    factory.assert_called_once_with()
