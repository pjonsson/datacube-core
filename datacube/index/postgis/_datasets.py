# This file is part of the Open Data Cube, see https://opendatacube.org for more information
#
# Copyright (c) 2015-2024 ODC Contributors
# SPDX-License-Identifier: Apache-2.0
"""
API for dataset indexing, access and search.
"""
import datetime
import json
import logging
import warnings
from collections import namedtuple
from time import monotonic
from typing import Iterable, Mapping, Union, Optional, Any, NamedTuple, Sequence, cast
from uuid import UUID

from deprecat import deprecat

from datacube.drivers.postgis._fields import SimpleDocField, PgField, PgExpression
from datacube.drivers.postgis._schema import Dataset as SQLDataset, search_field_map
from datacube.drivers.postgis._api import non_native_fields, extract_dataset_fields, mk_simple_offset_field
from datacube.utils.uris import split_uri
from datacube.drivers.postgis._spatial import generate_dataset_spatial_values, extract_geometry_from_eo3_projection
from datacube.migration import ODC2DeprecationWarning
from datacube.index.abstract import AbstractDatasetResource, DSID, BatchStatus, DatasetTuple, DatasetSpatialMixin
from datacube.utils.documents import JsonDict
from datacube.model._base import QueryField
from datacube.index.postgis._transaction import IndexResourceAddIn
from datacube.model import Dataset, Product, Range, LineageTree
from datacube.model.fields import Field
from datacube.utils import jsonify_document, _readable_offset, changes
from datacube.utils.changes import get_doc_changes, Offset
from odc.geo import CRS, Geometry
from datacube.index import fields, extract_geom_from_query, strip_all_spatial_fields_from_query

_LOG = logging.getLogger(__name__)


# It's a public api, so we can't reorganise old methods.
# pylint: disable=too-many-public-methods, too-many-lines


