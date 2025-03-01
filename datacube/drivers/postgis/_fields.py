# This file is part of the Open Data Cube, see https://opendatacube.org for more information
#
# Copyright (c) 2015-2024 ODC Contributors
# SPDX-License-Identifier: Apache-2.0
"""
Build and index fields within documents.
"""
import math

from collections import namedtuple
from datetime import timezone
from datetime import datetime, date
from decimal import Decimal
from typing import Any, Callable, Type, Tuple, Union

from sqlalchemy.types import TIMESTAMP
from sqlalchemy import cast, func, and_
from sqlalchemy.dialects import postgresql as postgres
from sqlalchemy.dialects.postgresql import NUMRANGE, TSTZRANGE, Range as PgRange
from sqlalchemy.dialects.postgresql import INTERVAL
from sqlalchemy.sql import ColumnElement, FromClause, ColumnExpressionArgument
from sqlalchemy.orm import aliased

from datacube import utils
from datacube.model.fields import Expression, Field
from datacube.model import Range
from datacube.utils import get_doc_offset

from datacube.drivers.postgis._schema import Dataset, search_field_index_map
from datacube.utils import cached_property
from datacube.utils.dates import tz_as_utc

DatasetJoinArgs = tuple[FromClause] | tuple[FromClause, ColumnExpressionArgument]


class PgField(Field):
    """
    Postgis implementation of a searchable field. May be a value inside
    a JSONB column.
    """

    def __init__(self,
                 name: str, description: str,
                 alchemy_column: ColumnElement,
                 indexed: bool,
                 alchemy_table: FromClause | None = None):
        super().__init__(name, description)

        # The underlying SQLAlchemy column. (eg. DATASET.c.metadata)
        self.alchemy_column = alchemy_column
        self.indexed = indexed
        self.alchemy_table = alchemy_table

    @property
    def select_alchemy_table(self) -> FromClause:
        if self.alchemy_table is not None:
            return self.alchemy_table
        return self.alchemy_column.table

    def normalise_value(self, value):
        """
        Wrap the given value with any necessary type casts/conversions for this field.

        Overridden by other classes as needed.
        """
        # Default do nothing (eg. string datatypes)
        return value

    @cached_property
    def search_index_table(self) -> FromClause:
        if self.indexed:
            search_table = search_field_index_map[self.type_name]
            return aliased(  # type: ignore[return-value]
                search_table, name=f"{search_table.__tablename__}-{self.name}")  # type: ignore[attr-defined]
        else:
            return self.select_alchemy_table

    @cached_property
    def dataset_join_args(self) -> DatasetJoinArgs:
        if self.indexed:
            return (
                self.search_index_table,
                and_(
                    Dataset.id == self.search_index_table.dataset_ref,
                    self.search_index_table.search_key == self.name
                )
            )
        else:
            return (self.search_index_table,)

    @property
    def alchemy_expression(self) -> ColumnExpressionArgument:
        """
        Get an SQLAlchemy expression for accessing this field.
        :return:
        """
        raise NotImplementedError('alchemy expression')

    @property
    def search_alchemy_expression(self) -> ColumnExpressionArgument:
        if self.indexed:
            return self.search_index_table.search_val
        else:
            return self.alchemy_expression

    @property
    def sql_expression(self):
        """
        Get the raw SQL expression for this field as a string.
        :rtype: str
        """
        return str(self.alchemy_expression.compile(
            dialect=postgres.dialect(),
            compile_kwargs={"literal_binds": True}
        ))

    def __eq__(self, value):
        """
        :rtype: Expression
        """
        return EqualsExpression(self, value)

    def between(self, low, high):
        """
        :rtype: Expression
        """
        raise NotImplementedError('between expression')


