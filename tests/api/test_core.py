# This file is part of the Open Data Cube, see https://opendatacube.org for more information
#
# Copyright (c) 2015-2024 ODC Contributors
# SPDX-License-Identifier: Apache-2.0
from unittest.mock import MagicMock

import xarray as xr
import numpy as np
import datetime
from uuid import UUID
from types import SimpleNamespace
import pytest

from datacube.api.query import GroupBy
from datacube.api.core import _calculate_chunk_sizes, output_geobox
from datacube import Datacube
from datacube.testutils.geom import AlbersGS
from datacube.testutils import mk_sample_dataset, suppress_deprecations


def test_grouping_datasets():
    def group_func(d):
        return d.time
    dimension = 'time'
    units = None
    datasets = [
        SimpleNamespace(time=datetime.datetime(2016, 1, 1), value='foo', id=UUID(int=10)),
        SimpleNamespace(time=datetime.datetime(2016, 2, 1), value='bar', id=UUID(int=1)),
        SimpleNamespace(time=datetime.datetime(2016, 1, 1), value='flim', id=UUID(int=9)),
    ]

    group_by = GroupBy(group_func, dimension, units, sort_key=group_func)
    grouped = Datacube.group_datasets(datasets, group_by)
    dss = grouped.isel(time=0).values[()]
    assert isinstance(dss, tuple)
    assert len(dss) == 2
    assert [ds.value for ds in dss] == ['flim', 'foo']

    dss = grouped.isel(time=1).values[()]
    assert isinstance(dss, tuple)
    assert len(dss) == 1
    assert [ds.value for ds in dss] == ['bar']

    assert str(grouped.time.dtype) == 'datetime64[ns]'
    assert grouped.loc['2016-01-01':'2016-01-15']


def test_group_datasets_by_time():
    bands = [dict(name='a')]
    # Same time instant but one explicitly marked as UTC
    ds1 = mk_sample_dataset(bands, timestamp="2019-01-01T23:24:00Z")
    ds2 = mk_sample_dataset(bands, timestamp="2019-01-01T23:24:00")
    # Same "time" but in a different timezone, and actually later
    ds3 = mk_sample_dataset(bands, timestamp="2019-01-01T23:24:00-1")
    assert ds1.center_time.tzinfo is not None
    assert ds2.center_time.tzinfo is None
    assert ds3.center_time.tzinfo is not None

    xx = Datacube.group_datasets([ds1, ds2, ds3], 'time')
    assert xx.time.shape == (2,)
    assert len(xx.data[0]) == 2
    assert len(xx.data[1]) == 1


def test_grouped_datasets_should_be_in_consistent_order():
    datasets = [
        {'time': datetime.datetime(2016, 1, 1, 0, 1), 'value': 'foo'},
        {'time': datetime.datetime(2016, 1, 1, 0, 2), 'value': 'flim'},
        {'time': datetime.datetime(2016, 2, 1, 0, 1), 'value': 'bar'}
    ]

    grouped = _group_datasets_by_date(datasets)

    # Swap the two elements which get grouped together
    datasets[0], datasets[1] = datasets[1], datasets[0]
    grouped_2 = _group_datasets_by_date(datasets)

    assert len(grouped) == len(grouped_2) == 2
    assert all(grouped.values == grouped_2.values)


def _group_datasets_by_date(datasets):
    def group_func(d):
        return d['time'].date()

    def sort_key(d):
        return d['time']
    dimension = 'time'
    units = None

    group_by = GroupBy(group_func, dimension, units, sort_key)
    return Datacube.group_datasets(datasets, group_by)


def test_dask_chunks():
    coords = {'time': np.arange(10)}

    sources = xr.DataArray(coords['time'],
                           coords=coords,
                           dims=list(coords))
    geobox = AlbersGS.tile_geobox((0, 0))[:6, :7]

    assert geobox.dimensions == ('y', 'x')
    assert sources.dims == ('time',)

    assert _calculate_chunk_sizes(sources, geobox, {}) == ((1,), (6, 7))
    assert _calculate_chunk_sizes(sources, geobox, {'time': -1}) == ((10,), (6, 7))
    assert _calculate_chunk_sizes(sources, geobox, {'time': 'auto', 'x': 'auto'}) == ((1,), (6, 7))
    assert _calculate_chunk_sizes(sources, geobox, {'y': -1, 'x': 3}) == ((1,), (6, 3))
    assert _calculate_chunk_sizes(sources, geobox, {'y': 2, 'x': 3}) == ((1,), (2, 3))

    with pytest.raises(ValueError):
        _calculate_chunk_sizes(sources, geobox, {'x': "aouto"})

    with pytest.raises(KeyError):
        _calculate_chunk_sizes(sources, geobox, {'zz': 1})


def test_index_validation():
    index = MagicMock()
    with pytest.raises(ValueError) as e:
        dc = Datacube(index=index, config=["/a/path", "/a/nother/path"], env="prod", app="this_is_me", raw_config="{}")
    estr = str(e.value)
    assert "config,raw_config,app,env" in estr


def test_output_geobox():
    from odc.geo.geobox import GeoBox as ODCGeoGeoBox, CRS as ODCGeoCRS
    with suppress_deprecations():
        from datacube.utils.geometry import GeoBox as LegacyGeoGeoBox, CRS as LegacyCRS
    from odc.geo.xr import xr_zeros

    odc_gbox = ODCGeoGeoBox.from_bbox(
        (-2_000_000, -5_000_000, 2_250_000, -1_000_000),
        "epsg:3577",
        resolution=1000
    )
    legacy_gbox = LegacyGeoGeoBox(width=odc_gbox.width, height=odc_gbox.height,
                                  affine=odc_gbox.affine,
                                  crs=LegacyCRS(str(odc_gbox.crs)))

    assert legacy_gbox != odc_gbox

    xra = xr_zeros(odc_gbox, chunks=(1000, 1000))

    assert xra.odc.geobox == odc_gbox

    gbox = output_geobox(like=xra)
    assert gbox == odc_gbox

    gbox = output_geobox(like=legacy_gbox)
    assert gbox == odc_gbox
    assert isinstance(gbox.crs, ODCGeoCRS)