class DatasetResource(AbstractDatasetResource, IndexResourceAddIn):
    """
    :type _db: datacube.drivers.postgis._connections.PostgresDb
    :type products: datacube.index._products.ProductResource
    """

    def __init__(self, db, index):
        """
        :type db: datacube.drivers.postgis._connections.PostgresDb
        :type product_resource: datacube.index._products.ProductResource
        """
        self._db = db
        super().__init__(index)

    def get_unsafe(self, id_: DSID,
                   include_sources: bool = False, include_deriveds: bool = False, max_depth: int = 0) -> Dataset:
        """
        Get dataset by id (raise KeyError if not found)

        :param id_: id of the dataset to retrieve
        :param include_sources: include the full provenance tree for the dataset.
        :param include_deriveds: include the full derivative tree for the dataset.
        :param max_depth: The maximum depth of the source and/or derived tree.  Defaults to 0, meaning no limit.
        :rtype: Dataset model (None if not found)
        """
        if isinstance(id_, str):
            id_ = UUID(id_)

        source_tree = derived_tree = None
        if include_sources:
            source_tree = self._index.lineage.get_source_tree(id_, max_depth=max_depth)
        if include_deriveds:
            derived_tree = self._index.lineage.get_derived_tree(id_, max_depth=max_depth)

        with self._db_connection() as connection:
            dataset = connection.get_dataset(id_)
            if not dataset:
                raise KeyError(id_)
            return self._make(dataset, full_info=True, source_tree=source_tree, derived_tree=derived_tree)

    def bulk_get(self, ids):
        def to_uuid(x):
            return x if isinstance(x, UUID) else UUID(x)

        ids = [to_uuid(i) for i in ids]

        with self._db_connection() as connection:
            rows = connection.get_datasets(ids)
            return [self._make(r, full_info=True) for r in rows]

    @deprecat(
        reason="The 'get_derived' static method is deprecated in favour of the new lineage API.",
        version='1.9.0',
        category=ODC2DeprecationWarning)
    def get_derived(self, id_):
        """
        Get all derived datasets

        :param Union[str,UUID] id_: dataset id
        :rtype: list[Dataset]
        """
        if not isinstance(id_, UUID):
            id_ = UUID(id_)
        with self._db_connection() as connection:
            return [
                self._make(result, full_info=True)
                for result in connection.get_derived_datasets(id_)
            ]

    def has(self, id_):
        """
        Have we already indexed this dataset?

        :param typing.Union[UUID, str] id_: dataset id
        :rtype: bool
        """
        with self._db_connection() as connection:
            return connection.contains_dataset(id_)

    def bulk_has(self, ids_):
        """
        Like `has` but operates on a list of ids.

        For every supplied id check if database contains a dataset with that id.

        :param [typing.Union[UUID, str]] ids_: list of dataset ids

        :rtype: [bool]
        """
        with self._db_connection() as connection:
            existing = set(connection.datasets_intersection(ids_))

        return [x in existing for x in
                map((lambda x: UUID(x) if isinstance(x, str) else x), ids_)]

    def add(self, dataset: Dataset,
            with_lineage: bool = True, archive_less_mature: Optional[int] = None) -> Dataset:
        """
        Add ``dataset`` to the index. No-op if it is already present.

        :param dataset: dataset to add

        :param with_lineage:
           - no effect in this index driver

        :param archive_less_mature: if integer, search for less
               mature versions of the dataset with the int value as a millisecond
               delta in timestamp comparison

        :rtype: Dataset
        """
        _LOG.info('Indexing %s', dataset.id)

        if self.has(dataset.id):
            _LOG.warning('Dataset %s is already in the database', dataset.id)
            return dataset
        with self._db_connection(transaction=True) as transaction:
            # 1a. insert (if not already exists)
            product_id = dataset.product.id
            if product_id is None:
                # don't assume the product has an id value since it's optional
                # but we should error if the product doesn't exist in the db
                product_id = self.products.get_by_name_unsafe(dataset.product.name).id
            is_new = transaction.insert_dataset(dataset.metadata_doc_without_lineage(), dataset.id, product_id)
            if is_new:
                # 1b. Prepare spatial index extents
                transaction.update_spindex(dsids=[dataset.id])
                transaction.update_search_index(dsids=[dataset.id])
                # 1c. Store locations
                if dataset.uri is not None:
                    if dataset.has_multiple_uris():
                        raise ValueError('Postgis driver does not support multiple locations for a dataset.')
                    self._ensure_new_locations(dataset, transaction=transaction)
            if archive_less_mature is not None:
                self.archive_less_mature(dataset, archive_less_mature)
            if dataset.source_tree is not None:
                self._index.lineage.add(dataset.source_tree)
            if dataset.derived_tree is not None:
                self._index.lineage.add(dataset.derived_tree)

        return dataset

    def _init_bulk_add_cache(self):
        return {}

    def _add_batch(self, batch_ds: Iterable[DatasetTuple], cache: Mapping[str, Any]) -> BatchStatus:
        # Add a "batch" of datasets.
        b_started = monotonic()
        crses = self._db.spatially_indexed_crses()

        class BatchRep(NamedTuple):
            datasets: list[dict[str, Any]]
            uris: list[dict[str, Any]]
            search_indexes: dict[str, list[dict[str, Any]]]
            spatial_indexes: dict[CRS, list[dict[str, Any]]]

        batch = BatchRep(
            datasets=[], uris=[],
            search_indexes={
                "string": [],
                "numeric": [],
                "datetime": []
            },
            spatial_indexes={crs: [] for crs in crses}
        )
        dsids = []
        for prod, metadata_doc, uri in batch_ds:
            dsid = UUID(str(metadata_doc["id"]))
            dsids.append(dsid)
            if isinstance(uri, list):
                uri = uri[0]
            scheme, body = split_uri(uri)
            batch.datasets.append(
                {
                    "id": dsid,
                    "product_ref": prod.id,
                    "metadata": metadata_doc,
                    "metadata_type_ref": prod.metadata_type.id,
                    "uri_scheme": scheme,
                    "uri_body": body,
                }
            )
            extent = extract_geometry_from_eo3_projection(
                metadata_doc["grid_spatial"]["projection"]  # type: ignore[misc,call-overload,index]
            )
            if extent:
                for crs in crses:
                    values = generate_dataset_spatial_values(dsid, crs, extent)
                    if values is not None:
                        batch.spatial_indexes[crs].append(values)
            if prod.metadata_type.name in cache:
                search_fields = cache[prod.metadata_type.name]
            else:
                search_fields = non_native_fields(prod.metadata_type.definition)
                cache[prod.metadata_type.name] = search_fields  # type: ignore[index]
            search_field_vals = extract_dataset_fields(metadata_doc, search_fields)
            for fname, finfo in search_field_vals.items():
                ftype, fval = finfo
                if isinstance(fval, Range):
                    fval = list(fval)
                search_key = search_field_map[ftype]
                batch.search_indexes[search_key].append({
                    "dataset_ref": dsid,
                    "search_key": fname,
                    "search_val": fval
                })
        with self._db_connection(transaction=True) as connection:
            if batch.datasets:
                b_added, b_skipped = connection.insert_dataset_bulk(batch.datasets)
            for crs in crses:
                crs_values = batch.spatial_indexes[crs]
                if crs_values:
                    connection.insert_dataset_spatial_bulk(crs, crs_values)
            for search_type, values in batch.search_indexes.items():
                connection.insert_dataset_search_bulk(search_type, values)
        return BatchStatus(b_added, b_skipped, monotonic() - b_started)

    def search_product_duplicates(self, product: Product, *args):
        """
        Find dataset ids who have duplicates of the given set of field names.

        Product is always inserted as the first grouping field.

        Returns each set of those field values and the datasets that have them.
        """
        dataset_fields = product.metadata_type.dataset_fields

        def load_field(f: Union[str, fields.Field]) -> fields.Field:
            if isinstance(f, str):
                return dataset_fields[f]
            assert isinstance(f, fields.Field), "Not a field: %r" % (f,)
            return f

        group_fields = [cast(PgField, load_field(f)) for f in args]
        expressions = [cast(PgExpression, dataset_fields.get('product') == product.name)]

        with self._db_connection() as connection:
            for record in connection.get_duplicates(group_fields, expressions):
                if 'ids' in record:
                    ids = record.pop('ids')
                    yield namedtuple('search_result', record.keys())(**record), set(ids)

    def can_update(self, dataset, updates_allowed=None):
        """
        Check if dataset can be updated. Return bool,safe_changes,unsafe_changes

        :param Dataset dataset: Dataset to update
        :param dict updates_allowed: Allowed updates
        :rtype: bool,list[change],list[change]
        """
        need_sources = dataset.sources is not None
        # TODO: Source retrieval is broken.
        need_sources = False
        existing = self.get(dataset.id, include_sources=need_sources)
        if not existing:
            raise ValueError('Unknown dataset %s, cannot update – did you intend to add it?' % dataset.id)

        if dataset.product.name != existing.product.name:
            raise ValueError('Changing product is not supported. From %s to %s in %s' % (existing.product.name,
                                                                                         dataset.product.name,
                                                                                         dataset.id))

        if dataset.has_multiple_uris():
            raise ValueError('Postgis driver does not support multiple locations for a dataset.')

        # TODO: figure out (un)safe changes from metadata type?
        allowed = {
            # can always add more metadata
            tuple(): changes.allow_extension,
        }
        allowed.update(updates_allowed or {})

        doc_changes = get_doc_changes(existing.metadata_doc, jsonify_document(dataset.metadata_doc))
        good_changes, bad_changes = changes.classify_changes(doc_changes, allowed)

        return not bad_changes, good_changes, bad_changes

    def update(self, dataset: Dataset, updates_allowed=None, archive_less_mature=False):
        """
        Update dataset metadata and location
        :param Dataset dataset: Dataset to update
        :param updates_allowed: Allowed updates
        :param archive_less_mature: if integer, search for less
               mature versions of the dataset with the int value as a millisecond
               delta in timestamp comparison
        :rtype: Dataset
        """
        can_update, safe_changes, unsafe_changes = self.can_update(dataset, updates_allowed)

        if not safe_changes and not unsafe_changes:
            self._ensure_new_locations(dataset)
            _LOG.info("No changes detected for dataset %s", dataset.id)
            return dataset

        for offset, old_val, new_val in safe_changes:
            _LOG.info("Safe change in %s from %r to %r", _readable_offset(offset), old_val, new_val)

        for offset, old_val, new_val in unsafe_changes:
            _LOG.warning("Unsafe change in %s from %r to %r", _readable_offset(offset), old_val, new_val)

        if not can_update:
            raise ValueError(f"Unsafe changes in {dataset.id}: " + (
                ", ".join(
                    _readable_offset(offset)
                    for offset, _, _ in unsafe_changes
                )
            ))

        _LOG.info("Updating dataset %s", dataset.id)

        product = self.products.get_by_name(dataset.product.name)
        with self._db_connection(transaction=True) as transaction:
            if not transaction.update_dataset(dataset.metadata_doc_without_lineage(), dataset.id, product.id):
                raise ValueError("Failed to update dataset %s..." % dataset.id)
            transaction.update_spindex(dsids=[dataset.id])
            transaction.update_search_index(dsids=[dataset.id])
            if archive_less_mature is not None:
                self.archive_less_mature(dataset, archive_less_mature)

        self._ensure_new_locations(dataset)

        return dataset

    def _ensure_new_locations(self, dataset, transaction=None):
        if transaction:
            transaction.insert_dataset_location(dataset.id, dataset.uri)
        else:
            with self._db_connection(transaction=True) as tr:
                tr.insert_dataset_location(dataset.id, dataset.uri)

    def archive(self, ids):
        """
        Mark datasets as archived

        :param Iterable[UUID] ids: list of dataset ids to archive
        """
        with self._db_connection(transaction=True) as transaction:
            for id_ in ids:
                transaction.archive_dataset(id_)

    def restore(self, ids):
        """
        Mark datasets as not archived

        :param Iterable[UUID] ids: list of dataset ids to restore
        """
        with self._db_connection(transaction=True) as transaction:
            for id_ in ids:
                transaction.restore_dataset(id_)

    def purge(self, ids: Iterable[DSID], allow_delete_active: bool = False) -> Sequence[DSID]:
        """
        Delete datasets

        :param ids: iterable of dataset ids to purge
        :param allow_delete_active: whether active datasets can be deleted
        :return: list of purged dataset ids
        """
        purged = []
        with self._db_connection(transaction=True) as transaction:
            for id_ in ids:
                ds = self.get(id_)
                if ds is None:
                    continue
                if not ds.is_archived and not allow_delete_active:
                    _LOG.warning(f"Cannot purge unarchived dataset: {id_}")
                    continue
                transaction.delete_dataset(id_)
                purged.append(id_)

        return purged

    def get_all_dataset_ids(self, archived: bool | None = False):
        """
        Get list of all dataset IDs based only on archived status

        This will be very slow and inefficient for large databases, and is really
        only intended for small and/or experimental databases.

        :param archived:
        :rtype: list[UUID]
        """
        with self._db_connection(transaction=True) as transaction:
            return [dsid[0] for dsid in transaction.all_dataset_ids(archived)]

    @deprecat(
        reason="Multiple locations per dataset are now deprecated.  Please use the 'get_location' method.",
        version="1.9.0",
        category=ODC2DeprecationWarning
    )
    def get_locations(self, id_):
        """
        Get the list of storage locations for the given dataset id

        :param typing.Union[UUID, str] id_: dataset id
        :rtype: list[str]
        """
        return [self.get_location(id_)]

    def get_location(self, id_):
        """
        Get the list of storage locations for the given dataset id

        :param typing.Union[UUID, str] id_: dataset id
        :rtype: list[str]
        """
        with self._db_connection() as connection:
            location = connection.get_location(id_)
            if location:
                return location[0]
            else:
                return None

    @deprecat(
        reason="Multiple locations per dataset are now deprecated. "
               "Archived locations may not be accessible in future releases.",
        version="1.9.0",
        category=ODC2DeprecationWarning
    )
    def get_archived_locations(self, id_):
        """
        Find locations which have been archived for a dataset

        :param typing.Union[UUID, str] id_: dataset id
        :rtype: list[str]
        """
        return []

    @deprecat(
        reason="Multiple locations per dataset are now deprecated. "
               "Archived locations may not be accessible in future releases.",
        version="1.9.0",
        category=ODC2DeprecationWarning
    )
    def get_archived_location_times(self, id_):
        """
        Get each archived location along with the time it was archived.

        :param typing.Union[UUID, str] id_: dataset id
        :rtype: list[Tuple[str, datetime.datetime]]
        """
        return []

    @deprecat(
        reason="Multiple locations per dataset are now deprecated. "
               "Dataset location can be set or updated with the update() method.",
        version="1.9.0",
        category=ODC2DeprecationWarning
    )
    def add_location(self, id_, uri):
        """
        Add a location to the dataset if it doesn't already exist.

        :param typing.Union[UUID, str] id_: dataset id
        :param str uri: fully qualified uri
        :returns bool: Was one added?
        """
        if not uri:
            warnings.warn("Cannot add empty uri. (dataset %s)" % id_)
            return False

        existing = self.get_location(id_)
        if existing == uri:
            warnings.warn(f"Dataset {id_} already has uri {uri}")
            return False
        elif existing is not None and existing != uri:
            raise ValueError("Postgis index does not support multiple dataset locations.")

        with self._db_connection() as connection:
            return connection.insert_dataset_location(id_, uri)

    def get_datasets_for_location(self, uri, mode=None):
        """
        Find datasets that exist at the given URI

        :param uri: search uri
        :param str mode: 'exact', 'prefix' or None (to guess)
        :return:
        """
        with self._db_connection() as connection:
            return (self._make(row) for row in connection.get_datasets_for_location(uri, mode=mode))

    @deprecat(
        reason="Multiple locations per dataset are now deprecated. "
               "Dataset location can be set or updated with the update() method.",
        version="1.9.0",
        category=ODC2DeprecationWarning
    )
    def remove_location(self, id_, uri):
        """
        Remove a location from the dataset if it exists.

        :param typing.Union[UUID, str] id_: dataset id
        :param str uri: fully qualified uri
        :returns bool: Was one removed?
        """
        with self._db_connection() as connection:
            was_removed = connection.remove_location(id_, uri)
            return was_removed

    @deprecat(
        reason="The PostGIS index does not support archived locations. "
               "Dataset location can be set or updated with the update() method.",
        version="1.9.0",
        category=ODC2DeprecationWarning
    )
    def archive_location(self, id_, uri):
        """
        Archive a location of the dataset if it exists.

        :param typing.Union[UUID, str] id_: dataset id
        :param str uri: fully qualified uri
        :return bool: location was able to be archived
        """
        return False

    @deprecat(
        reason="The PostGIS index does not support archived locations. "
               "Dataset location can be set or updated with the update() method.",
        version="1.9.0",
        category=ODC2DeprecationWarning
    )
    def restore_location(self, id_, uri):
        """
        Un-archive a location of the dataset if it exists.

        :param typing.Union[UUID, str] id_: dataset id
        :param str uri: fully qualified uri
        :return bool: location was able to be restored
        """
        return False

    def _make(self, dataset_res, full_info=False, product=None,
              source_tree: LineageTree | None = None,
              derived_tree: LineageTree | None = None):
        """
        :rtype Dataset

        :param bool full_info: Include all available fields
        """
        kwargs = {}
        if not isinstance(dataset_res, dict):
            dataset_res = dataset_res._asdict()
        if "uri" in dataset_res:
            kwargs["uri"] = dataset_res["uri"]

        return Dataset(
            product=product or self.products.get(dataset_res["product_id"]),
            metadata_doc=dataset_res["metadata_doc"],
            indexed_by=dataset_res["indexed_by"] if full_info else None,
            indexed_time=dataset_res["indexed_time"] if full_info else None,
            archived_time=dataset_res["archived"],
            source_tree=source_tree,
            derived_tree=derived_tree,
            **kwargs
        )

    def _make_many(self, query_result, product=None):
        """
        :rtype list[Dataset]
        """
        return (self._make(dataset, product=product) for dataset in query_result)

    def search_by_metadata(self, metadata: JsonDict, archived: bool | None = False):
        """
        Perform a search using arbitrary metadata, returning results as Dataset objects.

        Caution – slow! This will usually not use indexes.

        :param dict metadata:
        :rtype: list[Dataset]
        """
        with self._db_connection() as connection:
            for dataset in self._make_many(connection.search_datasets_by_metadata(metadata, archived)):
                yield dataset

    @deprecat(
        deprecated_args={
            "source_filter": {
                "reason": "Filtering by source metadata is deprecated and will be removed in future.",
                "version": "1.9.0",
                "category": ODC2DeprecationWarning

            }
        }
    )
    def search(self, limit=None, archived: bool | None = False, order_by=None, **query):
        """
        Perform a search, returning results as Dataset objects.

        :param Union[str,float,Range,list] query:
        :param int limit: Limit number of datasets
        :param Iterable[str|Field|Function] order_by:
        :rtype: __generator[Dataset]
        """
        source_filter = query.pop('source_filter', None)
        for product, datasets in self._do_search_by_product(query,
                                                            source_filter=source_filter,
                                                            limit=limit,
                                                            archived=archived,
                                                            order_by=order_by):
            yield from self._make_many(datasets, product)

    def search_by_product(self, archived: bool | None = False, **query):
        """
        Perform a search, returning datasets grouped by product type.

        :param dict[str,str|float|datacube.model.Range] query:
        :rtype: __generator[(Product,  __generator[Dataset])]]
        """
        for product, datasets in self._do_search_by_product(query, archived=archived):
            yield product, self._make_many(datasets, product)

    def search_returning(self,
                         field_names: Iterable[str] | None = None,
                         custom_offsets: Mapping[str, Offset] | None = None,
                         limit: int | None = None,
                         archived: bool | None = False,
                         order_by: Iterable[Any] | None = None,
                         **query: QueryField):
        """
        Perform a search, returning only the specified fields.

        This method can be faster than normal search() if you don't need all fields of each dataset.

        It also allows for returning rows other than datasets, such as a row per uri when requesting field 'uri'.

        :param tuple[str] field_names:
        :param Union[str,float,Range,list] query:
        :param int limit: Limit number of datasets
        :param Iterable[Any] order_by: sql text, dataset field, or sqlalchemy expression
        by which to order results
        :returns __generator[tuple]: sequence of results, each result is a namedtuple of your requested fields
        """
        field_name_d: dict[str, None] = {}
        if field_names is None and custom_offsets is None:
            for f in self._index.products.get_field_names():
                field_name_d[f] = None
        elif field_names:
            for f in field_names:
                field_name_d[f] = None
        if custom_offsets:
            custom_fields = {
                name: mk_simple_offset_field(name, name, offset)
                for name, offset in custom_offsets.items()
            }
            for name in custom_fields:
                field_name_d[name] = None
        else:
            custom_fields = {}

        result_type = namedtuple('search_result', list(field_name_d.keys()))  # type: ignore[misc]

        for _, results in self._do_search_by_product(query,
                                                     return_fields=True,
                                                     select_field_names=list(field_name_d.keys()),
                                                     additional_fields=custom_fields,
                                                     limit=limit,
                                                     archived=archived,
                                                     order_by=order_by):
            for columns in results:
                def extract_field(f):
                    # Custom fields are not type-aware and returned as stringified json.
                    return json.loads(columns.get(f)) if f in custom_fields else columns.get(f)
                kwargs = {f: extract_field(f) for f in field_name_d}
                yield result_type(**kwargs)

    def count(self, archived: bool | None = False, **query):
        """
        Perform a search, returning count of results.

        :param dict[str,str|float|datacube.model.Range] query:
        :rtype: int
        """
        # This may be optimised into one query in the future.
        result = 0
        for product_type, count in self._do_count_by_product(query, archived=archived):
            result += count

        return result

    def count_by_product(self, archived: bool | None = False, **query):
        """
        Perform a search, returning a count of for each matching product type.

        :param dict[str,str|float|datacube.model.Range] query:
        :returns: Sequence of (product, count)
        :rtype: __generator[(Product,  int)]]
        """
        return self._do_count_by_product(query, archived=archived)

    def count_by_product_through_time(self, period, **query):
        """
        Perform a search, returning counts for each product grouped in time slices
        of the given period.

        :param dict[str,str|float|datacube.model.Range] query:
        :param str period: Time range for each slice: '1 month', '1 day' etc.
        :returns: For each matching product type, a list of time ranges and their count.
        :rtype: __generator[(Product, list[(datetime.datetime, datetime.datetime), int)]]
        """
        return self._do_time_count(period, query)

    def count_product_through_time(self, period, **query):
        """
        Perform a search, returning counts for a single product grouped in time slices
        of the given period.

        Will raise an error if the search terms match more than one product.

        :param dict[str,str|float|datacube.model.Range] query:
        :param str period: Time range for each slice: '1 month', '1 day' etc.
        :returns: For each matching product type, a list of time ranges and their count.
        :rtype: list[(str, list[(datetime.datetime, datetime.datetime), int)]]
        """
        return next(self._do_time_count(period, query, ensure_single=True))[1]

    def _get_product_queries(self, query):
        for product, q in self.products.search_robust(**query):
            q['product_id'] = product.id
            yield q, product

    # pylint: disable=too-many-locals
    def _do_search_by_product(self, query, return_fields=False,
                              additional_fields: Mapping[str, Field] | None = None,
                              select_field_names=None,
                              with_source_ids=False, source_filter=None, limit=None,
                              archived: bool | None = False, order_by=None):
        assert not with_source_ids
        assert source_filter is None
        product_queries = list(self._get_product_queries(query))
        if not product_queries:
            product = query.get('product', None)
            if product is None:
                raise ValueError('No products match search terms: %r' % query)
            else:
                raise ValueError(f"No such product: {product}")

        for q, product in product_queries:
            _LOG.warning("Querying product %s", product)
            # Extract Geospatial search geometry
            geom = extract_geom_from_query(**q)
            q = strip_all_spatial_fields_from_query(q)
            dataset_fields = product.metadata_type.dataset_fields
            if additional_fields:
                dataset_fields.update(additional_fields)
            query_exprs = tuple(fields.to_expressions(dataset_fields.get, **q))
            select_fields = None
            if return_fields:
                # if no fields specified, select all
                if select_field_names is None:
                    select_fields = tuple(field for name, field in dataset_fields.items()
                                          if not field.affects_row_selection)
                else:
                    # Allow placeholder columns for requested fields that are not
                    # valid for this product query.
                    select_fields = tuple(dataset_fields[field_name]
                                          for field_name in select_field_names
                                          if field_name in dataset_fields)
            with self._db_connection() as connection:
                yield (product,
                       connection.search_datasets(
                           query_exprs,
                           select_fields=select_fields,
                           limit=limit,
                           with_source_ids=with_source_ids,
                           geom=geom,
                           archived=archived,
                           order_by=order_by
                       ))

    def _do_count_by_product(self, query, archived: bool | None = False):
        product_queries = self._get_product_queries(query)

        for q, product in product_queries:
            geom = extract_geom_from_query(**q)
            q = strip_all_spatial_fields_from_query(q)
            dataset_fields = product.metadata_type.dataset_fields
            query_exprs = tuple(fields.to_expressions(dataset_fields.get, **q))
            with self._db_connection() as connection:
                count = connection.count_datasets(query_exprs, archived=archived, geom=geom)
            if count > 0:
                yield product, count

    def _do_time_count(self, period, query, ensure_single=False):
        if 'time' not in query:
            raise ValueError('Counting through time requires a "time" range query argument')

        query = dict(query)

        start, end = query['time']
        del query['time']

        product_queries = list(self._get_product_queries(query))
        if ensure_single:
            if len(product_queries) == 0:
                raise ValueError('No products match search terms: %r' % query)
            if len(product_queries) > 1:
                raise ValueError('Multiple products match single query search: %r' %
                                 ([dt.name for q, dt in product_queries],))

        for q, product in product_queries:
            dataset_fields = product.metadata_type.dataset_fields
            query_exprs = tuple(fields.to_expressions(dataset_fields.get, **q))
            with self._db_connection() as connection:
                yield product, list(connection.count_datasets_through_time(
                    start,
                    end,
                    period,
                    dataset_fields.get('time'),
                    query_exprs
                ))

    @deprecat(
        reason="This method is deprecated and will be removed in 2.0.  "
               "Consider migrating to search_returning()",
        version="1.9.0",
        category=ODC2DeprecationWarning
    )
    def search_summaries(self, archived: bool | None = False, **query):
        """
        Perform a search, returning just the search fields of each dataset.

        :param dict[str,str|float|datacube.model.Range] query:
        :rtype: __generator[dict]
        """
        for _, results in self._do_search_by_product(query, return_fields=True, archived=archived):
            for columns in results:
                output = columns._asdict()
                _LOG.warning("search results: %s (%s)", output["id"], output["product"])
                yield output

    @deprecat(
        reason="This method is deprecated and will be removed in 2.0.  "
               "Consider migrating to search_returning()",
        version="1.9.0",
        category=ODC2DeprecationWarning
    )
    # pylint: disable=redefined-outer-name
    def search_returning_datasets_light(self, field_names: tuple, custom_offsets=None,
                                        limit=None, archived: bool | None = False,
                                        **query):
        """
        This is a dataset search function that returns the results as objects of a dynamically
        generated Dataset class that is a subclass of tuple.

        Only the requested fields will be returned together with related derived attributes as property functions
        similar to the datacube.model.Dataset class. For example, if 'extent'is requested all of
        'crs', 'extent', 'transform', and 'bounds' are available as property functions.

        The field_names can be custom fields in addition to those specified in metadata_type, fixed fields, or
        native fields. The field_names can also be derived fields like 'extent', 'crs', 'transform',
        and 'bounds'. The custom fields require custom offsets of the metadata doc be provided.

        The datasets can be selected based on values of custom fields as long as relevant custom
        offsets are provided. However custom field values are not transformed so must match what is
        stored in the database.

        :param field_names: A tuple of field names that would be returned including derived fields
                            such as extent, crs
        :param custom_offsets: A dictionary of offsets in the metadata doc for custom fields
        :param limit: Number of datasets returned per product.
        :param query: key, value mappings of query that will be processed against metadata_types,
                      product definitions and/or dataset table.
        :return: A Dynamically generated DatasetLight (a subclass of namedtuple and possibly with
        property functions).
        """

        assert field_names

        for product, query_exprs in self.make_query_expr(query, custom_offsets):

            select_fields = self.make_select_fields(product, field_names, custom_offsets)
            select_field_names = tuple(field.name for field in select_fields)
            result_type = namedtuple('DatasetLight', select_field_names)  # type: ignore

            if 'grid_spatial' in select_field_names:
                class DatasetLight(result_type, DatasetSpatialMixin):
                    pass
            else:
                class DatasetLight(result_type):  # type: ignore
                    __slots__ = ()

            with self._db_connection() as connection:
                results = connection.search_datasets(
                    query_exprs,
                    select_fields=select_fields,
                    limit=limit,
                    archived=archived
                )

            for result in results:
                field_values = dict()
                for i_, field in enumerate(select_fields):
                    # We need to load the simple doc fields
                    if isinstance(field, SimpleDocField):
                        field_values[field.name] = json.loads(result[i_])
                    else:
                        field_values[field.name] = result[i_]

                yield DatasetLight(**field_values)  # type: ignore

    def make_select_fields(self, product, field_names, custom_offsets):
        """
        Parse and generate the list of select fields to be passed to the database API.
        """

        assert product and field_names

        dataset_fields = product.metadata_type.dataset_fields
        dataset_section = product.metadata_type.definition['dataset']

        select_fields = []
        for field_name in field_names:
            if dataset_fields.get(field_name):
                select_fields.append(dataset_fields[field_name])
            else:
                # try to construct the field
                if field_name in {'transform', 'extent', 'crs', 'bounds'}:
                    grid_spatial = dataset_section.get('grid_spatial')
                    if grid_spatial:
                        select_fields.append(SimpleDocField(
                            'grid_spatial', 'grid_spatial', SQLDataset.metadata_doc,
                            False,
                            offset=grid_spatial
                        ))
                elif custom_offsets and field_name in custom_offsets:
                    select_fields.append(SimpleDocField(
                        field_name, field_name, SQLDataset.metadata_doc,
                        False,
                        offset=custom_offsets[field_name]
                    ))
                elif field_name == 'uris':
                    select_fields.append(Field('uris', 'uris'))

        return select_fields

    def make_query_expr(self, query, custom_offsets):
        """
        Generate query expressions including queries based on custom fields
        """

        product_queries = list(self._get_product_queries(query))
        custom_query = dict()
        if not product_queries:
            # The key, values in query that are un-machable with info
            # in metadata types and product definitions, perhaps there are custom
            # fields, will need to handle custom fields separately

            canonical_query = query.copy()
            custom_query = {key: canonical_query.pop(key) for key in custom_offsets
                            if key in canonical_query}
            product_queries = list(self._get_product_queries(canonical_query))

            if not product_queries:
                raise ValueError('No products match search terms: %r' % query)

        for q, product in product_queries:
            dataset_fields = product.metadata_type.dataset_fields
            query_exprs = tuple(fields.to_expressions(dataset_fields.get, **q))
            custom_query_exprs = tuple(self.get_custom_query_expressions(custom_query, custom_offsets))

            yield product, query_exprs + custom_query_exprs

    def get_custom_query_expressions(self, custom_query, custom_offsets):
        """
        Generate query expressions for custom fields. it is assumed that custom fields are to be found
        in metadata doc and their offsets are provided. custom_query is a dict of key fields involving
        custom fields.
        """
        custom_exprs = []
        for key in custom_query:
            # for now we assume all custom query fields are SimpleDocFields
            custom_field = SimpleDocField(
                custom_query[key], custom_query[key], Dataset.metadata,
                False, offset=custom_offsets[key]
            )
            custom_exprs.append(fields.as_expression(custom_field, custom_query[key]))

        return custom_exprs

    def temporal_extent(self, ids: Iterable[DSID]) -> tuple[datetime.datetime, datetime.datetime]:
        """
        Returns the minimum and maximum acquisition time of the specified datasets.
        """
        with self._db_connection() as connection:
            return connection.temporal_extent_by_ids(ids)

    def spatial_extent(self, ids: Iterable[DSID], crs: CRS = CRS("EPSG:4326")) -> Geometry | None:
        with self._db_connection() as connection:
            return connection.spatial_extent(ids, crs)

    def get_all_docs_for_product(self, product: Product, batch_size: int = 1000) -> Iterable[DatasetTuple]:
        local_product = self.products.get_by_name(product.name)
        product_search_key = [local_product.id]
        with self._db_connection(transaction=True) as connection:
            for row in connection.bulk_simple_dataset_search(products=product_search_key, batch_size=batch_size):
                prod_id, metadata_doc, uris = tuple(row)
                yield DatasetTuple(product, metadata_doc, uris)