class NativeField(PgField):
    """
    Fields hard-coded into the schema. (not user configurable)
    """

    def __init__(self, name: str, description: str,
                 alchemy_column: ColumnElement,
                 alchemy_expression: ColumnExpressionArgument | None = None,
                 join_clause: ColumnExpressionArgument | None = None,
                 alchemy_table: FromClause | None = None,
                 # Should this be selected by default when selecting all fields?
                 affects_row_selection: bool = False):
        super(NativeField, self).__init__(name, description, alchemy_column, indexed=False, alchemy_table=alchemy_table)
        self._expression = alchemy_expression
        self.affects_row_selection = affects_row_selection
        self.join_clause = join_clause

    @property
    def alchemy_expression(self) -> ColumnExpressionArgument:
        expression = self._expression if self._expression is not None else self.alchemy_column
        return expression.label(self.name)  # type: ignore[union-attr]

    @cached_property
    def dataset_join_args(self) -> DatasetJoinArgs:
        if self.join_clause is None:
            return super().dataset_join_args
        return (self.select_alchemy_table, self.join_clause)


class PgDocField(PgField):
    """
    A field extracted from inside a (jsonb) document.
    """
    def value_to_alchemy(self, value):
        """
        Wrap the given value with any necessary type casts/conversions for this field.

        Overridden by other classes as needed.
        """
        # Default do nothing (eg. string datatypes)
        return value

    def search_value_to_alchemy(self, value):
        """
        Value to use in search tables.  Identical to value_to_alchemy, unless it needs to be promoted to range type.

        :param value:
        :return:
        """
        return self.value_to_alchemy(value)

    def parse_value(self, value):
        """
        Parse the value from a string. May be overridden by subclasses.
        """
        return value

    def _alchemy_offset_value(self,
                              doc_offsets: Tuple[Tuple[str]],
                              agg_function: Callable[[Any], ColumnElement],
                              type_: Type | None = None) -> ColumnElement:
        """
        Get an sqlalchemy value for the given offsets of this field's sqlalchemy column.
        If there are multiple they will be combined using the given aggregate function.

        Offsets can either be single:
            ('platform', 'code')
        Or multiple:
            (('platform', 'code'), ('satellite', 'name'))

        In the latter case, the multiple values are combined using the given aggregate function
        (defaults to using coalesce: grab the first non-null value)
        """
        if not doc_offsets:
            raise ValueError("Value requires at least one offset")

        if isinstance(doc_offsets[0], str):
            # It's a single offset.
            doc_offsets = [doc_offsets]

        alchemy_values = [self.value_to_alchemy(self.alchemy_column[offset].astext) for offset in doc_offsets]
        if type_ is not None:
            alchemy_values = [cast(v, type_) for v in alchemy_values]
        # If there's multiple fields, we aggregate them (eg. "min()"). Otherwise use the one.
        return agg_function(*alchemy_values) if len(alchemy_values) > 1 else alchemy_values[0]

    def _extract_offset_value(self, doc, doc_offsets, agg_function):
        """
        Extract a value for the given document offsets.

        Same as _alchemy_offset_value(), but returns the value instead of an sqlalchemy expression to calc the value.
        """
        if not doc_offsets:
            raise ValueError("Value requires at least one offset")

        if isinstance(doc_offsets[0], str):
            # It's a single offset.
            doc_offsets = [doc_offsets]

        values = (get_doc_offset(offset, doc) for offset in doc_offsets)
        values = [self.parse_value(v) for v in values if v is not None]

        if not values:
            return None
        if len(values) == 1:
            return values[0]
        return agg_function(*values)


class SimpleDocField(PgDocField):
    """
    A field with a single value (eg. String, int) calculated as an offset inside a (jsonb) document.
    """

    def __init__(self, name, description, alchemy_column, indexed, offset=None, selection='first'):
        super(SimpleDocField, self).__init__(name, description, alchemy_column, indexed)
        self.offset = offset
        if selection not in SELECTION_TYPES:
            raise ValueError(
                "Unknown field selection type %s. Expected one of: %r" % (selection, (SELECTION_TYPES,),)
            )
        self.aggregation = SELECTION_TYPES[selection]

    @property
    def alchemy_expression(self):
        return self._alchemy_offset_value(self.offset, self.aggregation.pg_calc).label(self.name)

    def __eq__(self, value):
        """
        :rtype: Expression
        """
        return EqualsExpression(self, value)

    def between(self, low, high):
        """
        :rtype: Expression
        """
        raise NotImplementedError('Simple field between expression')

    can_extract = True

    def extract(self, document):
        return self._extract_offset_value(document, self.offset, self.aggregation.calc)

    def evaluate(self, ctx):
        return self.extract(ctx)


