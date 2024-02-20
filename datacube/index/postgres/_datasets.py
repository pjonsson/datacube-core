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
from typing import Iterable, List, Union, Mapping, Any, Optional
from uuid import UUID
from deprecat import deprecat

from datacube.drivers.postgres._fields import SimpleDocField
from datacube.drivers.postgres._schema import DATASET
from datacube.index.abstract import (AbstractDatasetResource, DatasetSpatialMixin, DSID,
                                     DatasetTuple, BatchStatus)
from datacube.index.postgres._transaction import IndexResourceAddIn
from datacube.model import Dataset, Product
from datacube.model.fields import Field
from datacube.model.utils import flatten_datasets
from datacube.utils import jsonify_document, _readable_offset, changes
from datacube.utils.changes import get_doc_changes
from datacube.index import fields
from datacube.drivers.postgres._api import split_uri
from datacube.migration import ODC2DeprecationWarning

_LOG = logging.getLogger(__name__)


# It's a public api, so we can't reorganise old methods.
# pylint: disable=too-many-public-methods, too-many-lines
class DatasetResource(AbstractDatasetResource, IndexResourceAddIn):
    """
    :type _db: datacube.drivers.postgres._connections.PostgresDb
    """

    def __init__(self, db, index):
        """
        :type db: datacube.drivers.postgres._connections.PostgresDb
        :type dataset_type_resource: datacube.index._products.ProductResource
        """
        self._db = db
        super().__init__(index)

    def get_unsafe(self, id_: DSID, include_sources: bool = False,
                   include_deriveds: bool = False, max_depth: int = 0) -> Dataset:
        """
        Get dataset by id (raise KeyError if not in index)

        :param UUID id_: id of the dataset to retrieve
        :param bool include_sources: get the full provenance graph?
        :rtype: Dataset
        """
        # include_derived and max_depth arguments not supported.
        self._check_get_legacy(include_deriveds, max_depth)
        if isinstance(id_, str):
            id_ = UUID(id_)

        with self._db_connection() as connection:
            if not include_sources:
                dataset = connection.get_dataset(id_)
                return self._make(dataset, full_info=True) if dataset else None

            datasets = {result.id: (self._make(result, full_info=True), result)
                        for result in connection.get_dataset_sources(id_)}

        if not datasets:
            # No dataset found
            raise KeyError(id_)

        for dataset, result in datasets.values():
            dataset.metadata.sources = {
                classifier: datasets[source][0].metadata_doc
                for source, classifier in zip(result.sources, result.classes) if source
            }
            dataset.sources = {
                classifier: datasets[source][0]
                for source, classifier in zip(result.sources, result.classes) if source
            }
        return datasets[id_][0]

    def bulk_get(self, ids):
        def to_uuid(x):
            return x if isinstance(x, UUID) else UUID(x)

        ids = [to_uuid(i) for i in ids]

        with self._db_connection() as connection:
            rows = connection.get_datasets(ids)
            return [self._make(r, full_info=True) for r in rows]

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
           - ``True (default)`` attempt adding lineage datasets if missing
           - ``False`` record lineage relations, but do not attempt
             adding lineage datasets to the db

        :param archive_less_mature: if integer, search for less
               mature versions of the dataset with the int value as a millisecond
               delta in timestamp comparison

        :rtype: Dataset
        """

        def process_bunch(dss, main_ds, transaction):
            edges = []

            # First insert all new datasets
            for ds in dss:
                is_new = transaction.insert_dataset(ds.metadata_doc_without_lineage(), ds.id, ds.product.id)
                sources = ds.sources
                if is_new and sources is not None:
                    edges.extend((name, ds.id, src.id)
                                 for name, src in sources.items())

            # Second insert lineage graph edges
            for ee in edges:
                transaction.insert_dataset_source(*ee)

            # Finally update location for top-level dataset only
            if main_ds.uri is not None:
                self._ensure_new_locations(main_ds, transaction=transaction)

        _LOG.info('Indexing %s', dataset.id)

        if with_lineage:
            ds_by_uuid = flatten_datasets(dataset)
            all_uuids = list(ds_by_uuid)

            present = {k: v for k, v in zip(all_uuids, self.bulk_has(all_uuids))}

            if present[dataset.id]:
                _LOG.warning('Dataset %s is already in the database', dataset.id)
                return dataset

            dss = [ds for ds in [dss[0] for dss in ds_by_uuid.values()] if not present[ds.id]]
        else:
            if self.has(dataset.id):
                _LOG.warning('Dataset %s is already in the database', dataset.id)
                return dataset

            dss = [dataset]

        with self._db_connection(transaction=True) as transaction:
            process_bunch(dss, dataset, transaction)
            if archive_less_mature is not None:
                self.archive_less_mature(dataset, archive_less_mature)

        return dataset

    def _add_batch(self, batch_ds: Iterable[DatasetTuple], cache: Mapping[str, Any]) -> BatchStatus:
        b_started = monotonic()
        batch = {
            "datasets": [],
            "uris": [],
        }
        for prod, metadata_doc, uris in batch_ds:
            dsid = UUID(metadata_doc["id"])
            batch["datasets"].append(
                {
                    "id": dsid,
                    "dataset_type_ref": prod.id,
                    "metadata": metadata_doc,
                    "metadata_type_ref": prod.metadata_type.id
                }
            )
            for uri in uris:
                scheme, body = split_uri(uri)
                batch["uris"].append(
                    {
                        "dataset_ref": dsid,
                        "uri_scheme": scheme,
                        "uri_body": body,
                    }
                )
        with self._db_connection(transaction=True) as connection:
            if batch["datasets"]:
                b_added, b_skipped = connection.insert_dataset_bulk(batch["datasets"])
            if batch["uris"]:
                connection.insert_dataset_location_bulk(batch["uris"])
        return BatchStatus(b_added, b_skipped, monotonic() - b_started)

    def search_product_duplicates(self, product: Product, *args):
        """
        Find dataset ids who have duplicates of the given set of field names.

        Product is always inserted as the first grouping field.

        Returns each set of those field values and the datasets that have them.
        """

        def load_field(f: Union[str, fields.Field]) -> fields.Field:
            if isinstance(f, str):
                return product.metadata_type.dataset_fields[f]
            assert isinstance(f, fields.Field), "Not a field: %r" % (f,)
            return f

        group_fields: List[fields.Field] = [load_field(f) for f in args]
        expressions = [product.metadata_type.dataset_fields.get('product') == product.name]

        with self._db_connection() as connection:
            for record in connection.get_duplicates(group_fields, expressions):
                as_dict = record._asdict()
                if "ids" in as_dict.keys():
                    ids = as_dict.pop('ids')
                    yield namedtuple('search_result', as_dict.keys())(**as_dict), set(ids)

    def can_update(self, dataset, updates_allowed=None):
        """
        Check if dataset can be updated. Return bool,safe_changes,unsafe_changes

        :param Dataset dataset: Dataset to update
        :param dict updates_allowed: Allowed updates
        :rtype: bool,list[change],list[change]
        """
        need_sources = dataset.sources is not None
        existing = self.get(dataset.id, include_sources=need_sources)
        if not existing:
            raise ValueError('Unknown dataset %s, cannot update – did you intend to add it?' % dataset.id)

        if dataset.product.name != existing.product.name:
            raise ValueError('Changing product is not supported. From %s to %s in %s' % (existing.product.name,
                                                                                         dataset.product.name,
                                                                                         dataset.id))

        # TODO: figure out (un)safe changes from metadata type?
        allowed = {
            # can always add more metadata
            tuple(): changes.allow_extension,
        }
        allowed.update(updates_allowed or {})

        doc_changes = get_doc_changes(existing.metadata_doc, jsonify_document(dataset.metadata_doc))
        good_changes, bad_changes = changes.classify_changes(doc_changes, allowed)

        return not bad_changes, good_changes, bad_changes

    def update(self, dataset: Dataset, updates_allowed=None, archive_less_mature: Optional[int] = None):
        """
        Update dataset metadata and location
        :param Dataset dataset: Dataset to update
        :param updates_allowed: Allowed updates
        :param archive_less_mature: if integer, search for less
               mature versions of the dataset with the int value as a millisecond
               delta in timestamp comparison
        :rtype: Dataset
        """
        existing = self.get(dataset.id)
        can_update, safe_changes, unsafe_changes = self.can_update(dataset, updates_allowed)

        if not safe_changes and not unsafe_changes:
            self._ensure_new_locations(dataset, existing)
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
            if archive_less_mature is not None:
                self.archive_less_mature(dataset, archive_less_mature)

        self._ensure_new_locations(dataset, existing)

        return dataset

    def _ensure_new_locations(self, dataset, existing=None, transaction=None):
        old_uris = set()
        if existing:
            old_uris.update(existing._uris)
        new_uris = dataset._uris

        def ensure_locations_in_transaction(old_uris, new_uris, transaction):
            if len(old_uris) <= 1 and len(new_uris) == 1 and new_uris[0] not in old_uris:
                # Only one location, so treat as an update.
                if len(old_uris):
                    transaction.remove_location(dataset.id, old_uris.pop())
                transaction.insert_dataset_location(dataset.id, new_uris[0])
            else:
                for uri in new_uris[::-1]:
                    if uri not in old_uris:
                        transaction.insert_dataset_location(dataset.id, uri)

        if transaction:
            ensure_locations_in_transaction(old_uris, new_uris, transaction)
        else:
            with self._db_connection(transaction=True) as tr:
                ensure_locations_in_transaction(old_uris, new_uris, tr)

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

    def purge(self, ids: Iterable[DSID]):
        """
        Delete archived datasets

        :param ids: iterable of dataset ids to purge
        """
        with self._db_connection(transaction=True) as transaction:
            for id_ in ids:
                transaction.delete_dataset(id_)

    def get_all_dataset_ids(self, archived: bool):
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
        with self._db_connection() as connection:
            return connection.get_locations(id_)

    def get_location(self, id_):
        """
        Get the list of storage locations for the given dataset id

        :param typing.Union[UUID, str] id_: dataset id
        :rtype: list[str]
        """
        with self._db_connection() as connection:
            locations = connection.get_locations(id_)
        if not locations:
            return None
        return locations[0]

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
        with self._db_connection() as connection:
            return [uri for uri, archived_dt in connection.get_archived_locations(id_)]

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
        :rtype: List[Tuple[str, datetime.datetime]]
        """
        with self._db_connection() as connection:
            return list(connection.get_archived_locations(id_))

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
        reason="Multiple locations per dataset are now deprecated. "
               "Archived locations may not be accessible in future releases. "
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
        with self._db_connection() as connection:
            was_archived = connection.archive_location(id_, uri)
            return was_archived

    @deprecat(
        reason="Multiple locations per dataset are now deprecated. "
               "Archived locations may not be restorable in future releases. "
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
        with self._db_connection() as connection:
            was_restored = connection.restore_location(id_, uri)
            return was_restored

    def _make(self, dataset_res, full_info=False, product=None):
        """
        :rtype Dataset

        :param bool full_info: Include all available fields
        """
        if dataset_res.uris:
            if len(dataset_res.uris) > 1:
                # Deprecated legacy code path
                kwargs = {"uris": [uri for uri in dataset_res.uris if uri]}
            else:
                kwargs = {"uri": dataset_res.uris[0]}
        else:
            kwargs = {}

        return Dataset(
            product=product or self.products.get(dataset_res.dataset_type_ref),
            metadata_doc=dataset_res.metadata,
            indexed_by=dataset_res.added_by if full_info else None,
            indexed_time=dataset_res.added if full_info else None,
            archived_time=dataset_res.archived,
            **kwargs
        )

    def _make_many(self, query_result, product=None, fetch_all: bool = False):
        """
        :rtype list[Dataset]
        """
        if fetch_all:
            return [self._make(dataset, product=product) for dataset in query_result]
        else:
            return (self._make(dataset, product=product) for dataset in query_result)

    def search_by_metadata(self, metadata, archived: bool | None = False, fetch_all: bool = False):
        """
        Perform a search using arbitrary metadata, returning results as Dataset objects.

        Caution – slow! This will usually not use indexes.

        :param dict metadata:
        :rtype: list[Dataset]
        """
        if fetch_all:
            results = []
        with self._db_connection() as connection:
            for dataset in self._make_many(connection.search_datasets_by_metadata(metadata, archived)):
                if fetch_all:
                    results.append(dataset)
                else:
                    yield dataset
        if fetch_all:
            return results

    @deprecat(
        deprecated_args={
            "source_filter": {
                "reason": "Filtering by source metadata is deprecated and will be removed in future.",
                "version": "1.9.0",
                "category": ODC2DeprecationWarning

            }
        }
    )
    def search(self, limit=None, source_filter=None, archived: bool | None = False, fetch_all: bool = False, **query):
        """
        Perform a search, returning results as Dataset objects.

        :param Union[str,float,Range,list] query:
        :param int source_filter: query terms against source datasets
        :param int limit: Limit number of datasets
        :rtype: __generator[Dataset]
        """
        if fetch_all:
            results = []
        for product, datasets in self._do_search_by_product(query,
                                                            source_filter=source_filter,
                                                            limit=limit,
                                                            archived=archived):
            if fetch_all:
                results.extend(self._make_many(datasets, product))
            else:
                yield from self._make_many(datasets, product)
        if fetch_all:
            return results

    def search_by_product(self, archived: bool | None = False, fetch_all: bool = False, **query):
        """
        Perform a search, returning datasets grouped by product type.

        :param dict[str,str|float|datacube.model.Range] query:
        :rtype: __generator[(Product,  __generator[Dataset])]]
        """
        if fetch_all:
            results = []
        for product, datasets in self._do_search_by_product(query, archived=archived):
            if fetch_all:
                results.append((product, self._make_many(datasets, product, fetch_all=True)))
            else:
                yield product, self._make_many(datasets, product)
        if fetch_all:
            return results

    def search_returning(self,
                         field_names=None,
                         limit=None, archived: bool | None = False, fetch_all: bool = False,
                         **query):
        """
        Perform a search, returning only the specified fields.

        This method can be faster than normal search() if you don't need all fields of each dataset.

        It also allows for returning rows other than datasets, such as a row per uri when requesting field 'uri'.

        :param tuple[str] field_names: defaults to all known search fields
        :param Union[str,float,Range,list] query:
        :param int limit: Limit number of datasets
        :returns __generator[tuple]: sequence of results, each result is a namedtuple of your requested fields
        """
        if field_names is None:
            field_names = self._index.products.get_field_names()
        result_type = namedtuple('search_result', field_names)
        if fetch_all:
            results = []
        for _, p_results in self._do_search_by_product(query,
                                                       return_fields=True,
                                                       select_field_names=field_names,
                                                       limit=limit,
                                                       archived=archived):
            for columns in p_results:
                coldict = columns._asdict()
                kwargs = {
                    field: coldict.get(field)
                    for field in field_names
                }
                if fetch_all:
                    results.append(result_type(**kwargs))
                else:
                    yield result_type(**kwargs)
        if fetch_all:
            return results

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

    def _get_dataset_types(self, q):
        types = set()
        if 'product' in q.keys():
            types.add(self.products.get_by_name(q['product']))
        else:
            # Otherwise search any metadata type that has all the given search fields.
            types = self.products.get_with_fields(tuple(q.keys()))
            if not types:
                raise ValueError('No type of dataset has fields: {}'.format(q.keys()))

        return types

    def _get_product_queries(self, query):
        for product, q in self.products.search_robust(**query):
            q['dataset_type_id'] = product.id
            yield q, product

    # pylint: disable=too-many-locals
    def _do_search_by_product(self, query, return_fields=False, select_field_names=None,
                              with_source_ids=False, source_filter=None,
                              limit=None,
                              archived: bool | None = False):
        if source_filter:
            product_queries = list(self._get_product_queries(source_filter))
            if not product_queries:
                # No products match our source filter, so there will be no search results regardless.
                raise ValueError('No products match source filter: ' % source_filter)
            if len(product_queries) > 1:
                raise RuntimeError("Multi-product source filters are not supported. Try adding 'product' field")

            source_queries, source_product = product_queries[0]
            dataset_fields = source_product.metadata_type.dataset_fields
            source_exprs = tuple(fields.to_expressions(dataset_fields.get, **source_queries))
        else:
            source_exprs = None

        product_queries = list(self._get_product_queries(query))
        if not product_queries:
            product = query.get('product', None)
            if product is None:
                raise ValueError('No products match search terms: %r' % query)
            else:
                raise ValueError(f"No such product: {product}")

        for q, product in product_queries:
            dataset_fields = product.metadata_type.dataset_fields
            query_exprs = tuple(fields.to_expressions(dataset_fields.get, **q))
            select_fields = None
            if return_fields:
                # if no fields specified, select all
                if select_field_names is None:
                    select_fields = None
                else:
                    select_fields = tuple(
                        dataset_fields[field_name]
                        for field_name in select_field_names
                        if field_name in dataset_fields
                    )
            with self._db_connection() as connection:
                yield (product,
                       connection.search_datasets(
                           query_exprs,
                           source_exprs,
                           select_fields=select_fields,
                           limit=limit,
                           with_source_ids=with_source_ids,
                           archived=archived
                       ))

    def _do_count_by_product(self, query, archived: bool | None = False):
        product_queries = self._get_product_queries(query)

        for q, product in product_queries:
            dataset_fields = product.metadata_type.dataset_fields
            query_exprs = tuple(fields.to_expressions(dataset_fields.get, **q))
            with self._db_connection() as connection:
                count = connection.count_datasets(query_exprs, archived=archived)
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
                yield columns._asdict()

    def spatial_extent(self, ids, crs=None):
        return None

    def temporal_extent(
            self,
            ids: Iterable[DSID] | None = None
    ) -> tuple[datetime.datetime, datetime.datetime]:
        """
        Returns the minimum and maximum acquisition time of the specified datasets.
        """
        raise NotImplementedError("Sorry Temporal Extent by dataset ids is not supported in postgres driver.")

    # pylint: disable=redefined-outer-name
    def search_returning_datasets_light(self, field_names: tuple, custom_offsets=None, limit=None,
                                        archived: bool | None = False,
                                        **query):
        """
        This is a dataset search function that returns the results as objects of a dynamically
        generated Dataset class that is a subclass of tuple.

        Only the requested fields will be returned together with related derived attributes as property functions
        similer to the datacube.model.Dataset class. For example, if 'extent'is requested all of
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
                results = connection.search_unique_datasets(
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
                            'grid_spatial', 'grid_spatial', DATASET.c.metadata,
                            False,
                            offset=grid_spatial
                        ))
                elif custom_offsets and field_name in custom_offsets:
                    select_fields.append(SimpleDocField(
                        field_name, field_name, DATASET.c.metadata,
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
                custom_query[key], custom_query[key], DATASET.c.metadata,
                False, offset=custom_offsets[key]
            )
            custom_exprs.append(fields.as_expression(custom_field, custom_query[key]))

        return custom_exprs

    def get_all_docs_for_product(self, product: Product, batch_size: int = 1000) -> Iterable[DatasetTuple]:
        product_search_key = [product.name]
        with self._db_connection(transaction=True) as connection:
            for row in connection.bulk_simple_dataset_search(products=product_search_key, batch_size=batch_size):
                prod_name, metadata_doc, uris = tuple(row)
                yield DatasetTuple(product, metadata_doc, uris)