class UnindexableValue(Exception):
    pass


class NumericDocField(SimpleDocField):
    type_name = 'numeric'

    def value_to_alchemy(self, value):
        return cast(value, postgres.NUMERIC)

    def search_value_to_alchemy(self, value):
        if isinstance(value, float) and math.isnan(value):
            raise UnindexableValue("Cannot index NaNs")
        alc_val = self.value_to_alchemy(value)
        return func.numrange(
            alc_val, alc_val,
            # Inclusive on both sides.
            '[]',
            type_=NUMRANGE,
        )

    def between(self, low, high):
        # Numeric fields actually stored as ranges in current schema.
        # return ValueBetweenExpression(self, low, high)
        return RangeBetweenExpression(self, low, high, _range_class=PgRange)

    def parse_value(self, value):
        return Decimal(value)


class IntDocField(NumericDocField):
    type_name = 'integer'

    def parse_value(self, value):
        return int(value)


class DoubleDocField(NumericDocField):
    type_name = 'double'

    def parse_value(self, value):
        return float(value)


DateFieldLike = Union[datetime, date, str, ColumnElement]


class DateDocField(SimpleDocField):
    type_name = 'datetime'

    def value_to_alchemy(self, value: DateFieldLike) -> DateFieldLike:
        """
        Wrap a value as needed for this field type.
        """
        if isinstance(value, (datetime, str)):
            return self.normalise_value(value)
        elif isinstance(value, ColumnElement):
            # SQLAlchemy expression or string are parsed in pg as dates.
            # NB: Do not cast here - casting here breaks expected behaviour in other timezones
            return value
        else:
            raise ValueError("Value not readable as date: %r" % value)

    def normalise_value(self, value):
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return tz_as_utc(value)

    def search_value_to_alchemy(self, value):
        if isinstance(value, datetime):
            value = tz_as_utc(value)
        value = cast(value, TIMESTAMP(timezone=True))
        return func.tstzrange(
            value, value,
            # Inclusive on both sides.
            '[]',
            type_=TSTZRANGE,
        )

    def between(self, low, high):
        return ValueBetweenExpression(self, low, high)

    def parse_value(self, value):
        return utils.parse_time(value)

    @property
    def day(self):
        """Get field truncated to the day"""
        return NativeField(
            '{}_day'.format(self.name),
            'Day of {}'.format(self.description),
            self.alchemy_column,
            alchemy_expression=cast(func.date_trunc('day', self.alchemy_expression), TIMESTAMP(timezone=True))
        )


class RangeDocField(PgDocField):
    """
    A range of values. Has min and max values, which may be calculated from multiple
    values in the document.
    """
    FIELD_CLASS = SimpleDocField

    def __init__(self, name, description, alchemy_column, indexed, min_offset=None, max_offset=None):
        super(RangeDocField, self).__init__(name, description, alchemy_column, indexed)
        self.lower = self.FIELD_CLASS(
            name + '_lower',
            description,
            alchemy_column,
            indexed=False,
            offset=min_offset,
            selection='least'
        )
        self.greater = self.FIELD_CLASS(
            name + '_greater',
            description,
            alchemy_column,
            indexed=False,
            offset=max_offset,
            selection='greatest'
        )

    def value_to_alchemy(self, value):
        raise NotImplementedError('range type')

    @property
    def alchemy_expression(self):
        return self.value_to_alchemy((self.lower.alchemy_expression, self.greater.alchemy_expression)).label(self.name)

    def __eq__(self, value):
        """
        :rtype: Expression
        """
        # Lower and higher are interchangeable here: they're the same type.
        casted_val = self.lower.value_to_alchemy(value)
        return RangeContainsExpression(self, casted_val)

    can_extract = True

    def extract(self, document):
        min_val = self.lower.extract(document)
        max_val = self.greater.extract(document)
        if not min_val and not max_val:
            return None
        return Range(min_val, max_val)


class NumericRangeDocField(RangeDocField):
    FIELD_CLASS = NumericDocField
    type_name = 'numeric-range'

    def value_to_alchemy(self, value):
        low, high = value
        return func.numrange(
            low, high,
            # Inclusive on both sides.
            '[]',
            type_=NUMRANGE,
        )

    def search_value_to_alchemy(self, value):
        low, high = value
        if isinstance(low, float) and math.isnan(low):
            raise UnindexableValue("Cannot index NaNs")
        if isinstance(high, float) and math.isnan(high):
            raise UnindexableValue("Cannot index NaNs")
        return self.value_to_alchemy(value)

    def between(self, low, high):
        """
        :rtype: Expression
        """
        return RangeBetweenExpression(self, low, high, _range_class=PgRange)


class IntRangeDocField(NumericRangeDocField):
    FIELD_CLASS = IntDocField
    type_name = 'integer-range'


class DoubleRangeDocField(NumericRangeDocField):
    FIELD_CLASS = DoubleDocField
    type_name = 'double-range'


class DateRangeDocField(RangeDocField):
    FIELD_CLASS = DateDocField
    type_name = 'datetime-range'

    def value_to_alchemy(self, value):
        low, high = value
        # Is OK to cast, because we are wrapping it in timezone-aware datatype.
        if isinstance(low, (ColumnElement, str)):
            low = cast(low, TIMESTAMP(timezone=True))
        if isinstance(high, (ColumnElement, str)):
            high = cast(high, TIMESTAMP(timezone=True))
        return func.tstzrange(
            low, high,
            # Inclusive on both sides.
            '[]',
            type_=TSTZRANGE,
        )

    def search_value_to_alchemy(self, value):
        low, high = value
        if isinstance(low, datetime):
            low = tz_as_utc(low)
        if isinstance(high, datetime):
            high = tz_as_utc(high)
        return func.tstzrange(
            low, high,
            # Inclusive on both sides.
            '[]',
            type_=TSTZRANGE,
        )

    def normalise_value(self, value):
        if isinstance(value, datetime):
            return tz_as_utc(value)
        elif isinstance(value, PgRange):
            return PgRange(lower=tz_as_utc(value.lower), upper=tz_as_utc(value.upper), bounds=value.bounds)
        else:
            return tuple(tz_as_utc(v) for v in value)

    def between(self, low, high):
        """
        :rtype: Expression
        """
        low = _number_implies_year(low)
        high = _number_implies_year(high)

        if isinstance(low, datetime) and isinstance(high, datetime):
            return RangeBetweenExpression(
                self,
                tz_as_utc(low).astimezone(timezone.utc),
                tz_as_utc(high).astimezone(timezone.utc),
                _range_class=PgRange
            )
        else:
            raise ValueError("Unknown comparison type for date range: "
                             "expecting datetimes, got: (%r, %r)" % (low, high))

    @property
    def expression_with_leniency(self):
        low = cast(self.lower.alchemy_expression, TIMESTAMP(timezone=True)) - cast('500 milliseconds', INTERVAL)
        high = cast(self.greater.alchemy_expression, TIMESTAMP(timezone=True)) + cast('500 milliseconds', INTERVAL)
        return func.tstzrange(
            low, high,
            # Inclusive on both sides.
            '[]',
            type_=TSTZRANGE,
        )


def _number_implies_year(v: Union[int, datetime]) -> datetime:
    """
    >>> _number_implies_year(1994)
    datetime.datetime(1994, 1, 1, 0, 0)
    >>> _number_implies_year(datetime(1994, 4, 4))
    datetime.datetime(1994, 4, 4, 0, 0)
    """
    if isinstance(v, int):
        return datetime(v, 1, 1)
    # The expression module parses all number ranges as floats.
    if isinstance(v, float):
        return datetime(int(v), 1, 1)

    return v


class PgExpression(Expression):
    def __init__(self, field):
        super(PgExpression, self).__init__()
        #: :type: PgField
        self.field = field

    @property
    def alchemy_expression(self):
        """
        Get an SQLAlchemy expression for accessing this field.
        :return:
        """
        raise NotImplementedError('alchemy expression')


class ValueBetweenExpression(PgExpression):
    def __init__(self, field, low_value, high_value):
        super(ValueBetweenExpression, self).__init__(field)
        self.low_value = low_value
        self.high_value = high_value

    @property
    def alchemy_expression(self):
        if self.low_value is not None and self.high_value is not None:
            return and_(self.field.search_alchemy_expression >= self.low_value,
                        self.field.search_alchemy_expression <= self.high_value)
        if self.low_value is not None:
            return self.field.search_alchemy_expression >= self.low_value
        if self.high_value is not None:
            return self.field.search_alchemy_expression <= self.high_value
        raise ValueError('Expect at least one of [low,high] to be set')


class RangeBetweenExpression(PgExpression):
    def __init__(self, field, low_value, high_value, _range_class):
        super(RangeBetweenExpression, self).__init__(field)
        self.low_value = low_value
        self.high_value = high_value
        self._range_class = _range_class
        self._alc_val = self._range_class(self.low_value, self.high_value, bounds='[]')

    @property
    def alchemy_expression(self):
        return self.field.search_alchemy_expression.overlaps(self._alc_val)


class RangeContainsExpression(PgExpression):
    def __init__(self, field, value):
        super(RangeContainsExpression, self).__init__(field)
        self.value = value

    @property
    def alchemy_expression(self):
        return self.field.search_alchemy_expression.contains(self.value)


class EqualsExpression(PgExpression):
    def __init__(self, field, value):
        super(EqualsExpression, self).__init__(field)
        self.value = value

    @property
    def alchemy_expression(self):
        return self.field.search_alchemy_expression == self.value

    def evaluate(self, ctx):
        return self.field.evaluate(ctx) == self.value


def parse_fields(doc, table_column):
    """
    Parse a field spec document into objects.

    Example document:

    ::

        {
            # Field name:
            'lat': {
                # Field type & properties.
                'type': 'float-range',
                'min_offset': [
                    # Offsets within a dataset document for this field.
                    ['extent', 'coord', 'ul', 'lat'],
                    ['extent', 'coord', 'll', 'lat']
                ],
                'max_offset': [
                    ['extent', 'coord', 'ur', 'lat'],
                    ['extent', 'coord', 'lr', 'lat']
                ]
            }
        }

    :param table_column: SQLAlchemy jsonb column for the document we're reading fields from.
    :type doc: dict
    :rtype: dict[str, PgField]
    """

    # Implementations of fields for this driver
    types = {
        SimpleDocField,
        IntDocField,
        DoubleDocField,
        DateDocField,

        NumericRangeDocField,
        IntRangeDocField,
        DoubleRangeDocField,
        DateRangeDocField,
    }
    type_map = {f.type_name: f for f in types}
    # An alias for backwards compatibility
    type_map['float-range'] = NumericRangeDocField

    # No later field should have overridden string
    assert type_map['string'] == SimpleDocField

    def _get_field(name, descriptor, column):
        """
        :type name: str
        :type descriptor: dict
        :param column: SQLAlchemy table column
        :rtype: PgField
        """
        ctorargs = descriptor.copy()
        type_name = ctorargs.pop('type', 'string')
        description = ctorargs.pop('description', None)
        indexed_val = ctorargs.pop('indexed', "true")
        indexed = indexed_val.lower() == 'true' if isinstance(indexed_val, str) else indexed_val

        field_class = type_map.get(type_name)
        if not field_class:
            raise ValueError(('Field %r has unknown type %r.'
                              ' Available types are: %r') % (name, type_name, list(type_map.keys())))
        try:
            return field_class(name, description, column, indexed, **ctorargs)
        except TypeError as e:
            raise RuntimeError(
                'Field {name} has unexpected argument for a {type}'.format(
                    name=name, type=type_name
                ), e
            )

    return {name: _get_field(name, descriptor, table_column) for name, descriptor in doc.items()}


def _coalesce(*values):
    """
    Return first non-none value.
    Return None if all values are None, or there are no values passed in.

    >>> _coalesce(1, 2)
    1
    >>> _coalesce(None, 2, 3)
    2
    >>> _coalesce(None, None, 3, None, 5)
    3
    """
    for v in values:
        if v is not None:
            return v
    return None


# How to choose/combine multiple doc values.
ValueAggregation = namedtuple('ValueAggregation', ('calc', 'pg_calc'))
SELECTION_TYPES = {
    # First non-null
    'first': ValueAggregation(_coalesce, func.coalesce),
    # min/max
    'least': ValueAggregation(min, func.least),
    'greatest': ValueAggregation(max, func.greatest),
}
